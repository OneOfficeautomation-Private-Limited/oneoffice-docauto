"""PostgreSQL for DocFlow: the server in DOCAUTO_DATABASE_URL, or an embedded one started on demand.

* DOCAUTO_DATABASE_URL set: that server is used (it must use UTF8; any PostgreSQL 13+).
* not set: an embedded PostgreSQL 16 (the pgserver package) runs from <data dir>/postgres with a database
  "docflow" (UTF8, C collation), so a plain install needs no set-up. It stops with the last DocFlow process.

Every tenant has its own schema (schema_for); each Store sets its search_path on the connections it uses.
Connections come from a small thread-safe pool per server (DOCAUTO_DB_POOL_SIZE, default 20).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

log = logging.getLogger("docauto.db")
EMBEDDED_DB = "docflow"
DEFAULT_PREFIX = "docflow_"
_lock = threading.Lock()
_pools: dict[str, "Pool"] = {}
_servers: dict[str, object] = {}       # pgdata -> pgserver.PostgresServer (kept alive for the process)


# ------------------------------------------------------------------ where the database is
def embedded_url(pgdata: str | Path) -> str:
    """Start (or join) the embedded server in pgdata and return the URL of its "docflow" database."""
    key = str(Path(pgdata).resolve())
    with _lock:
        srv = _servers.get(key)
        if srv is None:
            import pgserver
            logging.getLogger("pgserver").setLevel(logging.WARNING)   # it logs every command it runs at INFO
            Path(key).mkdir(parents=True, exist_ok=True)
            _prune_handles(Path(key))
            log.info("starting the embedded PostgreSQL server in %s", key)
            srv = _servers[key] = pgserver.get_server(key, cleanup_mode="stop")
    _ensure_database(srv.get_uri(), EMBEDDED_DB)
    return srv.get_uri(database=EMBEDDED_DB)


def _prune_handles(pgdata: Path) -> None:
    """pgserver stops the server when its last user process exits; a process that was killed never says goodbye.
    Forget such processes, or the server would never stop on its own again."""
    f = pgdata / ".handle_pids.json"
    try:
        import json

        import psutil
        pids = json.loads(f.read_text())
        alive = [p for p in pids if psutil.pid_exists(p)]
        if alive != pids:
            f.write_text(json.dumps(alive))
    except (OSError, ValueError, ImportError):
        pass


def _ensure_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as c:
        if c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            return
        try:   # C collation: text sorts byte-wise, like SQLite did (ISO dates, names)
            c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'")
                      .format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            pass   # another DocFlow process created it first


def resolve_url(url: Optional[str], data_dir: str | Path) -> str:
    """The database URL: the configured one, else DOCAUTO_DATABASE_URL, else the embedded server of data_dir."""
    url = (url or os.environ.get("DOCAUTO_DATABASE_URL") or "").strip()
    return url or embedded_url(Path(data_dir) / "postgres")


def redact(url: str) -> str:
    """The URL without its password, for logs."""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


def schema_for(tenant: str, prefix: Optional[str] = None) -> str:
    """The schema of a tenant: <prefix><tenant> (default prefix docflow_), shortened with a hash past 63 bytes."""
    prefix = DEFAULT_PREFIX if prefix is None else prefix
    name = f"{prefix}{tenant}".lower()
    if len(name.encode()) > 63:
        name = name[:50] + "_" + hashlib.sha1(name.encode()).hexdigest()[:12]
    return name


# ------------------------------------------------------------------ pool
class _Conn:
    __slots__ = ("conn", "schema")

    def __init__(self, conn: psycopg.Connection):
        self.conn, self.schema = conn, None


class Pool:
    """A small thread-safe connection pool (autocommit connections, dict rows). getconn blocks when all
    max_size connections are in use; idle connections beyond max_idle are closed."""

    def __init__(self, url: str, max_size: int = 20, max_idle: int = 6, timeout: float = 60.0):
        self.url, self.max_size, self.max_idle, self.timeout = url, max_size, max_idle, timeout
        self._idle: list[_Conn] = []
        self._sem = threading.BoundedSemaphore(max_size)
        self._lock = threading.Lock()
        self._checked = False

    def _connect(self) -> _Conn:
        conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row, connect_timeout=15)
        if not self._checked:
            enc = conn.execute("SHOW server_encoding").fetchone()["server_encoding"]
            if enc.upper() not in ("UTF8", "UTF-8"):
                conn.close()
                raise RuntimeError(f"the DocFlow database must use UTF8 encoding (it uses {enc}): Arabic text "
                                   "cannot be stored otherwise")
            self._checked = True
        return _Conn(conn)

    @contextmanager
    def connection(self, schema: str) -> Iterator[psycopg.Connection]:
        if not self._sem.acquire(timeout=self.timeout):
            raise RuntimeError(f"no free database connection after {self.timeout:.0f} s (pool of {self.max_size})")
        c: Optional[_Conn] = None
        try:
            with self._lock:
                while self._idle and c is None:
                    c = self._idle.pop()
                    if c.conn.closed or c.conn.broken:
                        c = None
            if c is None:
                c = self._connect()
            if c.schema != schema:
                c.conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
                c.schema = schema
            yield c.conn
        except psycopg.OperationalError:
            if c is not None:   # the server went away: never reuse this connection
                c.conn.close()
            raise
        finally:
            if c is not None and not c.conn.closed:
                if c.conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    try:
                        c.conn.rollback()
                    except psycopg.Error:
                        c.conn.close()
                with self._lock:
                    if not c.conn.closed and len(self._idle) < self.max_idle:
                        self._idle.append(c)
                        c = None
                if c is not None:
                    c.conn.close()
            self._sem.release()

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for c in idle:
            c.conn.close()


def pool(url: str) -> Pool:
    with _lock:
        p = _pools.get(url)
        if p is None:
            size = int(os.environ.get("DOCAUTO_DB_POOL_SIZE") or 20)
            p = _pools[url] = Pool(url, max_size=size)
            log.info("database: %s", redact(url))
        return p


def wait_until_ready(url: str, timeout: float = 30.0) -> None:
    """Wait for a server that is still starting (docker compose)."""
    end = time.time() + timeout
    while True:
        try:
            with psycopg.connect(url, connect_timeout=5):
                return
        except psycopg.OperationalError:
            if time.time() > end:
                raise
            time.sleep(1)

