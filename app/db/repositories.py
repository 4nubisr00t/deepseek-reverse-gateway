"""
数据仓储层 (Repositories)
提供面向对象的强类型数据访问与增删改查接口
"""

import json
import time
from typing import Any, Dict, List, Optional
from app.db.database import db
from app.db.schema import init_schema


_UNSET = object()


def _dict_from_row(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


class AccountRepo:
    """账号池数据仓储"""

    def __init__(self):
        init_schema()

    def get(self, account_label: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone(
            "SELECT * FROM accounts WHERE account_label = ?",
            (account_label,),
        )
        return _dict_from_row(row)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = db.fetchall("SELECT * FROM accounts ORDER BY created_at ASC")
        return [dict(r) for r in rows]

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """新增或全量更新一个账号"""
        now = int(time.time())
        label = data.get("account_label")
        if not label:
            raise ValueError("account_label 不能为空")

        token = data.get("token") or ""
        cookie = data.get("cookie") or ""
        email = data.get("_email") or data.get("email") or label
        password = data.get("_password") or data.get("password") or ""
        proxy = data.get("proxy")
        session_id = data.get("session_id")
        is_valid = 1 if data.get("is_valid", True) else 0
        status = data.get("status") or ("ACTIVE" if is_valid else "MUTED")
        mute_until = data.get("mute_until")
        mute_count = int(data.get("mute_count") or 0)
        ban_kind = data.get("ban_kind")
        last_used = float(data.get("last_used") or 0.0)

        existing = self.get(label)
        created_at = existing["created_at"] if existing else now
        pool_tag = data.get("pool_tag") or (existing.get("pool_tag") if existing else "public") or "public"

        credential_id = data.get("credential_id")
        mfa_private_key = data.get("mfa_private_key")
        mfa_public_key = data.get("mfa_public_key")
        mfa_key_version = data.get("mfa_key_version")
        device_id = data.get("device_id")
        device_fingerprint = data.get("device_fingerprint")
        if isinstance(device_fingerprint, (dict, list)):
            device_fingerprint = json.dumps(device_fingerprint, ensure_ascii=False)
        profile_path = data.get("profile_path")
        expire_at = data.get("expire_at")

        raw_tok_upd = data.get("token_updated_at")
        if raw_tok_upd is not None:
            token_updated_at = int(raw_tok_upd)
        elif not token:
            token_updated_at = 0
        elif existing and (existing.get("token") or "") == token and (existing.get("token_updated_at") or 0) > 0:
            token_updated_at = int(existing["token_updated_at"])
        else:
            token_updated_at = now

        sql = """
        INSERT INTO accounts (
            account_label, token, cookie, email, password, proxy, session_id,
            is_valid, status, pool_tag, mute_until, mute_count, ban_kind,
            credential_id, mfa_private_key, mfa_public_key, mfa_key_version, device_id,
            device_fingerprint, profile_path, expire_at,
            last_used, created_at, updated_at, token_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_label) DO UPDATE SET
            token = excluded.token,
            cookie = excluded.cookie,
            email = excluded.email,
            password = excluded.password,
            proxy = excluded.proxy,
            session_id = excluded.session_id,
            is_valid = excluded.is_valid,
            status = excluded.status,
            pool_tag = excluded.pool_tag,
            mute_until = excluded.mute_until,
            mute_count = excluded.mute_count,
            ban_kind = excluded.ban_kind,
            credential_id = COALESCE(NULLIF(excluded.credential_id, ''), accounts.credential_id),
            mfa_private_key = COALESCE(NULLIF(excluded.mfa_private_key, ''), accounts.mfa_private_key),
            mfa_public_key = COALESCE(NULLIF(excluded.mfa_public_key, ''), accounts.mfa_public_key),
            mfa_key_version = COALESCE(NULLIF(excluded.mfa_key_version, ''), accounts.mfa_key_version),
            device_id = COALESCE(NULLIF(excluded.device_id, ''), accounts.device_id),
            device_fingerprint = COALESCE(NULLIF(excluded.device_fingerprint, ''), accounts.device_fingerprint),
            profile_path = COALESCE(NULLIF(excluded.profile_path, ''), accounts.profile_path),
            expire_at = COALESCE(excluded.expire_at, accounts.expire_at),
            last_used = excluded.last_used,
            updated_at = excluded.updated_at,
            token_updated_at = excluded.token_updated_at
        """
        db.execute(
            sql,
            (
                label, token, cookie, email, password, proxy, session_id,
                is_valid, status, pool_tag, mute_until, mute_count, ban_kind,
                credential_id, mfa_private_key, mfa_public_key, mfa_key_version, device_id,
                device_fingerprint, profile_path, expire_at,
                last_used, created_at, now, token_updated_at,
            ),
        )
        return self.get(label)  # type: ignore

    def save_batch(self, accounts: List[Dict[str, Any]]) -> int:
        now = int(time.time())
        sql = """
        INSERT INTO accounts (
            account_label, token, cookie, email, password, proxy, session_id,
            is_valid, status, mute_until, mute_count, ban_kind,
            credential_id, mfa_private_key, mfa_public_key, mfa_key_version, device_id,
            device_fingerprint, profile_path, expire_at,
            last_used, created_at, updated_at, token_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_label) DO UPDATE SET
            token = excluded.token,
            cookie = excluded.cookie,
            email = excluded.email,
            password = excluded.password,
            proxy = excluded.proxy,
            session_id = excluded.session_id,
            is_valid = excluded.is_valid,
            status = excluded.status,
            mute_until = excluded.mute_until,
            mute_count = excluded.mute_count,
            ban_kind = excluded.ban_kind,
            credential_id = COALESCE(NULLIF(excluded.credential_id, ''), accounts.credential_id),
            mfa_private_key = COALESCE(NULLIF(excluded.mfa_private_key, ''), accounts.mfa_private_key),
            mfa_public_key = COALESCE(NULLIF(excluded.mfa_public_key, ''), accounts.mfa_public_key),
            mfa_key_version = COALESCE(NULLIF(excluded.mfa_key_version, ''), accounts.mfa_key_version),
            device_id = COALESCE(NULLIF(excluded.device_id, ''), accounts.device_id),
            device_fingerprint = COALESCE(NULLIF(excluded.device_fingerprint, ''), accounts.device_fingerprint),
            profile_path = COALESCE(NULLIF(excluded.profile_path, ''), accounts.profile_path),
            expire_at = COALESCE(excluded.expire_at, accounts.expire_at),
            last_used = excluded.last_used,
            updated_at = excluded.updated_at,
            token_updated_at = CASE
                WHEN excluded.token = '' THEN 0
                WHEN excluded.token = accounts.token AND COALESCE(accounts.token_updated_at, 0) > 0 THEN accounts.token_updated_at
                ELSE excluded.token_updated_at
            END
        """
        params = []
        for a in accounts:
            lbl = a.get("account_label")
            if not lbl:
                continue
            is_valid = 1 if a.get("is_valid", True) else 0
            status = a.get("status") or ("ACTIVE" if is_valid else "MUTED")
            _fp = a.get("device_fingerprint")
            if isinstance(_fp, (dict, list)):
                _fp = json.dumps(_fp, ensure_ascii=False)
            tok = a.get("token") or ""
            raw_tok_upd = a.get("token_updated_at")
            tok_upd = int(raw_tok_upd) if raw_tok_upd is not None else (now if tok else 0)
            params.append((
                lbl,
                tok,
                a.get("cookie") or "",
                a.get("_email") or a.get("email") or lbl,
                a.get("_password") or a.get("password") or "",
                a.get("proxy"),
                a.get("session_id"),
                is_valid,
                status,
                a.get("mute_until"),
                int(a.get("mute_count") or 0),
                a.get("ban_kind"),
                a.get("credential_id"),
                a.get("mfa_private_key"),
                a.get("mfa_public_key"),
                a.get("mfa_key_version"),
                a.get("device_id"),
                _fp,
                a.get("profile_path"),
                a.get("expire_at"),
                float(a.get("last_used") or 0.0),
                now,
                now,
                tok_upd,
            ))
        with db.transaction():
            db.executemany(sql, params)
        return len(params)

    def update_runtime_state(
        self,
        account_label: str,
        status: Any = _UNSET,
        is_valid: Any = _UNSET,
        mute_until: Any = _UNSET,
        mute_count: Any = _UNSET,
        ban_kind: Any = _UNSET,
        last_used: Any = _UNSET,
        session_id: Any = _UNSET,
        proxy: Any = _UNSET,
        token: Any = _UNSET,
        token_updated_at: Any = _UNSET,
    ) -> bool:
        now = int(time.time())
        sets = ["updated_at = ?"]
        params: List[Any] = [now]

        if token is not _UNSET:
            sets.append("token = ?")
            params.append(token)
        if token_updated_at is not _UNSET:
            sets.append("token_updated_at = ?")
            params.append(int(token_updated_at) if token_updated_at is not None else 0)
        elif token is not _UNSET:
            if not token:
                sets.append("token_updated_at = ?")
                params.append(0)
            else:
                existing = self.get(account_label)
                same_tok = existing and (existing.get("token") or "") == token and (existing.get("token_updated_at") or 0) > 0
                if not same_tok:
                    sets.append("token_updated_at = ?")
                    params.append(now)
        if status is not _UNSET:
            sets.append("status = ?")
            params.append(status)
        if is_valid is not _UNSET:
            sets.append("is_valid = ?")
            params.append(1 if is_valid else 0 if is_valid is not None else None)
        if mute_until is not _UNSET:
            sets.append("mute_until = ?")
            params.append(mute_until)
        if mute_count is not _UNSET:
            sets.append("mute_count = ?")
            params.append(int(mute_count) if mute_count is not None else 0)
        if ban_kind is not _UNSET:
            sets.append("ban_kind = ?")
            params.append(ban_kind)
        if last_used is not _UNSET:
            sets.append("last_used = ?")
            params.append(float(last_used) if last_used is not None else 0.0)
        if session_id is not _UNSET:
            sets.append("session_id = ?")
            params.append(session_id)
        if proxy is not _UNSET:
            sets.append("proxy = ?")
            params.append(proxy)

        params.append(account_label)
        sql = f"UPDATE accounts SET {', '.join(sets)} WHERE account_label = ?"
        cur = db.execute(sql, params)
        return cur.rowcount > 0

    def update_pool_tag(self, account_label: str, pool_tag: str) -> bool:
        cur = db.execute(
            "UPDATE accounts SET pool_tag = ?, updated_at = ? WHERE account_label = ?",
            (pool_tag, int(time.time()), account_label),
        )
        return cur.rowcount > 0

    def delete(self, account_label: str) -> bool:
        cur = db.execute(
            "DELETE FROM accounts WHERE account_label = ?",
            (account_label,),
        )
        return cur.rowcount > 0

    def count(self) -> int:
        row = db.fetchone("SELECT COUNT(*) as c FROM accounts")
        return int(row["c"]) if row else 0


class ApiKeyRepo:
    """API Key 数据仓储"""

    def __init__(self):
        init_schema()

    def _normalize_row(self, row) -> Optional[Dict[str, Any]]:
        d = _dict_from_row(row)
        if d is None:
            return None
        # 反序列化 JSON 数组 account_labels
        raw_labels = d.get("account_labels")
        if isinstance(raw_labels, str):
            try:
                d["account_labels"] = json.loads(raw_labels)
            except Exception:
                d["account_labels"] = []
        elif not isinstance(raw_labels, list):
            d["account_labels"] = []
        d["enabled"] = bool(d.get("enabled", 1))
        d["pool_fallback"] = bool(d.get("pool_fallback", 0))
        d["sensitive_filter"] = bool(d.get("sensitive_filter", 1))
        d["revoked"] = bool(d.get("revoked", 0))
        d["key_type"] = d.get("key_type") or "normal"
        d["expires_at"] = d.get("expires_at")
        d["card_category"] = d.get("card_category")
        return d

    def get_by_id(self, key_id: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        return self._normalize_row(row)

    def get_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        return self._normalize_row(row)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = db.fetchall("SELECT * FROM api_keys ORDER BY created_at ASC")
        return [self._normalize_row(r) for r in rows if r]  # type: ignore

    def save(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        kid = rec.get("id")
        if not kid:
            raise ValueError("key id 不能为空")

        labels = rec.get("account_labels") or []
        if isinstance(labels, (list, tuple)):
            labels_json = json.dumps(list(labels), ensure_ascii=False)
        else:
            labels_json = "[]"

        sql = """
        INSERT INTO api_keys (
            id, user_id, name, owner, key_hash, prefix, account_labels,
            key_type, expires_at, card_category,
            enabled, pool_fallback, sensitive_filter, quota_daily_requests,
            quota_daily_tokens, created_at, revoked, revoked_at, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_id = excluded.user_id,
            name = excluded.name,
            owner = excluded.owner,
            key_hash = excluded.key_hash,
            prefix = excluded.prefix,
            account_labels = excluded.account_labels,
            key_type = excluded.key_type,
            expires_at = excluded.expires_at,
            card_category = excluded.card_category,
            enabled = excluded.enabled,
            pool_fallback = excluded.pool_fallback,
            sensitive_filter = excluded.sensitive_filter,
            quota_daily_requests = excluded.quota_daily_requests,
            quota_daily_tokens = excluded.quota_daily_tokens,
            revoked = excluded.revoked,
            revoked_at = excluded.revoked_at,
            last_used_at = excluded.last_used_at
        """
        db.execute(
            sql,
            (
                kid,
                rec.get("user_id"),
                rec.get("name") or "default",
                rec.get("owner") or "",
                rec.get("key_hash") or "",
                rec.get("prefix") or "",
                labels_json,
                rec.get("key_type") or "normal",
                rec.get("expires_at"),
                rec.get("card_category"),
                1 if rec.get("enabled", True) else 0,
                1 if rec.get("pool_fallback", False) else 0,
                1 if rec.get("sensitive_filter", True) else 0,
                int(rec.get("quota_daily_requests") or 0),
                int(rec.get("quota_daily_tokens") or 0),
                int(rec.get("created_at") or time.time()),
                1 if rec.get("revoked", False) else 0,
                rec.get("revoked_at"),
                rec.get("last_used_at"),
            ),
        )
        return self.get_by_id(kid)  # type: ignore

    def save_batch(self, keys: List[Dict[str, Any]]) -> int:
        with db.transaction():
            for k in keys:
                self.save(k)
        return len(keys)

    def touch_last_used(self, key_id: str, ts: int) -> bool:
        cur = db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (ts, key_id),
        )
        return cur.rowcount > 0

    def revoke(self, key_id: str) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE api_keys SET revoked = 1, revoked_at = ?, enabled = 0, account_labels = '[]' WHERE id = ?",
            (now, key_id),
        )
        return cur.rowcount > 0

    def delete(self, key_id: str) -> bool:
        cur = db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        return cur.rowcount > 0

    def purge_revoked(self) -> int:
        """彻底物理删除所有已作废或已禁用的 API Key 记录"""
        cur = db.execute("DELETE FROM api_keys WHERE revoked = 1 OR enabled = 0")
        return cur.rowcount

    def count(self) -> int:
        row = db.fetchone("SELECT COUNT(*) as c FROM api_keys")
        return int(row["c"]) if row else 0

    def list_by_user_id(self, user_id: str) -> List[Dict[str, Any]]:
        rows = db.fetchall(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return [self._normalize_row(r) for r in rows if r]  # type: ignore

    def revoke_by_user(self, user_id: str, key_id: str) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE api_keys SET revoked = 1, revoked_at = ?, enabled = 0, account_labels = '[]' WHERE id = ? AND user_id = ?",
            (now, key_id, user_id),
        )
        return cur.rowcount > 0

    def update_user_key(self, user_id: str, key_id: str, name: Optional[str] = None, enabled: Optional[bool] = None) -> bool:
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name.strip()[:40])
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if not updates:
            return False
        params.extend([key_id, user_id])
        sql = f"UPDATE api_keys SET {', '.join(updates)} WHERE id = ? AND user_id = ?"
        cur = db.execute(sql, tuple(params))
        return cur.rowcount > 0


class UserRepo:
    """用户数据仓储"""

    def __init__(self):
        init_schema()

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return _dict_from_row(row)

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone("SELECT * FROM users WHERE lower(email) = ?", (email.strip().lower(),))
        return _dict_from_row(row)

    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        if not username:
            return None
        row = db.fetchone("SELECT * FROM users WHERE lower(username) = ?", (username.strip().lower(),))
        return _dict_from_row(row)

    def get_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        """按邮箱或用户名统一检索用户"""
        clean_id = (identifier or "").strip().lower()
        if not clean_id:
            return None
        row = db.fetchone(
            "SELECT * FROM users WHERE lower(email) = ? OR lower(username) = ?",
            (clean_id, clean_id),
        )
        return _dict_from_row(row)

    def get_by_invite_code(self, invite_code: str) -> Optional[Dict[str, Any]]:
        clean_code = (invite_code or "").strip().lower()
        if not clean_code:
            return None
        row = db.fetchone("SELECT * FROM users WHERE lower(invite_code) = ? OR lower(id) = ?", (clean_code, clean_code))
        return _dict_from_row(row)

    def list_all(self) -> List[Dict[str, Any]]:
        rows = db.fetchall("SELECT * FROM users ORDER BY created_at ASC")
        return [dict(r) for r in rows]

    def create(
        self,
        user_id: str,
        email: str,
        password_hash: str,
        role: str = "user",
        tier: str = "free",
        username: Optional[str] = None,
        invite_code: Optional[str] = None,
        invited_by: Optional[str] = None,
        bonus_tokens: int = 0,
        created_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = int(time.time())
        c_time = int(created_at or now)
        email_clean = email.strip().lower()
        u_name = username.strip().lower() if username else None
        db.execute(
            """
            INSERT INTO users (id, email, password_hash, role, tier, is_active, created_at, updated_at, username, invite_code, invited_by, bonus_tokens)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, email_clean, password_hash, role, tier, c_time, now, u_name, invite_code, invited_by, int(bonus_tokens or 0)),
        )
        return self.get_by_id(user_id)  # type: ignore

    def update_profile(self, user_id: str, username: Optional[str] = None, email: Optional[str] = None) -> bool:
        now = int(time.time())
        updates = []
        params = []
        if username is not None:
            updates.append("username = ?")
            params.append(username.strip().lower() if username.strip() else None)
        if email is not None:
            updates.append("email = ?")
            params.append(email.strip().lower())
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(now)
        params.append(user_id)
        cur = db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
        return cur.rowcount > 0

    def add_bonus_tokens(self, user_id: str, tokens: int) -> bool:
        """为用户追加算力额度奖励 (佣金折算)"""
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET bonus_tokens = coalesce(bonus_tokens, 0) + ?, updated_at = ? WHERE id = ?",
            (int(tokens), now, user_id),
        )
        return cur.rowcount > 0

    def update_password(self, user_id: str, password_hash: str) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, user_id),
        )
        return cur.rowcount > 0

    def update_tier(self, user_id: str, tier: str) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET tier = ?, updated_at = ? WHERE id = ?",
            (tier, now, user_id),
        )
        return cur.rowcount > 0

    def update_bonus_tokens(self, user_id: str, bonus_tokens: int) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET bonus_tokens = ?, updated_at = ? WHERE id = ?",
            (int(bonus_tokens), now, user_id),
        )
        return cur.rowcount > 0


    def update_role(self, user_id: str, role: str) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role, now, user_id),
        )
        return cur.rowcount > 0

    def update_status(self, user_id: str, is_active: bool) -> bool:
        now = int(time.time())
        cur = db.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, now, user_id),
        )
        return cur.rowcount > 0

    def get_personal_pool(self, user_id: str) -> List[str]:
        """获取用户个人号池列表（名下所有 Key 共享此池）"""
        row = db.fetchone("SELECT account_labels FROM users WHERE id = ?", (user_id,))
        if not row or not row["account_labels"]:
            return []
        try:
            return json.loads(row["account_labels"])
        except Exception:
            return []

    def set_personal_pool(self, user_id: str, labels: List[str], auto_replenish: Optional[bool] = None) -> bool:
        """设置用户个人号池（名下所有 Key 自动同步生效）"""
        now = int(time.time())
        clean_labels = json.dumps(list(dict.fromkeys([str(x).strip() for x in labels if str(x).strip()])))
        if auto_replenish is not None:
            cur = db.execute(
                "UPDATE users SET account_labels = ?, auto_replenish = ?, updated_at = ? WHERE id = ?",
                (clean_labels, 1 if auto_replenish else 0, now, user_id),
            )
        else:
            cur = db.execute(
                "UPDATE users SET account_labels = ?, updated_at = ? WHERE id = ?",
                (clean_labels, now, user_id),
            )
        return cur.rowcount > 0

    def add_to_personal_pool(self, user_id: str, label: str) -> bool:
        pool = self.get_personal_pool(user_id)
        clean = str(label).strip()
        if clean and clean not in pool:
            pool.append(clean)
            return self.set_personal_pool(user_id, pool)
        return True

    def remove_from_personal_pool(self, user_id: str, label: str) -> bool:
        pool = self.get_personal_pool(user_id)
        clean = str(label).strip()
        if clean in pool:
            pool.remove(clean)
            return self.set_personal_pool(user_id, pool)
        return True

    def count(self) -> int:
        row = db.fetchone("SELECT COUNT(*) as c FROM users")
        return int(row["c"]) if row else 0


class UsageRepo:
    """用量数据仓储"""

    def __init__(self):
        init_schema()

    def get_day_usage(self, key_id: str, day: str) -> Optional[Dict[str, Any]]:
        row = db.fetchone(
            "SELECT * FROM key_usage_daily WHERE key_id = ? AND day = ?",
            (key_id, day),
        )
        return _dict_from_row(row)

    def list_history(self, key_id: str, limit_days: int = 14) -> List[Dict[str, Any]]:
        rows = db.fetchall(
            """
            SELECT * FROM key_usage_daily
            WHERE key_id = ?
            ORDER BY day DESC
            LIMIT ?
            """,
            (key_id, limit_days),
        )
        return [dict(r) for r in rows]

    def list_all(self, limit: int = 1000) -> List[Dict[str, Any]]:
        rows = db.fetchall(
            """
            SELECT * FROM key_usage_daily
            ORDER BY day DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def upsert_usage(
        self,
        key_id: str,
        day: str,
        requests: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        errors: int = 0,
        rejected: int = 0,
        duration_ms: float = 0.0,
        last_used_at: Optional[int] = None,
        last_error: str = "",
    ):
        sql = """
        INSERT INTO key_usage_daily (
            key_id, day, requests, prompt_tokens, completion_tokens, cached_tokens,
            errors, rejected, duration_ms, last_used_at, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_id, day) DO UPDATE SET
            requests = key_usage_daily.requests + excluded.requests,
            prompt_tokens = key_usage_daily.prompt_tokens + excluded.prompt_tokens,
            completion_tokens = key_usage_daily.completion_tokens + excluded.completion_tokens,
            cached_tokens = key_usage_daily.cached_tokens + excluded.cached_tokens,
            errors = key_usage_daily.errors + excluded.errors,
            rejected = key_usage_daily.rejected + excluded.rejected,
            duration_ms = key_usage_daily.duration_ms + excluded.duration_ms,
            last_used_at = COALESCE(excluded.last_used_at, key_usage_daily.last_used_at),
            last_error = CASE WHEN excluded.last_error != '' THEN excluded.last_error ELSE key_usage_daily.last_error END
        """
        db.execute(
            sql,
            (
                key_id, day, requests, prompt_tokens, completion_tokens, cached_tokens,
                errors, rejected, duration_ms, last_used_at, last_error,
            ),
        )

    def save_day_snapshot(
        self,
        key_id: str,
        day: str,
        requests: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        errors: int = 0,
        rejected: int = 0,
        duration_ms: float = 0.0,
        last_used_at: Optional[int] = None,
        last_error: str = "",
    ):
        sql = """
        INSERT INTO key_usage_daily (
            key_id, day, requests, prompt_tokens, completion_tokens, cached_tokens,
            errors, rejected, duration_ms, last_used_at, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_id, day) DO UPDATE SET
            requests = excluded.requests,
            prompt_tokens = excluded.prompt_tokens,
            completion_tokens = excluded.completion_tokens,
            cached_tokens = excluded.cached_tokens,
            errors = excluded.errors,
            rejected = excluded.rejected,
            duration_ms = excluded.duration_ms,
            last_used_at = COALESCE(excluded.last_used_at, key_usage_daily.last_used_at),
            last_error = CASE WHEN excluded.last_error != '' THEN excluded.last_error ELSE key_usage_daily.last_error END
        """
        db.execute(
            sql,
            (
                key_id, day, requests, prompt_tokens, completion_tokens, cached_tokens,
                errors, rejected, duration_ms, last_used_at, last_error,
            ),
        )

    def reset_day(self, key_id: str, day: str) -> bool:
        cur = db.execute(
            """
            UPDATE key_usage_daily
            SET requests = 0, prompt_tokens = 0, completion_tokens = 0, cached_tokens = 0,
                errors = 0, rejected = 0, duration_ms = 0.0
            WHERE key_id = ? AND day = ?
            """,
            (key_id, day),
        )
        return cur.rowcount > 0

    def delete_by_key(self, key_id: str) -> bool:
        cur = db.execute("DELETE FROM key_usage_daily WHERE key_id = ?", (key_id,))
        return cur.rowcount > 0

    def get_user_daily_summary(self, user_id: str, days: int = 14) -> Dict[str, Any]:
        """按用户聚合名下所有 Key 的用量（包含今日总量、历史按天聚合趋势）"""
        keys_rows = db.fetchall("SELECT id FROM api_keys WHERE user_id = ?", (user_id,))
        key_ids = [r["id"] for r in keys_rows]
        today_str = time.strftime("%Y-%m-%d", time.localtime())
        if not key_ids:
            return {
                "today": {
                    "day": today_str,
                    "requests": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                    "errors": 0,
                },
                "history": [],
            }

        placeholders = ",".join("?" for _ in key_ids)
        today_sql = f"""
        SELECT 
            COALESCE(SUM(requests), 0) as requests,
            COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) as completion_tokens,
            COALESCE(SUM(cached_tokens), 0) as cached_tokens,
            COALESCE(SUM(errors), 0) as errors
        FROM key_usage_daily
        WHERE key_id IN ({placeholders}) AND day = ?
        """
        today_row = db.fetchone(today_sql, (*key_ids, today_str))

        history_sql = f"""
        SELECT 
            day,
            SUM(requests) as requests,
            SUM(prompt_tokens) as prompt_tokens,
            SUM(completion_tokens) as completion_tokens,
            SUM(cached_tokens) as cached_tokens,
            SUM(errors) as errors
        FROM key_usage_daily
        WHERE key_id IN ({placeholders})
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
        """
        hist_rows = db.fetchall(history_sql, (*key_ids, days))

        req = int(today_row["requests"]) if today_row else 0
        prompt = int(today_row["prompt_tokens"]) if today_row else 0
        comp = int(today_row["completion_tokens"]) if today_row else 0
        cached = int(today_row["cached_tokens"]) if today_row else 0

        return {
            "today": {
                "day": today_str,
                "requests": req,
                "prompt_tokens": prompt,
                "completion_tokens": comp,
                "cached_tokens": cached,
                "total_tokens": prompt + comp,
                "errors": int(today_row["errors"]) if today_row else 0,
            },
            "history": [
                {
                    "day": r["day"],
                    "requests": int(r["requests"]),
                    "prompt_tokens": int(r["prompt_tokens"]),
                    "completion_tokens": int(r["completion_tokens"]),
                    "total_tokens": int(r["prompt_tokens"]) + int(r["completion_tokens"]),
                    "cached_tokens": int(r["cached_tokens"]),
                    "errors": int(r["errors"]),
                }
                for r in hist_rows
            ],
        }



class SettingsRepo:
    """系统全局配置仓储 (替代 config.json 中的全局参数)"""

    def __init__(self):
        init_schema()

    def get(self, key: str, default: Any = None) -> Any:
        row = db.fetchone("SELECT value FROM system_settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def set(self, key: str, value: Any):
        now = int(time.time())
        val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, (int, float, bool, str)) else json.dumps(value)
        db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, val_str, now),
        )

    def get_all(self) -> Dict[str, Any]:
        rows = db.fetchall("SELECT key, value FROM system_settings")
        result = {}
        for r in rows:
            k = r["key"]
            v = r["value"]
            try:
                result[k] = json.loads(v)
            except Exception:
                result[k] = v
        return result

    def set_batch(self, mapping: Dict[str, Any]):
        now = int(time.time())
        with db.transaction():
            for k, v in mapping.items():
                val_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, (int, float, bool, str)) else json.dumps(v)
                db.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (k, val_str, now),
                )


account_repo = AccountRepo()
api_key_repo = ApiKeyRepo()
user_repo = UserRepo()
usage_repo = UsageRepo()
settings_repo = SettingsRepo()


class EmailVerificationRepo:
    """C 端用户注册邮箱验证码存储、频控与防爆破原子仓储"""

    CODE_TTL_SECONDS = 300        # 验证码有效期 5 分钟 (300s)
    RESEND_COOLDOWN_SECONDS = 60  # 单邮箱发送冷却 60 秒
    MAX_DAILY_SENDS_PER_EMAIL = 5 # 单邮箱每日最多发送 5 次
    MAX_VERIFY_ATTEMPTS = 5       # 单个验证码最多允许输错 5 次（防枚举爆破）

    def __init__(self):
        init_schema()

    @staticmethod
    def _hash_code(email: str, code: str, secret: str) -> str:
        import hmac
        import hashlib
        msg = f"{email.strip().lower()}:{code.strip()}"
        return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()

    def check_and_prepare_send(
        self,
        email: str,
        code: str,
        client_ip: str,
        secret: str,
        cooldown_seconds: Optional[int] = None,
        max_daily_sends: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """
        原子检查发送冷却与每日上限，通过后写入 HMAC-SHA256 验证码摘要。
        返回 None 表示允许发送；返回非空字符串表示限流拒绝原因。
        """
        now = int(time.time())
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        email_clean = email.strip().lower()
        cd = self.RESEND_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
        daily_max = self.MAX_DAILY_SENDS_PER_EMAIL if max_daily_sends is None else max_daily_sends
        ttl = self.CODE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        code_hash = self._hash_code(email_clean, code, secret)

        with db.transaction():
            row = db.fetchone(
                "SELECT * FROM email_verifications WHERE email = ?",
                (email_clean,),
            )
            if row:
                elapsed = now - int(row["last_sent_at"] or 0)
                if cd > 0 and elapsed < cd:
                    wait_s = cd - elapsed
                    return f"验证码发送过于频繁，请 {wait_s} 秒后再试"

                send_cnt = int(row["send_count_today"] or 0) if row["day_str"] == today else 0
                if daily_max > 0 and send_cnt >= daily_max:
                    return f"该邮箱今日验证码发送次数已达上限（{daily_max}次/日），请明日再试"
                new_cnt = send_cnt + 1
            else:
                new_cnt = 1

            db.execute(
                """
                INSERT INTO email_verifications (
                    email, code_hash, attempts, send_count_today, day_str, last_sent_at, expires_at, request_ip
                ) VALUES (?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    attempts = 0,
                    send_count_today = excluded.send_count_today,
                    day_str = excluded.day_str,
                    last_sent_at = excluded.last_sent_at,
                    expires_at = excluded.expires_at,
                    request_ip = excluded.request_ip
                """,
                (email_clean, code_hash, new_cnt, today, now, now + ttl, client_ip or ""),
            )
        return None

    def verify_and_consume(self, email: str, code: str, secret: str) -> tuple[bool, str]:
        """原子核验并一次性消费验证码；输错累加 attempts，满 5 次立即作废。"""
        import hmac
        now = int(time.time())
        email_clean = email.strip().lower()
        incoming_hash = self._hash_code(email_clean, code, secret)

        with db.transaction():
            row = db.fetchone(
                "SELECT * FROM email_verifications WHERE email = ?",
                (email_clean,),
            )
            if not row or not row["code_hash"]:
                return False, "请先获取邮箱验证码"
            if int(row["attempts"] or 0) >= self.MAX_VERIFY_ATTEMPTS:
                return False, "验证码错误次数过多已失效，请重新获取"
            if now > int(row["expires_at"] or 0):
                return False, "验证码已过期（有效期 5 分钟），请重新获取"

            if hmac.compare_digest(str(row["code_hash"]), incoming_hash):
                db.execute(
                    "UPDATE email_verifications SET code_hash = '', expires_at = 0, attempts = 0 WHERE email = ?",
                    (email_clean,),
                )
                return True, "ok"
            else:
                new_attempts = int(row["attempts"] or 0) + 1
                db.execute(
                    "UPDATE email_verifications SET attempts = ? WHERE email = ?",
                    (new_attempts, email_clean),
                )
                left = max(0, self.MAX_VERIFY_ATTEMPTS - new_attempts)
                if left == 0:
                    return False, "验证码错误次数已达上限（5次），该验证码已作废，请重新获取"
                return False, f"验证码不正确（还剩 {left} 次尝试机会）"


email_verification_repo = EmailVerificationRepo()


class AlertRepo:
    """封号与风控消息收件箱仓储 (支持时间戳、已读/未读状态、一键全读与清理)"""

    def __init__(self):
        init_schema()

    def add_alert(
        self,
        account: str,
        alert_type: str,
        message: str,
        wake_at: Optional[str] = None,
        caller_key: Optional[str] = None,
        caller_owner: Optional[str] = None,
        caller_ip: Optional[str] = None,
        created_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        import os
        now = created_at or int(time.time())
        alert_id = f"alt_{now}_{int(time.time() * 1000) % 1000}_{os.urandom(3).hex()}"
        sql = """
        INSERT INTO system_alerts (
            id, account, alert_type, message, wake_at,
            caller_key, caller_owner, caller_ip, is_read, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """
        db.execute(
            sql,
            (
                alert_id,
                account,
                alert_type,
                message,
                wake_at,
                caller_key,
                caller_owner,
                caller_ip,
                now,
            ),
        )
        try:
            db.execute("""
                DELETE FROM system_alerts
                WHERE id NOT IN (
                    SELECT id FROM system_alerts ORDER BY created_at DESC LIMIT 200
                ) AND is_read = 1
            """)
        except Exception:
            pass
        return {
            "id": alert_id,
            "account": account,
            "alert_type": alert_type,
            "message": message,
            "wake_at": wake_at,
            "caller_key": caller_key,
            "caller_owner": caller_owner,
            "caller_ip": caller_ip,
            "is_read": False,
            "created_at": now,
            "time_fmt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        }

    def list_alerts(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = db.fetchall(
            """
            SELECT * FROM system_alerts
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        res = []
        for r in rows:
            d = dict(r)
            d["is_read"] = bool(d.get("is_read", 0))
            ts = d.get("created_at") or 0
            d["time_fmt"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
            res.append(d)
        return res

    def get_unread_count(self) -> int:
        row = db.fetchone("SELECT COUNT(*) as c FROM system_alerts WHERE is_read = 0")
        return int(row["c"]) if row else 0

    def mark_read(self, alert_id: Optional[str] = None) -> int:
        """标记单个或全部已读"""
        if alert_id and alert_id != "all":
            cur = db.execute("UPDATE system_alerts SET is_read = 1 WHERE id = ?", (alert_id,))
            return cur.rowcount
        else:
            cur = db.execute("UPDATE system_alerts SET is_read = 1 WHERE is_read = 0")
            return cur.rowcount

    def clear_read(self) -> int:
        """清理所有已读通知"""
        cur = db.execute("DELETE FROM system_alerts WHERE is_read = 1")
        return cur.rowcount


alert_repo = AlertRepo()
