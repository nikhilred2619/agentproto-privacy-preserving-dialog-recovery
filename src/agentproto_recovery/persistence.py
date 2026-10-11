"""SQLite persistence with explicit transactions.

Each administrative domain gets its own database file. Nothing in this
module lets one domain read another domain's file; sharing a file is a
deployment decision made in evaluation.Deployment and documented there.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StoreUnavailable
from .failure_injection import FailureInjector


class Database:
    def __init__(self, path: Path, name: str, injector: FailureInjector | None = None,
                 schema: str = "") -> None:
        self.path = Path(path)
        self.name = name
        self.injector = injector
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if schema:
            c = self._connect()
            c.executescript(schema)

    @property
    def unavailable_point(self) -> str:
        return f"store.{self.name}.unavailable"

    def _connect(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10.0, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def conn(self) -> sqlite3.Connection:
        if self.injector is not None and self.injector.active(self.unavailable_point):
            raise StoreUnavailable(self.name)
        try:
            return self._connect()
        except sqlite3.Error as e:  # pragma: no cover - environment dependent
            raise StoreUnavailable(str(e)) from e

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT; any exception rolls back."""
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise StoreUnavailable(str(e)) from e
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            try:
                c.execute("COMMIT")
            except sqlite3.OperationalError as e:
                c.execute("ROLLBACK")
                raise StoreUnavailable(str(e)) from e

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            return self.conn().execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            raise StoreUnavailable(str(e)) from e

    def size_bytes(self) -> int:
        c = self.conn()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return self.path.stat().st_size

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
        self._local = threading.local()
