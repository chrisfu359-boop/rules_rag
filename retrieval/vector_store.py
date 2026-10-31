"""向量召回通道（BGE-M3 dense）：Milvus 优先，本地 .npz 兜底。

两套后端并存的原因：
  Milvus 跑在 Docker 里，Docker daemon 挂掉时 19530 就连不上（本项目真的遇到过）。
  条款只有 406 条，暴力内积单次 <1ms，npz 完全够用；但"够用"和"该有"是两件事——
  真实业务里规则库会长到几万条、还要按平台/场景做标量过滤，这就是向量库的活。
  所以两套都实现，用同一个 search(query, top_k, scene) 签名，
  由 get_vector_index() 按可用性选择，上层（hybrid / app）完全无感。

Milvus 侧的关键点：
  向量建集合时必须带 doc_name/clause_no/scene/risk_level/platform 标量字段，
  否则检索回来只有 id，还得回 MySQL 查一遍元数据，多一次往返；
  更重要的是 scene 过滤能下推到索引层，而不是捞回来再筛。

这就是"先过滤再召回"在向量层的落地：
  scene 过滤在候选集上做（mask），失效条款根本不参与打分。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

MODEL_PATH = os.getenv("BGE_M3_PATH", r"D:/hm_st/EduRAG/code/edu_rag/rag_qa/models/bge-m3")
CACHE = Path(__file__).resolve().parent.parent / "data" / "processed" / "vectors.npz"


class VectorIndex:
    """条款向量索引。首次 build 时编码并落盘，之后直接加载。"""

    def __init__(self, model_path: str = MODEL_PATH, cache: Path = CACHE,
                 max_length: int = 512):
        self.model_path = model_path
        self.cache = Path(cache)
        self.max_length = max_length
        self._model = None
        self._enc_failed = False
        self.ids: np.ndarray | None = None
        self.mat: np.ndarray | None = None
        self.scenes: list[str] = []
        self.load_time_ms = 0.0

    # ---------------------------------------------------------------- model
    @property
    def model(self):
        if self._model is None:
            os.environ.setdefault("OMP_NUM_THREADS", "4")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            from FlagEmbedding import BGEM3FlagModel
            self._model = BGEM3FlagModel(self.model_path, use_fp16=False, device="cpu")
        return self._model

    def encoder_ready(self) -> bool:
        """编码器是否真的能跑。

        npz 里只存了「历史条款向量」，查询侧仍要现算 query 向量——所以
        「矩阵加载成功」不等于「这条通道可用」。以前只用 self.mat 判断，
        结果在「装了 vectors.npz 但没装 FlagEmbedding」的机器上，
        每次提问都在 encode() 里抛 ModuleNotFoundError，直接变成 500。
        失败一次就记住，不要每条查询都重试一次 import。
        """
        if self._enc_failed:
            return False
        try:
            self.model
            return True
        except Exception as e:
            self._enc_failed = True
            print("[vector] 编码器不可用，关闭向量通道:", str(e)[:100])
            return False

    def encode(self, texts: list[str], batch_size: int = 16) -> np.ndarray:
        if not self.encoder_ready():
            raise RuntimeError("向量编码器不可用（未安装 FlagEmbedding 或模型路径缺失）")
        out = self.model.encode(texts, batch_size=batch_size,
                                max_length=self.max_length)["dense_vecs"]
        out = np.asarray(out, dtype=np.float32)
        # 归一化后内积 == 余弦相似度
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.clip(norms, 1e-8, None)

    # ---------------------------------------------------------------- build
    def build(self, clauses: list[dict], force: bool = False) -> dict:
        """编码全部条款并落盘。clauses 需含 id / content / scene。"""
        if self.cache.exists() and not force:
            return {"skipped": True, "path": str(self.cache)}
        t0 = time.time()
        texts = [self._doc_text(c) for c in clauses]
        mat = self.encode(texts)
        self.ids = np.array([c["id"] for c in clauses], dtype=np.int64)
        self.scenes = [(c.get("scene") or "") for c in clauses]
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.cache, ids=self.ids, mat=mat,
                            scenes=np.array(self.scenes, dtype=object))
        self.mat = mat
        return {"count": len(clauses), "dim": int(mat.shape[1]),
                "sec": round(time.time() - t0, 1), "path": str(self.cache)}

    def load(self) -> bool:
        if not self.cache.exists():
            return False
        t0 = time.time()
        z = np.load(self.cache, allow_pickle=True)
        self.ids, self.mat = z["ids"], z["mat"].astype(np.float32)
        self.scenes = [str(s) for s in z["scenes"]]
        self.load_time_ms = round((time.time() - t0) * 1000, 1)
        return True

    # --------------------------------------------------------------- search
    @staticmethod
    def _doc_text(c: dict) -> str:
        """编码对象：文档名 + 条款号 + 正文。
        带上文档名和条款号，是因为用户会用"广告法怎么规定"这种带出处的问法。"""
        return f"《{c.get('doc_name', '')}》{c.get('clause_no', '')} {c.get('content', '')}"

    def search(self, query: str, top_k: int = 5, scene: str | None = None,
               min_score: float = 0.0) -> list[tuple[int, float]]:
        """返回 [(clause_id, score)]。scene 为软过滤：命中场景的条款加权，
        而不是硬过滤——硬过滤会在 scene 判错时彻底丢答案，这是有意的设计取舍。"""
        if not self.encoder_ready():
            return []
        if self.mat is None and not self.load():
            return []
        q = self.encode([query])[0]
        scores = self.mat @ q                       # 归一化向量内积
        if scene:
            head = scene.split(".")[0]
            mask = np.array([s.split(".")[0] == head if s else False
                             for s in self.scenes])
            scores = scores + mask * 0.04           # 同场景 +0.04（约 4% 相对提升）
        idx = np.argsort(-scores)[:top_k]
        return [(int(self.ids[i]), float(scores[i])) for i in idx
                if float(scores[i]) >= min_score]

    def stats(self) -> dict:
        return {"size": 0 if self.mat is None else int(self.mat.shape[0]),
                "dim": 0 if self.mat is None else int(self.mat.shape[1]),
                "loaded": self.mat is not None,
                "load_ms": self.load_time_ms,
                "backend": "numpy-local(npz)"}


class MilvusStore:
    """Milvus 后端，接口与 VectorIndex 对齐（load / search / stats / encode）。

    scene 过滤用 expr 下推：scene like 'aftersale%'。
    这里刻意用 like 而不是 ==，因为 scene 是两段式（aftersale.return），
    查询侧只判到大类；写死等值会在 scene 细分后悄悄丢结果。
    """

    COLLECTION = "rules_clause"

    def __init__(self, host: str = None, port: str = None, model_path: str = MODEL_PATH,
                 max_length: int = 512):
        self.host = host or os.getenv("MILVUS_HOST", "127.0.0.1")
        self.port = port or os.getenv("MILVUS_PORT", "19530")
        self.max_length = max_length
        self.model_path = model_path
        self._model = None
        self._enc_failed = False
        self.col = None
        self.load_time_ms = 0.0
        self.search_ms: list[float] = []

    @property
    def model(self):
        if self._model is None:
            os.environ.setdefault("OMP_NUM_THREADS", "4")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            from FlagEmbedding import BGEM3FlagModel
            self._model = BGEM3FlagModel(self.model_path, use_fp16=False, device="cpu")
        return self._model

    def encoder_ready(self) -> bool:
        if self._enc_failed:
            return False
        try:
            self.model
            return True
        except Exception as e:
            self._enc_failed = True
            print("[milvus] 编码器不可用，关闭向量通道:", str(e)[:100])
            return False

    def encode(self, texts: list[str]) -> np.ndarray:
        if not self.encoder_ready():
            raise RuntimeError("向量编码器不可用（未安装 FlagEmbedding 或模型路径缺失）")
        out = self.model.encode(texts, batch_size=8, max_length=self.max_length)["dense_vecs"]
        out = np.asarray(out, dtype=np.float32)
        return out / np.clip(np.linalg.norm(out, axis=1, keepdims=True), 1e-8, None)

    def load(self) -> bool:
        try:
            from pymilvus import Collection, connections, utility
            connections.connect(alias="default", host=self.host, port=self.port, timeout=3)
            if not utility.has_collection(self.COLLECTION):
                return False
            t0 = time.time()
            self.col = Collection(self.COLLECTION)
            self.col.load()
            self.load_time_ms = round((time.time() - t0) * 1000, 1)
            return True
        except Exception as e:
            print("[milvus] 连接失败，降级 npz:", str(e)[:80])
            return False

    def search(self, query: str, top_k: int = 5, scene: str | None = None,
               min_score: float = 0.0) -> list[tuple[int, float]]:
        if not self.encoder_ready():
            return []
        if self.col is None and not self.load():
            return []
        t0 = time.time()
        # 不做 scene 硬过滤：场景判错时硬过滤会把正确答案整个丢掉（同 npz 后端的取舍）。
        # 这里用"加权重排"实现软过滤，与 npz 后端口径保持一致，两套后端结果才可比。
        expr = ""
        q = self.encode([query])[0].tolist()
        res = self.col.search(data=[q], anns_field="vector", param={"metric_type": "IP",
                                                                    "params": {"nprobe": 16}},
                              limit=max(top_k * 3, 20), expr=expr,
                              output_fields=["doc_name", "clause_no", "scene"])
        hits = []
        boost = 0.04 if scene else 0.0
        for h in res[0]:
            s = float(h.score)
            if scene and (h.entity.get("scene") or "").split(".")[0] == scene.split(".")[0]:
                s += boost
            if s < min_score:
                continue
            hits.append((int(h.id), s))
        hits.sort(key=lambda x: -x[1])
        self.search_ms.append((time.time() - t0) * 1000)
        self.search_ms = self.search_ms[-50:]
        return hits[:top_k]

    def stats(self) -> dict:
        try:
            n = int(self.col.num_entities) if self.col is not None else 0
        except Exception:
            n = 0
        return {"size": n, "dim": 1024, "loaded": self.col is not None,
                "load_ms": self.load_time_ms,
                "backend": "milvus(IVF_FLAT/IP)",
                "search_ms_p50": round(sorted(self.search_ms)[len(self.search_ms) // 2], 1)
                if self.search_ms else 0}


def get_vector_index(prefer: str = None):
    """按可用性选后端。Milvus 通了用 Milvus，否则 npz——上层不需要知道这件事。"""
    prefer = (prefer or os.getenv("VECTOR_BACKEND", "milvus")).lower()
    if prefer == "milvus":
        m = MilvusStore()
        if m.load():
            return m
    return VectorIndex()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import common
    vi = VectorIndex()
    cl = common.load_clauses()
    print(vi.build(cl, force=True))
    print(vi.stats())
    for q, exp in [("买家申请仅退款，我多久要处理？", "第十四条"),
                   ("广告里能不能用最高级", "第九条")]:
        t = time.time()
        hits = vi.search(q, 3)
        print(f"[{round((time.time()-t)*1000)}ms] {q}")
        id2 = {c["id"]: c for c in cl}
        for cid, s in hits:
            print(f"    {s:.3f} {id2[cid]['doc_name']} {id2[cid]['clause_no']}")
