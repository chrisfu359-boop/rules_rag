"""平台规则粘贴入库入口。

平台规则页是前端渲染 + 需登录，抓不到正文，只能人工复制。本脚本负责把
复制出来的正文包上元信息头（doc_name / platform / 生效期 / source_url），
落到 data/raw/，后续由 clause_splitter 自动识别编号体系并切分。

用法（在 rules_rag 目录下）：

    # 从文件读
    python pipeline/ingest.py --name "商家售后服务管理规范" --platform douyin \
        --effective-from 2026-09-15 \
        --source-url "https://school.jinritemai.com/doudian/web/article/104600" \
        --file 粘贴内容.txt

    # 从标准输入读（Windows: 先 Ctrl+A Ctrl+C 复制页面，再 type 一个文件）
    python pipeline/ingest.py --name "..." --source-url "..." < 粘贴内容.txt

写完执行：
    python pipeline/clause_splitter.py && python pipeline/build_store.py

纪律：source_url 必填。没有出处的条款不入库——编条款号比没有条款更糟。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="把平台规则正文包上元信息，落到 data/raw/")
    ap.add_argument("--name", required=True, help="规则全名，如 商家售后服务管理规范")
    ap.add_argument("--source-url", required=True, help="原文页面 URL（必填，用于溯源）")
    ap.add_argument("--platform", default="all", help="douyin / taobao / jd / all")
    ap.add_argument("--doc-type", default="平台规则")
    ap.add_argument("--publish-date", default="")
    ap.add_argument("--effective-from", default="")
    ap.add_argument("--mode", default="", choices=["", "platform3", "platform2", "legal"],
                    help="留空自动识别；识别错时可强制指定")
    ap.add_argument("--file", default="", help="正文文件；留空读标准输入")
    ap.add_argument("--date-tag", default=date.today().isoformat())
    a = ap.parse_args()

    body = (Path(a.file).read_text(encoding="utf-8", errors="ignore")
            if a.file else sys.stdin.read())
    if not body.strip():
        sys.exit("正文为空，没东西可入库")
    if body.lstrip().startswith("---"):
        sys.exit("正文已带元信息头，直接放 data/raw/ 后跑 clause_splitter.py 即可")

    head = ["---",
            f"doc_name: {a.name}",
            f"doc_type: {a.doc_type}",
            f"platform: {a.platform}"]
    if a.publish_date:
        head.append(f"publish_date: {a.publish_date}")
    if a.effective_from:
        head.append(f"effective_from: {a.effective_from}")
    head.append(f"source_url: {a.source_url}")
    if a.mode:
        head.append(f"mode: {a.mode}")
    head.append("---")

    root = Path(__file__).resolve().parent.parent
    safe = a.name.replace("/", "_").replace("\\", "_").strip()
    out = root / "data" / "raw" / f"{safe}_{a.date_tag}.md"
    out.write_text("\n".join(head) + "\n\n" + body.strip() + "\n", encoding="utf-8")

    print(f"已写入 {out}")
    print(f"正文 {len(body)} 字，{body.count(chr(10)) + 1} 行")
    print("下一步：python pipeline/clause_splitter.py && python pipeline/build_store.py")


if __name__ == "__main__":
    main()
