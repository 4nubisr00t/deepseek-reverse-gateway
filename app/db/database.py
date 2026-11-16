"""
SQLite 数据库核心引擎 (WAL 模式)
- 零外部依赖，使用 Python 标准库 sqlite3
- 开启 WAL (Write-Ahead Logging) 模式，读写并发互不阻塞
- 线程安全连接管理，支持事务上下文
- 路径支持环境变量 DSG_DB_FILE 覆盖（供测试隔离）
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 数据库文件路径：默认为项目根目录下的 data/gateway.db
# 可通过环境变量 DSG_DB_FILE 覆盖（测试隔离必须）
DEFAULT_DB_FILE = PROJECT_ROOT / "data" / "gateway.db"
DB_FILE = Path(os.environ.get("DSG_DB_FILE", str(DEFAULT_DB_FILE)))


class DatabaseManager:
    """线程安全的 SQLite 数据库管理器"""

    def __init__(self, db_path: Optional[Path] = None):
        self._explicit_path = Path(db_path) if db_path is not None else None
        self._local = threading.local()
        self._lock = threading.RLock()
        self._initialized = False

    @property
    def db_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        # 支持运行时环境变量动态覆盖（如测试期间被 patch）
        env_val = os.environ.get("DSG_DB_FILE")
        if env_val:
            return Path(env_val)
        return DEFAULT_DB_FILE

    def _ensure_dir(self):
        parent = self.db_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        self._ensure_dir()
        conn = getattr(self._local, "conn", None)
        current_path = str(self.db_path.resolve())
        conn_path = getattr(self._local, "conn_path", None)

        if conn is None or conn_path != current_path:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = sqlite3.connect(
                current_path,
                timeout=10.0,
                check_same_thread=False,
                isolation_level=None,  # 开启 autocommit 模式，便于显式控制事务
            )
            conn.row_factory = sqlite3.Row
            # 性能与可靠性参数调优
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
            self._local.conn_path = current_path
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """原子事务上下文管理器"""
        conn = self.get_connection()
        with self._lock:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        conn = self.get_connection()
        with self._lock:
            return conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        conn = self.get_connection()
        with self._lock:
            return conn.executemany(sql, seq_of_params)

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        cur = self.execute(sql, params)
        return cur.fetchone()

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        cur = self.execute(sql, params)
        return cur.fetchall()

    def close(self):
        """关闭当前线程连接"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
            self._local.conn_path = None


db = DatabaseManager()
