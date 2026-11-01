"""查询理解与风险路由（纯规则，单次 < 5ms）。

对应 docs/design/04 §1 ② 层：意图 → 场景/状态 → 风险等级。
不调 LLM 的理由见代码注释末尾：高频请求过不起大模型，这层是成本闸门。
"""
from __future__ import annotations

import re

SCENE_KW = {
    "aftersale.return": ["退货", "退款", "仅退款", "换货", "修理", "七日", "无理由", "售后"],
    "logistics.delivery": ["发货", "物流", "快递", "签收", "运输", "交付", "揽收"],
    "marketing.ad": ["广告", "宣传", "极限词", "最高级", "最佳", "国家级", "虚假", "误导"],
    "price.label": ["价格", "标价", "明码", "收费", "押金", "运费", "费用"],
    "privacy.data": ["个人信息", "隐私", "收集信息", "手机号", "数据"],
    "platform.onboarding": ["入驻", "资质", "营业执照", "登记", "许可证", "开店"],
    "governance.penalty": ["处罚", "罚款", "扣分", "违规", "封店", "下架", "申诉"],
}

STATE_KW = [("已发货", "shipped"), ("未发货", "unshipped"), ("已签收", "received"),
            ("已退款", "refunded"), ("待付款", "unpaid")]

L3_KW = ["罚多少", "罚款", "会被罚", "处罚", "扣分", "封店", "封禁", "坐牢", "刑事责任",
         "吊销", "保证金扣", "会不会违法", "违法吗", "起诉", "法律责任",
         # 违规认定类：能不能用某词 / 算不算违规，属于定性判断，不由 AI 拍板
         "极限词", "最高级", "最佳", "国家级", "算不算违规", "违规吗", "能不能用", "是否违反"]
L2_KW = ["多久", "几天", "几小时", "多长时间", "时限", "期限", "日内", "什么时候", "多少天"]
# 时限单位的完整口径。原来只认「日/天/小时/工作日/个月」，于是
# "保存三年""四个星期内答复"这类时限永远进不了 timeouts 字段 ——
# 用户问"多久"，答复里也就没有那个数字。漏的不是表达力，是一条事实。
# (?!\d{3,}) 用它挡住 4 位年份："自2023年5月1日起施行"不是时限。
# (?!岁) 挡住"十四周岁"被截成"十四周"。
TIMEOUT_RE = re.compile(r"(?<![\d.])(?!\d{3,})(\d{1,3}|[一二三四五六七八九十百]{1,4})\s*个?"
                        r"(自然日|工作日|小时|分钟|日|天|周|个月|年)(?!岁)")


def detect(query: str) -> dict:
    best, best_hit = None, 0
    for sc, kws in SCENE_KW.items():
        hit = sum(1 for k in kws if k in query)
        if hit > best_hit:
            best, best_hit = sc, hit
    state = next((v for k, v in STATE_KW if k in query), None)
    actor = "merchant" if any(k in query for k in ["我", "商家", "卖家", "店铺", "被罚", "扣分"]) else "buyer"
    return {"scene": best, "state_in": state, "actor": actor, "scene_hits": best_hit}


def _bert_risk(query: str) -> str | None:
    """BERT 风险分类器；没装 torch / 没训练模型时返回 None，全部走规则。"""
    try:
        from nlp.bert_classifier import predict
        return predict(query)
    except Exception:
        return None


def risk(query: str, order: dict | None = None) -> tuple[str, str]:
    # ① L3 是安全线：规则命中必须转人工，绝不交给模型兜底
    if any(k in query for k in L3_KW):
        return "L3", "涉及处罚金额/法律定性，超出 AI 可答范围"
    # ② BERT 分类器主判（蒸馏自规则，泛化更好）
    bv = _bert_risk(query)
    if bv == "L3":
        return "L3", "BERT 判定涉及处罚金额/法律定性，超出 AI 可答范围"
    # ③ 时限/售后状态仍按规则判 L2：这两类靠关键词和订单态比模型更准
    if any(k in query for k in L2_KW):
        return "L2", "涉及时限，需给条款原文并提示以平台最新公告为准"
    if order and order.get("aftersale_type"):
        return "L2", "涉及售后处理时效"
    if bv == "L2":
        return "L2", "BERT 判定涉及时限或需给条款原文核验"
    return "L1", "定义/原则类问题，可直接依据条款回答"


def extract_timeouts(text: str) -> list[str]:
    out = []
    for m in TIMEOUT_RE.finditer(text):
        s = m.group(0).strip()
        if s not in out:
            out.append(s)
    return out[:4]


# 为什么不用 BERT / LLM：
# 1) 这层每次请求都要跑，LLM 单次 300–800ms、成本按 token 计，高频场景不划算；
# 2) 场景词表只有 7 类、风险词表 17 个，规则可解释、可审计，出错能立刻定位；
# 3) 后续要提升召回，按 drama_project 的级联思路升级（规则兜底 → 轻量模型 → 大模型），
#    而不是推翻重写。
