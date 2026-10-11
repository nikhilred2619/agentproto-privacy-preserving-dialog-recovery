"""Gateway-operator correlation store.

Maps (tenant, upstream_id) -> downstream_id plus lifecycle state. Every
row carries an HMAC tag over all of its fields (integrity). Every update is
a compare-and-set on `version` inside BEGIN IMMEDIATE (no lost updates, no
older checkpoint overwriting newer state). The tag does NOT provide
freshness: a validly tagged older row restored from a backup verifies fine
and is only caught by epoch checks at the endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from .errors import Code, RecoveryError, VersionConflict
from .lifecycle import State, check_transition
from .persistence import Database
from .security import RecordSigner

SCHEMA = """
CREATE TABLE IF NOT EXISTS correlation (
    tenant          TEXT NOT NULL,
    upstream_id     TEXT NOT NULL,
    principal       TEXT NOT NULL,
    downstream_id   TEXT NOT NULL,
    target_agent    TEXT NOT NULL,
    state           TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    owner           TEXT,
    lease_until     REAL,
    last_request_id TEXT,
    last_result     TEXT,
    tag             TEXT NOT NULL,
    PRIMARY KEY (tenant, upstream_id)
);
"""

_COLS = ("tenant", "upstream_id", "principal", "downstream_id", "target_agent",
         "state", "epoch", "version", "owner", "lease_until", "last_request_id",
         "last_result")


@dataclass(frozen=True)
class CorrelationRecord:
    tenant: str
    upstream_id: str
    principal: str
    downstream_id: str
    target_agent: str
    state: str
    epoch: int
    version: int
    owner: str | None = None
    lease_until: float | None = None
    last_request_id: str | None = None
    last_result: str | None = None
    tag: str = ""

    def fields(self) -> dict:
        d = asdict(self)
        d.pop("tag")
        return d


Hook = Callable[[], None] | None


class CorrelationStore:
    def __init__(self, db: Database, signer: RecordSigner) -> None:
        self.db = db
        self.signer = signer
        db.conn().executescript(SCHEMA)

    def _sign(self, rec: CorrelationRecord) -> CorrelationRecord:
        return replace(rec, tag=self.signer.tag(rec.fields()))

    def _row_to_rec(self, row) -> CorrelationRecord:
        rec = CorrelationRecord(**{k: row[k] for k in _COLS}, tag=row["tag"])
        if not self.signer.verify(rec.fields(), rec.tag):
            raise RecoveryError(Code.MAPPING_INTEGRITY_FAILURE, "tag mismatch")
        try:
            State(rec.state)
        except ValueError as e:
            raise RecoveryError(Code.MAPPING_INTEGRITY_FAILURE, "bad state") from e
        return rec

    def insert(self, rec: CorrelationRecord, before_commit: Hook = None) -> CorrelationRecord:
        rec = self._sign(rec)
        with self.db.tx() as c:
            c.execute(
                f"INSERT INTO correlation ({','.join(_COLS)},tag) VALUES ({','.join('?' * (len(_COLS) + 1))})",
                (*[getattr(rec, k) for k in _COLS], rec.tag))
            if before_commit:
                before_commit()
        return rec

    def get(self, tenant: str, upstream_id: str) -> CorrelationRecord | None:
        rows = self.db.query(
            "SELECT * FROM correlation WHERE tenant=? AND upstream_id=?", (tenant, upstream_id))
        return self._row_to_rec(rows[0]) if rows else None

    def cas(self, current: CorrelationRecord, before_commit: Hook = None,
            **changes) -> CorrelationRecord:
        """Apply `changes` iff the stored version still equals current.version."""
        if "state" in changes:
            check_transition(current.state, changes["state"])
        new = self._sign(replace(current, version=current.version + 1, **changes))
        with self.db.tx() as c:
            row = c.execute(
                "SELECT version FROM correlation WHERE tenant=? AND upstream_id=?",
                (current.tenant, current.upstream_id)).fetchone()
            if row is None or row["version"] != current.version:
                raise VersionConflict(f"expected v{current.version}, found {row and row['version']}")
            c.execute(
                f"UPDATE correlation SET {','.join(f'{k}=?' for k in _COLS)}, tag=? "
                "WHERE tenant=? AND upstream_id=?",
                (*[getattr(new, k) for k in _COLS], new.tag, new.tenant, new.upstream_id))
            if before_commit:
                before_commit()
        return new

    def restore_checkpoint(self, snapshot: CorrelationRecord) -> None:
        """Write back a saved record only if it is not older than what is stored."""
        with self.db.tx() as c:
            row = c.execute(
                "SELECT version FROM correlation WHERE tenant=? AND upstream_id=?",
                (snapshot.tenant, snapshot.upstream_id)).fetchone()
            if row is not None and row["version"] > snapshot.version:
                raise RecoveryError(Code.STALE_REFERENCE,
                                    f"checkpoint v{snapshot.version} older than v{row['version']}")
            c.execute("DELETE FROM correlation WHERE tenant=? AND upstream_id=?",
                      (snapshot.tenant, snapshot.upstream_id))
            c.execute(
                f"INSERT INTO correlation ({','.join(_COLS)},tag) VALUES ({','.join('?' * (len(_COLS) + 1))})",
                (*[getattr(snapshot, k) for k in _COLS], snapshot.tag))

    def count(self) -> int:
        return self.db.query("SELECT COUNT(*) AS n FROM correlation")[0]["n"]
