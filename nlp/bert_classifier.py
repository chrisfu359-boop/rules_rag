"""BERT 风险分类器（L1 / L2 / L3）。

模型由 `pipeline/train_bert_risk.py` 训练产出（蒸馏自规则路由）。
设计要点：
- **懒加载**：没装 torch / 模型文件不存在时，`predict()` 返回 None，
  调用方（`nlp/router.risk`）自动退回规则判断。分类器是增强，不是唯一依赖。
- 单例 + 线程锁：FastAPI 是多线程的，模型只加载一次。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.getenv("BERT_RISK_MODEL", str(ROOT / "models" / "bert_risk_classifier")))
LABELS = ["L1", "L2", "L3"]

_lock = threading.Lock()
_state: dict = {"loaded": False, "ok": False, "model": None, "tok": None}


def available() -> bool:
    """模型文件是否就绪（不触发加载）。"""
    return (MODEL_DIR / "config.json").exists()


def _load() -> bool:
    if _state["loaded"]:
        return _state["ok"]
    with _lock:
        if _state["loaded"]:
            return _state["ok"]
        _state["loaded"] = True
        if not available():
            return False
        try:
            import torch  # noqa: F401
            from transformers import BertForSequenceClassification, BertTokenizer
            tok = BertTokenizer.from_pretrained(str(MODEL_DIR))
            model = BertForSequenceClassification.from_pretrained(str(MODEL_DIR))
            model.eval()
            _state.update(model=model, tok=tok, ok=True)
        except Exception:
            _state["ok"] = False
        return _state["ok"]


def predict(query: str) -> str | None:
    """返回 L1/L2/L3；模型不可用或推理异常返回 None（由调用方退回规则）。"""
    if not query or not _load():
        return None
    try:
        import torch
        with torch.no_grad():
            enc = _state["tok"](query, truncation=True, padding="max_length",
                                max_length=64, return_tensors="pt")
            idx = int(_state["model"](**enc).logits.argmax(-1)[0])
        return LABELS[idx] if 0 <= idx < len(LABELS) else None
    except Exception:
        return None
