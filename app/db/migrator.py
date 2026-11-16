"""
数据自动迁移工具 (Dual-Rail Auto-Migrator)
- 启动时自动检测：若 SQLite 数据库为空，自动将存量 JSON 数据无损灌入数据库
- 支持 config.json (accounts 资产)、api_keys.json、key_usage.json
- 绝对保留原始 JSON 文件，支持零风险回滚与平滑割接
"""

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from app.db.schema import init_schema
from app.db.repositories import (
    account_repo,
    api_key_repo,
    user_repo,
    usage_repo,
)

CONFIG_FILE = Path(os.environ.get("DSG_CONFIG_FILE", str(PROJECT_ROOT / "config.json")))
KEYS_FILE = Path(os.environ.get("DSG_KEYS_FILE", str(PROJECT_ROOT / "api_keys.json")))
USAGE_FILE = Path(os.environ.get("DSG_USAGE_FILE", str(PROJECT_ROOT / "key_usage.json")))


def _hash_password(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def auto_migrate_if_needed() -> Dict[str, Any]:
    """
    检查并在必要时执行从 JSON 到 SQLite 的自动无损迁移
    返回迁移统计字典
    """
    init_schema()
    stats = {
        "migrated": False,
        "accounts_count": 0,
        "keys_count": 0,
        "users_count": 0,
        "usage_records_count": 0,
    }

    # 1. 账号资产迁移 (从 config.json 的 accounts 数组迁移)
    if account_repo.count() == 0 and CONFIG_FILE.exists():
        try:
            cfg_data = json.loads(CONFIG_FILE.read_text("utf-8"))
            raw_accounts = cfg_data.get("accounts", [])
            if raw_accounts and isinstance(raw_accounts, list):
                valid_accounts = [a for a in raw_accounts if isinstance(a, dict) and a.get("account_label")]
                if valid_accounts:
                    imported = account_repo.save_batch(valid_accounts)
                    stats["accounts_count"] = imported
                    print(f"[DB-Migration] ✅ 已自动将 {imported} 个账号资产从 config.json 无损迁移至 SQLite", flush=True)
        except Exception as e:
            print(f"[DB-Migration] ⚠️ 迁移 config.json 账号失败: {e}", flush=True)

    # 2. API Key 迁移 (从 api_keys.json 迁移)
    if api_key_repo.count() == 0 and KEYS_FILE.exists():
        try:
            raw_keys = json.loads(KEYS_FILE.read_text("utf-8"))
            if raw_keys and isinstance(raw_keys, list):
                valid_keys = [k for k in raw_keys if isinstance(k, dict) and k.get("id")]
                if valid_keys:
                    imported = api_key_repo.save_batch(valid_keys)
                    stats["keys_count"] = imported
                    print(f"[DB-Migration] ✅ 已自动将 {imported} 个 API Key 从 api_keys.json 无损迁移至 SQLite", flush=True)
        except Exception as e:
            print(f"[DB-Migration] ⚠️ 迁移 api_keys.json 失败: {e}", flush=True)

    # 3. 初始 Admin 用户自愈建立
    if user_repo.count() == 0:
        try:
            admin_pwd = "admin123"
            try:
                from app.config import config_manager
                admin_pwd = config_manager.config.admin_password or admin_pwd
            except Exception:
                pass
            user_repo.create(
                user_id=f"u_{secrets.token_hex(6)}",
                email="admin@localhost",
                password_hash=_hash_password(admin_pwd),
                role="admin",
                tier="ultra",
            )
            stats["users_count"] = 1
            print("[DB-Migration] ✅ 已自动建立默认管理员用户 admin@localhost", flush=True)
        except Exception as e:
            print(f"[DB-Migration] ⚠️ 创建初始 admin 用户失败: {e}", flush=True)

    # 4. 用量数据迁移 (从 key_usage.json 迁移)
    if USAGE_FILE.exists():
        try:
            raw_usage = json.loads(USAGE_FILE.read_text("utf-8"))
            if isinstance(raw_usage, dict):
                count = 0
                for kid, v in raw_usage.items():
                    if not isinstance(v, dict):
                        continue
                    day = v.get("day")
                    if day:
                        usage_repo.save_day_snapshot(
                            key_id=kid,
                            day=day,
                            requests=int(v.get("requests") or 0),
                            prompt_tokens=int(v.get("prompt_tokens") or 0),
                            completion_tokens=int(v.get("completion_tokens") or 0),
                            cached_tokens=int(v.get("cached_tokens") or 0),
                            errors=int(v.get("errors") or 0),
                            rejected=int(v.get("rejected") or 0),
                            duration_ms=float(v.get("duration_ms") or 0.0),
                            last_used_at=v.get("last_used_at"),
                            last_error=str(v.get("last_error") or ""),
                        )
                        count += 1
                    # 历史
                    hist = v.get("history") or {}
                    for h_day, h_v in hist.items():
                        if isinstance(h_v, dict):
                            usage_repo.save_day_snapshot(
                                key_id=kid,
                                day=h_day,
                                requests=int(h_v.get("requests") or 0),
                                prompt_tokens=int(h_v.get("prompt_tokens") or 0),
                                completion_tokens=int(h_v.get("completion_tokens") or 0),
                                cached_tokens=int(h_v.get("cached_tokens") or 0),
                                errors=int(h_v.get("errors") or 0),
                                duration_ms=float(h_v.get("duration_ms") or 0.0),
                            )
                            count += 1
                if count > 0:
                    stats["usage_records_count"] = count
                    print(f"[DB-Migration] ✅ 已自动迁移 {count} 条用量流水记录至 SQLite", flush=True)
        except Exception as e:
            print(f"[DB-Migration] ⚠️ 迁移 key_usage.json 失败: {e}", flush=True)

    # 5. 系统全局配置迁移 (从 config.json 迁移至 system_settings)
    try:
        from app.db.repositories import settings_repo
        if not settings_repo.get_all() and CONFIG_FILE.exists():
            cfg_data = json.loads(CONFIG_FILE.read_text("utf-8"))
            settings_data = {k: v for k, v in cfg_data.items() if k != "accounts"}
            if settings_data:
                settings_repo.set_batch(settings_data)
                print(f"[DB-Migration] ✅ 已自动将 {len(settings_data)} 项系统全局配置从 config.json 迁移至 SQLite", flush=True)
    except Exception as e:
        print(f"[DB-Migration] ⚠️ 迁移系统配置失败: {e}", flush=True)

    stats["migrated"] = bool(stats["accounts_count"] or stats["keys_count"] or stats["usage_records_count"])
    return stats

