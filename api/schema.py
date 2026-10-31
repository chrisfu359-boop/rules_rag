"""系统数据模型：用户、店铺、会话、消息、反馈、偏好、审计。

为什么单独一层 DDL 而不是散在业务代码里：
  表结构是系统的契约，改字段必须能一眼看到影响面。散在 API 里写 CREATE TABLE，
  三个月后没人知道 t_user 到底有哪些列。

双后端：MySQL 与 SQLite 各一套 DDL。两者都从 IF NOT EXISTS 开始，可重复执行。
  不引入 Alembic 之类的迁移工具——这个规模用不上，而且多一个依赖就多一个装不上的风险。
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import bcrypt

import common

# ---------------------------------------------------------------- 建表

_MYSQL_DDL = [
    """CREATE TABLE IF NOT EXISTS t_shop (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        platform VARCHAR(32) NOT NULL DEFAULT '淘宝',
        category VARCHAR(64) DEFAULT '',
        contact_phone VARCHAR(32) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 订单事实表：平台仲裁判「有没有超时」只认这里的时间戳，不认当事人陈述。
    # 原本由 pipeline 建表，这里补一份 IF NOT EXISTS，避免空库启动时 _order() 直接报错。
    """CREATE TABLE IF NOT EXISTS t_order (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_no VARCHAR(32) NOT NULL UNIQUE,
        shop_name VARCHAR(64) NOT NULL,
        category VARCHAR(32) NOT NULL DEFAULT '',
        amount DECIMAL(10,2) NOT NULL DEFAULT 0,
        status VARCHAR(16) NOT NULL DEFAULT '',
        paid_at DATETIME NULL, shipped_at DATETIME NULL, received_at DATETIME NULL,
        aftersale_type VARCHAR(32) NULL, aftersale_applied_at DATETIME NULL,
        -- 商品明细：只有金额没有商品，仲裁员看不出「268 元判退」退的是什么，
        -- 也没法核对买家说的「实木底座」是不是这件商品。
        item_title VARCHAR(255) DEFAULT '',
        sku VARCHAR(128) DEFAULT '',
        qty INT NOT NULL DEFAULT 1,
        unit_price DECIMAL(10,2) NOT NULL DEFAULT 0,
        -- 物流：虚假发货的判定只能靠「发货时间 vs 揽收时间」，
        -- 之前订单表连运单号都没有，规则 2.4 里那句「上传无效运单号扣 4 分」
        -- 在系统里根本无法落地。
        carrier VARCHAR(32) DEFAULT '',
        tracking_no VARCHAR(64) DEFAULT '',
        picked_at DATETIME NULL,
        remarks VARCHAR(255) NULL,
        KEY idx_od_shop (shop_name),
        KEY idx_od_track (tracking_no)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 订单商品行。t_order 上的 item_title 只够描述「一单一品」，而真实的
    # 售后争议里一半是多商品订单：买家要退的是其中一件，商家说整单已拆包，
    # 金额怎么摊、退哪几件、运费谁承担，都不是一个字段能表达的。
    # t_order 上的那几列保留（老数据、快速列表要它），但明细以本表为准：
    # 有子表行就用子表，没有就回退到主表单行——这样升级不改任何存量数据。
    """CREATE TABLE IF NOT EXISTS t_order_item (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        order_no VARCHAR(32) NOT NULL,
        line_no INT NOT NULL DEFAULT 1,
        title VARCHAR(255) NOT NULL DEFAULT '',
        sku VARCHAR(128) DEFAULT '',
        qty INT NOT NULL DEFAULT 1,
        unit_price DECIMAL(10,2) NOT NULL DEFAULT 0,
        refund_qty INT NOT NULL DEFAULT 0,
        refund_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
        category VARCHAR(32) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_oi_line (order_no, line_no),
        KEY idx_oi_order (order_no)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_user (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(64) NOT NULL UNIQUE,
        email VARCHAR(128) NOT NULL UNIQUE,
        password_hash VARCHAR(100) NOT NULL,
        display_name VARCHAR(64) NOT NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'merchant',
        shop_id INT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        avatar_color VARCHAR(16) DEFAULT '#4f7cff',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_chat_session (
        id VARCHAR(32) PRIMARY KEY,
        user_id INT NOT NULL,
        title VARCHAR(200) NOT NULL DEFAULT '新会话',
        scene VARCHAR(32) DEFAULT '',
        message_count INT NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_sess_user (user_id, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_chat_message (
        id INT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(32) NOT NULL,
        user_id INT NOT NULL,
        role VARCHAR(16) NOT NULL,
        content LONGTEXT,
        risk_level VARCHAR(8) DEFAULT '',
        citations LONGTEXT,
        generator VARCHAR(64) DEFAULT '',
        latency_ms INT DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_msg_sess (session_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_feedback (
        id INT AUTO_INCREMENT PRIMARY KEY,
        message_id INT NOT NULL,
        user_id INT NOT NULL,
        score TINYINT NOT NULL,
        comment VARCHAR(500) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_fb (message_id, user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_user_pref (
        user_id INT PRIMARY KEY,
        theme VARCHAR(16) DEFAULT 'auto',
        default_scene VARCHAR(32) DEFAULT '',
        answer_style VARCHAR(16) DEFAULT 'business',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NULL,
        action VARCHAR(64) NOT NULL,
        target VARCHAR(128) DEFAULT '',
        ip VARCHAR(64) DEFAULT '',
        detail VARCHAR(500) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_audit_time (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # ---------------- 智能客服 ----------------
    # 高频问答对。这就是"知识库"，机器人回答的第一优先级来源。
    # 单独成表而不是塞进 t_clause：条款是法规原文（只读、有版本），
    # 问答对是运营每天改的（可编辑、会下线），生命周期和权限完全不同。
    """CREATE TABLE IF NOT EXISTS t_faq (
        id INT AUTO_INCREMENT PRIMARY KEY,
        question VARCHAR(255) NOT NULL,
        answer TEXT NOT NULL,
        category VARCHAR(32) NOT NULL DEFAULT '通用',
        -- 知识域：这条知识是回答谁的。buyer=买家问店铺 / merchant=商家问平台规则 / ops=运营问治理口径
        -- 不分域的话，买家和商家问「扣几分」会命中同一条答案，这是最早的错误
        audience VARCHAR(16) NOT NULL DEFAULT 'buyer',
        -- 知识归属：NULL=平台通用（所有店铺共用），非空=该店铺专属话术。
        -- 分域只解决了「谁在问」，没解决「问的是哪家店」——74 条买家知识全局共享，
        -- 三家店问「什么时候发货」拿到同一个答案，店家的真实承诺反而永远答不出来。
        -- 规则域与治理域是平台统一口径，shop_id 必须为空（由接口层强制）。
        shop_id INT NULL,
        -- 类目模板（v11）：shop_id 为空但 cat_scope 非空时，表示该条目是
        -- 对该品类所有店铺生效的基线话术。新开同品类店铺自动继承，不必从零手抄。
        cat_scope VARCHAR(64) DEFAULT '',
        keywords VARCHAR(255) DEFAULT '',
        aliases VARCHAR(500) DEFAULT '',
        priority INT NOT NULL DEFAULT 0,
        status VARCHAR(16) NOT NULL DEFAULT 'enabled',
        hit_count INT NOT NULL DEFAULT 0,
        source VARCHAR(16) NOT NULL DEFAULT 'manual',
        created_by INT NULL,
        -- 审核流：人工沉淀出来的条目先 pending，由 ops/admin 审核后 enabled 才进检索。
        -- 直接 enabled 的风险是坐席答错当场污染全店口径，比没有知识更糟。
        reviewed_by INT NULL,
        reviewed_at DATETIME NULL,
        review_note VARCHAR(255) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_faq_status (status, priority),
        KEY idx_faq_cat (category),
        KEY idx_faq_aud (audience, status, priority),
        KEY idx_faq_shop (audience, shop_id, status, priority)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_cs_session (
        id VARCHAR(32) PRIMARY KEY,
        shop_id INT NULL,
        buyer_id INT NULL,
        buyer_name VARCHAR(64) NOT NULL DEFAULT '买家',
        buyer_tag VARCHAR(32) DEFAULT '',
        channel VARCHAR(16) NOT NULL DEFAULT '淘宝',
        -- 会话板块：shop=买家问店铺（进坐席台）/ rule=商家问平台 / gov=运营问治理
        scope VARCHAR(16) NOT NULL DEFAULT 'shop',
        status VARCHAR(16) NOT NULL DEFAULT 'bot',
        last_msg VARCHAR(255) DEFAULT '',
        msg_count INT NOT NULL DEFAULT 0,
        handoff TINYINT NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_cs_shop (shop_id, updated_at),
        KEY idx_cs_status (status, updated_at),
        KEY idx_cs_scope (scope, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_cs_message (
        id INT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(32) NOT NULL,
        role VARCHAR(16) NOT NULL,
        content TEXT,
        source VARCHAR(16) DEFAULT '',
        score DECIMAL(6,4) DEFAULT 0,
        faq_id INT NULL,
        intent VARCHAR(32) DEFAULT '',
        latency_ms INT DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_csmsg_sess (session_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 转人工工单。机器人和人的交接凭据：没有它，"转人工"就只是句客气话。
    """CREATE TABLE IF NOT EXISTS t_ticket (
        id INT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(32) NOT NULL,
        reason VARCHAR(32) NOT NULL DEFAULT 'low_conf',
        question TEXT,
        bot_reply TEXT,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        priority INT NOT NULL DEFAULT 0,
        agent_id INT NULL,
        agent_reply TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        claimed_at DATETIME NULL,
        closed_at DATETIME NULL,
        KEY idx_tk_status (status, created_at),
        KEY idx_tk_sess (session_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # ---------------- 平台监管 ----------------
    # 纠纷单：买家申请平台介入的唯一凭据。
    # 为什么不是复用 t_ticket：工单是"店里没人回"，纠纷是"平台要判谁对"。
    # 前者由商家处理，后者由平台裁决，权限、时效、后果完全不同，混在一张表里
    # 会出现"商家把自己名下的裁决单关掉"这种权限漏洞。
    """CREATE TABLE IF NOT EXISTS t_dispute (
        id INT AUTO_INCREMENT PRIMARY KEY,
        dispute_no VARCHAR(32) NOT NULL UNIQUE,
        session_id VARCHAR(32) NULL,
        order_no VARCHAR(40) DEFAULT '',
        shop_id INT NOT NULL,
        buyer_id INT NULL,
        buyer_name VARCHAR(64) DEFAULT '',
        dtype VARCHAR(24) NOT NULL,
        title VARCHAR(200) NOT NULL,
        claim TEXT,
        claim_type VARCHAR(24) DEFAULT 'refund',
        claim_amount DECIMAL(10,2) DEFAULT 0,
        evidence TEXT,
        status VARCHAR(20) DEFAULT 'ai_review',
        stage VARCHAR(16) DEFAULT 'ai',
        liability VARCHAR(16) DEFAULT '',
        ai_confidence DECIMAL(5,4) DEFAULT 0,
        ai_remedy VARCHAR(24) DEFAULT '',
        ai_amount DECIMAL(10,2) DEFAULT 0,
        ai_penalty VARCHAR(24) DEFAULT '',
        ai_points DECIMAL(6,2) DEFAULT 0,
        ai_reasoning TEXT,
        ai_basis VARCHAR(255) DEFAULT '',
        verdict TEXT,
        verdict_by INT NULL,
        verdict_at DATETIME NULL,
        penalty_id INT NULL,
        priority INT DEFAULT 0,
        sla_due DATETIME NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        closed_at DATETIME NULL,
        KEY idx_dp_shop (shop_id, created_at),
        KEY idx_dp_status (status, priority),
        KEY idx_dp_buyer (buyer_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS t_dispute_msg (
        id INT AUTO_INCREMENT PRIMARY KEY,
        dispute_id INT NOT NULL,
        role VARCHAR(16) NOT NULL,
        content TEXT,
        kind VARCHAR(16) DEFAULT 'text',
        meta VARCHAR(500) DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_dpmsg (dispute_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 处罚单。扣分不是"记一笔"，而是会影响店铺搜索权重、营销资格、账期的资产。
    """CREATE TABLE IF NOT EXISTS t_penalty (
        id INT AUTO_INCREMENT PRIMARY KEY,
        shop_id INT NOT NULL,
        dispute_id INT NULL,
        ptype VARCHAR(24) NOT NULL,
        points DECIMAL(6,2) DEFAULT 0,
        node VARCHAR(24) DEFAULT '',
        reason VARCHAR(255) DEFAULT '',
        basis VARCHAR(255) DEFAULT '',
        clause_no VARCHAR(64) DEFAULT '',
        status VARCHAR(16) DEFAULT 'active',
        auto TINYINT DEFAULT 1,
        operator_id INT NULL,
        effective_at DATETIME NULL,
        expire_at DATETIME NULL,
        revoked_at DATETIME NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_pn_shop (shop_id, created_at),
        KEY idx_pn_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 申诉单。商家申诉处罚、买家申诉裁决，共用一张表 + target_type 区分。
    """CREATE TABLE IF NOT EXISTS t_appeal (
        id INT AUTO_INCREMENT PRIMARY KEY,
        target_type VARCHAR(16) NOT NULL,
        target_id INT NOT NULL,
        shop_id INT NOT NULL,
        dispute_id INT NULL,
        appellant_id INT NULL,
        appellant_role VARCHAR(16) DEFAULT 'merchant',
        reason TEXT,
        evidence TEXT,
        status VARCHAR(20) DEFAULT 'pending',
        ai_view TEXT,
        ai_suggest VARCHAR(16) DEFAULT '',
        ai_confidence DECIMAL(5,4) DEFAULT 0,
        result TEXT,
        reviewer_id INT NULL,
        -- 补充材料与二次复核（v12）：被驳回后允许补充一次材料申请复议
        supplement_count INT DEFAULT 0,
        supplement_history TEXT,
        review_deadline DATETIME NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        decided_at DATETIME NULL,
        KEY idx_ap_target (target_type, target_id),
        KEY idx_ap_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # 店铺信用档案。扣分按自然年累计（对应规则 1.2），跨年清零但档案留痕。
    # 单开一张表而不是往 t_shop 加列：信用是会被频繁更新、被反复查询的账本，
    # 和店铺基础信息（名字、类目）的读写频率差两个数量级。
    """CREATE TABLE IF NOT EXISTS t_shop_credit (
        shop_id INT PRIMARY KEY,
        credit_score INT NOT NULL DEFAULT 100,
        level VARCHAR(16) DEFAULT '良好',
        points_year DECIMAL(6,2) DEFAULT 0,
        dispute_count INT NOT NULL DEFAULT 0,
        dispute_lost INT NOT NULL DEFAULT 0,
        penalty_count INT NOT NULL DEFAULT 0,
        appeal_count INT NOT NULL DEFAULT 0,
        status VARCHAR(16) DEFAULT 'normal',
        suspend_until DATETIME NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

_SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS t_shop (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '淘宝',
        category TEXT DEFAULT '', contact_phone TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_order (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT NOT NULL UNIQUE,
        shop_name TEXT NOT NULL,
        buyer_id INTEGER,
        category TEXT NOT NULL DEFAULT '',
        amount REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',
        paid_at DATETIME, shipped_at DATETIME, received_at DATETIME,
        aftersale_type TEXT, aftersale_applied_at DATETIME,
        item_title TEXT DEFAULT '', sku TEXT DEFAULT '',
        qty INTEGER NOT NULL DEFAULT 1, unit_price REAL NOT NULL DEFAULT 0,
        carrier TEXT DEFAULT '', tracking_no TEXT DEFAULT '', picked_at DATETIME,
        remarks TEXT)""",

    """CREATE TABLE IF NOT EXISTS t_order_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT NOT NULL,
        line_no INTEGER NOT NULL DEFAULT 1,
        title TEXT NOT NULL DEFAULT '', sku TEXT DEFAULT '',
        qty INTEGER NOT NULL DEFAULT 1, unit_price REAL NOT NULL DEFAULT 0,
        refund_qty INTEGER NOT NULL DEFAULT 0, refund_amount REAL NOT NULL DEFAULT 0,
        category TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (order_no, line_no))""",

    """CREATE TABLE IF NOT EXISTS t_user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'merchant', shop_id INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        avatar_color TEXT DEFAULT '#4f7cff',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME)""",

    """CREATE TABLE IF NOT EXISTS t_chat_session (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '新会话', scene TEXT DEFAULT '',
        message_count INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_chat_message (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, user_id INTEGER NOT NULL,
        role TEXT NOT NULL, content TEXT, risk_level TEXT DEFAULT '',
        citations TEXT, generator TEXT DEFAULT '', latency_ms INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        score INTEGER NOT NULL, comment TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (message_id, user_id))""",

    """CREATE TABLE IF NOT EXISTS t_user_pref (
        user_id INTEGER PRIMARY KEY, theme TEXT DEFAULT 'auto',
        default_scene TEXT DEFAULT '', answer_style TEXT DEFAULT 'business',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        action TEXT NOT NULL, target TEXT DEFAULT '', ip TEXT DEFAULT '',
        detail TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_faq (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL, answer TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '通用',
        audience TEXT NOT NULL DEFAULT 'buyer', shop_id INTEGER,
        cat_scope TEXT DEFAULT '',
        keywords TEXT DEFAULT '', aliases TEXT DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'enabled',
        hit_count INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'manual',
        created_by INTEGER, reviewed_by INTEGER, reviewed_at DATETIME,
        review_note TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_cs_session (
        id TEXT PRIMARY KEY, shop_id INTEGER, buyer_id INTEGER,
        buyer_name TEXT NOT NULL DEFAULT '买家', buyer_tag TEXT DEFAULT '',
        channel TEXT NOT NULL DEFAULT '淘宝', scope TEXT NOT NULL DEFAULT 'shop',
        status TEXT NOT NULL DEFAULT 'bot',
        last_msg TEXT DEFAULT '', msg_count INTEGER NOT NULL DEFAULT 0,
        handoff INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_cs_message (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
        source TEXT DEFAULT '', score REAL DEFAULT 0, faq_id INTEGER,
        intent TEXT DEFAULT '', latency_ms INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_ticket (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT 'low_conf',
        question TEXT, bot_reply TEXT, status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER NOT NULL DEFAULT 0, agent_id INTEGER, agent_reply TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, claimed_at DATETIME,
        closed_at DATETIME)""",

    """CREATE TABLE IF NOT EXISTS t_dispute (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_no TEXT NOT NULL UNIQUE, session_id TEXT,
        order_no TEXT DEFAULT '', shop_id INTEGER NOT NULL, buyer_id INTEGER,
        buyer_name TEXT DEFAULT '', dtype TEXT NOT NULL, title TEXT NOT NULL,
        claim TEXT, claim_type TEXT DEFAULT 'refund',
        claim_amount REAL DEFAULT 0, evidence TEXT,
        status TEXT DEFAULT 'ai_review', stage TEXT DEFAULT 'ai',
        liability TEXT DEFAULT '', ai_confidence REAL DEFAULT 0,
        ai_remedy TEXT DEFAULT '', ai_amount REAL DEFAULT 0,
        ai_penalty TEXT DEFAULT '', ai_points REAL DEFAULT 0,
        ai_reasoning TEXT, ai_basis TEXT DEFAULT '',
        verdict TEXT, verdict_by INTEGER, verdict_at DATETIME, penalty_id INTEGER,
        priority INTEGER DEFAULT 0, sla_due DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, closed_at DATETIME)""",

    """CREATE TABLE IF NOT EXISTS t_dispute_msg (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dispute_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
        kind TEXT DEFAULT 'text', meta TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_penalty (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL, dispute_id INTEGER, ptype TEXT NOT NULL,
        points REAL DEFAULT 0, node TEXT DEFAULT '', reason TEXT DEFAULT '',
        basis TEXT DEFAULT '', clause_no TEXT DEFAULT '',
        status TEXT DEFAULT 'active', auto INTEGER DEFAULT 1, operator_id INTEGER,
        effective_at DATETIME, expire_at DATETIME, revoked_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",

    """CREATE TABLE IF NOT EXISTS t_appeal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_type TEXT NOT NULL, target_id INTEGER NOT NULL,
        shop_id INTEGER NOT NULL, dispute_id INTEGER, appellant_id INTEGER,
        appellant_role TEXT DEFAULT 'merchant', reason TEXT, evidence TEXT,
        status TEXT DEFAULT 'pending', ai_view TEXT, ai_suggest TEXT DEFAULT '',
        ai_confidence REAL DEFAULT 0, result TEXT, reviewer_id INTEGER,
        supplement_count INTEGER DEFAULT 0, supplement_history TEXT,
        review_deadline DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, decided_at DATETIME)""",

    """CREATE TABLE IF NOT EXISTS t_shop_credit (
        shop_id INTEGER PRIMARY KEY, credit_score INTEGER NOT NULL DEFAULT 100,
        level TEXT DEFAULT '良好', points_year REAL DEFAULT 0,
        dispute_count INTEGER NOT NULL DEFAULT 0,
        dispute_lost INTEGER NOT NULL DEFAULT 0,
        penalty_count INTEGER NOT NULL DEFAULT 0,
        appeal_count INTEGER NOT NULL DEFAULT 0,
        status TEXT DEFAULT 'normal', suspend_until DATETIME,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
]

_SQLITE_IDX = [
    "CREATE INDEX IF NOT EXISTS idx_sess_user ON t_chat_session(user_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_msg_sess ON t_chat_message(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_time ON t_audit_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_faq_status ON t_faq(status, priority)",
    "CREATE INDEX IF NOT EXISTS idx_faq_shop ON t_faq(audience, shop_id, status, priority)",
    "CREATE INDEX IF NOT EXISTS idx_cs_shop ON t_cs_session(shop_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_csmsg_sess ON t_cs_message(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_tk_status ON t_ticket(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_dp_shop ON t_dispute(shop_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_dp_status ON t_dispute(status, priority)",
    "CREATE INDEX IF NOT EXISTS idx_dp_buyer ON t_dispute(buyer_id)",
    "CREATE INDEX IF NOT EXISTS idx_dpmsg ON t_dispute_msg(dispute_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_pn_shop ON t_penalty(shop_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ap_target ON t_appeal(target_type, target_id)",
]

# ---------------------------------------------------------------- 密码

def hash_password(raw: str) -> str:
    """bcrypt，cost=12（约 250ms/次）。

    为什么用 bcrypt 不用 sha256：sha256 太快，GPU 每秒能算十亿次，撞库成本极低。
    bcrypt 的 cost 是故意慢的，且自带 salt。12 是"登录不卡、暴力破解很贵"的平衡点。
    """
    return bcrypt.hashpw(raw.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode()


def check_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8")[:72], hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------- 初始化

def init_schema() -> None:
    ddl = _MYSQL_DDL if common.backend() == "mysql" else _SQLITE_DDL
    for sql in ddl:
        common.execute(sql)
    if common.backend() != "mysql":
        # 建索引前先补列：老库的 t_faq 还没有 shop_id，直接 CREATE INDEX 会报
        # "no such column"。CREATE TABLE IF NOT EXISTS 不会改已存在的表，
        # 所以「建表 → 补列 → 建索引」这个顺序不能反。
        _migrate()
        for sql in _SQLITE_IDX:
            try:
                common.execute(sql)
            except Exception:
                # 索引缺失只影响性能，不该让服务起不来
                pass


SEED_USERS = [
    # username, email, password, display, role, shop_index
    ("admin",    "admin@rules.local",    "Admin@12345", "系统管理员", "admin",    None),
    ("ops",      "ops@rules.local",      "Ops@12345",   "平台运营",   "ops",      None),
    ("fengxia",  "fengxia@rules.local",  "123456",      "冯霞",       "merchant", 0),
    ("xumeng",   "xumeng@rules.local",   "123456",      "许梦",       "merchant", 1),
    # 买家必须有独立账号。之前让商家兼职当买家，结果是同一个账号既能答辩又能投诉，
    # 三方角色在数据里根本分不开，页面上的「买家视角」也就只是换了套文案。
    ("lichen",   "lichen@rules.local",   "123456",      "李澈",       "buyer",    None),
]

SEED_SHOPS = [
    ("霞光家居旗舰店", "淘宝", "家居/家纺"),
    ("梦境数码专营店", "抖音", "3C/数码"),
    ("无名小店", "拼多多", "日用百货"),
    ("鲜达生鲜直营店", "京东", "生鲜"),
    ("美妆小站旗舰店", "淘宝", "美妆个护"),
    ("潮流服饰工厂店", "抖音", "服饰鞋包"),
    ("母婴优选馆", "京东", "母婴"),
    ("运动装备仓", "拼多多", "运动户外"),
]


def seed() -> list[str]:
    """建默认店铺与账号。可重复执行：只补缺失的，不动已有的。

    以前是「有用户就整体跳过」，导致新增角色（买家账号）在老库上永远进不来。
    账号是基础设施，补种必须幂等，否则每加一个角色都要手工改库。
    """
    ts = now()
    shop_ids = [r["id"] for r in common.fetch_all("SELECT id FROM t_shop ORDER BY id")]
    if len(shop_ids) < len(SEED_SHOPS):
        for name, plat, cat in SEED_SHOPS[len(shop_ids):]:
            shop_ids.append(common.insert(
                "INSERT INTO t_shop (name, platform, category, created_at) VALUES (%s,%s,%s,%s)",
                (name, plat, cat, ts)))

    created = []
    for i, (u, e, p, d, r, si) in enumerate(SEED_USERS):
        if common.fetch_all("SELECT 1 AS x FROM t_user WHERE username=%s", (u,)):
            continue
        sid = shop_ids[si] if si is not None and si < len(shop_ids) else None
        uid = common.insert(
            """INSERT INTO t_user (username, email, password_hash, display_name,
                                   role, shop_id, avatar_color, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (u, e, hash_password(p), d, r, sid,
             ["#4f7cff", "#0ea5a5", "#f59e0b", "#a855f7"][i % 4], ts))
        common.insert("INSERT INTO t_user_pref (user_id, updated_at) VALUES (%s,%s)", (uid, ts))
        created.append(u)
    return created


def ensure_ready() -> None:
    init_schema()
    _migrate()
    new = seed()
    if new:
        print(f"[schema] 初始化完成，默认账号: {', '.join(new)}")
    n = seed_faq()
    if n:
        print(f"[schema] 高频问答库初始化：{n} 条")
    ns = seed_shop_faq()
    if ns:
        print(f"[schema] 店铺专属话术初始化：{ns} 条")
    c = init_credit()
    if c:
        print(f"[schema] 店铺信用档案初始化：{c} 家")


def init_credit() -> int:
    """为每家店铺建信用档案。缺一行就会在"CASE WHEN"里被当成 NULL 静默漏掉，
    所以建表后立刻补齐，而不是等第一次处罚时按需创建。"""
    shops = common.fetch_all("SELECT id FROM t_shop")
    have = {r["shop_id"] for r in common.fetch_all("SELECT shop_id FROM t_shop_credit")}
    n = 0
    for s in shops:
        if s["id"] in have:
            continue
        try:
            common.execute(
                "INSERT INTO t_shop_credit (shop_id, credit_score, level, updated_at) "
                "VALUES (%s,100,'良好',%s)", (s["id"], now()))
            n += 1
        except Exception:
            pass
    return n


def _migrate() -> None:
    """给老库补列。CREATE TABLE IF NOT EXISTS 不会改已存在的表，
    新增字段必须显式 ALTER，否则跑起来就是 "no such column"。
    失败不报错：列已存在时数据库会抛错，这正是"已经迁移过"的正常信号。"""
    for sql in ("ALTER TABLE t_faq ADD COLUMN aliases VARCHAR(500) DEFAULT ''",
                "ALTER TABLE t_faq ADD COLUMN aliases TEXT DEFAULT ''",
                "ALTER TABLE t_faq ADD COLUMN aliases TEXT"):
        try:
            common.execute(sql)
            break
        except Exception:
            continue

    # 会话绑定买家账号：不绑的话「我的咨询」只能靠昵称字符串认人。
    # 单独一段，不能塞进上面那个循环——那里一成功就 break，后面的列永远轮不到。
    try:
        common.execute("ALTER TABLE t_cs_session ADD COLUMN buyer_id INT NULL")
    except Exception:
        pass

    # 知识域与会话板块（v7）。
    # 老库里的 74 条 FAQ 全是「买家问店铺」，所以默认值必须是 buyer；
    # 老会话也全是买家咨询，默认 shop。这样迁移完语义不变，不需要回填。
    for sql in ("ALTER TABLE t_faq ADD COLUMN audience VARCHAR(16) NOT NULL DEFAULT 'buyer'",
                "ALTER TABLE t_faq ADD COLUMN audience TEXT NOT NULL DEFAULT 'buyer'"):
        try:
            common.execute(sql)
            break
        except Exception:
            continue
    for sql in ("ALTER TABLE t_cs_session ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'shop'",
                "ALTER TABLE t_cs_session ADD COLUMN scope TEXT NOT NULL DEFAULT 'shop'"):
        try:
            common.execute(sql)
            break
        except Exception:
            continue

    # 知识按店铺隔离（v8）。老库的 FAQ 全部是平台通用条目，默认 NULL 语义不变。
    for sql in ("ALTER TABLE t_faq ADD COLUMN shop_id INT NULL",
                "ALTER TABLE t_faq ADD COLUMN shop_id INTEGER"):
        try:
            common.execute(sql)
            break
        except Exception:
            continue

    # 订单商品明细与运单号（v8）。
    # 每条 ALTER 都必须单独 try：一条成功就 break 的写法会让后面的列永远加不上，
    # 而 SQLite 对重复列报的错和 MySQL 不同，混在一起判断必然出错。
    for sql, sqlite_sql in (
        ("ALTER TABLE t_order ADD COLUMN item_title VARCHAR(255) DEFAULT ''",
         "ALTER TABLE t_order ADD COLUMN item_title TEXT DEFAULT ''"),
        ("ALTER TABLE t_order ADD COLUMN sku VARCHAR(128) DEFAULT ''",
         "ALTER TABLE t_order ADD COLUMN sku TEXT DEFAULT ''"),
        ("ALTER TABLE t_order ADD COLUMN qty INT NOT NULL DEFAULT 1",
         "ALTER TABLE t_order ADD COLUMN qty INTEGER NOT NULL DEFAULT 1"),
        ("ALTER TABLE t_order ADD COLUMN unit_price DECIMAL(10,2) NOT NULL DEFAULT 0",
         "ALTER TABLE t_order ADD COLUMN unit_price REAL NOT NULL DEFAULT 0"),
        ("ALTER TABLE t_order ADD COLUMN carrier VARCHAR(32) DEFAULT ''",
         "ALTER TABLE t_order ADD COLUMN carrier TEXT DEFAULT ''"),
        ("ALTER TABLE t_order ADD COLUMN tracking_no VARCHAR(64) DEFAULT ''",
         "ALTER TABLE t_order ADD COLUMN tracking_no TEXT DEFAULT ''"),
        ("ALTER TABLE t_order ADD COLUMN picked_at DATETIME NULL",
         "ALTER TABLE t_order ADD COLUMN picked_at DATETIME"),
    ):
        try:
            common.execute(sql if common.backend() == "mysql" else sqlite_sql)
        except Exception:
            pass

    # 知识沉淀审核流（v11）。人工沉淀先 pending，过审才 enabled。
    # 老库历史条目直接保持 enabled（它们本来就是人工确认过的），新沉淀才走待审。
    for sql, sqlite_sql in (
        ("ALTER TABLE t_faq ADD COLUMN reviewed_by INT NULL",
         "ALTER TABLE t_faq ADD COLUMN reviewed_by INTEGER"),
        ("ALTER TABLE t_faq ADD COLUMN reviewed_at DATETIME NULL",
         "ALTER TABLE t_faq ADD COLUMN reviewed_at DATETIME"),
        ("ALTER TABLE t_faq ADD COLUMN review_note VARCHAR(255) DEFAULT ''",
         "ALTER TABLE t_faq ADD COLUMN review_note TEXT DEFAULT ''"),
    ):
        try:
            common.execute(sql if common.backend() == "mysql" else sqlite_sql)
        except Exception:
            pass

    # 类目模板（v11）。cat_scope 空=平台通用基线；非空=该品类店铺共用的话术。
    for sql, sqlite_sql in (
        ("ALTER TABLE t_faq ADD COLUMN cat_scope VARCHAR(64) DEFAULT ''",
         "ALTER TABLE t_faq ADD COLUMN cat_scope TEXT DEFAULT ''"),
    ):
        try:
            common.execute(sql if common.backend() == "mysql" else sqlite_sql)
        except Exception:
            pass

    # 申诉补充材料与二次复核（v12）。
    for sql, sqlite_sql in (
        ("ALTER TABLE t_appeal ADD COLUMN supplement_count INT NOT NULL DEFAULT 0",
         "ALTER TABLE t_appeal ADD COLUMN supplement_count INTEGER NOT NULL DEFAULT 0"),
        ("ALTER TABLE t_appeal ADD COLUMN supplement_history TEXT",
         "ALTER TABLE t_appeal ADD COLUMN supplement_history TEXT"),
        ("ALTER TABLE t_appeal ADD COLUMN review_deadline DATETIME NULL",
         "ALTER TABLE t_appeal ADD COLUMN review_deadline DATETIME"),
    ):
        try:
            common.execute(sql if common.backend() == "mysql" else sqlite_sql)
        except Exception:
            pass


# ---------------------------------------------------------------- FAQ 种子

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 三个知识域各自的种子文件。分开存不是为了好看，是为了让「这条知识回答谁」
# 在文件层面就是显式的——同一句话（"扣几分"）在三个域里有三个正确答案。
FAQ_SEEDS = {
    "buyer": DATA_DIR / "faq_seed.json",       # 买家问店铺：发货、退款、发票、尺码
    "merchant": DATA_DIR / "faq_rule.json",    # 商家问平台：违规判定、扣分、申诉
    "ops": DATA_DIR / "faq_gov.json",          # 运营问治理：条款依据、法律法规、处置流程
}

# 店铺专属话术的种子（v8）。为什么单开一个文件：这些条目带 shop 归属，
# 混进 faq_seed.json 会让「哪条是平台通用、哪条是这家店的」在文件里看不出来。
FAQ_SHOP_SEED = DATA_DIR / "faq_shop.json"


def seed_shop_faq() -> int:
    """给演示店铺灌几条本店专属话术。

    只在「该店一条专属知识都没有」时灌，且必须能对上店铺名——
    对不上说明是别的库，宁可一条不灌，也不能把 A 店的话术挂到 B 店。
    """
    import json
    if not FAQ_SHOP_SEED.exists():
        return 0
    rows = json.loads(FAQ_SHOP_SEED.read_text(encoding="utf-8"))
    # 先按店铺分组：判断"这家店有没有专属知识"必须在**整组插入之前**做一次。
    # 写在循环里会导致插完第一条就把后面几条全跳过（n>0 立即成立）。
    by_shop: dict[str, list[dict]] = {}
    for r in rows:
        by_shop.setdefault(r["shop"], []).append(r)

    total = 0
    ts = now()
    for shop_name, items in by_shop.items():
        shop = common.fetch_all("SELECT id FROM t_shop WHERE name=%s", (shop_name,))
        if not shop:
            continue
        sid = int(shop[0]["id"])
        n = common.fetch_all(
            "SELECT COUNT(*) AS n FROM t_faq WHERE audience='buyer' AND shop_id=%s",
            (sid,))[0]["n"] or 0
        if n > 0:
            continue
        for r in items:
            common.insert(
                """INSERT INTO t_faq (question, answer, category, audience, shop_id, keywords,
                       aliases, priority, status, hit_count, source, created_at, updated_at)
                   VALUES (%s,%s,%s,'buyer',%s,%s,%s,%s,'enabled',0,'seed',%s,%s)""",
                (r["q"][:250], r["a"], r.get("cat", "通用"), sid,
                 ",".join(r.get("kw") or [])[:250], "|".join(r.get("alt") or [])[:490],
                 int(r.get("priority", 10)), ts, ts))
            total += 1
    return total


def seed_faq() -> int:
    """按知识域灌种子。**每域只在为空时灌一次**——运营改过的内容不能被重启覆盖。

    以前是「全表为空才灌」，结果新增知识域永远进不来老库：老库有 74 条买家知识，
    判断非空直接 return，商家的规则库、运营的法规库一条都没有，页面上点快捷问题
    全部落到人工兜底。改成分域判断后，老库重启即可补上新域，且不会覆盖已有修改。

    这条约束比"可重复执行"更重要：种子数据一旦可以被启动流程覆盖，
    线上手工维护的知识库就会在某次重启后悄悄回到出厂状态。
    """
    import json
    total = 0
    for aud, path in FAQ_SEEDS.items():
        n = common.fetch_all(
            "SELECT COUNT(*) AS n FROM t_faq WHERE audience=%s", (aud,))[0]["n"] or 0
        if n > 0 or not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        ts = now()
        for r in rows:
            common.insert(
                """INSERT INTO t_faq (question, answer, category, audience, keywords,
                       aliases, priority, status, hit_count, source, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'enabled',0,'seed',%s,%s)""",
                (r["q"][:250], r["a"], r.get("cat", "通用"), aud,
                 ",".join(r.get("kw") or [])[:250],
                 "|".join(r.get("alt") or [])[:490],
                 int(r.get("priority", 0)), ts, ts))
        total += len(rows)
    return total


# ---------------------------------------------------------------- 工具

def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_sid() -> str:
    return uuid.uuid4().hex


def audit(user_id: int | None, action: str, target: str = "", ip: str = "", detail: str = "") -> None:
    """审计日志失败不能影响主流程——登录成功但日志写不进去，不该因此登不上。"""
    try:
        common.execute(
            "INSERT INTO t_audit_log (user_id, action, target, ip, detail, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, action, target, ip, detail[:480], now()))
    except Exception:
        pass
