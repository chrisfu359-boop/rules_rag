"""大模型生成层：一套 OpenAI 兼容协议接所有厂商，填 key 即通。

为什么只用 OpenAI 兼容协议：
  DeepSeek / 通义千问(DashScope compatible-mode) / Kimi / 智谱 GLM / 本地 Ollama /
  vLLM / LM Studio 全部提供 /v1/chat/completions。写两套 SDK（openai + dashscope）
  等于维护两条代码路径、两套错误处理，收益为零。所以统一走 openai SDK，
  只改 base_url —— 换厂商是改配置，不是改代码。

配置优先级（后者覆盖前者）：
  1. LLM_PROVIDER 预设（deepseek/dashscope/moonshot/zhipu/ollama/openai）
  2. LLM_BASE_URL + LLM_API_KEY + LLM_MODEL 显式指定
  3. 都没有 → available() 为 False，上层自动退回模板生成，服务不会因为没配 key 挂掉

三个必须守住的工程点：
  * 超时：LLM 是外部依赖，必须有自己的超时上限，不能拖垮整个 /api/ask。
  * 降级：任何异常都返回 None，由调用方退回模板，绝不抛到接口层。
  * 可审计：每次调用记录 model / 耗时 / token，写进 trace 和审计日志。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 各厂商预设：base_url / 默认模型 / 取 key 的环境变量名
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "deepseek":  ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "DASHSCOPE_API_KEY"),
    "moonshot":  ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
    "zhipu":     ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "ZHIPU_API_KEY"),
    "openai":    ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "ollama":    ("http://127.0.0.1:11434/v1", "qwen2.5:7b", ""),
}

SYSTEM_PROMPT = """你是电商平台的「商家规则合规助手」，服务对象是平台商家和运营人员。

必须遵守的硬性规则：
1. 只能依据下面提供的【条款】回答，禁止使用条款之外的法律知识，禁止补充条款里没有的数字、期限、金额。
2. 每个结论后面必须标注来源，格式为「《法规名》第X条」或「《平台规则名》2.3.1」。
   只能用【条款】里出现过的编号，编造编号属于严重错误。
3. 涉及罚款金额、扣分后果、违法认定、诉讼胜败的问题，不要给结论，只说明"该事项需人工/法务判定"，
   并列出相关条款原文指向。
4. 条款之间冲突或存在版本差异时，指出冲突并提示以最新公布版本为准，不要替用户选一个。
5. 语言简洁，面向业务人员，不要写"根据您提供的信息"这类废话。

输出格式：严格的 JSON，不要包裹 markdown 代码块，不要输出 JSON 之外的任何字符。
{"conclusion": "回答正文，可含换行", "notice": "一句风险提示", "handoff": false}
handoff 为 true 表示需要转人工（涉及金额、定性、条款冲突）。"""


def _load_dotenv() -> None:
    """读项目根目录 .env。

    不用 python-dotenv 也行，但它在环境里现成，而且比手写解析靠谱（处理引号、注释、export 前缀）。
    只在进程启动时读一次，且不覆盖已有的真实环境变量——线上用容器注入 env 时优先级更高。
    """
    p = ROOT / ".env"
    if not p.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(p, override=False, encoding="utf-8")
    except Exception as e:
        print("[llm] .env 解析失败:", str(e)[:80])


class LLMClient:
    def __init__(self) -> None:
        _load_dotenv()

        provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        self.provider = provider or "custom"

        preset_url, preset_model, key_env = PROVIDERS.get(provider, ("", "", ""))

        self.base_url = (os.getenv("LLM_BASE_URL", "").strip() or preset_url).rstrip("/")
        self.model = os.getenv("LLM_MODEL", "").strip() or preset_model
        self.api_key = (os.getenv("LLM_API_KEY", "").strip()
                        or (os.getenv(key_env, "").strip() if key_env else "")
                        or ("ollama" if provider == "ollama" else ""))
        self.timeout = float(os.getenv("LLM_TIMEOUT", "20"))
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "600"))
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        self.enabled = os.getenv("LLM", "auto").strip().lower() not in ("0", "off", "false")

        self._client = None
        self._failed = False
        self.calls = 0
        self.failures = 0
        self.total_tokens = 0
        self.last_ms = 0.0
        self.last_error = ""

    # ---------- 可用性 ----------
    def available(self) -> bool:
        if not self.enabled or self._failed:
            return False
        return bool(self.base_url and self.model and (self.api_key or self.provider == "ollama"))

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            # max_retries=0：重试由我们自己控制（要带上"上次编造了哪些条款号"的上下文重试），
            # SDK 内部静默重试会让超时不可预测。
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key or "ollama",
                                  timeout=self.timeout, max_retries=0)
        return self._client

    def stats(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model or None,
            "base_url": self.base_url or None,
            "enabled": self.enabled,
            "available": self.available(),
            "calls": self.calls,
            "failures": self.failures,
            "total_tokens": self.total_tokens,
            "last_ms": self.last_ms,
            "last_error": self.last_error[:160] if self.last_error else "",
        }

    # ---------- 调用 ----------
    def chat(self, messages: list[dict], temperature: float | None = None,
             max_tokens: int | None = None) -> tuple[str | None, dict]:
        """返回 (文本, 元信息)。失败返回 (None, 元信息) 而不是抛异常。"""
        if not self.available():
            return None, {"error": "not_configured"}

        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens= max_tokens or self.max_tokens,
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
            self.last_ms = round((time.time() - t0) * 1000, 1)
            self.calls += 1
            self.total_tokens += tokens
            return text, {"ms": self.last_ms, "tokens": tokens, "model": self.model}
        except Exception as e:
            self.last_ms = round((time.time() - t0) * 1000, 1)
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[llm] 调用失败，本次降级模板: {self.last_error[:120]}")
            # 连续失败 5 次标记不可用，避免每个请求都白等一个超时
            if self.failures >= 5 and self.calls == 0:
                self._failed = True
                print("[llm] 连续失败，本次进程内关闭 LLM 通道")
            return None, {"error": self.last_error[:200], "ms": self.last_ms}


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


# ---------------- prompt 构造 ----------------

# ---------------- 上下文窗口 / 摘要压缩 ----------------

# 固定上下文预算（字符数，约等于 token 的 2 倍）：system 之外，
# 条款 + 历史 + 问题必须塞进这个预算，否则先缩条款。
CONTEXT_WINDOW_CHARS = 6000
CLAUSE_LIMIT_CHARS = 400      # 单条条款进入提示词的最大长度
HISTORY_MSG_CHARS = 200       # 历史单条最大长度
RECENT_ROUNDS = 4             # 最近 N 轮逐字保留，更早的压成摘要
SUMMARY_MAX_CHARS = 300       # 早期摘要上限


def summarize_history(history: list[dict] | None,
                      keep_rounds: int = RECENT_ROUNDS) -> tuple[str, list[dict]]:
    """把历史拆成「早期摘要 + 最近若干轮」。

    只截断不压缩的问题：10 轮 × 2 条 × 200 字 = 4000 字，会把条款上下文挤爆；
    直接丢掉早期轮次，模型又会忘记用户最初问的是什么。
    这里用确定性的抽取式摘要（问什么/答了什么各留 60 字），零模型调用、零成本。
    """
    if not history:
        return "", []
    msgs = [h for h in history
            if (h or {}).get("role") in ("user", "assistant")
            and str((h or {}).get("content") or "").strip()]
    keep = keep_rounds * 2
    if len(msgs) <= keep:
        return "", msgs
    old, recent = msgs[:-keep], msgs[-keep:]
    lines = []
    for h in old:
        tag = "问" if h["role"] == "user" else "答"
        txt = str(h["content"]).strip().replace("\n", " ")
        lines.append(f"{tag}：{txt[:60]}")
    return ("【早期对话摘要】" + "；".join(lines))[:SUMMARY_MAX_CHARS], recent


def build_context(clauses: list[dict], limit_chars: int = CLAUSE_LIMIT_CHARS) -> str:
    """条款拼装。带上法规名+条款号+生效日期：模型需要"这条属于哪部法"这个信号，
    否则会把《广告法》的绝对化用语要求套到《电商法》头上。"""
    parts = []
    for i, c in enumerate(clauses, 1):
        eff = str(c.get("effective_from") or "")
        head = f"[{i}] 《{c['doc_name']}》{c['clause_no']}"
        if eff:
            head += f"（生效 {eff}）"
        parts.append(f"{head}\n{c['content'][:limit_chars]}")
    return "\n\n".join(parts)


def _risk_hint(risk_level: str) -> str:
    if risk_level == "L3":
        return "\n\n注意：本次问题被判定为 L3 高风险（涉及处罚金额或法律定性），handoff 必须为 true，不要给结论。"
    if risk_level == "L2":
        return "\n\n注意：本次问题涉及时限，若条款之间存在版本差异必须指出，handoff 设为 true。"
    return ""


def _assemble(ctx: str, summary: str, recent: list[dict], query: str, extra: str) -> list[dict]:
    user = (f"【条款】\n{ctx}\n\n"
            f"【商家问题】\n{query}\n"
            f"{extra}\n"
            f"请严格按 JSON 格式输出。")
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    if summary:
        # 摘要作为一条单独的 user 消息：模型把它当"之前聊过什么"的背景。
        msgs.append({"role": "user", "content": summary})
    for h in recent:
        msgs.append({"role": h["role"], "content": str(h["content"]).strip()[:HISTORY_MSG_CHARS]})
    msgs.append({"role": "user", "content": user})
    return msgs


def build_messages(query: str, clauses: list[dict], risk_level: str,
                   scene: str | None = None,
                   history: list[dict] | None = None) -> list[dict]:
    """拼 prompt，并保证整体不超过 CONTEXT_WINDOW_CHARS。

    超预算时优先缩短「单条条款」：历史只有几十字，条款才是大头；
    条款短到 120 字还不够，才退化成少带一条。
    """
    summary, recent = summarize_history(history)
    extra = _risk_hint(risk_level)
    limit = CLAUSE_LIMIT_CHARS
    msgs = _assemble(build_context(clauses, limit), summary, recent, query, extra)
    while sum(len(m["content"]) for m in msgs) > CONTEXT_WINDOW_CHARS:
        if limit > 120:
            limit = max(120, limit - 80)
        elif len(clauses) > 1:
            clauses = clauses[:-1]
        else:
            break
        msgs = _assemble(build_context(clauses, limit), summary, recent, query, extra)
    return msgs


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def parse_output(text: str) -> dict | None:
    """解析模型输出。小模型经常裹 ```json 或前后加客套话，先剥再找最外层大括号。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        d = json.loads(t)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    m = _JSON_RE.search(t)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    # 解析不出 JSON 就当成纯文本答案，总比丢掉好
    return {"conclusion": t, "notice": "", "handoff": False}
