"""把本地 npz 向量写入 Milvus，建成真正的向量库（Docker 恢复后启用）。

之前 Milvus 连不上，向量只能存本地 .npz 做暴力检索。现在 19530 通了，
把同一批向量灌进 Milvus 并建 IVF_FLAT 索引，检索链路里的"向量通道"
就从 numpy 矩阵内积变成真正的向量数据库检索。

为什么要带标量字段（scene / platform / risk_level）：
  规则检索的第一性问题是"这条规则现在有没有效、适不适用于这个平台"，
  这类过滤必须发生在检索前而不是检索后。Milvus 支持标量过滤表达式，
  可以把 scene='aftersale.return' 直接下推到索引层。

运行：python pipeline/build_milvus.py [--recreate]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COLLECTION = "rules_clause"
DIM = 1024


def build(recreate: bool = False) -> dict:
    from pymilvus import (Collection, CollectionSchema, DataType, FieldSchema,
                          connections, utility)

    connections.connect(alias="default", host="127.0.0.1", port="19530", timeout=10)

    if utility.has_collection(COLLECTION):
        if not recreate:
            col = Collection(COLLECTION)
            col.load()
            return {"skipped": True, "count": col.num_entities}
        utility.drop_collection(COLLECTION)

    fields = [
        FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
        FieldSchema("doc_name", DataType.VARCHAR, max_length=128),
        FieldSchema("clause_no", DataType.VARCHAR, max_length=64),
        FieldSchema("scene", DataType.VARCHAR, max_length=64),
        FieldSchema("risk_level", DataType.VARCHAR, max_length=8),
        FieldSchema("platform", DataType.VARCHAR, max_length=32),
        FieldSchema("vector", DataType.FLOAT_VECTOR, dim=DIM),
    ]
    col = Collection(COLLECTION, CollectionSchema(fields, description="电商规则条款向量"))

    z = np.load(ROOT / "data" / "processed" / "vectors.npz", allow_pickle=True)
    ids = z["ids"].astype(np.int64)
    mat = z["mat"].astype(np.float32)
    # 元数据从库里取：clauses.json 是切分产物，没有 id / scene / risk_level，
    # 这三个字段是入库时打的标。向量 build 时用的就是 load_clauses() 的顺序，
    # 所以 npz 里的 id 可以直接和库里的主键对上。
    import os
    os.environ.setdefault("DB_BACKEND", "mysql")
    import common
    clauses = {c["id"]: c for c in common.load_clauses()}

    rows = [[], [], [], [], [], [], []]
    for i, cid in enumerate(ids.tolist()):
        c = clauses.get(int(cid))
        if not c:
            continue
        rows[0].append(int(cid))
        rows[1].append((c.get("doc_name") or "")[:128])
        rows[2].append((c.get("clause_no") or "")[:64])
        rows[3].append((c.get("scene") or "none")[:64])
        rows[4].append((c.get("risk_level") or "L1")[:8])
        rows[5].append((c.get("platform") or "all")[:32])
        rows[6].append(mat[i].tolist())

    t0 = time.time()
    col.insert(rows)
    col.flush()

    # IVF_FLAT + IP：向量已 L2 归一化，内积即余弦，与本地 npz 口径一致
    col.create_index("vector", {"index_type": "IVF_FLAT", "metric_type": "IP",
                                "params": {"nlist": 64}})
    col.create_index("scene", {"index_type": "INVERTED"})
    col.create_index("platform", {"index_type": "INVERTED"})
    col.load()
    return {"count": col.num_entities, "sec": round(time.time() - t0, 1),
            "index": "IVF_FLAT/IP nlist=64"}


if __name__ == "__main__":
    print(build(recreate="--recreate" in sys.argv))
