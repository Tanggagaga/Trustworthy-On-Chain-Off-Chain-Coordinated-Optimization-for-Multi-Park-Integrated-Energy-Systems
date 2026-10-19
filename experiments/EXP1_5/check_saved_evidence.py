"""Recheck a completed EXP15 run from saved artifacts; never submits transactions."""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

run = Path(sys.argv[1])
status = json.loads((run / "status.json").read_text(encoding="utf-8"))
assert status["status"] == "PASS", status["status"]
result = status["result"]
events = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
assert len(manifest["deployments"]) == 3
package_path = next(Path(p) for p in manifest["hashes"] if p.endswith("network/energy.tar.gz"))
package_hash = manifest["hashes"][str(package_path)]
assert hashlib.sha256(package_path.read_bytes()).hexdigest() == package_hash
expected_package = "energy_2.6:" + package_hash
for org, evidence in manifest["deployments"].items():
    definition = json.loads(evidence["stdout"])
    approved = json.loads(evidence["approved"]["stdout"])
    assert definition["version"] == approved["version"] == "2.6", org
    assert definition["sequence"] == approved["sequence"], org
    assert approved["source"]["Type"]["LocalPackage"]["package_id"] == expected_package, org

def matching_query(function, schedule, iteration, park):
    return [e["result"] for e in events if e["kind"] == "query" and "result" in e
            and len(e["args"]) > 3 and e["args"][2] == function
            and e["args"][3] == [schedule, str(iteration), park]]

solutions = []
verdicts = []
for e in events:
    args = e.get("args", [])
    if e["kind"] == "query" and "result" in e and len(args) > 3:
        if args[2] == "QueryMaskedSolution":
            solutions.append(e)
        if args[2] == "QueryVerdict":
            verdicts.append(e)
assert len(verdicts) == 9, len(verdicts)
assert len(solutions) == 9, len(solutions)
for e in verdicts:
    schedule, iteration, park = e["args"][3]
    v = e["result"]
    sol = matching_query("QueryMaskedSolution", schedule, iteration, park)
    assert sol and v["solutionHash"] == sol[-1]["solutionHash"]
assert sum(e["result"]["pass"] != e["result"]["reportedPass"] for e in verdicts) == 2

schedule = verdicts[0]["args"][3][0]
va = matching_query("QueryVerdict", schedule, 1, "Park1")[-1]
vb = matching_query("QueryVerdict", schedule, 2, "Park1")[-1]
assert (va["pass"], va["reportedPass"]) == (True, False)
assert (vb["pass"], vb["reportedPass"]) == (False, True)
assert result["A"]["iter_record_exists"] and result["A"]["coordinate_step_ok"]
assert not result["B"]["iter_record_exists"] and result["B"]["coordinate_step_blocked"]

# The bad solution must satisfy the registered constraints while failing actual-state KKT.
mask_path = next(Path(p) for p in manifest["hashes"] if p.endswith("mask_registration_park1.json"))
assert hashlib.sha256(mask_path.read_bytes()).hexdigest() == manifest["hashes"][str(mask_path)]
mask = json.loads(mask_path.read_text(encoding="utf-8"))
y = np.asarray(matching_query("QueryMaskedSolution", schedule, 2, "Park1")[-1]["yScaled"], dtype=float) / 1e6
G = np.asarray(mask["gMaskedScaled"], dtype=float) / 1e6
h = np.asarray(mask["hVecScaled"], dtype=float) / 1e6
max_primal_violation = float(np.max(G @ y - h))
assert max_primal_violation <= 0.01, max_primal_violation
actual_check = json.loads((run / "attack_input_check.json").read_text(encoding="utf-8"))
assert actual_check["kkt_pass"] is False and actual_check["stat"] > 10_000
assert vb["stationarityResScaled"] > 10_000

# Match verifier anomalies to this schedule and the attacked iteration.
anomaly_details = {}
for iteration, verifier in [(1, result["A"]["verifier"]), (2, result["B"]["verifier"])]:
    hits = []
    for e in events:
        a = e.get("args", [])
        if e["kind"] == "query" and "result" in e and len(a) > 3 and a[2] == "QueryAuditTrail":
            for item in e["result"] or []:
                if (item.get("scheduleId") == schedule and item.get("parkId") == verifier
                    and item.get("type") == "VERDICT_MISMATCH"
                    and f"iter {iteration} park Park1:" in item.get("detail", "")):
                    hits.append(item)
    assert hits, (iteration, verifier)
    anomaly_details[str(iteration)] = hits[-1]["detail"]

# In the saved event order, the last two ADMM state queries are before and after
# the attempted coordination step in scenario B.
states = [e["result"] for e in events if e["kind"] == "query" and "result" in e
          and len(e.get("args", [])) > 3 and e["args"][2] == "QueryADMMState"
          and e["args"][3] == [schedule]]
assert len(states) >= 2 and states[-2] == states[-1]
assert states[-1]["iter"] == 2 and states[-1]["status"] == "DISPATCHING"

out = {"status": "PASS", "scheduleID": schedule, "endorsed_package": expected_package,
       "normal_verdict_count": 7, "false_report_count": 2,
       "scenario_A": {"reported_pass": False, "onchain_pass": True,
                      "onchain_stationarity": va["stationarityResScaled"],
                      "iteration_record_exists": True, "coordination_advanced": True},
       "scenario_B": {"reported_pass": True, "onchain_pass": False,
                      "onchain_stationarity": vb["stationarityResScaled"],
                      "max_primal_violation": max_primal_violation,
                      "iteration_record_exists": False, "coordination_blocked": True,
                      "state_unchanged": True},
       "anomalies": anomaly_details,
       "scope": "Two application-client false-report scenarios; does not test colluding endorsing peers."}
(run / "evidence_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=2))
