"""缓存与限流：Redis 优先，不可用时退化为进程内存。

两个原则：
  1. 缓存 key 必须带**规则库版本号**。规则变了答案就变了，key 不变就是把旧答案当新答案返回。
  2. 限流不能因为 Redis 挂了就失效。缓存是性能设施，可以降级；限流是保护设施，降级也要有下限。
"""
from __future__ import annotations

import json
import os
import time

RC = None
MEM: dict[str, tuple[dict, float]] = {}      # key -> (value, expire_at)
_RATE: dict[str, list[float]] = {}


def init_redis():
    global RC
    try:
        import redis
        r = redis.Redis(host=os.getenv("REDIS_HOST", "127.0.0.1"),
                        port=int(os.getenv("REDIS_PORT", "6379")),
                        password=os.getenv("REDIS_PASSWORD", "123456"),
                        socket_connect_timeout=1)
        r.ping()
        RC = r
    except Exception:
        RC = None
    return RC


def get(key: str) -> dict | None:
    if RC:
        try:
            v = RC.get(key)
            return json.loads(v) if v else None
        except Exception:
            pass
    item = MEM.get(key)
    if not item:
        return None
    val, exp = item
    if exp and exp < time.time():
        MEM.pop(key, None)
        return None
    return val


def set(key: str, val: dict, ttl: int = 600) -> None:
    if RC:
        try:
            RC.setex(key, ttl, json.dumps(val, ensure_ascii=False, default=str))
            return
        except Exception:
            pass
    MEM[key] = (val, time.time() + ttl if ttl else 0)


def rate_ok(key: str, limit: int) -> tuple[bool, int]:
    """滑动窗口，60 秒。Redis 用 incr+expire，内存用时间戳数组。"""
    now = time.time()
    if RC:
        try:
            k = f"rl:{key}"
            n = RC.incr(k)
            if n == 1:
                RC.expire(k, 60)
            return n <= limit, n
        except Exception:
            pass
    arr = [t for t in _RATE.get(key, []) if now - t < 60]
    arr.append(now)
    _RATE[key] = arr
    return len(arr) <= limit, len(arr)


# ---------------------------------------------------------------- 规则库版本

RULE_VERSION_KEY = "rr:rule_version"
RULE_VERSION_FILE = __import__("pathlib").Path(__file__).resolve().parent.parent \
    / "data" / "rule_version.json"
_DEFAULT_RULE_VERSION = "v5"


def rule_version() -> str:
    """规则库版本号 —— 所有问答缓存 key 的前缀。

    为什么必须是"可变的号"而不是代码里的常量：
      常量 RULE_VERSION = "v5" 永远不会变，于是规则更新之后，
      旧答案仍然挂在 rr:v5:chat:... 上被当新答案返回 ——
      用户看到的是修改前的条款，而系统日志一切正常。这是最坏的一类故障：
      状态有两个来源（代码常量 / 数据库内容），却没有唯一权威。
    现在这个号由一个地方维护（Redis 计数，无 Redis 时落本地文件），
    规则变更流水线改一次，全量缓存自动隔离。
    """
    if RC:
        try:
            v = RC.get(RULE_VERSION_KEY)
            if v:
                return v.decode() if isinstance(v, bytes) else str(v)
        except Exception:
            pass
    try:
        import json as _json
        if RULE_VERSION_FILE.exists():
            return str(_json.loads(RULE_VERSION_FILE.read_text(encoding="utf-8"))["version"])
    except Exception:
        pass
    return _DEFAULT_RULE_VERSION


def bump_rule_version(reason: str = "") -> tuple[str, str]:
    """版本号 +1，返回 (旧, 新)。规则变更流水线在发布成功后调用。"""
    old = rule_version()
    seq = 0
    try:
        import re as _re
        m = _re.search(r"(\d+)$", old)
        seq = int(m.group(1)) if m else 5
    except Exception:
        seq = 5
    new = "v" + str(seq + 1)
    if RC:
        try:
            RC.set(RULE_VERSION_KEY, new)
            return old, new
        except Exception:
            pass
    try:
        import json as _json
        RULE_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULE_VERSION_FILE.write_text(
            _json.dumps({"version": new, "bumped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "reason": reason}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:
        print("[cache] 版本号落盘失败:", str(e)[:80])
    return old, new


def set_rule_version(v: str) -> None:
    """回滚用：把版本号写回 change 之前的号。"""
    if RC:
        try:
            RC.set(RULE_VERSION_KEY, v)
            return
        except Exception:
            pass
    try:
        import json as _json
        RULE_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULE_VERSION_FILE.write_text(
            _json.dumps({"version": v, "restored_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                        ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def stats() -> dict:
    return {"ok": bool(RC), "mem_keys": len(MEM), "rule_version": rule_version()}
