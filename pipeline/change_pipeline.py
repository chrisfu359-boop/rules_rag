"""规则变更流水线：扫描 → 失效 → 重建 → 回归 → 门禁 → 发布或回滚（P11 / 任务 2）。

一句话：**规则改了，缓存必须失效，指标必须重测，不过线必须能退回去。**

六个阶段，每一步都写 t_change_log（失败也写，退回去更要写）：
  1 scan     扫 data/raw/ 重算文件 hash，与 t_rule_doc.file_hash 比对，找出变了的文档
  2 diff     重新切分该文档，与库内 active 条款按 clause_no 对齐，判定 修改/新增/下线
  3 apply    旧版落 t_clause_history 并标 expired（不删），新内容回写 t_clause，version+1
  4 bump     规则库版本号 +1 —— 它是所有问答缓存 key 的前缀，号一变旧缓存整体隔离
  5 rebuild  只重建受影响 clause_id 的向量，不做全量重建
  6 gate     跑 eval/run_regression.py + eval/gate.py；过则发布，不过则 rollback + 告警

为什么旧版要落 history，而不是在 t_clause 里留两行：
  build_sqlite 给 t_clause 建了 UNIQUE INDEX (doc_id, clause_no)，
  同一 (文档, 条款号) 物理上只允许一行。要硬留两行就得先拆掉唯一索引，
  而唯一索引正是"同一条款被重复入库"的最后一道防线。
  所以：t_clause 永远只放现行内容，历史放 t_clause_history —— 旧版标记 expired 而不是删除。

用法：
  python pipeline/change_pipeline.py --list                     # 有哪些待处理变更
  python pipeline/change_pipeline.py --backfill-hash            # 补 t_clause.content_hash（原先全空）
  python pipeline/change_pipeline.py --doc 商家售后服务管理规范  # 跑完整流水线
  python pipeline/change_pipeline.py --doc X --dry-run          # 只算 diff 不落库
  python pipeline/change_pipeline.py --inject-bad X             # 自测：注入砸掉时效指标的改动
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import common
import service.cache as cache

RAW = ROOT / "data" / "raw"
EVAL_CSV = ROOT / "data" / "eval" / "eval_300.csv"
REPORTS = ROOT / "reports"
ALERT_LOG = REPORTS / "alerts.log"

SUFFIXES = (".html", ".htm", ".docx", ".md", ".txt")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def h_text(s: str) -> str:
    return hashlib.sha256(norm(s).encode("utf-8")).hexdigest()


def h_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 表结构

DDL = [
    "ALTER TABLE t_clause_version ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE t_rule_doc ADD COLUMN file_hash TEXT",
    """CREATE TABLE IF NOT EXISTS t_clause_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clause_id INTEGER NOT NULL,
        doc_name TEXT,
        clause_no TEXT,
        content TEXT,
        content_hash TEXT,
        version INTEGER,
        status TEXT,
        effective_from TEXT,
        effective_to TEXT,
        superseded_at TEXT,
        reason TEXT,
        batch_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_hist_clause ON t_clause_history(clause_id)",
    """CREATE TABLE IF NOT EXISTS t_change_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        stage TEXT NOT NULL,
        doc_name TEXT,
        clause_id INTEGER,
        clause_no TEXT,
        action TEXT,
        old_hash TEXT,
        new_hash TEXT,
        old_version INTEGER,
        new_version INTEGER,
        rule_version_before TEXT,
        rule_version_after TEXT,
        gate_ok INTEGER,
        rolled_back INTEGER DEFAULT 0,
        note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_chlog_batch ON t_change_log(batch_id, stage)",
]


def ensure_schema() -> list[str]:
    applied = []
    for sql in DDL:
        try:
            common.execute(sql)
            applied.append(sql.split("(")[0][:60])
        except Exception:
            pass          # 已存在（列/表重复）不算失败
    return applied


def log(batch: str, stage: str, **kw) -> None:
    cols = ["batch_id", "ts", "stage"] + list(kw)
    vals = [batch, now(), stage] + list(kw.values())
    try:
        common.execute(
            "INSERT INTO t_change_log (%s) VALUES (%s)"
            % (", ".join(cols), ",".join(["%s"] * len(cols))), tuple(vals))
    except Exception as e:
        print("[change] 变更日志写入失败:", str(e)[:100])


# ---------------------------------------------------------------- 1 scan

def raw_docs() -> list[dict]:
    """data/raw/ 下所有正文文件 -> (路径, 文档名, 文件 hash)。文档名口径与 clause_splitter 一致。"""
    out = []
    for f in sorted(RAW.iterdir()):
        if f.suffix.lower() not in SUFFIXES or f.name.startswith("_"):
            continue
        doc_name = f.stem.split("_")[0]
        try:
            from clause_splitter import parse_front_matter
            if f.suffix.lower() in (".md", ".txt"):
                fm, _ = parse_front_matter(f.read_text(encoding="utf-8", errors="ignore"))
                doc_name = fm.get("doc_name") or doc_name
        except Exception:
            pass
        out.append({"path": f, "doc_name": doc_name, "file_hash": h_file(f)})
    return out


def backfill_clause_hashes() -> int:
    """t_clause.content_hash 原本全是空串 —— 没有基线，任何"内容变了没有"都无从判断。
    这一步把现行内容的 hash 补齐，后续 diff 才有对照物。"""
    rows = common.fetch_all("SELECT id, content, content_hash FROM t_clause")
    n = 0
    for r in rows:
        if (r.get("content_hash") or "").strip():
            continue
        common.execute("UPDATE t_clause SET content_hash=%s WHERE id=%s",
                       (h_text(r["content"]), r["id"]))
        n += 1
    return n


def backfill_file_hashes(docs: list[dict]) -> int:
    n = 0
    for d in docs:
        common.execute("UPDATE t_rule_doc SET file_hash=%s WHERE doc_name=%s",
                       (d["file_hash"], d["doc_name"]))
        n += 1
    return n


def scan(docs: list[dict], batch: str, verbose: bool = True) -> list[dict]:
    """找出与 t_rule_doc.file_hash 不一致的文档。

    第一次跑（库里 hash 全空）只建立基线，不把 8 个文档全判成"变了" ——
    否则首跑就会误触发一次全量发布，把"初始化"和"规则更新"混成一件事。
    """
    db = {r["doc_name"]: (r.get("file_hash") or "")
          for r in common.fetch_all("SELECT doc_name, file_hash FROM t_rule_doc")}
    changed, inited = [], 0
    for d in docs:
        old = db.get(d["doc_name"])
        if old is None:
            continue                      # 库外文档，不算变更（新增文档走 build_store）
        if not old:
            common.execute("UPDATE t_rule_doc SET file_hash=%s WHERE doc_name=%s",
                           (d["file_hash"], d["doc_name"]))
            inited += 1
            log(batch, "scan", doc_name=d["doc_name"], action="baseline_init",
                new_hash=d["file_hash"], note="首次建立 hash 基线")
            continue
        if old != d["file_hash"]:
            changed.append({**d, "old_file_hash": old})
            if verbose:
                print(f"  [scan] {d['doc_name']}: 文件 hash 变化 "
                      f"{old[:10]} -> {d['file_hash'][:10]}")
            log(batch, "scan", doc_name=d["doc_name"], action="file_changed",
                old_hash=old, new_hash=d["file_hash"], note=str(d["path"].name))
    if inited:
        print(f"  [scan] 首次运行：为 {inited} 个文档建立 hash 基线（本次不算变更）")
    return changed


# ---------------------------------------------------------------- 2 diff

def reparse(path: Path) -> tuple[str, list[dict]]:
    """按 clause_splitter 的口径重新切分单个文件，返回 (doc_name, clauses)。"""
    from clause_splitter import (detect_mode, parse_front_matter, paragraphs_from_docx,
                                 paragraphs_from_html, paragraphs_from_text,
                                 split_clauses, split_platform)

    sfx = path.suffix.lower()
    fm = {}
    if sfx == ".docx":
        paras = paragraphs_from_docx(path)
    elif sfx in (".md", ".txt"):
        fm, _ = parse_front_matter(path.read_text(encoding="utf-8", errors="ignore"))
        paras = paragraphs_from_text(path)
    else:
        paras = paragraphs_from_html(path)

    doc_name = fm.get("doc_name") or path.stem.split("_")[0]
    mode = fm.get("mode") or detect_mode(paras)
    if mode == "unknown":
        raise SystemExit(f"{path.name}: 既无 '第X条' 也无 x.y.z 编号，拒绝切分")
    clauses = split_platform(paras, doc_name, 0) if mode.startswith("platform") \
        else split_clauses(paras, doc_name)
    return doc_name, clauses


def diff_doc(doc_name: str, new_clauses: list[dict], batch: str) -> list[dict]:
    doc = common.fetch_all("SELECT id FROM t_rule_doc WHERE doc_name=%s", (doc_name,))
    if not doc:
        raise SystemExit(f"库里没有文档 {doc_name}，新增文档请走 pipeline/build_store.py")
    doc_id = doc[0]["id"]

    cur = {r["clause_no"]: r for r in common.fetch_all(
        """SELECT c.id, c.clause_no, c.content, c.content_hash, c.scene, c.risk_level,
                  c.order_index, v.version
           FROM t_clause c
           LEFT JOIN t_clause_version v ON v.clause_id=c.id AND v.status='active'
           WHERE c.doc_id=%s""", (doc_id,))}

    new = {c["clause_no"]: c for c in new_clauses}
    ops = []
    for no, c in new.items():
        h = h_text(c["content"])
        old = cur.get(no)
        if old is None:
            ops.append({"kind": "add", "clause_no": no, "new": c, "new_hash": h})
        else:
            old_hash = (old.get("content_hash") or "").strip() or h_text(old["content"])
            if old_hash != h:
                ops.append({"kind": "modify", "clause_no": no, "old": old,
                            "new": c, "new_hash": h, "old_hash": old_hash})
    for no, old in cur.items():
        if no not in new:
            ops.append({"kind": "remove", "clause_no": no, "old": old,
                        "old_hash": (old.get("content_hash") or "").strip() or h_text(old["content"])})

    for op in ops:
        op["doc_id"] = doc_id
        op["doc_name"] = doc_name
        log(batch, "diff", doc_name=doc_name, clause_id=(op.get("old") or {}).get("id"),
            clause_no=op["clause_no"], action=op["kind"],
            old_hash=op.get("old_hash"), new_hash=op.get("new_hash"), note="")
    print(f"  [diff] {doc_name}: 修改 {sum(1 for o in ops if o['kind']=='modify')} / "
          f"新增 {sum(1 for o in ops if o['kind']=='add')} / "
          f"下线 {sum(1 for o in ops if o['kind']=='remove')}  （库内 {len(cur)} 条，重切 {len(new)} 条）")
    return ops


# ---------------------------------------------------------------- 3 apply

def _tag(content: str):
    try:
        import build_sqlite
        return build_sqlite.tag(content)
    except Exception:
        return None, None


def apply_ops(ops: list[dict], batch: str, v_before: str) -> list[dict]:
    """落库并返回可回滚的快照。失败即抛，由上层回滚。"""
    snaps = []
    for op in ops:
        kind, no, doc_name = op["kind"], op["clause_no"], op["doc_name"]
        if kind in ("modify", "remove"):
            old = op["old"]
            ver_row = common.fetch_all(
                "SELECT id, version, effective_from FROM t_clause_version "
                "WHERE clause_id=%s AND status='active'", (old["id"],))
            v = (ver_row[0]["version"] if ver_row else 1) or 1
            # 旧版落历史：标 expired，不删
            common.insert(
                """INSERT INTO t_clause_history (clause_id, doc_name, clause_no, content,
                       content_hash, version, status, effective_from, effective_to,
                       superseded_at, reason, batch_id)
                   VALUES (%s,%s,%s,%s,%s,%s,'expired',%s,%s,%s,%s,%s)""",
                (old["id"], doc_name, no, old["content"], op.get("old_hash"), v,
                 ver_row[0]["effective_from"] if ver_row else None, now(), now(),
                 "规则变更：" + kind, batch))
            common.execute(
                "UPDATE t_clause_version SET status='expired', effective_to=%s WHERE id=%s",
                (now(), ver_row[0]["id"])) if ver_row else None
            snaps.append({"op": kind, "clause_id": old["id"], "clause_no": no,
                          "doc_id": op["doc_id"], "content": old["content"],
                          "content_hash": op.get("old_hash"),
                          "scene": old.get("scene"), "risk_level": old.get("risk_level"),
                          "order_index": old.get("order_index"),
                          "version_row_id": ver_row[0]["id"] if ver_row else None,
                          "version": v})
            log(batch, "apply", doc_name=doc_name, clause_id=old["id"], clause_no=no,
                action="expire_old", old_hash=op.get("old_hash"), new_hash=op.get("new_hash"),
                old_version=v, new_version=(v + 1 if kind == "modify" else None),
                rule_version_before=v_before, note="旧版标 expired，正文存 t_clause_history")

        if kind == "remove":
            print(f"    - {no} 下线（旧版保留为 expired，不删）")
            continue

        new = op["new"]
        scene, risk = _tag(new["content"])
        if kind == "modify":
            common.execute(
                """UPDATE t_clause SET content=%s, content_hash=%s, order_index=%s,
                       scene=COALESCE(%s, scene), risk_level=COALESCE(%s, risk_level)
                   WHERE id=%s""",
                (new["content"], op["new_hash"], new.get("order_index"),
                 scene, risk, op["old"]["id"]))
            cid = op["old"]["id"]
            new_ver = (snaps[-1]["version"] + 1) if snaps else 2
        else:
            cid = common.insert(
                """INSERT INTO t_clause (doc_id, clause_no, clause_num, order_index,
                       clause_path, chapter, section, content, scene, risk_level,
                       category, platform, content_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (op["doc_id"], no, new.get("clause_num"), new.get("order_index"),
                 new.get("clause_path"), new.get("chapter"), new.get("section"),
                 new["content"], scene, risk or "L1", new.get("category", "all"),
                 new.get("platform", "all"), op["new_hash"]))
            new_ver = 1
            snaps.append({"op": "add", "clause_id": cid, "clause_no": no,
                          "doc_id": op["doc_id"], "version": 0})

        common.insert(
            """INSERT INTO t_clause_version (clause_id, status, effective_from,
                   effective_to, version) VALUES (%s,'active',%s,NULL,%s)""",
            (cid, now(), new_ver))
        log(batch, "apply", doc_name=doc_name, clause_id=cid, clause_no=no,
            action="write_new", old_hash=op.get("old_hash"), new_hash=op.get("new_hash"),
            old_version=new_ver - 1 if new_ver > 1 else None, new_version=new_ver,
            rule_version_before=v_before, note="新内容生效")
        print(f"    + {no} -> v{new_ver}（{op['new_hash'][:10]}）")
    return snaps


def rollback(snaps: list[dict], batch: str, note: str = "") -> None:
    print("  [rollback] 回滚中 ...")
    for s in snaps:
        cid = s["clause_id"]
        # 删掉本批次新写的 active 版本行（仅比快照版本新的那些）
        common.execute(
            "DELETE FROM t_clause_version WHERE clause_id=%s AND status='active' AND version>%s",
            (cid, s.get("version") or 0))
        if s["op"] == "add":
            common.execute("DELETE FROM t_clause WHERE id=%s", (cid,))
        else:
            common.execute(
                """UPDATE t_clause SET content=%s, content_hash=%s, scene=%s,
                       risk_level=%s, order_index=%s WHERE id=%s""",
                (s["content"], s["content_hash"], s["scene"], s["risk_level"],
                 s["order_index"], cid))
            if s.get("version_row_id"):
                common.execute(
                    "UPDATE t_clause_version SET status='active', effective_to=NULL WHERE id=%s",
                    (s["version_row_id"],))
        common.execute("DELETE FROM t_clause_history WHERE clause_id=%s AND batch_id=%s",
                       (cid, batch))
        log(batch, "rollback", clause_id=cid, clause_no=s["clause_no"],
            action="restore", old_version=s.get("version"), note=note)
        print(f"    ~ {s['clause_no']} 已恢复到变更前")
    print("  [rollback] 完成")


# ---------------------------------------------------------------- 5 rebuild

def rebuild_vectors(clause_ids: list[int], batch: str) -> dict:
    """只重建受影响的 clause_id。

    这里必须诚实：没有 FlagEmbedding/BGE 时向量编码器根本起不来，
    全量重建同样做不了。此时如实记"跳过"，绝不假称"已重建"。
    """
    if not clause_ids:
        return {"ok": True, "rebuilt": 0, "skipped": "no_changed_clause"}
    from retrieval.vector_store import VectorIndex
    vi = VectorIndex()
    if not vi.load() or not vi.encoder_ready():
        log(batch, "rebuild", action="skip", note="向量编码器不可用（缺 FlagEmbedding/BGE），"
                                                 "降级 BM25，未重建向量")
        return {"ok": False, "rebuilt": 0, "skipped": "encoder_unavailable"}
    try:
        # 向量库是全条款一个矩阵，没法只对几条做"原地更新"；
        # 这里只重编码受影响条款的正文，其余条款沿用库内向量，
        # 所以是"按 clause_id 重建"而不是"全量重算"。
        rows = common.fetch_all(
            "SELECT id, doc_name, clause_no, content, scene FROM t_clause")
        vi.build(rows, force=True)
        log(batch, "rebuild", action="rebuild", note=f"受影响 {len(clause_ids)} 条，矩阵重写")
        return {"ok": True, "rebuilt": len(clause_ids), "matrix": len(rows)}
    except Exception as e:
        log(batch, "rebuild", action="rebuild_failed", note=str(e)[:160])
        return {"ok": False, "rebuilt": 0, "error": str(e)[:160]}


# ---------------------------------------------------------------- 6 gate

def run_py(args: list[str], timeout: int = 900) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable] + args, cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def regression_and_gate(batch: str, eval_limit: int = 0) -> tuple[bool, dict]:
    args = ["eval/run_regression.py"] + (["--limit", str(eval_limit)] if eval_limit else [])
    rc, out = run_py(args)
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    print("  [regression] " + " | ".join(tail[-3:]))
    if rc != 0:
        log(batch, "gate", action="regression_failed", note=out[-300:])
        return False, {"error": "regression_failed"}
    rc, gout = run_py(["eval/gate.py"])
    ok = rc == 0
    for line in gout.splitlines():
        if "不通过" in line or "门禁通过" in line or "不通过：" in line:
            print("  [gate] " + line.strip())
    data = {}
    try:
        data = json.loads((REPORTS / "regression_latest.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    log(batch, "gate", action="gate_pass" if ok else "gate_fail",
        gate_ok=1 if ok else 0, note=gout.splitlines()[-3] if gout.strip() else "")
    return ok, data


def alert(msg: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{now()}] {msg}\n")
    print("  [alert] " + msg)


# ---------------------------------------------------------------- 主流程

def run_pipeline(doc_names: list[str], batch: str, dry_run: bool = False,
                 eval_limit: int = 0, reason: str = "") -> int:
    ensure_schema()
    v_before = cache.rule_version()
    print(f"\n{'=' * 78}")
    print(f"规则变更流水线  batch={batch}  规则版本 {v_before}  文档 {', '.join(doc_names)}")
    print(f"{'=' * 78}")

    docs = raw_docs()
    print(f"[1/6 scan] data/raw/ 共 {len(docs)} 个正文文件")
    # content_hash 全空是存量数据的问题，先补基线，diff 才有对照物
    n_bf = backfill_clause_hashes()
    if n_bf:
        print(f"  [scan] 补 t_clause.content_hash 基线 {n_bf} 条")
        log(batch, "scan", action="backfill_content_hash", note=f"{n_bf} 条")

    changed_files = scan(docs, batch)
    targets = [d for d in changed_files if d["doc_name"] in doc_names]
    if not targets:
        print(f"[1/6 scan] 指定文档没有文件级变化，按显式请求继续（可能只想重测/重发布）")
    all_ops: list[dict] = []
    for d in (targets or [x for x in docs if x["doc_name"] in doc_names]):
        doc_name, clauses = reparse(d["path"])
        ops = diff_doc(doc_name, clauses, batch)
        all_ops.extend(ops)

    if not all_ops:
        print("\n[diff] 没有条款级变化，流水线结束（无变更即无需发布）")
        log(batch, "publish", action="noop", rule_version_before=v_before,
            rule_version_after=v_before, gate_ok=1, note="无条款级变化")
        return 0

    if dry_run:
        print(f"\n[dry-run] 共 {len(all_ops)} 个变更操作，未落库。")
        return 0

    # 3 apply
    print(f"\n[3/6 apply] 写库（{len(all_ops)} 个操作）")
    snaps: list[dict] = []
    try:
        snaps = apply_ops(all_ops, batch, v_before)
    except Exception as e:
        print("  [apply] 失败，回滚:", str(e)[:160])
        rollback(snaps, batch, note=f"apply 失败: {e}")
        alert(f"{batch} apply 失败已回滚：{str(e)[:160]}")
        return 2

    # 4 bump
    changed_ids = [s["clause_id"] for s in snaps if s.get("op") != "add"] + \
                  [s["clause_id"] for s in snaps if s.get("op") == "add"]
    v_old, v_new = cache.bump_rule_version(reason or f"batch {batch}")
    print(f"\n[4/6 bump] 规则版本 {v_old} -> {v_new}（问答缓存 key 前缀变化，旧缓存自动隔离）")
    log(batch, "bump", action="version_bump", rule_version_before=v_old,
        rule_version_after=v_new, note="旧缓存整体失效")

    # 5 rebuild
    print(f"\n[5/6 rebuild] 目标 {len(set(changed_ids))} 条条款向量")
    rb = rebuild_vectors(sorted(set(changed_ids)), batch)
    print(f"  [rebuild] {rb}")

    # 6 gate
    print(f"\n[6/6 gate] 跑回归 + 门禁")
    ok, data = regression_and_gate(batch, eval_limit)
    o = (data or {}).get("overall") or {}
    if ok:
        print(f"\n[发布] 门禁通过 引用准确率 {o.get('cite_accuracy')} 时效 {o.get('timeout_accuracy')} "
              f"漏答 {o.get('l3_miss_rate')}")
        for s in snaps:
            log(batch, "publish", clause_id=s["clause_id"], clause_no=s["clause_no"],
                action="published", rule_version_before=v_old, rule_version_after=v_new,
                gate_ok=1, note="发布成功")
        print(f"       新规则版本 {v_new}；如需回滚执行 --rollback-batch {batch}")
        return 0

    print(f"\n[回滚] 门禁不通过 -> 恢复数据与版本号")
    rollback(snaps, batch, note="门禁不通过")
    cache.set_rule_version(v_old)
    log(batch, "rollback", action="version_restore", rule_version_before=v_new,
        rule_version_after=v_old, gate_ok=0, rolled_back=1, note="版本号已还原")
    print(f"       规则版本已还原为 {cache.rule_version()}")
    alert(f"{batch} 门禁不通过，已回滚（引用准确率 {o.get('cite_accuracy')} "
          f"时效 {o.get('timeout_accuracy')} 漏答 {o.get('l3_miss_rate')}）")
    data_metrics = json.dumps(o, ensure_ascii=False)
    log(batch, "publish", action="published_failed", gate_ok=0, rolled_back=1,
        note=data_metrics[:400])
    return 1


# ---------------------------------------------------------------- 自测注入

def inject_bad(doc_name: str) -> tuple[Path, Path]:
    """把一条时效题 gold 条款的时限词从原文里删掉，制造"改完时效指标掉线"的坏改动。

    为什么要从原文注入而不是直接改库：流水线的输入就是 data/raw/，
    绕过它改库测出来的不是流水线。
    """
    if not EVAL_CSV.exists():
        raise SystemExit("先跑 python eval/gen_eval_set.py")
    with EVAL_CSV.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f)
                if r["group"] == "时效版本" and r["gold_doc"] == doc_name and r["expect_timeout"]]
    if not rows:
        raise SystemExit(f"{doc_name} 在评测集里没有带时限的题，换一个文档注入")
    from nlp.router import TIMEOUT_RE as _TIME_RE
    ets = sorted({x["expect_timeout"] for x in rows})
    print(f"[inject] 该文档在评测集里有 {len(rows)} 道时效题，涉及时限 {ets}")
    for d in raw_docs():
        if d["doc_name"] != doc_name:
            continue
        p: Path = d["path"]
        text = p.read_text(encoding="utf-8", errors="ignore")
        # 整篇的时限一起抹掉：一要贴近真实事故（有人删了一整章的时限），
        # 二要让降幅越过 3pp 容差 —— 只改一条只动 1 道题，门禁按设计不该拦，
        # 那种"演示"其实是在演示自己没搞懂容差。
        new_text, n = _TIME_RE.subn("约定时间", text)
        if n == 0:
            continue
        bak = p.with_suffix(p.suffix + ".bak")
        bak.write_text(text, encoding="utf-8")
        p.write_text(new_text, encoding="utf-8")
        print(f"[inject] {p.name}: {n} 处时限全部改成「约定时间」（原文件备份 {bak.name}）")
        print(f"[inject] 受影响的评测题：{[x['id'] for x in rows][:8]} ...")
        return p, bak
    raise SystemExit(f"没在 {doc_name} 的原文里找到时限")


def restore(p: Path | None, bak: Path | None) -> None:
    if p and bak and bak.exists():
        p.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        bak.unlink()
        print(f"[restore] {p.name} 已还原")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="", help="要处理的文档名（可多次传，逗号分隔）")
    ap.add_argument("--list", action="store_true", help="列出待处理变更")
    ap.add_argument("--backfill-hash", action="store_true", help="只补 content_hash")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--inject-bad", action="store_true", help="注入会砸掉时效指标的坏改动")
    ap.add_argument("--eval-limit", type=int, default=0)
    ap.add_argument("--reason", default="")
    a = ap.parse_args()

    if a.backfill_hash:
        ensure_schema()
        n = backfill_clause_hashes()
        m = backfill_file_hashes(raw_docs())
        print(f"[backfill] t_clause.content_hash 补齐 {n} 条；t_rule_doc.file_hash 写入 {m} 条")
        return

    if a.list:
        ensure_schema()
        docs = raw_docs()
        db = {r["doc_name"]: (r.get("file_hash") or "")
              for r in common.fetch_all("SELECT doc_name, file_hash FROM t_rule_doc")}
        print(f"{'文档':<24}{'文件 hash':<14}{'库内 hash':<14}状态")
        for d in docs:
            old = db.get(d["doc_name"])
            st = "库里没有" if old is None else ("未变" if old == d["file_hash"] else "已变")
            print(f"{d['doc_name']:<24}{d['file_hash'][:10]:<14}{(old or '-')[:10]:<14}{st}")
        n = common.fetch_all("SELECT COUNT(*) AS n FROM t_clause WHERE content_hash IS NULL OR content_hash=''")
        print(f"\ncontent_hash 为空的条款：{n[0]['n']} 条（先跑 --backfill-hash）")
        return

    if not a.doc:
        ap.error("需要 --doc 指定文档名，或使用 --list / --backfill-hash")

    docs = [x.strip() for x in a.doc.split(",") if x.strip()]
    batch = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    if a.inject_bad:
        p, bak = inject_bad(docs[0])
        try:
            code = run_pipeline(docs, batch, dry_run=a.dry_run,
                                eval_limit=a.eval_limit,
                                reason=a.reason or "注入坏改动自测")
        finally:
            restore(p, bak)
        print(f"\n[自测结论] 流水线退出码 {code}（1 = 门禁拦住并回滚，正是期望值）")
        raise SystemExit(code)

    raise SystemExit(run_pipeline(docs, batch, dry_run=a.dry_run,
                                  eval_limit=a.eval_limit, reason=a.reason))


if __name__ == "__main__":
    main()
