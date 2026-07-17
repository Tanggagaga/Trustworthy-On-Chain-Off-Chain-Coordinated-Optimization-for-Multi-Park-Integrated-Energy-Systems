"""
M4 large-payload smoke test using one real-size M3 KKT case.

Scope:
  This is NOT a full real ADMM experiment. The M3 JSON has H_masked/c_masked/
  G_masked/h_masked/y/mu, but it does not contain the original HE0/C0 split nor
  E_rows/x0_e. Therefore this test uses the real-size KKT matrices and solution
  certificate, while setting E_rows/x0_e to zero so recovered e_m is zero.

What it proves:
  - The Fabric CLI/client path and chaincode can carry real-size mask/KKT
    payloads for one Park1 case.
  - The identity separation, solutionHash binding, verdict recording, and
    on-chain e_m recovery path still work with real dimensions.

What it does not prove:
  - Full real masked ADMM equivalence.
  - Real E_rows/x0_e recovery accuracy.
"""

import json
import math
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fabric_client as fc
from config import CONTRACT_ADMM, CONTRACT_MASK
from preheat_chaincode_peers import preheat_org

MASK_SCALE = 1_000_000
DATA_OWNER = "Park1"
ITER = 0
ALL_PARK_ENDORSERS = ["Park1", "Park2", "Park3"]
M3_CASES = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/M1a离线Mask验证/M3go文件/m3_test_cases.json"
CHUNK_CHARS = 50_000


def to_scaled(value):
    return int(round(float(value) * MASK_SCALE))


def divround(num, den):
    if den == 0:
        raise ZeroDivisionError("den is zero")
    if den < 0:
        num, den = -num, -den
    sign = -1 if num < 0 else 1
    n = abs(num)
    q, r = divmod(n, den)
    if r * 2 >= den:
        q += 1
    return sign * q


def load_first_real_case():
    with open(M3_CASES, encoding="utf-8") as f:
        data = json.load(f)
    for case in data["cases"]:
        if case["name"].startswith("iter") and "park1" in case["name"] and case.get("expected_pass", False):
            return case
    raise RuntimeError("no passing Park1 real-size case found")


def scale_case(case):
    h_mat = [[to_scaled(x) for x in row] for row in case["H_masked"]]
    c_vec = [to_scaled(x) for x in case["c_masked"]]
    g_mat = [[to_scaled(x) for x in row] for row in case["G_masked"]]
    h_vec = [to_scaled(x) for x in case["h_masked"]]
    y_vec = [to_scaled(x) for x in case["y"]]
    mu_vec = [to_scaled(x) for x in case["mu"]]
    return h_mat, c_vec, g_mat, h_vec, y_vec, mu_vec


def kkt_residuals(h_mat, c_vec, g_mat, h_vec, y_vec, mu_vec):
    k0 = len(y_vec)
    qrows = len(mu_vec)
    hy = [sum(divround(h_mat[i][j] * y_vec[j], MASK_SCALE) for j in range(k0)) for i in range(k0)]
    gtmu = [sum(divround(g_mat[r][i] * mu_vec[r], MASK_SCALE) for r in range(qrows)) for i in range(k0)]
    stat = [hy[i] + c_vec[i] + gtmu[i] for i in range(k0)]
    stat_res = int(round(math.sqrt(sum(s * s for s in stat))))
    slack = [sum(divround(g_mat[r][j] * y_vec[j], MASK_SCALE) for j in range(k0)) - h_vec[r] for r in range(qrows)]
    compl = [abs(divround(mu_vec[r] * slack[r], MASK_SCALE)) for r in range(qrows)]
    compl_res = max(compl) if compl else 0
    primal_ok = all(s <= int(0.01 * MASK_SCALE) for s in slack)
    dual_ok = all(m >= -int(0.01 * MASK_SCALE) for m in mu_vec)
    pass_ok = primal_ok and dual_ok and stat_res <= int(0.01 * MASK_SCALE) and compl_res <= int(0.01 * MASK_SCALE)
    return pass_ok, stat_res, compl_res


def invoke(label, **kwargs):
    print(f"  -> {label}")
    return fc.invoke(**kwargs)


def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def upload_large_mask(schedule_id, k0, qrows, h_mat, c_vec, g_mat, h_vec, e_rows, x0e):
    invoke(
        "BeginMaskedSubproblemUpload",
        caller_org=DATA_OWNER,
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK,
        function="BeginMaskedSubproblemUpload",
        args=[schedule_id, DATA_OWNER, str(k0), str(qrows), str(MASK_SCALE)],
        wait_seconds=3.0,
    )
    parts = {
        "hE0": json.dumps(h_mat, separators=(",", ":")),
        "gMasked": json.dumps(g_mat, separators=(",", ":")),
        "hVec": json.dumps(h_vec, separators=(",", ":")),
        "c0": json.dumps(c_vec, separators=(",", ":")),
        "eRows": json.dumps(e_rows, separators=(",", ":")),
        "x0e": json.dumps(x0e, separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        payload_chunks = chunks(payload)
        print(f"  -> Upload {part_name}: {len(payload)} chars in {len(payload_chunks)} chunks")
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks, part_name=part_name: fc.invoke(
                    caller_org=DATA_OWNER,
                    endorsing_orgs=ALL_PARK_ENDORSERS,
                    contract=CONTRACT_MASK,
                    function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, DATA_OWNER, part_name, str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4,
                delay_seconds=1.5,
            )
        time.sleep(1.0)
    print("  -> FinalizeMaskedSubproblemUpload")
    fc.retry(
        lambda: fc.invoke(
            caller_org=DATA_OWNER,
            endorsing_orgs=ALL_PARK_ENDORSERS,
            contract=CONTRACT_MASK,
            function="FinalizeMaskedSubproblemUpload",
            args=[schedule_id, DATA_OWNER],
            wait_seconds=2.0,
        ),
        attempts=6,
        delay_seconds=2.0,
    )


def main():
    toy_date = "9999-M4PAYLOAD-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    schedule_id = "SCHEDULE_" + toy_date

    print("=" * 72)
    print(f"M4 real-size payload smoke test  scheduleID={schedule_id}")
    print("=" * 72)
    print("Scope: real H/G/h/c/y/mu dimensions; zero E_rows/x0_e recovery placeholder.")

    for park in ["Park1", "Park2", "Park3"]:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            return 1

    case = load_first_real_case()
    h_mat, c_vec, g_mat, h_vec, y_vec, mu_vec = scale_case(case)
    k0 = len(y_vec)
    qrows = len(mu_vec)
    e_rows = [[0 for _ in range(k0)] for _ in range(96)]
    x0e = [0 for _ in range(96)]
    pass_ok, stat_res, compl_res = kkt_residuals(h_mat, c_vec, g_mat, h_vec, y_vec, mu_vec)

    print(f"Loaded case: {case['name']}  k0={k0} qRows={qrows}")
    print(f"Local fixed KKT check: pass={pass_ok} stat={stat_res} compl={compl_res}")
    if not pass_ok:
        print("FAIL: selected real-size certificate does not pass local fixed KKT check")
        return 2

    invoke(
        "PublishNetworkParams",
        caller_org="Observer",
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM,
        function="PublishNetworkParams",
        args=[toy_date, json.dumps([99000, 99000, 99000]), json.dumps([99000, 99000, 99000]),
              json.dumps([99000, 99000]), json.dumps([99000, 99000, 99000])],
        wait_seconds=2.0,
    )
    invoke(
        "TriggerDispatch",
        caller_org="Park1",
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM,
        function="TriggerDispatch",
        args=[toy_date, "10000", "10000", "10000", "100", "4500", json.dumps([430000, 530000, 240000])],
        wait_seconds=2.0,
    )
    upload_large_mask(schedule_id, k0, qrows, h_mat, c_vec, g_mat, h_vec, e_rows, x0e)
    invoke(
        "AssignSolverAndVerifier",
        caller_org="Park1",
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK,
        function="AssignSolverAndVerifier",
        args=[schedule_id, str(ITER), DATA_OWNER],
        wait_seconds=2.0,
    )
    assignment = fc.query_with_retry("Park1", CONTRACT_MASK, "QuerySolverAssignment", [schedule_id, str(ITER), DATA_OWNER])
    solver = assignment["solverParkId"]
    verifier = assignment["verifierParkId"]
    print(f"Assignment: owner={DATA_OWNER} solver={solver} verifier={verifier}")
    invoke(
        "SubmitMaskedSolution(real-size y/mu)",
        caller_org=solver,
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK,
        function="SubmitMaskedSolution",
        args=[schedule_id, str(ITER), DATA_OWNER, json.dumps(y_vec), json.dumps(mu_vec)],
        wait_seconds=2.0,
    )
    msol = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution", [schedule_id, str(ITER), DATA_OWNER])
    invoke(
        "RecordVerdict(pass=true)",
        caller_org=verifier,
        endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK,
        function="RecordVerdict",
        args=[schedule_id, str(ITER), DATA_OWNER, "true", str(stat_res), str(compl_res), msol["solutionHash"]],
        wait_seconds=2.0,
    )
    rec = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord", [schedule_id, str(ITER), DATA_OWNER])
    all_zero = all(v == 0 for v in rec["eExScaled"] + rec["gExScaled"] + rec["hExScaled"] + rec["cExScaled"])
    print("Recovered e_m all zero:", all_zero)
    print("PASS: real-size payload path completed" if all_zero else "FAIL: recovered e_m mismatch")
    return 0 if all_zero else 3


if __name__ == "__main__":
    raise SystemExit(main())
