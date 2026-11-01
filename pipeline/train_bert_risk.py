"""训练 BERT 风险分类器（L1 / L2 / L3）。

为什么训练而不是直接用现成的 bert_query_classifier2：
那个是 EduRAG 的「通用知识 vs 专业咨询」二分类，跟本项目的 L1/L2/L3 完全不同。

数据来源：评测集 300 题 + 知识库问题 + 客服演示问题，用现有规则路由打**伪标签**
（蒸馏：让 BERT 学会规则判断，同时天然多一层泛化）。
评测集只用来做验证集，不参与训练，避免自己考自己。

用法（需要 torch 环境，本项目是 conda edu_rag）：
    python pipeline/train_bert_risk.py
产出：
    models/bert_risk_classifier/   可被 nlp/bert_classifier.py 加载
"""
from __future__ import annotations

import csv
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BERT_BASE = os.getenv("BERT_BASE_PATH",
                      r"D:/hm_st/EduRAG/code/edu_rag/rag_qa/models/bert-base-chinese")
OUT = ROOT / "models" / "bert_risk_classifier"
LABELS = ["L1", "L2", "L3"]


def load_corpus() -> list[str]:
    qs: list[str] = []
    p = ROOT / "data" / "eval" / "eval_300.csv"
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            qs.append(str(r.get("question") or "").strip())
    try:
        os.environ.setdefault("DB_BACKEND", "sqlite")
        import common
        for r in common.fetch_all(
                "SELECT question FROM t_faq WHERE question IS NOT NULL AND question <> ''"):
            qs.append(str(r["question"]).strip())
        for r in common.fetch_all(
                "SELECT content FROM t_cs_message WHERE role='buyer' AND content IS NOT NULL"):
            qs.append(str(r["content"]).strip())
    except Exception as e:
        print("[data] 读库跳过:", str(e)[:60])
    # 去重 + 去掉过短/过长的
    out, seen = [], set()
    for q in qs:
        q = q.strip()
        if 3 <= len(q) <= 120 and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def main() -> int:
    import torch
    from datasets import Dataset
    from sklearn.model_selection import train_test_split
    from transformers import (BertForSequenceClassification, BertTokenizer,
                              Trainer, TrainingArguments)

    from nlp import router as rules

    qs = load_corpus()
    if len(qs) < 80:
        print(f"[×] 语料只有 {len(qs)} 条，不够训练")
        return 1
    # 伪标签：规则路由判定风险等级
    lab = []
    for q in qs:
        lv, _ = rules.risk(q)
        lab.append(LABELS.index(lv))
    dist = {LABELS[i]: lab.count(i) for i in range(3)}
    print(f"[data] 语料 {len(qs)} 条，伪标签分布 {dist}")

    Xtr, Xva, ytr, yva = train_test_split(qs, lab, test_size=0.15,
                                          random_state=42, stratify=lab)
    tok = BertTokenizer.from_pretrained(BERT_BASE)

    def encode(batch):
        return tok(batch["text"], truncation=True, padding="max_length", max_length=64)
    tr = Dataset.from_dict({"text": Xtr, "label": ytr}).map(encode, batched=True)
    va = Dataset.from_dict({"text": Xva, "label": yva}).map(encode, batched=True)
    tr.set_format("torch", columns=["input_ids", "attention_mask", "label"])
    va.set_format("torch", columns=["input_ids", "attention_mask", "label"])

    model = BertForSequenceClassification.from_pretrained(BERT_BASE, num_labels=3)
    args = TrainingArguments(
        output_dir=str(ROOT / "models" / "_train_tmp"),
        num_train_epochs=3, per_device_train_batch_size=16,
        per_device_eval_batch_size=32, learning_rate=3e-5,
        logging_steps=20, eval_strategy="epoch", save_strategy="no",
        report_to=[], use_cpu=True,
    )
    trainer = Trainer(model=model, args=args, train_dataset=tr, eval_dataset=va)
    trainer.train()

    preds = trainer.predict(va).predictions.argmax(-1)
    acc = float((preds == va["label"].numpy()).mean())
    print(f"[eval] 验证集准确率 {acc * 100:.1f}%")

    OUT.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(OUT))
    tok.save_pretrained(str(OUT))
    print(f"[save] 已保存 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
