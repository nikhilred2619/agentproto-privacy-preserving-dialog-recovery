"""Multi-process harness for the Agent A restart scenario (test support code).

Three kinds of genuinely separate OS processes, started with subprocess and a
fresh interpreter (no fork of the pytest process):

  server  Downstream side: gateways G1/G2 with their correlation stores,
          Agent B, the external tool and the mock authorization service.
          It outlives every Agent A process, so an operation that an Agent A
          process started keeps executing after that process is killed.
  agent   One Agent A process. It opens Agent A's durable SQLite ledger and
          executes JSON commands read from stdin, one JSON reply per line.
  pytest  The controller. It starts and SIGKILLs processes and reads all
          SQLite files read-only.

Synchronisation is event-based, never sleep-based: the server can hold a call
at two gates ("pre_b" just before Agent B receives the request, "tool" inside
the external tool call, after B has recorded PENDING) and reports when a call
has reached a gate and when a gateway call has completed.

Gates, gateway-down switches and the store-unavailable switch are test
instrumentation installed by this file; none of it is in the package.

Usage (normally via the Cluster class below):
  python restart_harness.py server <workdir> <mode> <socket>
  python restart_harness.py agent <ledger_dir> <socket> <id_scheme> <principal> <tenant> <name>
Environment: APR_AUTHKEY (hex), PYTHONPATH pointing at the package under test.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).resolve()
TENANT = "tenant-x"
PRINCIPALS = ("alice", "mallory")
WAIT = 60.0  # upper bound for event waits; a timeout is a test failure


def _authkey() -> bytes:
    return bytes.fromhex(os.environ["APR_AUTHKEY"])


def _jsonable(v: Any) -> Any:
    """OpResult / ResumeResult / Code / tuples -> plain JSON values."""
    if hasattr(v, "code") and hasattr(v, "detail"):
        out = {"code": v.code.value, "detail": v.detail}
        for k in ("status", "result", "epoch"):
            if hasattr(v, k):
                out[k] = getattr(v, k)
        return out
    if hasattr(v, "value") and type(v).__name__ == "Code":
        return v.value
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


# =============================================================================
# server process
# =============================================================================

class Gate:
    """One-shot gate: the next call through it blocks until released."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def arm(self) -> None:
        with self._lock:
            self.armed = True
            self.entered.clear()
            self.release.clear()

    def passage(self) -> None:
        with self._lock:
            if not self.armed:
                return
            self.armed = False
        self.entered.set()
        self.release.wait(WAIT * 4)


def serve(workdir: str, mode: str, address: str) -> None:
    from agentproto_recovery.evaluation import ACTIONS, Deployment

    dep = Deployment(mode, Path(workdir))
    for p in PRINCIPALS:
        dep.authz.grant(p, TENANT, ACTIONS, ttl=10**9)
    gates = {"tool": Gate(), "pre_b": Gate()}

    real_execute = dep.b.tool.execute

    def gated_execute(op_key: str, payload: str) -> str:
        gates["tool"].passage()
        return real_execute(op_key, payload)

    dep.b.tool.execute = gated_execute  # type: ignore[method-assign]

    real_b_invoke = dep.b.invoke

    def gated_b_invoke(*args: Any, **kwargs: Any) -> Any:
        gates["pre_b"].passage()
        return real_b_invoke(*args, **kwargs)

    dep.b.invoke = gated_b_invoke  # type: ignore[method-assign]

    cond = threading.Condition()
    calls: list[dict] = []   # every gateway call received
    done: list[dict] = []    # every gateway call completed (reply may be lost)

    def dispatch(msg: dict) -> dict:
        if msg["kind"] == "gw":
            entry = {"pid": msg["pid"], "gw": msg["gw"], "method": msg["method"]}
            with cond:
                calls.append(entry)
            value = getattr(dep.gateways[msg["gw"]], msg["method"])(*msg["args"], **msg["kwargs"])
            with cond:
                done.append({**entry, "reply": _jsonable(value)})
                cond.notify_all()
            return {"value": value}
        op = msg["op"]
        if op == "arm":
            gates[msg["gate"]].arm()
            return {"value": True}
        if op == "wait_entered":
            return {"value": gates[msg["gate"]].entered.wait(WAIT)}
        if op == "release":
            gates[msg["gate"]].release.set()
            return {"value": True}
        if op == "wait_done":
            def enough() -> bool:
                return sum(1 for d in done if d["method"] == msg["method"]
                           and (msg.get("pid") is None or d["pid"] == msg["pid"])) >= msg["n"]
            with cond:
                return {"value": cond.wait_for(enough, WAIT)}
        if op == "stats":
            with cond:
                return {"value": {"calls": list(calls), "done": list(done)}}
        if op == "set_alive":
            dep.gateways[msg["gw"]].alive = msg["alive"]
            return {"value": True}
        if op == "store_b_unavailable":
            if msg["on"]:
                dep.injector.arm("store.b.unavailable", "active", None)
            else:
                dep.injector.disarm("store.b.unavailable")
            return {"value": True}
        if op == "restart_b":
            # SIMULATED Agent B restart, inside this server process: AgentB
            # gets a new incarnation id, nothing else restarts. The executor
            # thread of the old incarnation keeps running (held at the tool
            # gate). Only Agent A restarts are real OS-process restarts.
            dep.b.restart()
            return {"value": dep.b.incarnation}
        if op == "shutdown":
            for g in gates.values():
                g.release.set()
            stop.set()
            return {"value": True}
        raise ValueError(op)

    stop = threading.Event()
    # Permit a bounded burst of simultaneous control/agent IPC clients after restart.
    # Listener defaults to backlog=1; increase queue depth for concurrent arrivals.
    listener = Listener(address, family="AF_UNIX", authkey=_authkey(), backlog=16)

    def handle(conn: Any) -> None:
        try:
            while True:
                try:
                    msg = conn.recv()
                except (EOFError, OSError):
                    return
                try:
                    reply = dispatch(msg)
                except Exception as e:  # reported to the caller, never swallowed
                    reply = {"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()}
                try:
                    conn.send(reply)
                except (OSError, EOFError):
                    return  # the caller is gone (e.g. SIGKILLed Agent A process)
                if stop.is_set():
                    return
        finally:
            conn.close()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn = listener.accept()
            except (OSError, EOFError):
                if stop.is_set():
                    return
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    print("READY", os.getpid(), flush=True)
    stop.wait()
    try:
        listener.close()
    except OSError:
        pass
    dep.close()


# =============================================================================
# agent process
# =============================================================================

class RemoteGateway:
    """Gateway proxy for an Agent A process: one connection per call, so it
    is safe to use from several threads. Optionally holds the reply of the
    next call to one method (to deliver it late)."""

    def __init__(self, address: str, name: str) -> None:
        self._address = address
        self._name = name
        self._hold: dict | None = None

    def hold_next(self, method: str) -> dict:
        self._hold = {"method": method, "used": False,
                      "held": threading.Event(), "release": threading.Event()}
        return self._hold

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with Client(self._address, family="AF_UNIX", authkey=_authkey()) as c:
            c.send({"kind": "gw", "gw": self._name, "method": method, "args": args,
                    "kwargs": kwargs, "pid": os.getpid()})
            rep = c.recv()
        if "error" in rep:
            raise RuntimeError(rep["error"] + "\n" + rep.get("tb", ""))
        h = self._hold
        if h is not None and h["method"] == method and not h["used"]:
            h["used"] = True
            h["held"].set()
            h["release"].wait(WAIT * 4)
        return rep["value"]

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)
        return lambda *a, **kw: self._call(method, *a, **kw)


def agent_main(ledger_dir: str, address: str, id_scheme: str, principal: str, tenant: str,
               name: str) -> None:
    from agentproto_recovery.agents import AgentA
    from agentproto_recovery.diagnostics import ObservationLog

    a = AgentA(Path(ledger_dir), ObservationLog(), id_scheme, principal, tenant, name=name)
    gws = {n: RemoteGateway(address, n) for n in ("g1", "g2")}
    bg: dict[str, Any] = {}

    def run(cmd: dict) -> Any:
        c = cmd["cmd"]
        gw = gws.get(cmd.get("gw", "g1"))
        if c == "hello":
            return {"pid": os.getpid(), "incarnation": getattr(a, "incarnation", None)}
        if c == "establish":
            code, d = a.establish(gw, cmd.get("task", "task"))
            return {"code": code.value, "dialog": d}
        if c == "invoke":
            return _jsonable(a.invoke(gw, cmd["dialog"], cmd["payload"], seq=cmd.get("seq")))
        if c == "resume":
            return _jsonable(a.resume(gw, cmd["dialog"]))
        if c == "settle":
            return _jsonable(a.settle_unknown_ops(gw, cmd["dialog"]))
        # -- commands below use the v0.2.2 recovery API -------------------
        if c == "recover":
            return _jsonable(a.recover_inflight())
        if c == "unresolved":
            return _jsonable(a.unresolved_ops(cmd.get("dialog")))
        if c == "reconcile":
            return _jsonable(a.reconcile_ops(gw, cmd["dialog"]))
        if c == "retry":
            return _jsonable(a.retry_op(gw, cmd["dialog"], cmd["seq"]))
        if c == "reconcile_async_hold":
            hold = gw.hold_next("resolve_op")
            out: dict = {}
            t = threading.Thread(target=lambda: out.setdefault(
                "r", a.reconcile_ops(gw, cmd["dialog"])), daemon=True)
            bg.update(hold=hold, thread=t, out=out)
            t.start()
            return True
        if c == "wait_held":
            return bg["hold"]["held"].wait(WAIT)
        if c == "release_held":
            bg["hold"]["release"].set()
            bg["thread"].join(WAIT)
            return _jsonable(bg["out"].get("r"))
        raise ValueError(c)

    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["cmd"] == "exit":
            break
        try:
            reply: Any = {"ok": run(cmd)}
        except Exception as e:
            reply = {"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


# =============================================================================
# controller side (used by pytest)
# =============================================================================

def _env(authkey_hex: str) -> dict:
    import agentproto_recovery
    src = str(Path(agentproto_recovery.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["APR_AUTHKEY"] = authkey_hex
    env["PYTHONUNBUFFERED"] = "1"
    return env


def read_rows(db: Path, sql: str, args: tuple = ()) -> list[dict]:
    """Read-only inspection of a SQLite file owned by another process."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


class AgentProc:
    def __init__(self, cluster: Cluster, label: str, principal: str = "alice") -> None:
        self.label = label
        self.name = f"{TENANT}-{principal}"
        self._err = open(cluster.base / f"agent_{label}.stderr", "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(HARNESS), "agent", str(cluster.ledger), cluster.address,
             cluster.id_scheme, principal, TENANT, self.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err, text=True,
            env=cluster.env)
        hello = self.call("hello")
        self.pid = hello["pid"]
        self.incarnation = hello["incarnation"]
        assert self.pid == self.proc.pid

    def send(self, cmd: str, **kw: Any) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
        self.proc.stdin.flush()

    def recv(self) -> Any:
        assert self.proc.stdout is not None
        r, _, _ = select.select([self.proc.stdout], [], [], WAIT)
        if not r:
            raise TimeoutError(f"{self.label}: no reply within {WAIT}s")
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"{self.label} exited (rc={self.proc.poll()})")
        rep = json.loads(line)
        if "error" in rep:
            raise RuntimeError(f"{self.label}: {rep['error']}\n{rep.get('tb', '')}")
        return rep["ok"]

    def call(self, cmd: str, **kw: Any) -> Any:
        self.send(cmd, **kw)
        return self.recv()

    def sigkill(self) -> int:
        os.kill(self.proc.pid, signal.SIGKILL)
        rc = self.proc.wait(WAIT)
        self._err.close()
        return rc

    def exit(self) -> int:
        if self.proc.poll() is None:
            self.send("exit")
            rc = self.proc.wait(WAIT)
        else:
            rc = self.proc.returncode
        self._err.close()
        return rc


class Cluster:
    def __init__(self, base: Path, mode: str) -> None:
        from agentproto_recovery.evaluation import MODES

        self.base = base
        self.mode = mode
        self.id_scheme = MODES[mode].id_scheme
        self.downstream = base / "downstream"
        self.ledger = base / "agent_a_ledger"
        self.downstream.mkdir(parents=True)
        self.ledger.mkdir(parents=True)
        self._sockdir = tempfile.mkdtemp(prefix="apr-", dir="/tmp")
        self.address = os.path.join(self._sockdir, "s")
        self.env = _env(os.urandom(16).hex())
        self._err = open(base / "server.stderr", "w")
        self.server = subprocess.Popen(
            [sys.executable, str(HARNESS), "server", str(self.downstream), mode, self.address],
            stdout=subprocess.PIPE, stderr=self._err, text=True, env=self.env)
        assert self.server.stdout is not None
        r, _, _ = select.select([self.server.stdout], [], [], WAIT)
        line = self.server.stdout.readline() if r else ""
        assert line.startswith("READY"), (line, (base / "server.stderr").read_text())
        self.server_pid = int(line.split()[1])
        self.agents: list[AgentProc] = []

    # -- control ---------------------------------------------------------
    def ctl(self, op: str, **kw: Any) -> Any:
        with Client(self.address, family="AF_UNIX",
                    authkey=bytes.fromhex(self.env["APR_AUTHKEY"])) as c:
            c.send({"kind": "ctl", "op": op, **kw})
            rep = c.recv()
        if "error" in rep:
            raise RuntimeError(rep["error"] + rep.get("tb", ""))
        return rep["value"]

    def agent(self, label: str, principal: str = "alice") -> AgentProc:
        p = AgentProc(self, label, principal)
        self.agents.append(p)
        return p

    def calls(self, method: str | None = None, pid: int | None = None) -> list[dict]:
        return [c for c in self.ctl("stats")["calls"]
                if (method is None or c["method"] == method) and (pid is None or c["pid"] == pid)]

    def done(self, method: str | None = None) -> list[dict]:
        return [d for d in self.ctl("stats")["done"] if method is None or d["method"] == method]

    # -- read-only inspection ------------------------------------------------
    def a_ops(self, name: str = f"{TENANT}-alice") -> list[dict]:
        return read_rows(self.ledger / f"domain_a_{name}.sqlite",
                         "SELECT * FROM ops ORDER BY dialog_id, seq")

    def a_dialogs(self, name: str = f"{TENANT}-alice") -> list[dict]:
        return read_rows(self.ledger / f"domain_a_{name}.sqlite",
                         "SELECT * FROM dialogs ORDER BY dialog_id")

    def b_ops(self) -> list[dict]:
        return read_rows(self.downstream / "domain_b.sqlite",
                         "SELECT * FROM ops ORDER BY dialog_id, seq")

    def b_table(self, table: str) -> list[dict]:
        return read_rows(self.downstream / "domain_b.sqlite", f"SELECT * FROM {table}")

    def executions(self) -> list[dict]:
        return read_rows(self.downstream / "domain_b_tool.sqlite",
                         "SELECT * FROM executions ORDER BY n")

    def snapshot(self) -> dict:
        out = {"a_ops": self.a_ops(), "a_dialogs": self.a_dialogs(), "b_ops": self.b_ops(),
               "executions": self.executions()}
        try:
            out["b_fences"] = self.b_table("op_fences")
        except sqlite3.OperationalError:
            pass
        return out

    def close(self) -> None:
        for p in self.agents:
            if p.proc.poll() is None:
                p.proc.kill()
                p.proc.wait(WAIT)
        if self.server.poll() is None:
            try:
                self.ctl("shutdown")
            except (OSError, EOFError, RuntimeError):
                pass
            try:
                self.server.wait(WAIT)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(WAIT)
        self._err.close()
        shutil.rmtree(self._sockdir, ignore_errors=True)


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "server":
        serve(*sys.argv[2:5])
    elif kind == "agent":
        agent_main(*sys.argv[2:8])
    else:
        raise SystemExit(f"unknown role {kind}")
