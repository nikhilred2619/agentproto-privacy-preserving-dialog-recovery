"""Render results/*.json into results/summary.md (Markdown tables).

Usage: python experiments/render_results.py [results_dir]
"""

import json
import sys
from pathlib import Path

ORDER = ["global-store", "global-endpoint", "scoped-store", "scoped-endpoint-noticket",
         "scoped-endpoint-ticket", "scoped-endpoint-medticket"]


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main(d: Path) -> None:
    md = ["# Generated result tables", "",
          f"Source: `{d}/*.json`. Regenerate with `python experiments/render_results.py`.", ""]
    env = json.loads((d / "environment_all.json").read_text())
    md += ["## Environment", "", "```json", json.dumps(env, indent=2, sort_keys=True), "```", ""]

    # scenario matrix
    import csv
    rows = list(csv.DictReader((d / "scenario_matrix.csv").open()))
    modes = ORDER + ["scoped-store+UNSAFE_GUESS"]
    scen = sorted({r["scenario"] for r in rows}, key=lambda s: (int(s[1:3]), s))
    cell = {(r["scenario"], r["mode"]): r for r in rows}
    trows = []
    for s in scen:
        line = [s]
        for m in modes:
            r = cell.get((s, m))
            if r is None:
                line.append("")
                continue
            mark = " ✗WRONG" if r["correct_association"] == "False" else ""
            line.append(r["code"] + mark)
        trows.append(line)
    md += ["## Scenario matrix (code returned to Agent A)", "",
           "✗WRONG = resumed a dialog other than the ground-truth one.", "",
           table(["scenario"] + modes, trows), ""]
    md += ["Scenario summary:", "", "```json",
           (d / "scenario_summary.json").read_text(), "```", ""]

    f = json.loads((d / "fault_summary.json").read_text())
    frows = []
    for m in ORDER + ["scoped-store+UNSAFE_GUESS"]:
        x = f[m]
        frows.append([m, x["attempts"], x["recovered"], x["explicit_failures"],
                      x["incorrect_associations"], x["safety_violations"],
                      x["recovery_rate_where_recovery_permitted"]])
    md += ["## Seeded fault schedule", "", f"Config: `{json.dumps(f['_config'])}`", "",
           table(["mode", "attempts", "recovered", "explicit failures", "incorrect assoc.",
                  "safety violations", "recovery rate (faults not in must_fail)"], frows), ""]

    p = json.loads((d / "privacy.json").read_text())
    prows = []
    for m in ORDER:
        x = p[m]
        pairs = x["identifier_join_fraction_by_domain_pair"]
        prows.append([m, ", ".join(x["domains"]), x["max_domains_sharing_one_value"],
                      pairs.get("A~B"), pairs.get("GW1~GW2", "n/a"),
                      x["mean_distinct_A_B_shared_values_per_dialog"],
                      json.dumps(x["gateway_can_link_upstream_downstream"]),
                      x["order_based_A_B_join_accuracy_sequential_workload"]])
    md += ["## Privacy / linkability (T17)", "",
           f"{p[ORDER[0]]['dialogs']} dialogs per mode, each resumed once through G2.", "",
           table(["mode", "domains", "max domains sharing one value", "A~B id join",
                  "GW1~GW2 id join", "distinct A-B shared values / dialog",
                  "gateway links up+down", "order-based A~B join"], prows), ""]

    b = json.loads((d / "benchmark_summary.json").read_text())
    brows = [[r["mode"], r["runs"], r["recovery_samples_per_run"], r["establish_median_ms"],
              r["lookup_median_ms"], r["lookup_p95_ms"], r["recovery_mean_ms"],
              r["recovery_median_ms"], r["recovery_p95_ms"],
              r["concurrent_throughput_per_s_median"], r["gw1_store_rows"],
              r["gw1_store_bytes"], r["baseline"] or "", r["recovery_median_ratio_vs_baseline"] or "",
              r["incorrect_associations_total"], r["non_ok_recoveries_total"]] for r in b]
    md += ["## Performance (median across runs of per-run statistic)", "",
           table(["mode", "runs", "recoveries/run", "establish med ms", "lookup med ms",
                  "lookup p95 ms", "recovery mean ms", "recovery med ms", "recovery p95 ms",
                  "concurrent recoveries/s", "store rows", "store bytes", "baseline",
                  "recovery ratio vs baseline", "incorrect assoc.", "non-OK"], brows), ""]
    raw = json.loads((d / "benchmark_raw.json").read_text())
    md += ["Per-run recovery medians (ms), to show run-to-run spread:", ""]
    md += [f"- {m}: {[x['recovery_after_failover']['median_ms'] for x in raw[m]]}"
           for m in ORDER if m in raw]
    (d / "summary.md").write_text("\n".join(md) + "\n")
    print("wrote", d / "summary.md")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results"))
