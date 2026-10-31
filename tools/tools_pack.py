"""打包可交付件：产出 dist/rules_rag-<version>.zip + 校验清单。

为什么需要它：这个仓库根目录有一堆不该进交付物的东西 ——
logs/ 里 10520 个文件、1150 MB 的 Edge 浏览器 profile，还有 .env、.jwt_secret、__pycache__。
靠人手工挑文件，一定会漏。

用法：
  python tools/tools_pack.py                 # 打包
  python tools/tools_pack.py --list          # 只列清单不打包
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION = "1.0.0"

# 交付物白名单：只带"跑起来 + 能演示 + 能验证"的东西
INCLUDE_DIRS = ("api", "service", "retrieval", "generation", "nlp", "pipeline",
                "eval", "web", "static", "tests", "docs", "data", "tools")
INCLUDE_FILES = ("app.py", "common.py", "run.py", "requirements.txt", "README.md",
                 "Dockerfile", "docker-compose.yml", ".dockerignore", ".gitignore",
                 ".env.example", "start.bat", "stop.bat", ".gitattributes")

EXCLUDE_DIR_NAMES = {"__pycache__", "logs", "dist", ".git", ".github", "shots"}
EXCLUDE_SUFFIX = {".pyc", ".bak", ".tmp", ".npz", ".pdf"}
EXCLUDE_REL = {"data/processed/clauses.json"}      # 3.4 MB 派生数据，可由 pipeline 重建


def wanted(p: Path) -> bool:
    rel = p.relative_to(ROOT)
    parts = set(rel.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return False
    if p.suffix.lower() in EXCLUDE_SUFFIX:
        return False
    if str(rel).replace("\\", "/") in EXCLUDE_REL:
        return False
    if p.name in (".env", ".jwt_secret") or p.name.endswith(".log"):
        return False
    # e2e 套件每次运行都会在 data/ 下生成临时库（_cs_e2e.db / _board_e2e.db ...）。
    # 只有种子库 rules.db 该进交付物，其余都是跑出来的。
    # 注意 .db-wal / .db-shm 的 suffix 不是 ".db"，早先只判断前者，
    # 结果把 7.7 MB 的 e2e WAL 一起打进了包里。
    if p.name != "rules.db":
        low = p.name.lower()
        if low.endswith(".db") or low.endswith(".db-wal") or low.endswith(".db-shm"):
            return False
    return True


def collect() -> list[Path]:
    out: list[Path] = []
    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.exists() and p.is_file():
            out.append(p)
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and wanted(p):
                out.append(p)
    # 评测/看板/变更记录这些证据产物，明确带上
    for name in ("reports/dashboard.html", "reports/dashboard_data.js",
                 "reports/baseline.json", "reports/regression_latest.json",
                 "reports/augment_report.json", "reports/split_report.md",
                 "data/rules.db", "data/eval/eval_300.csv", "data/rule_version.json"):
        p = ROOT / name
        if p.exists() and p.is_file():
            out.append(p)
    return sorted(set(out), key=lambda p: str(p).lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    files = collect()
    total = sum(p.stat().st_size for p in files)
    print(f"[pack] 交付文件 {len(files)} 个，合计 {total / 1024 / 1024:.2f} MB")
    if a.list:
        for p in files:
            print(f"  {p.stat().st_size:>9}  {p.relative_to(ROOT)}")
        return

    DIST.mkdir(exist_ok=True)
    name = f"rules_rag-{VERSION}.zip"
    out = DIST / name
    manifest = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            z.write(p, f"rules_rag/{rel}")
            manifest.append({"path": rel, "bytes": p.stat().st_size,
                             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16]})

    meta = {
        "name": name, "version": VERSION,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": len(files), "uncompressed_bytes": total,
        "zip_bytes": out.stat().st_size,
        "manifest": manifest,
    }
    (DIST / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[pack] 输出 {out.relative_to(ROOT)}  "
          f"{out.stat().st_size / 1024 / 1024:.2f} MB（压缩后）")
    print(f"[pack] 清单 {str((DIST / 'manifest.json').relative_to(ROOT))}")
    big = sorted(files, key=lambda p: -p.stat().st_size)[:6]
    print("[pack] 体积最大的 6 个文件：")
    for p in big:
        print(f"    {p.stat().st_size / 1024:>8.1f} KB  {p.relative_to(ROOT)}")
    leaked = [p for p in files if p.name in (".env", ".jwt_secret") or "logs" in p.parts]
    print("[pack] 敏感/垃圾文件泄漏检查：" + ("通过（0 个）" if not leaked else f"失败 {len(leaked)} 个"))


if __name__ == "__main__":
    main()
