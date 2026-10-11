"""Structured JSON diagnostic events (FR-15) and the observer instrumentation
used by the privacy evaluation.

Two separate things live here:

* EventEmitter writes operational logs through the standard `logging`
  module. Identifier values are always passed through a per-domain keyed
  Redactor; raw identifiers never reach a log record.

* ObservationLog is *evaluation instrumentation only*. It records which raw
  identifier values each administrative domain received over the wire, so
  the harness can compute linkability metrics. A real deployment would not
  have it; it models what an honest-but-curious domain could retain.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from .security import Redactor

LOGGER_NAME = "agentproto_recovery"
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
logging.getLogger(LOGGER_NAME).propagate = True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "domain": getattr(record, "domain", None),
            "event": getattr(record, "event", record.getMessage()),
        }
        body.update(getattr(record, "fields", {}) or {})
        return json.dumps(body, sort_keys=True)


def configure_json_logging(level: int = logging.INFO, stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger(LOGGER_NAME)
    log.addHandler(handler)
    log.setLevel(level)
    return handler


class EventEmitter:
    def __init__(self, component: str, domain: str, redactor: Redactor) -> None:
        self.component = component
        self.domain = domain
        self._redact = redactor
        self._log = logging.getLogger(f"{LOGGER_NAME}.{component}")

    def emit(self, event: str, ids: dict[str, str | None] | None = None,
             level: int = logging.INFO, **fields: Any) -> None:
        out: dict[str, Any] = dict(fields)
        for k, v in (ids or {}).items():
            out[k] = self._redact(v)
        self._log.log(level, event, extra={
            "component": self.component, "domain": self.domain,
            "event": event, "fields": out})


@dataclass
class Observation:
    seq: int
    domain: str
    kind: str
    value: str


@dataclass
class ObservationLog:
    entries: list[Observation] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def saw(self, domain: str, kind: str, value: str | None) -> None:
        if not value:
            return
        with self._lock:
            self.entries.append(Observation(len(self.entries), domain, kind, value))

    def values_by_domain(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for e in self.entries:
            out.setdefault(e.domain, set()).add(e.value)
        return out
