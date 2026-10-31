"""条款级切分器：把法律/规章全文切成"一条一个单元"。

设计要点（与 docs/design/00、docs/design/04 一致）：
1. 绝不跨条合并 —— 引用粒度 = 切分粒度。
2. 保留条款号原样（"第二十五条"不归一化成 25），因为答案要原样引用。
3. 目录页丢弃，防止把目录当条文。
4. 章/节作为 clause_path 保留，供检索时做层级过滤与展示。

支持输入：HTML（<p> 结构）与 .docx（word/document.xml）。
输出：list[dict]，字段见 CLAUSE_FIELDS。
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 常量

CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}

CLAUSE_RE = re.compile(r"^第([一二三四五六七八九十百零]+)条")
CHAPTER_RE = re.compile(r"^第([一二三四五六七八九十百零]+)章\s*(.*)$")
SECTION_RE = re.compile(r"^第([一二三四五六七八九十百零]+)节\s*(.*)$")
INLINE_CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零]+条")

CLAUSE_FIELDS = ("doc_name", "clause_no", "clause_num", "clause_path",
                 "chapter", "section", "content", "order_index", "char_start")


def cn2int(s: str) -> int:
    """中文数字转整数，覆盖 1–999（法条编号范围足够）。"""
    if not s:
        return -1
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in CN_NUM:
            num = CN_NUM[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return -1
    return total + section + num

# ---------------------------------------------------------------- 取文本


def paragraphs_from_html(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    raw = re.sub(r"<script[\s\S]*?</script>", "", raw, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", "", raw, flags=re.I)
    ps = re.findall(r"<p[^>]*>([\s\S]*?)</p>", raw, flags=re.I)
    out = []
    for p in ps:
        t = html.unescape(re.sub(r"<[^>]+>", "", p))
        t = t.replace("\u3000", " ").replace("\xa0", " ").strip()
        if len(t) > 1:
            out.append(t)
    return out


def paragraphs_from_docx(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    out = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        ts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S)
        t = html.unescape("".join(ts)).replace("\u3000", " ").strip()
        # docx 里偶发把 <w:autoSpaceDE/> 之类当文本带出来，过滤掉
        if not t or t.startswith("<w:"):
            continue
        out.append(t)
    return out

# ---------------------------------------------------------------- 切分


def split_clauses(paragraphs: list[str], doc_name: str) -> list[dict]:
    # 找到第一条出现的位置，之前的全是标题/目录/公布信息
    start = next((i for i, p in enumerate(paragraphs) if CLAUSE_RE.match(p)), None)
    if start is None:
        raise ValueError(f"{doc_name}: 未找到任何条款，检查正文提取是否失败")

    clauses: list[dict] = []
    chapter = section = ""
    cur: dict | None = None
    offset = 0

    for p in paragraphs[start:]:
        m_ch = CHAPTER_RE.match(p)
        m_se = SECTION_RE.match(p)
        m_cl = CLAUSE_RE.match(p)

        if m_ch and len(p) < 40:            # 排除正文里"第三章规定……"这类引用
            chapter, section = p.strip(), ""
            cur = None
            continue
        if m_se and len(p) < 40:
            section = p.strip()
            cur = None
            continue

        if m_cl:
            no = m_cl.group(1)
            body = p[m_cl.end():].strip()
            cur = {
                "doc_name": doc_name,
                "clause_no": f"第{no}条",
                "clause_num": cn2int(no),
                "chapter": chapter,
                "section": section,
                "content": body,
                "char_start": offset,
            }
            clauses.append(cur)
        elif cur is not None:
            # 续行：条文内换段（第二款、第（一）项等），并入上一条
            cur["content"] = (cur["content"] + "\n" + p).strip()
        offset += len(p) + 1

    for i, c in enumerate(clauses):
        c["order_index"] = i
        parts = [x for x in (c["chapter"], c["section"], c["clause_no"]) if x]
        c["clause_path"] = " / ".join(parts)
    return clauses

# ---------------------------------------------------------------- 平台规则（x.y.z 体系）
#
# 法条用"第X条"，平台规则用 2.3.1 这种多级编号，两套编号不能共用一套解析。
# 平台规则的两条纪律与法条一致：不跨条合并、保留原编号（答案要原样引用 2.3.1）。

FRONT_MATTER_RE = re.compile(r"^---[ \t]*\n([\s\S]*?)\n---[ \t]*\n")
PLAT3_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){2})(?![\d.])\s*[、.．]?\s*(.*)$")
PLAT2_RE = re.compile(r"^(\d{1,2}\.\d{1,2})(?![\d.])\s*[、.．]?\s*(\S.*)$")


def parse_front_matter(text: str) -> tuple[dict, str]:
    """平台规则原文带 --- 元信息头 ---，收集者在此填 doc_name/platform/生效日期/source_url。"""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def paragraphs_from_text(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    _, raw = parse_front_matter(raw)
    out = []
    for line in raw.splitlines():
        t = line.replace("\u3000", " ").replace("\xa0", " ").strip()
        t = re.sub(r"^[#>*\-+]+\s*", "", t)      # markdown 标记
        t = re.sub(r"\*\*|__", "", t)
        if len(t) > 1:
            out.append(t)
    return out


def detect_mode(paragraphs: list[str]) -> str:
    """自动判断文体：法条（第X条）/ 平台三级编号 / 平台二级编号。"""
    legal = sum(1 for p in paragraphs if CLAUSE_RE.match(p))
    plat3 = sum(1 for p in paragraphs if PLAT3_RE.match(p))
    plat2 = sum(1 for p in paragraphs if PLAT2_RE.match(p))
    if plat3 >= 3 and plat3 >= legal:
        return "platform3"
    if legal >= 3:
        return "legal"
    if plat2 >= 3:
        return "platform2"
    return "legal" if legal else "unknown"


# 混合层级：同时认 "2.1" 与 "2.3.1"。
# 踩过的坑：只认 level=3 时，"2.1 目的" 被当成小节标题不切分，
# 《商家售后服务管理规范》只切出 13 条（应为 20+ 条）。
PLAT_MIX_RE = re.compile(r"^(\d+(?:\.\d+){1,2})(?=[\s、．.]|$)")


def split_platform(paragraphs: list[str], doc_name: str, level: int = 0) -> list[dict]:
    """切分平台规则。level=0（默认）为混合模式，同时切 x.y 与 x.y.z。

    父条处理：若某条只有标题（正文字数 < 12）且紧接着出现更深的子编号，
    说明它是容器而非条款，丢弃，避免产生"空壳条款"。
    """
    clause_re = PLAT_MIX_RE if level == 0 else (PLAT3_RE if level == 3 else PLAT2_RE)
    clauses: list[dict] = []
    chapter = section = ""
    cur: dict | None = None
    offset = 0

    for p in paragraphs:
        m_ch = CHAPTER_RE.match(p)
        if m_ch and len(p) < 40:          # "第二章 基础规范"
            chapter, section, cur = p.strip(), "", None
            offset += len(p) + 1
            continue

        m_cl = clause_re.match(p)
        if m_cl and len(m_cl.group(1)) <= 10:
            no = m_cl.group(1)
            depth = no.count(".")
            body = p[m_cl.end(1):].strip()
            # 上一个编号是父容器（只有标题、且层级更浅）→ 丢弃，不产空壳条款
            if (cur is not None and clauses and cur is clauses[-1]
                    and len(cur["content"].strip()) < 12
                    and depth > cur["_depth"]):
                clauses.pop()
            cur = {
                "doc_name": doc_name,
                "clause_no": no,
                "clause_num": None,       # 平台编号非整数，排序靠 order_index
                "chapter": chapter,
                "section": section,
                "content": body,
                "char_start": offset,
                "_depth": depth,
            }
            clauses.append(cur)
        elif cur is not None:
            # 标题行之后的正文、表格行（售后类型/时限）都并入本条
            cur["content"] = (cur["content"] + "\n" + p).strip()
        offset += len(p) + 1

    for i, c in enumerate(clauses):
        c.pop("_depth", None)
        c["order_index"] = i
        c["clause_path"] = " / ".join(
            x for x in (c["chapter"], c["section"], c["clause_no"]) if x)
    return clauses

# ---------------------------------------------------------------- 校验


def validate(clauses: list[dict]) -> dict:
    nums = [c.get("clause_num") for c in clauses]
    # 平台规则的编号是 2.3.1，无法做整数连续性校验，只校验法条
    gaps = ([] if any(n is None for n in nums) else
            [(nums[i - 1], nums[i]) for i in range(1, len(nums)) if nums[i] != nums[i - 1] + 1])
    cross = 0
    for i, c in enumerate(clauses[:-1]):
        nxt = clauses[i + 1]["clause_no"]
        if nxt in c["content"]:          # 本条正文里出现了下一条的条文号 = 跨条
            cross += 1
    empty = [c["clause_no"] for c in clauses if len(c["content"]) < 4]
    return {
        "count": len(clauses),
        "sequence_gaps": gaps,
        "cross_clause_violations": cross,
        "empty_clauses": empty,
        "avg_len": round(sum(len(c["content"]) for c in clauses) / max(len(clauses), 1), 1),
    }

# ---------------------------------------------------------------- 文档元数据

DOC_META = {
    "电子商务法": {
        "doc_type": "法律", "platform": "all", "category": "all",
        "publish_date": "2018-08-31", "effective_from": "2019-01-01",
        "source_url": "http://www.npc.gov.cn/npc//c2/c30834/201905/t20190521_281583.html",
    },
    "网络交易监督管理办法": {
        "doc_type": "部门规章", "platform": "all", "category": "all",
        "publish_date": "2021-03-15", "effective_from": "2021-05-01",
        "source_url": "https://www.samr.gov.cn/cms_files/filemanager/1647978232/attach/20253/1712eca4467b4645bfe31872a52be673.docx",
    },
    "消费者权益保护法": {
        "doc_type": "法律", "platform": "all", "category": "all",
        "publish_date": "2013-10-25", "effective_from": "2014-03-15",
        "source_url": "http://www.npc.gov.cn/npc///c1773/c1848/c21114/c21170/c21174/201905/t20190521_210511.html",
    },
    "广告法": {
        "doc_type": "法律", "platform": "all", "category": "all",
        "publish_date": "2015-04-24", "effective_from": "2015-09-01",
        "source_url": "https://www.gov.cn/guoqing/2021-10/29/content_5647620.htm",
    },
    # 补入：这条是"退货运费谁承担""七天从哪天算"的直接依据（第十八条、第十条）
    "网络购买商品七日无理由退货暂行办法": {
        "doc_type": "部门规章", "platform": "all", "category": "all",
        "publish_date": "2020-11-03", "effective_from": "2017-03-15",
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_26ca8fe29e184edd899fa0a7a060d935.html",
    },
    # 补入：广告可识别性、直播带货责任、变相医疗广告（对应"标题/口播怎么算违规"）
    "互联网广告管理办法": {
        "doc_type": "部门规章", "platform": "all", "category": "all",
        "publish_date": "2023-02-25", "effective_from": "2023-05-01",
        "source_url": "https://www.samr.gov.cn/cms_files/filemanager/1647978232/attach/20234/W020230320579023676881.docx",
    },
}


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    raw_dir, out_dir = root / "data" / "raw", root / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_clauses, report = [], []
    for f in sorted(raw_dir.iterdir()):
        if f.suffix.lower() not in (".html", ".htm", ".docx", ".md", ".txt"):
            continue
        if f.name.startswith("_"):          # _ 开头是探测/临时文件
            continue

        sfx, fm = f.suffix.lower(), {}
        if sfx == ".docx":
            paras = paragraphs_from_docx(f)
        elif sfx in (".md", ".txt"):
            fm, _ = parse_front_matter(f.read_text(encoding="utf-8", errors="ignore"))
            paras = paragraphs_from_text(f)
        else:
            paras = paragraphs_from_html(f)

        doc_name = fm.get("doc_name") or f.stem.split("_")[0]
        mode = fm.get("mode") or detect_mode(paras)
        if mode == "unknown":
            raise ValueError(f"{f.name}: 既无'第X条'也无 x.y.z 编号，拒绝切分（宁可报错不产出垃圾）")

        if mode == "platform3":
            clauses = split_platform(paras, doc_name, 0)
        elif mode == "platform2":
            clauses = split_platform(paras, doc_name, 0)
        else:
            clauses = split_clauses(paras, doc_name)

        meta = dict(DOC_META.get(doc_name, {}))
        meta.update({k: v for k, v in fm.items()
                     if k in ("doc_type", "platform", "category",
                              "publish_date", "effective_from", "source_url")})
        for c in clauses:
            c.update(meta)

        v = validate(clauses)
        report.append({"doc": doc_name, "file": f.name, "mode": mode, **v,
                       "chars": sum(len(c["content"]) for c in clauses)})
        all_clauses.extend(clauses)

    # 全局重排 order_index
    for i, c in enumerate(all_clauses):
        c["order_index"] = i

    (out_dir / "clauses.json").write_text(
        json.dumps(all_clauses, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# 条款切分报告", "",
             "| 文档 | 文体 | 条数 | 平均字数 | 序号断点 | 跨条违规 | 空条款 |",
             "|---|---|---|---|---|---|---|"]
    for r in report:
        lines.append(f"| {r['doc']} | {r['mode']} | {r['count']} | {r['avg_len']} | "
                     f"{len(r['sequence_gaps'])} | {r['cross_clause_violations']} | {len(r['empty_clauses'])} |")
    lines += ["", f"**总计 {len(all_clauses)} 条**，"
                  f"跨条违规合计 {sum(r['cross_clause_violations'] for r in report)}。", ""]
    for r in report:
        if r["sequence_gaps"]:
            lines.append(f"- {r['doc']} 序号断点：{r['sequence_gaps'][:12]}")
    (root / "reports" / "split_report.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines))
    print(f"\n输出：{out_dir / 'clauses.json'}")


if __name__ == "__main__":
    main()
