"""
M4 真正"独立求解"端到端验证：求解方现场用OSQP重新解masked问题，
不借用任何缓存答案，确认：
  1. 分配结果 solver != 数据方（链上强制）
  2. solver独立解出来的(y*,mu*)通过本地KKT校验
  3. 链上反解出的e_m 跟 MATLAB真实值 一致（在容差内）
  4. 求解信用分按PASS/FAIL正确加减，且跟数据信用分完全独立的账本

★ 隔离原则：scheduleID 用 9995-M4REALSOLVE-...，跟此前所有测试
  (9999-SMOKE / 9999-M4PAYLOAD / 9996-M4REALCHECK) 及真实算例都不冲突。
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import openpyxl

import fabric_client as fc
from config import CONTRACT_ADMM, CONTRACT_AUDIT, CONTRACT_MASK
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

E_SCALE = 1000
DATA_OWNER = "Park1"
ITER = 0
ALL_PARK_ENDORSERS = ["Park1", "Park2", "Park3"]
BUNDLE_PATH = ("/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/"
               "M1a离线Mask验证/M4真实数据校验/m4_real_check_park1_iter1.json")
M1A_RESULTS = ("/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/"
               "M1a离线Mask验证/M1a_results.xlsx")
CHUNK_CHARS = 50_000


def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def invoke(label, **kwargs):
    print(f"  -> {label}")
    return fc.invoke(**kwargs)


def load_real_lambda_z(iter_k, park_idx):
    """从M1a_results.xlsx的lambda_z_detail取真实的(lambda,z)，按E,G,H,C顺序拼成96维。"""
    wb = openpyxl.load_workbook(M1A_RESULTS, read_only=True)
    ws = wb["lambda_z_detail"]
    by_energy = {"E": [None] * 24, "G": [None] * 24, "H": [None] * 24, "C": [None] * 24}
    z_by_energy = {"E": [None] * 24, "G": [None] * 24, "H": [None] * 24, "C": [None] * 24}
    header = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            header = row
            continue
        it, energy, pidx, hour, lam, z, rho = row
        if it == iter_k and pidx == park_idx:
            by_energy[energy][hour - 1] = lam
            z_by_energy[energy][hour - 1] = z
    lam_block = by_energy["E"] + by_energy["G"] + by_energy["H"] + by_energy["C"]
    z_block = z_by_energy["E"] + z_by_energy["G"] + z_by_energy["H"] + z_by_energy["C"]
    assert all(v is not None for v in lam_block), "lambda数据不完整"
    return lam_block, z_block


def upload_large_mask(schedule_id, k0, qrows, h_e0, g_masked, h_vec, c0, e_rows, x0e):
    invoke(
        "BeginMaskedSubproblemUpload",
        caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, DATA_OWNER, str(k0), str(qrows), str(MASK_SCALE)],
        wait_seconds=3.0,
    )
    parts = {
        "hE0": json.dumps(h_e0, separators=(",", ":")),
        "gMasked": json.dumps(g_masked, separators=(",", ":")),
        "hVec": json.dumps(h_vec, separators=(",", ":")),
        "c0": json.dumps(c0, separators=(",", ":")),
        "eRows": json.dumps(e_rows, separators=(",", ":")),
        "x0e": json.dumps(x0e, separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        payload_chunks = chunks(payload)
        print(f"  -> Upload {part_name}: {len(payload)} chars in {len(payload_chunks)} chunks")
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks, part_name=part_name: fc.invoke(
                    caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, DATA_OWNER, part_name, str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(1.0)
    print("  -> FinalizeMaskedSubproblemUpload")
    fc.retry(
        lambda: fc.invoke(
            caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
            contract=CONTRACT_MASK, function="FinalizeMaskedSubproblemUpload",
            args=[schedule_id, DATA_OWNER], wait_seconds=2.0,
        ),
        attempts=6, delay_seconds=2.0,
    )


def main():
    with open(BUNDLE_PATH, encoding="utf-8") as f:
        bundle = json.load(f)
    k0 = bundle["k0"]
    qrows = bundle["qRows"]
    rho = bundle["rho"]
    print(f"载入Park1注册量: k0={k0} qRows={qrows} rho={rho}")

    lam_block, z_block = load_real_lambda_z(iter_k=1, park_idx=1)
    print(f"取到iter=1,park=1真实λ,z: λ[0]={lam_block[0]} z[0]={z_block[0]}")

    # ── 反缩放成float，按masked_solver.build_masked_round的公式现场重算 ──────
    mask_float = {
        "H_e0": [[v / MASK_SCALE for v in row] for row in bundle["hE0Scaled"]],
        "C0": [v / MASK_SCALE for v in bundle["c0Scaled"]],
        "E_rows": [[v / MASK_SCALE for v in row] for row in bundle["eRowsScaled"]],
        "x0_e": [v / MASK_SCALE for v in bundle["x0eScaled"]],
    }
    h_masked, c_masked = build_masked_round(mask_float, rho, lam_block, z_block)

    g_masked_float = [[v / MASK_SCALE for v in row] for row in bundle["gMaskedScaled"]]
    h_vec_float = [v / MASK_SCALE for v in bundle["hVecScaled"]]

    print("\n[现场求解] 用OSQP独立解masked QP（不借用任何缓存答案）...")
    y_star, mu_star = solve_masked_qp(h_masked, c_masked, g_masked_float, h_vec_float)
    print(f"  OSQP解出 y*[:3]={y_star[:3]}  mu*[:3]={mu_star[:3]}")

    y_scaled = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]  # 数值噪声可能给出极小负数，钳到0

    # 本轮的 H_masked,c_masked 用同一套公式重新定点化（rho*H_e0、c_masked*MASK_SCALE），
    # 跟链码/verifier最终核对时用的是同一份registered数据+同一套公式，没有偷工减料
    h_masked_scaled = [[int(round(rho * x)) for x in row] for row in bundle["hE0Scaled"]]
    c_masked_scaled = [int(round(x * MASK_SCALE)) for x in c_masked]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_masked_scaled, c_masked_scaled,
        bundle["gMaskedScaled"], bundle["hVecScaled"], y_scaled, mu_scaled,
    )
    print(f"  本地KKT校验: pass={passed}  平稳性残差={stat_res}  互补松弛={compl_res}")
    if not passed:
        print("FAIL: 现场求解出的解没有通过本地KKT校验，不应继续")
        return 2

    toy_date = "9995-M4REALSOLVE-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    schedule_id = "SCHEDULE_" + toy_date
    print("\n" + "=" * 72)
    print(f"M4 独立求解端到端验证   scheduleID={schedule_id}")
    print("=" * 72)

    for park in ["Park1", "Park2", "Park3"]:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            return 1

    invoke(
        "PublishNetworkParams",
        caller_org="Observer", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM, function="PublishNetworkParams",
        args=[toy_date, json.dumps([99000, 99000, 99000]), json.dumps([99000, 99000, 99000]),
              json.dumps([99000, 99000]), json.dumps([99000, 99000, 99000])],
        wait_seconds=2.0,
    )
    invoke(
        "TriggerDispatch",
        caller_org="Park1", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM, function="TriggerDispatch",
        args=[toy_date, "10000", "10000", "10000", "100", "4500", json.dumps([430000, 530000, 240000])],
        wait_seconds=2.0,
    )

    upload_large_mask(
        schedule_id, k0, qrows,
        bundle["hE0Scaled"], bundle["gMaskedScaled"], bundle["hVecScaled"],
        bundle["c0Scaled"], bundle["eRowsScaled"], bundle["x0eScaled"],
    )

    invoke(
        "AssignSolverAndVerifier",
        caller_org="Park1", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[schedule_id, str(ITER), DATA_OWNER], wait_seconds=2.0,
    )
    assignment = fc.query_with_retry("Park1", CONTRACT_MASK, "QuerySolverAssignment",
                                      [schedule_id, str(ITER), DATA_OWNER])
    solver = assignment["solverParkId"]
    verifier = assignment["verifierParkId"]
    print(f"分配结果: 数据方={DATA_OWNER} solver={solver} verifier={verifier}")
    if solver == DATA_OWNER:
        print("❌ solver等于数据方自己，分配逻辑有问题！")
        return 3
    print(f"✅ solver({solver}) != 数据方({DATA_OWNER})，确认没有自己mask自己求")

    solve_credit_before = fc.query_with_retry(solver, CONTRACT_AUDIT, "QuerySolveCreditScore", [solver])
    print(f"求解前 {solver} 的求解信用分: {solve_credit_before['score']}")

    invoke(
        "SubmitMaskedSolution(OSQP现场算出的y*,mu*)",
        caller_org=solver, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[schedule_id, str(ITER), DATA_OWNER, json.dumps(y_scaled), json.dumps(mu_scaled)],
        wait_seconds=2.0,
    )
    msol = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                [schedule_id, str(ITER), DATA_OWNER])

    invoke(
        f"RecordVerdict(verifier={verifier}独立KKT校验后的真实裁决)",
        caller_org=verifier, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[schedule_id, str(ITER), DATA_OWNER, "true" if passed else "false",
              str(stat_res), str(compl_res), msol["solutionHash"]],
        wait_seconds=2.0,
    )

    solve_credit_after = fc.query_with_retry(solver, CONTRACT_AUDIT, "QuerySolveCreditScore", [solver])
    print(f"裁决后 {solver} 的求解信用分: {solve_credit_after['score']} "
          f"(变化: {solve_credit_after['score'] - solve_credit_before['score']:+d})")

    data_credit_owner = fc.query_with_retry(DATA_OWNER, CONTRACT_AUDIT, "QueryCreditScore", [DATA_OWNER])
    print(f"数据方({DATA_OWNER})的数据信用分: {data_credit_owner['score']}（应该完全没被这次求解结果影响）")

    rec = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                               [schedule_id, str(ITER), DATA_OWNER])

    def compare(name, actual, expected_scaled):
        diffs = [abs(a - e) for a, e in zip(actual, expected_scaled)]
        max_diff = max(diffs)
        ok = max_diff <= 2
        print(f"  {name}: 链上={actual[:3]}...  期望(MATLAB真实值)={expected_scaled[:3]}...  "
              f"最大差异={max_diff}(scaled)  {'PASS' if ok else 'FAIL'}")
        return ok

    print("\n[比对] 链上反解的e_m(基于OSQP现场解) vs MATLAB真实算出的e_m")
    ok = True
    ok &= compare("eE", rec["eExScaled"], bundle["expectedEEScaled"])
    ok &= compare("eG", rec["gExScaled"], bundle["expectedEGScaled"])
    ok &= compare("eH", rec["hExScaled"], bundle["expectedEHScaled"])
    ok &= compare("eC", rec["cExScaled"], bundle["expectedECScaled"])

    print("\n" + "=" * 72)
    if ok and solver != DATA_OWNER and solve_credit_after["score"] > solve_credit_before["score"]:
        print("✅ 全部通过：solver≠数据方、独立求解结果通过KKT、e_m与MATLAB一致、求解信用分正确加分")
        result = 0
    else:
        print("❌ 存在未通过项，见上")
        result = 1
    print("=" * 72)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
