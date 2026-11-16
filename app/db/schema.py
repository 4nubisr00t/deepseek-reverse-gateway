"""
SQLite 数据库 Schema 定义与初始化
包含 users, accounts, api_keys, key_usage_daily, system_meta 五张核心表
以及订阅商业化扩展：subscription_plans(内置常量), subscriptions, subscription_orders
"""

import time
from app.db.database import db

SCHEMA_VERSION = 3

INIT_SQL = """
-- 1. 用户表 users (面向未来商业化自注册与租户体系)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    tier TEXT NOT NULL DEFAULT 'free',
    is_active INTEGER NOT NULL DEFAULT 1,
    account_labels TEXT DEFAULT '[]',
    auto_replenish INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 2. 账号资产表 accounts (彻底将账号池从 config.json 中剥离解耦)
CREATE TABLE IF NOT EXISTS accounts (
    account_label TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    cookie TEXT DEFAULT '',
    email TEXT DEFAULT '',
    password TEXT DEFAULT '',
    proxy TEXT DEFAULT NULL,
    session_id TEXT DEFAULT NULL,
    is_valid INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    pool_tag TEXT NOT NULL DEFAULT 'public',
    mute_until INTEGER DEFAULT NULL,
    mute_count INTEGER NOT NULL DEFAULT 0,
    ban_kind TEXT DEFAULT NULL,
    credential_id TEXT DEFAULT NULL,
    mfa_private_key TEXT DEFAULT NULL,
    mfa_public_key TEXT DEFAULT NULL,
    mfa_key_version TEXT DEFAULT NULL,
    device_id TEXT DEFAULT NULL,
    device_fingerprint TEXT DEFAULT NULL,
    profile_path TEXT DEFAULT NULL,
    expire_at INTEGER DEFAULT NULL,
    last_used REAL NOT NULL DEFAULT 0.0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    token_updated_at INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_is_valid ON accounts(is_valid);

-- 3. API 密钥表 api_keys (支持一人多 Key, 关联用户)
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT DEFAULT NULL,
    name TEXT NOT NULL DEFAULT 'default',
    owner TEXT NOT NULL DEFAULT '',
    key_hash TEXT UNIQUE NOT NULL,
    prefix TEXT NOT NULL,
    account_labels TEXT DEFAULT '[]',
    key_type TEXT NOT NULL DEFAULT 'normal',
    expires_at INTEGER DEFAULT NULL,
    card_category TEXT DEFAULT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    pool_fallback INTEGER NOT NULL DEFAULT 0,
    sensitive_filter INTEGER NOT NULL DEFAULT 1,
    quota_daily_requests INTEGER NOT NULL DEFAULT 0,
    quota_daily_tokens INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    revoked_at INTEGER DEFAULT NULL,
    last_used_at INTEGER DEFAULT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_revoked ON api_keys(revoked);

-- 4. 日用量统计表 key_usage_daily (替代 key_usage.json)
CREATE TABLE IF NOT EXISTS key_usage_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL,
    day TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0.0,
    last_used_at INTEGER DEFAULT NULL,
    last_error TEXT DEFAULT '',
    UNIQUE(key_id, day)
);
CREATE INDEX IF NOT EXISTS idx_usage_key_day ON key_usage_daily(key_id, day);

-- 5. 系统元数据表 system_meta (记录数据库版本与迁移标记)
CREATE TABLE IF NOT EXISTS system_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 6. 全局系统配置表 system_settings (存储网关运行参数，彻底解耦 config.json)
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 7. 邮箱验证码与防刷审计表 email_verifications (支持 6 位验证码、5分钟过期、5次防爆破与单日频控)
CREATE TABLE IF NOT EXISTS email_verifications (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    send_count_today INTEGER NOT NULL DEFAULT 1,
    day_str TEXT NOT NULL,
    last_sent_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    request_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_verif_ip_time ON email_verifications(request_ip, last_sent_at);

-- 8. 封号与风控收件箱表 system_alerts (支持时间戳、已读/未读状态、一键全读与清空)
CREATE TABLE IF NOT EXISTS system_alerts (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    message TEXT NOT NULL,
    wake_at TEXT DEFAULT NULL,
    caller_key TEXT DEFAULT NULL,
    caller_owner TEXT DEFAULT NULL,
    caller_ip TEXT DEFAULT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_is_read ON system_alerts(is_read);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON system_alerts(created_at);

-- 9. 用户订阅表 subscriptions（每个用户当前有效订阅，一人一条有效记录）
--    周期语义：period_type = 'daily' | 'weekly' | 'monthly' | 'unlimited'
--    period_start / period_end 为该计费周期的起止（unix 秒），周期到期后由
--    `roll_subscription_period()` 自动滚动并重置周期用量。
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | expired | cancelled
    period_type TEXT NOT NULL DEFAULT 'monthly',
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_active_user
    ON subscriptions(user_id) WHERE status = 'active';

-- 10. 订阅订单表 subscription_orders（手动收款模式：用户下单 -> 管理员确认收款 -> 开通）
--     status: pending(待支付) | paid(已支付/已开通) | cancelled(已取消) | refunded(已退款)
--     method: alipay_qr(支付宝收款码) | wechat_qr(微信) | manual(线下/其他)
CREATE TABLE IF NOT EXISTS subscription_orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_email TEXT NOT NULL DEFAULT '',
    plan_id TEXT NOT NULL,
    plan_name TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,   -- 以「分」为单位，避免浮点误差
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'pending',
    method TEXT NOT NULL DEFAULT 'alipay_qr',
    user_note TEXT DEFAULT '',                 -- 用户付款备注（如支付宝订单号后 6 位）
    admin_note TEXT DEFAULT '',                -- 管理员备注
    paid_at INTEGER DEFAULT NULL,
    confirmed_by TEXT DEFAULT '',              -- 确认收款的管理员
    subscription_id TEXT DEFAULT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON subscription_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON subscription_orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON subscription_orders(created_at);
"""


def init_schema(target_db=None):
    """初始化数据库表结构"""
    use_db = target_db or db
    conn = use_db.get_connection()
    with use_db._lock:
        conn.executescript(INIT_SQL)
        # 版本号同步：老库升级时也要把 schema_version 提到当前版本。
        # 早先只在「没有记录」时 INSERT，导致 v1 的老库即使跑过新建表逻辑，
        # system_meta 里仍写着 v1 —— 后续按版本号判断的迁移会误判。
        now = int(time.time())
        row = use_db.fetchone("SELECT value FROM system_meta WHERE key = 'schema_version'")
        if not row:
            use_db.execute(
                "INSERT INTO system_meta (key, value, updated_at) VALUES ('schema_version', ?, ?)",
                (str(SCHEMA_VERSION), now),
            )
        else:
            try:
                old_ver = int(row["value"])
            except Exception:
                old_ver = 0
            if old_ver != SCHEMA_VERSION:
                use_db.execute(
                    "UPDATE system_meta SET value = ?, updated_at = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION), now),
                )

        # 兼容自动检测添加 accounts 字段 (ban_kind, pool_tag, 5件套 MFA)
        cols = [r[1] for r in use_db.fetchall("PRAGMA table_info(accounts)")]
        if cols and "ban_kind" not in cols:
            try:
                use_db.execute("ALTER TABLE accounts ADD COLUMN ban_kind TEXT DEFAULT NULL")
            except Exception:
                pass
        if cols and "pool_tag" not in cols:
            try:
                use_db.execute("ALTER TABLE accounts ADD COLUMN pool_tag TEXT NOT NULL DEFAULT 'public'")
            except Exception:
                pass
        for mfa_col in ("credential_id", "mfa_private_key", "mfa_public_key", "mfa_key_version", "device_id", "device_fingerprint", "profile_path"):
            if cols and mfa_col not in cols:
                try:
                    use_db.execute(f"ALTER TABLE accounts ADD COLUMN {mfa_col} TEXT DEFAULT NULL")
                except Exception:
                    pass
        if cols and "expire_at" not in cols:
            try:
                use_db.execute("ALTER TABLE accounts ADD COLUMN expire_at INTEGER DEFAULT NULL")
            except Exception:
                pass
        if cols and "token_updated_at" not in cols:
            try:
                use_db.execute("ALTER TABLE accounts ADD COLUMN token_updated_at INTEGER DEFAULT NULL")
                use_db.execute(
                    "UPDATE accounts SET token_updated_at = COALESCE(updated_at, 0) "
                    "WHERE token_updated_at IS NULL AND COALESCE(token, '') != ''"
                )
            except Exception:
                pass

        # 兼容自动检测添加 users 表 C 端商业化字段 (username, bonus_tokens, invite_code, invited_by)
        user_cols = [r[1] for r in use_db.fetchall("PRAGMA table_info(users)")]
        if user_cols:
            if "username" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT NULL")
                    use_db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
                except Exception:
                    pass
            if "bonus_tokens" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN bonus_tokens INTEGER NOT NULL DEFAULT 0")
                except Exception:
                    pass
            if "invite_code" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN invite_code TEXT DEFAULT NULL")
                    use_db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invite_code ON users(invite_code)")
                except Exception:
                    pass
            if "invited_by" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN invited_by TEXT DEFAULT NULL")
                except Exception:
                    pass
            if "account_labels" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN account_labels TEXT DEFAULT '[]'")
                except Exception:
                    pass
            if "auto_replenish" not in user_cols:
                try:
                    use_db.execute("ALTER TABLE users ADD COLUMN auto_replenish INTEGER NOT NULL DEFAULT 1")
                except Exception:
                    pass

        # 兼容自动检测添加 api_keys 表卡密生命周期字段 (key_type, expires_at, card_category)
        key_cols = [r[1] for r in use_db.fetchall("PRAGMA table_info(api_keys)")]
        if key_cols:
            if "key_type" not in key_cols:
                try:
                    use_db.execute("ALTER TABLE api_keys ADD COLUMN key_type TEXT NOT NULL DEFAULT 'normal'")
                    use_db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key_type ON api_keys(key_type)")
                except Exception:
                    pass
            if "expires_at" not in key_cols:
                try:
                    use_db.execute("ALTER TABLE api_keys ADD COLUMN expires_at INTEGER DEFAULT NULL")
                    use_db.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_expires_at ON api_keys(expires_at)")
                except Exception:
                    pass
            if "card_category" not in key_cols:
                try:
                    use_db.execute("ALTER TABLE api_keys ADD COLUMN card_category TEXT DEFAULT NULL")
                except Exception:
                    pass



