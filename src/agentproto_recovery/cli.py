"""Command-line entry point for experiments.

  agentproto-recovery scenarios  - T01-T20 scenario matrix across modes
  agentproto-recovery faults     - seeded fault schedule, safety counts
  agentproto-recovery privacy    - identifier exposure / linkability (T17)
  agentproto-recovery benchmark  - latency, store size, throughput
  agentproto-recovery all        - everything above

Correctness tests live in pytest; this CLI produces the evidence files.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from .errors import Code
from .evaluation import (MODES, SCENARIOS, Deployment, analyze_privacy, benchmark_mode,
                         dump_json, privacy_workload, run_scenario)
from .security import new_identifier

FAULTS = ["none", "mapping_deleted", "mapping_tampered", "ticket_lost", "revoked",
          "expired", "terminated", "stale_replay", "wrong_principal"]
MUST_FAIL = {"revoked", "expired", "terminated", "stale_replay", "wrong_principal"}


def environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0], "platform": platform.platform(),
        "machine": platform.machine(), "cpu_count": os.cpu_count(),
        "sqlite": sqlite3.sqlite_version,
        "cryptography": metadata.version("cryptography"),
        "agentproto_recovery": metadata.version("agentproto-recovery"),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = list(rows[0])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
                        for k, v in r.items()})


# --------------------------------------------------------------------------

def cmd_scenarios(out: Path, repeats: int) -> dict[str, Any]:
    rows, signatures = [], {}
    cells = [(s, m, False) for s in SCENARIOS for m in MODES]
    cells += [(s, "scoped-store", True) for s in ("T05", "T06")]  # negative control
    for s, m, unsafe in cells:
        sigs = set()
        for r in range(repeats):
            o = run_scenario(s, m, unsafe_guess=unsafe)
            sigs.add((o.code, o.correct_association, json.dumps(o.extra, sort_keys=True)))
            if r == 0:
                rows.append({"scenario": o.scenario, "mode": o.mode, "code": o.code,
                             "recovered": o.recovered,
                             "correct_association": o.correct_association,
                             "explicit_failure": o.explicit_failure, "extra": o.extra})
        signatures[f"{s}/{m}{'+UNSAFE' if unsafe else ''}"] = len(sigs)
    write_csv(out / "scenario_matrix.csv", rows)
    nondet = {k: v for k, v in signatures.items() if v != 1}
    summary = {"repeats_per_cell": repeats, "cells": len(cells),
               "cells_with_nondeterministic_outcome": nondet,
               "incorrect_associations": sum(1 for r in rows if r["correct_association"] is False),
               "incorrect_associations_excluding_negative_control":
                   sum(1 for r in rows if r["correct_association"] is False
                       and "UNSAFE" not in r["mode"])}
    dump_json(out / "scenario_summary.json", summary)
    return summary


# --------------------------------------------------------------------------

def _fault_run(mode: str, n: int, seed: int, unsafe: bool) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    schedule = [rng.choice(FAULTS) for _ in range(n)]
    rows = []
    with Deployment(mode, unsafe_guess=unsafe) as dep:
        users = [dep.agent_a(f"user{k}") for k in range(5)]
        mallory = dep.agent_a("mallory")
        dialogs = []
        for i, fault in enumerate(schedule):
            if fault in ("revoked", "expired"):
                a = dep.agent_a(f"solo{i}", ttl=60 if fault == "expired" else 3600)
            else:
                a = users[i % len(users)]
            _, d = dep.establish(a, f"job-{i % 7}")  # deliberately repetitive task text
            dialogs.append((i, fault, a, d))
        for i, fault, a, d in dialogs:
            if fault == "stale_replay":
                _, _, t0 = a.state(d)
                a.resume(dep.g1, d)
                a.resume(dep.g1, d)
                a._force_local(d, 0, t0)
            elif fault == "terminated":
                _, e, t = a.state(d)
                a.terminate(dep.g1, d)
                a._force_local(d, e, t)
        dep.crash_g1()
        dep.clock.advance(120)
        for i, fault, a, d in dialogs:
            if fault == "mapping_deleted":
                dep.raw_sql(dep.g1, "DELETE FROM correlation WHERE upstream_id=?", (d,))
            elif fault == "mapping_tampered":
                other = dialogs[(i + 1) % n][3]
                dep.raw_sql(dep.g1, "UPDATE correlation SET downstream_id=? WHERE upstream_id=?",
                            (dep.truth[other], d))
            elif fault == "ticket_lost":
                _, e, _ = a.state(d)
                a._force_local(d, e, None)
            elif fault == "revoked":
                dep.authz.revoke(a.ctx.principal, a.ctx.tenant)
        for i, fault, a, d in dialogs:
            if fault == "wrong_principal":
                _, e, t = a.state(d)
                r = dep.g2.resume(mallory.ctx, d, e, new_identifier("rq"), t)
            else:
                r = a.resume(dep.g2, d)
            correct = dep.resumed_correctly(d) if r.ok else None
            rows.append({"mode": dep.mode.name + ("+UNSAFE_GUESS" if unsafe else ""),
                         "seed": seed, "dialog": i, "fault": fault, "code": r.code.value,
                         "recovered": r.ok, "correct_association": correct,
                         "safety_violation": (r.ok and correct is False)
                         or (r.ok and fault in MUST_FAIL)})
    return rows


def cmd_faults(out: Path, n: int, seeds: list[int]) -> dict[str, Any]:
    rows = []
    for seed in seeds:
        for m in MODES:
            rows += _fault_run(m, n, seed, False)
        rows += _fault_run("scoped-store", n, seed, True)
    write_csv(out / "fault_schedule.csv", rows)
    summary: dict[str, Any] = {}
    for m in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == m]
        legit = [r for r in rs if r["fault"] not in MUST_FAIL]
        per_fault = {}
        for f in FAULTS:
            fr = [r for r in rs if r["fault"] == f]
            codes: dict[str, int] = {}
            for r in fr:
                codes[r["code"]] = codes.get(r["code"], 0) + 1
            per_fault[f] = {"n": len(fr), "codes": codes}
        summary[m] = {
            "attempts": len(rs),
            "recovered": sum(r["recovered"] for r in rs),
            "explicit_failures": sum(not r["recovered"] for r in rs),
            "incorrect_associations": sum(r["correct_association"] is False for r in rs),
            "safety_violations": sum(bool(r["safety_violation"]) for r in rs),
            "recovery_rate_where_recovery_permitted":
                round(sum(r["recovered"] for r in legit) / len(legit), 4) if legit else None,
            "by_fault": per_fault,
        }
    summary["_config"] = {"dialogs_per_run": n, "seeds": seeds, "faults": FAULTS,
                          "must_fail": sorted(MUST_FAIL)}
    dump_json(out / "fault_summary.json", summary)
    return summary


# --------------------------------------------------------------------------

def cmd_privacy(out: Path, n: int) -> dict[str, Any]:
    res = {}
    for m in MODES:
        with Deployment(m) as dep:
            res[m] = analyze_privacy(dep, privacy_workload(dep, n))
    dump_json(out / "privacy.json", res)
    rows = [{"mode": m, **{k: v for k, v in r.items() if k not in ("mode",)}}
            for m, r in res.items()]
    write_csv(out / "privacy.csv", rows)
    return res


# --------------------------------------------------------------------------

def cmd_benchmark(out: Path, n: int, threads: int, runs: int) -> dict[str, Any]:
    raw: dict[str, list[dict[str, Any]]] = {m: [] for m in MODES if m != "scoped-endpoint-noticket"}
    for m in raw:  # discarded warm-up (imports, page cache, first fsyncs)
        with tempfile.TemporaryDirectory(prefix="warm-") as td:
            benchmark_mode(m, 20, threads, Path(td))
    for r in range(runs):
        for m in raw:  # interleave modes across runs to spread drift
            with tempfile.TemporaryDirectory(prefix="bench-") as td:
                raw[m].append(benchmark_mode(m, n, threads, Path(td)))
    dump_json(out / "benchmark_raw.json", raw)
    summary_rows = []
    for m, rs in raw.items():
        def med(path: tuple[str, str]) -> float:
            return round(statistics.median(x[path[0]][path[1]] for x in rs), 4)
        summary_rows.append({
            "mode": m, "runs": len(rs), "dialogs_per_run": n, "threads": threads,
            "establish_median_ms": med(("establish", "median_ms")),
            "establish_p95_ms": med(("establish", "p95_ms")),
            "lookup_median_ms": med(("mapping_lookup", "median_ms")),
            "lookup_p95_ms": med(("mapping_lookup", "p95_ms")),
            "recovery_mean_ms": med(("recovery_after_failover", "mean_ms")),
            "recovery_median_ms": med(("recovery_after_failover", "median_ms")),
            "recovery_p95_ms": med(("recovery_after_failover", "p95_ms")),
            "recovery_samples_per_run": rs[0]["recovery_after_failover"]["n"],
            "concurrent_throughput_per_s_median":
                round(statistics.median(x["concurrent_throughput_per_s"] for x in rs), 2),
            "incorrect_associations_total": sum(x["incorrect_associations"] for x in rs),
            "non_ok_recoveries_total": sum(sum(v for k, v in x["recovery_codes"].items()
                                               if k != Code.OK.value)
                                           + sum(v for k, v in x["concurrent_codes"].items()
                                                 if k != Code.OK.value) for x in rs),
            "gw1_store_bytes": rs[0]["gw1_store_bytes"], "gw1_store_rows": rs[0]["gw1_store_rows"],
        })
    base = {r["mode"]: r for r in summary_rows}
    # like-for-like baselines: store-based vs store-based, endpoint vs endpoint
    for r in summary_rows:
        ref_name = {"scoped-store": "global-store", "scoped-endpoint-ticket": "global-endpoint",
                    "scoped-endpoint-medticket": "global-endpoint"}.get(r["mode"])
        r["baseline"] = ref_name
        r["recovery_median_ratio_vs_baseline"] = (
            round(r["recovery_median_ms"] / base[ref_name]["recovery_median_ms"], 3)
            if ref_name else None)
        r["establish_median_ratio_vs_baseline"] = (
            round(r["establish_median_ms"] / base[ref_name]["establish_median_ms"], 3)
            if ref_name else None)
    write_csv(out / "benchmark_summary.csv", summary_rows)
    dump_json(out / "benchmark_summary.json", summary_rows)
    return {"summary": summary_rows}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentproto-recovery")
    p.add_argument("command", choices=["scenarios", "faults", "privacy", "benchmark", "all"])
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--repeats", type=int, default=3, help="scenario repeats per cell")
    p.add_argument("--fault-dialogs", type=int, default=90)
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--privacy-dialogs", type=int, default=50)
    p.add_argument("--bench-dialogs", type=int, default=400)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--runs", type=int, default=5)
    a = p.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    env = environment()
    env["args"] = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()}
    started = time.perf_counter()
    done: dict[str, Any] = {}
    if a.command in ("scenarios", "all"):
        done["scenarios"] = cmd_scenarios(a.out, a.repeats)
        print("scenarios:", json.dumps(done["scenarios"]))
    if a.command in ("faults", "all"):
        s = cmd_faults(a.out, a.fault_dialogs, a.seeds)
        done["faults"] = {m: {k: v for k, v in x.items() if k != "by_fault"}
                          for m, x in s.items() if not m.startswith("_")}
        print("faults:", json.dumps(done["faults"], indent=1))
    if a.command in ("privacy", "all"):
        cmd_privacy(a.out, a.privacy_dialogs)
        print("privacy: written", a.out / "privacy.json")
    if a.command in ("benchmark", "all"):
        b = cmd_benchmark(a.out, a.bench_dialogs, a.threads, a.runs)
        for r in b["summary"]:
            print("bench:", json.dumps(r))
    env["wall_seconds"] = round(time.perf_counter() - started, 2)
    dump_json(a.out / f"environment_{a.command}.json", env)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
