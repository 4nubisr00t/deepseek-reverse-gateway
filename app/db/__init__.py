"""
ds-gateway 数据库模块 (SQLite WAL 模式)
提供线程安全、零外部依赖的结构化数据持久化与仓储管理
"""

from app.db import database
from app.db import schema
from app.db import repositories
from app.db import migrator

from app.db.database import db
from app.db.repositories import (
    account_repo,
    api_key_repo,
    user_repo,
    usage_repo,
)
from app.db.migrator import auto_migrate_if_needed

__all__ = [
    "database",
    "schema",
    "repositories",
    "migrator",
    "db",
    "account_repo",
    "api_key_repo",
    "user_repo",
    "usage_repo",
    "auto_migrate_if_needed",
]

