"""
tests/test_database_and_migration.py
测试 SQLite 数据库核心引擎、仓储层与自动数据迁移机制
"""

import json
import os
import sys
import tempfile
from pathlib import Path
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.db.database import DatabaseManager
from app.db.repositories import AccountRepo, ApiKeyRepo, UserRepo, UsageRepo
from app.db.migrator import auto_migrate_if_needed


@pytest.fixture
def isolated_db(tmp_path):
    """创建完全独立的临时测试数据库"""
    db_file = tmp_path / "test_gateway.db"
    mgr = DatabaseManager(db_file)
    from app.db.schema import init_schema
    init_schema(mgr)
    yield mgr
    mgr.close()


def test_db_wal_and_schema_initialization(isolated_db):
    conn = isolated_db.get_connection()
    # 验证 WAL 模式
    cur = conn.execute("PRAGMA journal_mode;")
    mode = cur.fetchone()[0]
    assert mode.lower() == "wal"

    # 初始化 schema
    from app.db.schema import init_schema
    init_schema()

    # 验证各表存在
    tables = [r[0] for r in isolated_db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "users" in tables
    assert "accounts" in tables
    assert "api_keys" in tables
    assert "key_usage_daily" in tables
    assert "system_meta" in tables


def test_account_repo_crud(isolated_db, monkeypatch):
    monkeypatch.setattr("app.db.repositories.db", isolated_db)
    repo = AccountRepo()

    # 1. 插入账号
    acc_data = {
        "account_label": "test_acc@example.com",
        "token": "bearer_token_123",
        "cookie": "cookie_456",
        "is_valid": True,
        "status": "ACTIVE",
    }
    saved = repo.save(acc_data)
    assert saved["account_label"] == "test_acc@example.com"
    assert saved["token"] == "bearer_token_123"
    assert repo.count() == 1

    # 2. 更新状态
    repo.update_runtime_state(
        "test_acc@example.com",
        status="MUTED",
        is_valid=False,
        mute_until=1800000000,
        mute_count=2,
    )
    updated = repo.get("test_acc@example.com")
    assert updated["status"] == "MUTED"
    assert updated["is_valid"] == 0
    assert updated["mute_until"] == 1800000000
    assert updated["mute_count"] == 2

    # 3. 批量插入
    batch = [
        {"account_label": f"acc_{i}@x.com", "token": f"tok_{i}"}
        for i in range(5)
    ]
    repo.save_batch(batch)
    assert repo.count() == 6

    # 4. 删除
    assert repo.delete("test_acc@example.com") is True
    assert repo.get("test_acc@example.com") is None
    assert repo.count() == 5


def test_api_key_repo_crud(isolated_db, monkeypatch):
    monkeypatch.setattr("app.db.repositories.db", isolated_db)
    repo = ApiKeyRepo()

    key_data = {
        "id": "key_123",
        "name": "test_key",
        "owner": "Alice",
        "key_hash": "hash_abc_123",
        "prefix": "sk-123456...",
        "account_labels": ["acc1", "acc2"],
        "enabled": True,
        "quota_daily_requests": 100,
        "quota_daily_tokens": 50000,
    }
    repo.save(key_data)

    # 查找
    fetched = repo.get_by_id("key_123")
    assert fetched is not None
    assert fetched["name"] == "test_key"
    assert fetched["owner"] == "Alice"
    assert fetched["account_labels"] == ["acc1", "acc2"]
    assert fetched["quota_daily_requests"] == 100

    # 按 hash 查找
    by_hash = repo.get_by_hash("hash_abc_123")
    assert by_hash is not None
    assert by_hash["id"] == "key_123"

    # 作废
    repo.revoke("key_123")
    revoked = repo.get_by_id("key_123")
    assert revoked["revoked"] is True
    assert revoked["enabled"] is False
    assert revoked["account_labels"] == []


def test_user_and_usage_repo(isolated_db, monkeypatch):
    monkeypatch.setattr("app.db.repositories.db", isolated_db)
    u_repo = UserRepo()
    usage = UsageRepo()

    # 创建用户
    user = u_repo.create("u_01", "alice@example.com", "hashed_pwd", role="user", tier="pro")
    assert user["email"] == "alice@example.com"
    assert user["tier"] == "pro"
    assert u_repo.get_by_email("Alice@example.com")["id"] == "u_01"

    # 用量记录
    usage.upsert_usage(
        key_id="k_test",
        day="2026-10-03",
        requests=5,
        prompt_tokens=1000,
        completion_tokens=500,
        cached_tokens=200,
    )
    rec = usage.get_day_usage("k_test", "2026-10-03")
    assert rec["requests"] == 5
    assert rec["prompt_tokens"] == 1000
    assert rec["completion_tokens"] == 500

    # 增量更新
    usage.upsert_usage(
        key_id="k_test",
        day="2026-10-03",
        requests=2,
        prompt_tokens=300,
        completion_tokens=100,
    )
    rec2 = usage.get_day_usage("k_test", "2026-10-03")
    assert rec2["requests"] == 7
    assert rec2["prompt_tokens"] == 1300


def test_auto_migration_from_json(tmp_path, monkeypatch):
    # 模拟旧环境中的三个 json
    cfg_file = tmp_path / "config.json"
    keys_file = tmp_path / "api_keys.json"
    usage_file = tmp_path / "key_usage.json"
    db_file = tmp_path / "migrated.db"

    cfg_file.write_text(json.dumps({
        "admin_password": "supersecretpassword",
        "accounts": [
            {"account_label": "old_acc_1@x.com", "token": "tok1", "status": "ACTIVE"},
            {"account_label": "old_acc_2@x.com", "token": "tok2", "status": "INACTIVE_COLD"}
        ]
    }), encoding="utf-8")

    keys_file.write_text(json.dumps([
        {
            "id": "old_key_1",
            "name": "developer",
            "key_hash": "hash_dev",
            "prefix": "sk-dev...",
            "account_labels": ["old_acc_1@x.com"],
            "enabled": True,
        }
    ]), encoding="utf-8")

    usage_file.write_text(json.dumps({
        "old_key_1": {
            "day": "2026-10-02",
            "requests": 15,
            "prompt_tokens": 12000,
            "completion_tokens": 3000,
        }
    }), encoding="utf-8")

    # 环境变量指向该临时路径
    monkeypatch.setenv("DSG_DB_FILE", str(db_file))
    monkeypatch.setattr("app.db.migrator.CONFIG_FILE", cfg_file)
    monkeypatch.setattr("app.db.migrator.KEYS_FILE", keys_file)
    monkeypatch.setattr("app.db.migrator.USAGE_FILE", usage_file)

    # 重新初始化 database 实例
    test_db = DatabaseManager(db_file)
    monkeypatch.setattr("app.db.database.db", test_db)
    monkeypatch.setattr("app.db.repositories.db", test_db)

    # 执行自动迁移
    stats = auto_migrate_if_needed()
    assert stats["migrated"] is True
    assert stats["accounts_count"] == 2
    assert stats["keys_count"] == 1
    assert stats["users_count"] == 1
    assert stats["usage_records_count"] == 1

    # 验证账号和 Key 是否已经正确落盘在 SQLite
    acc_repo = AccountRepo()
    assert acc_repo.count() == 2
    cold = acc_repo.get("old_acc_2@x.com")
    assert cold["status"] == "INACTIVE_COLD"

    k_repo = ApiKeyRepo()
    assert k_repo.count() == 1
    dev_key = k_repo.get_by_id("old_key_1")
    assert dev_key["account_labels"] == ["old_acc_1@x.com"]

    u_repo = UserRepo()
    admin = u_repo.get_by_email("admin@localhost")
    assert admin is not None
    assert admin["role"] == "admin"

    # 原 JSON 依然健在，绝不删除
    assert cfg_file.exists()
    assert keys_file.exists()
    assert usage_file.exists()
