# -*- coding: utf-8 -*-
"""
EXP-1.5：验证方作恶实验（M7 链上KKT复算版专属）
==================================================
旧机制（M4-M6）下 KKT 残差由验证方链下自报，验证方是单点信任：
谎报 PASS 可为合谋求解方洗白，谎报 FAIL 可陷害诚实求解方。
M7 链码定点复算后，裁决以链码为准，本实验验证两类谎报都失效：

  场景 A【谎报 FAIL，陷害】：求解方提交正确 (y*,μ*)，
    验证方却 RecordVerdict(pass=false, 巨大假残差)。
    预期：链上 Verdict.pass=true（链码复算），reportedPass=false 存档，
          验证方被记 VERDICT_MISMATCH 异常；IterationRecord 正常生成；
          求解方信用 +2（而非被扣 10）；本轮 CoordinateStep 正常推进。

  场景 B【谎报 PASS，合谋洗白】：求解方提交 L2 式可行非最优解
    （λ=0,z=0 的本地最优），验证方却 RecordVerdict(pass=true, 假小残差)。
    预期：链上 Verdict.pass=false，reportedPass=true 存档，
          验证方被记 VERDICT_MISMATCH，求解方被记 KKT_VERIFICATION_FAILED
          并扣求解信用 10；IterationRecord 不生成；CoordinateStep 被阻断。

scheduleID 前缀：9994-M75-（与其他实验完全隔离）
输出目录：results/EXP1_5_verifier/
"""

import json, os, sys, time, uuid
from datetime import datetime
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, CONTRACT_AUDIT, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup, wait_state_after_coordinate
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

ALL_3       = ["Park1", "Park2", "Park3"]
TARGET_PARK = "Park1"   # 两个场景都针对 Park1 的子问题
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "results", "EXP1_5_verifier"))
CHUNK_CHARS = 50_000

os.makedirs(OUT_DIR, exist_ok=True)


# ── 工具（与 L 系列脚本同构）────────────────────────────────────────────────
def chunks(text, size=CHUNK_CHARS):
    return [text[i:i+size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    with open(os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(sid, park_id, bundle):
    fc.invoke(caller_org=park_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
              args=[sid, park_id, str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
              wait_seconds=2.0)
    parts = {
        "hE0":     json.dumps(bundle["hE0Scaled"],     separators=(",",":")),
        "gMasked": json.dumps(bundle["gMaskedScaled"], separators=(",",":")),
        "hVec":    json.dumps(bundle["hVecScaled"],    separators=(",",":")),
        "c0":      json.dumps(bundle["c0Scaled"],      separators=(",",":")),
        "eRows":   json.dumps(bundle["eRowsScaled"],   separators=(",",":")),
        "x0e":     json.dumps(bundle["x0eScaled"],     separators=(",",":")),
    }
    for part_name, payload in parts.items():
        cks = chunks(payload)
        for idx_c, chunk in enumerate(cks):
            fc.retry(
                lambda i=idx_c, c=chunk, cs=cks, pn=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=ALL_3,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[sid, park_id, pn, str(i), str(len(cs)), c],
                    wait_seconds=0.0),
                attempts=4, delay_seconds=1.5)
        time.sleep(0.5)
    fc.retry(
        lambda: fc.invoke(caller_org=park_id, endorsing_orgs=ALL_3,
                          contract=CONTRACT_MASK, function="FinalizeMaskedSubproblemUpload",
                          args=[sid, park_id], wait_seconds=1.5),
        attempts=6, delay_seconds=2.0)


def to_float_mask(bundle):
    return {
        "H_e0":   [[v/MASK_SCALE for v in row] for row in bundle["hE0Scaled"]],
        "C0":     [v/MASK_SCALE  for v in bundle["c0Scaled"]],
        "E_rows": [[v/MASK_SCALE for v in row] for row in bundle["eRowsScaled"]],
        "x0_e":   [v/MASK_SCALE  for v in bundle["x0eScaled"]],
    }


def query_solve_credit(park_id):
    try:
        raw = fc.query_with_retry("Park1", CONTRACT_AUDIT,
                                  "QuerySolveCreditScore", [park_id])
        if isinstance(raw, (int, float)):
            return int(raw)
        return int(raw.get("score", raw.get("Score", -1)))
    except Exception as e:
        return f"查询失败: {e}"


def count_mismatch_anomalies(park_id):
    """统计某园区名下 VERDICT_MISMATCH 异常条数。"""
    try:
        trail = fc.query_with_retry("Park1", CONTRACT_AUDIT, "QueryAuditTrail", [park_id])
        if not trail:
            return 0, []
        hits = [a for a in trail if a.get("type") == "VERDICT_MISMATCH"]
        return len(hits), hits
    except Exception:
        return 0, []


def blocks_for(park, lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c):
    idx = PARK_IDS.index(park)
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0]*24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0]*24
    return (lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx],
            z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx])


def solve_correct(park, mask_float, g_masked_float, h_vec_float, bundle,
                  lam_block, z_block, rho):
    """按真实链上协调量求解，返回 scaled (y,μ) 和链下自检残差。"""
    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star = solve_masked_qp(h_masked, c_masked,
                                      g_masked_float[park], h_vec_float[park])
    y_sc  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_sc = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    h_sc  = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_sc  = [int(round(v * MASK_SCALE)) for v in c_masked]
    passed, stat, compl = check_kkt_scaled(
        h_sc, c_sc, bundle["gMaskedScaled"], bundle["hVecScaled"], y_sc, mu_sc)
    return y_sc, mu_sc, passed, stat, compl


def submit_and_verdict(sid, k, park, y_sc, mu_sc,
                       reported_pass, reported_stat, reported_compl):
    """走完 分配→提交→(按指定内容)裁决 三步，返回 (assignment, verdict)。"""
    fc.invoke(caller_org=park, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
              args=[sid, str(k), park], wait_seconds=1.0)
    assignment = fc.query_with_retry(park, CONTRACT_MASK,
                                     "QuerySolverAssignment", [sid, str(k), park])
    solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]

    fc.invoke(caller_org=solver, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_sc), json.dumps(mu_sc)],
              wait_seconds=1.0)
    msol = fc.query_with_retry(park, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])

    fc.invoke(caller_org=verifier, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park,
                    "true" if reported_pass else "false",
                    str(reported_stat), str(reported_compl), msol["solutionHash"]],
              wait_seconds=1.5)
    verdict = fc.query_with_retry(park, CONTRACT_MASK,
                                  "QueryVerdict", [sid, str(k), park])
    return assignment, verdict


def iter_record_exists(sid, k, park):
    try:
        fc.query_with_retry(park, CONTRACT_ADMM, "QueryIterationRecord",
                            [sid, str(k), park], attempts=3, delay_seconds=1.0)
        return True
    except Exception:
        return False


def normal_round_for(sid, k, park, mask_float, g_masked_float, h_vec_float, bundle,
                     lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho):
    """诚实求解 + 诚实裁决的一轮（用于非目标园区）。"""
    lam_block, z_block = blocks_for(park, lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c)
    y_sc, mu_sc, passed, stat, compl = solve_correct(
        park, mask_float, g_masked_float, h_vec_float, bundle, lam_block, z_block, rho)
    assert passed, f"正常路径 KKT 意外失败: iter={k} {park} stat={stat}"
    _, verdict = submit_and_verdict(sid, k, park, y_sc, mu_sc, True, stat, compl)
    assert verdict["pass"], f"正常路径链上裁决意外 FAIL: iter={k} {park}"


# ── 主实验 ─────────────────────────────────────────────────────────────────────
def run():
    print("\n[预热] 三个园区容器")
    for p in ALL_3:
        ready, msg = preheat_org(p)
        print(("  OK  " if ready else "  FAIL ") + msg)
        if not ready:
            raise RuntimeError(f"{p} 未就绪")

    bundles, mask_float, g_masked_float, h_vec_float = {}, {}, {}, {}
    for i, p in enumerate(ALL_3, start=1):
        b = load_bundle(i)
        bundles[p]        = b
        mask_float[p]     = to_float_mask(b)
        g_masked_float[p] = [[v/MASK_SCALE for v in row] for row in b["gMaskedScaled"]]
        h_vec_float[p]    = [v/MASK_SCALE  for v in b["hVecScaled"]]

    date_str = f"9994-M75-{datetime.now().strftime('%Y%m%d-%H%M%S')}-V15-{uuid.uuid4().hex[:4]}"
    sid = setup(date_str, max_iter=10)
    print(f"\nscheduleID={sid}")

    print("\n[注册] 上传三个园区的 masked 结构")
    for p in ALL_3:
        upload_large_mask(sid, p, bundles[p])
        print(f"  -> {p} 注册完成")

    results = {}

    # ═══ iter 0：全诚实热身轮 ═══════════════════════════════════════════════
    print("\n--- iter 0（热身，全诚实）---")
    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
    for p in ALL_3:
        normal_round_for(sid, 0, p, mask_float, g_masked_float, h_vec_float,
                         bundles[p], lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho)
        print(f"  {p}: 正常完成")
    pa.coordinate_step(sid, 0)
    wait_state_after_coordinate(sid, 0)  # 必须等 iter-1 的 λ,z 提交后再取，否则求解方拿到陈旧协调量

    # ═══ iter 1：场景 A —— 验证方谎报 FAIL（陷害诚实求解方）══════════════════
    print("\n--- iter 1【场景 A：谎报 FAIL / 陷害】---")
    k = 1
    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)

    lam_block, z_block = blocks_for(TARGET_PARK, lam_e, lam_g, lam_h, lam_c,
                                    z_e, z_g, z_h, z_c)
    y_sc, mu_sc, passed, stat, compl = solve_correct(
        TARGET_PARK, mask_float, g_masked_float, h_vec_float, bundles[TARGET_PARK],
        lam_block, z_block, rho)
    assert passed, "场景A要求求解方诚实求得正确解"
    print(f"  求解方诚实解: 链下自检 stat={stat} compl={compl} (PASS)")

    credits_before_A = {p: query_solve_credit(p) for p in ALL_3}
    assignment_A, verdict_A = submit_and_verdict(
        sid, k, TARGET_PARK, y_sc, mu_sc,
        reported_pass=False, reported_stat=99_999_999, reported_compl=99_999_999)
    credits_after_A = {p: query_solve_credit(p) for p in ALL_3}
    rec_exists_A = iter_record_exists(sid, k, TARGET_PARK)
    n_mm_A, _ = count_mismatch_anomalies(assignment_A["verifierParkId"])

    print(f"  验证方({assignment_A['verifierParkId']})谎报 FAIL(stat=99999999) →")
    print(f"    链上 Verdict.pass={verdict_A['pass']}  reportedPass={verdict_A['reportedPass']}")
    print(f"    链码复算残差 stat={verdict_A['stationarityResScaled']} compl={verdict_A['complementResScaled']}")
    print(f"    IterationRecord 存在={rec_exists_A}")
    print(f"    VERDICT_MISMATCH 异常(验证方名下)={n_mm_A} 条")
    print(f"    求解信用 {assignment_A['solverParkId']}: "
          f"{credits_before_A[assignment_A['solverParkId']]}→{credits_after_A[assignment_A['solverParkId']]}")

    results["A"] = {
        "scenario": "A_frame_honest_solver", "iter": k,
        "solver": assignment_A["solverParkId"], "verifier": assignment_A["verifierParkId"],
        "reported_pass": False, "reported_stat": 99_999_999, "reported_compl": 99_999_999,
        "chain_pass": verdict_A["pass"],
        "chain_stat": verdict_A["stationarityResScaled"],
        "chain_compl": verdict_A["complementResScaled"],
        "verdict_reported_pass_field": verdict_A["reportedPass"],
        "iter_record_exists": rec_exists_A,
        "verifier_mismatch_anomalies": n_mm_A,
        "solver_credit_before": credits_before_A[assignment_A["solverParkId"]],
        "solver_credit_after": credits_after_A[assignment_A["solverParkId"]],
        "offchain_honest_stat": stat, "offchain_honest_compl": compl,
    }

    # 完成本轮其余园区并推进（场景A不应阻断闭环）
    for p in ALL_3:
        if p != TARGET_PARK:
            normal_round_for(sid, k, p, mask_float, g_masked_float, h_vec_float,
                             bundles[p], lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho)
    coord_ok_A = True
    try:
        pa.coordinate_step(sid, k)
        wait_state_after_coordinate(sid, k)  # 等 iter-2 协调量提交
    except Exception as e:
        coord_ok_A = False
        print(f"  [!] CoordinateStep 意外失败: {e}")
    results["A"]["coordinate_step_ok"] = coord_ok_A
    print(f"    CoordinateStep(1) {'正常推进' if coord_ok_A else '被阻断（不符合预期）'}")

    # ═══ iter 2：场景 B —— 验证方谎报 PASS（与求解方合谋洗白坏解）═════════════
    print("\n--- iter 2【场景 B：谎报 PASS / 合谋洗白】---")
    k = 2
    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)

    # 求解方作恶：L2 式可行非最优解（λ=0,z=0 的本地最优）
    lam_zero = [0.0] * 96
    z_zero   = [0.0] * 96
    h_L2, c_L2 = build_masked_round(mask_float[TARGET_PARK], rho, lam_zero, z_zero)
    y_L2, mu_L2 = solve_masked_qp(h_L2, c_L2, g_masked_float[TARGET_PARK],
                                  h_vec_float[TARGET_PARK])
    y_bad  = [int(round(v * MASK_SCALE)) for v in y_L2]
    mu_bad = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_L2]
    print(f"  求解方提交 L2 式坏解 y_bad[0]={y_bad[0]/MASK_SCALE:.6f}")

    credits_before_B = {p: query_solve_credit(p) for p in ALL_3}
    assignment_B, verdict_B = submit_and_verdict(
        sid, k, TARGET_PARK, y_bad, mu_bad,
        reported_pass=True, reported_stat=1_000, reported_compl=10)
    credits_after_B = {p: query_solve_credit(p) for p in ALL_3}
    rec_exists_B = iter_record_exists(sid, k, TARGET_PARK)
    n_mm_B, _ = count_mismatch_anomalies(assignment_B["verifierParkId"])

    print(f"  验证方({assignment_B['verifierParkId']})谎报 PASS(stat=1000) →")
    print(f"    链上 Verdict.pass={verdict_B['pass']}  reportedPass={verdict_B['reportedPass']}")
    print(f"    链码复算残差 stat={verdict_B['stationarityResScaled']} compl={verdict_B['complementResScaled']}")
    print(f"    IterationRecord 存在={rec_exists_B}")
    print(f"    VERDICT_MISMATCH 异常(验证方名下)={n_mm_B} 条")
    print(f"    求解信用 {assignment_B['solverParkID'] if 'solverParkID' in assignment_B else assignment_B['solverParkId']}: "
          f"{credits_before_B[assignment_B['solverParkId']]}→{credits_after_B[assignment_B['solverParkId']]}")

    # 其余园区正常，然后 CoordinateStep 应因缺 Park1 记录被阻断
    for p in ALL_3:
        if p != TARGET_PARK:
            normal_round_for(sid, k, p, mask_float, g_masked_float, h_vec_float,
                             bundles[p], lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho)
    coord_blocked_B = False
    coord_err = ""
    try:
        fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                  contract=CONTRACT_ADMM, function="CoordinateStep",
                  args=[sid, str(k)], wait_seconds=2.0)
    except Exception as e:
        coord_err = str(e)[:300]
        coord_blocked_B = "missing" in coord_err.lower()
    print(f"    CoordinateStep(2) {'被阻断 ✓' if coord_blocked_B else '未被阻断（不符合预期）'}")

    results["B"] = {
        "scenario": "B_collude_whitewash_bad_solution", "iter": k,
        "solver": assignment_B["solverParkId"], "verifier": assignment_B["verifierParkId"],
        "reported_pass": True, "reported_stat": 1_000, "reported_compl": 10,
        "chain_pass": verdict_B["pass"],
        "chain_stat": verdict_B["stationarityResScaled"],
        "chain_compl": verdict_B["complementResScaled"],
        "verdict_reported_pass_field": verdict_B["reportedPass"],
        "iter_record_exists": rec_exists_B,
        "verifier_mismatch_anomalies": n_mm_B,
        "solver_credit_before": credits_before_B[assignment_B["solverParkId"]],
        "solver_credit_after": credits_after_B[assignment_B["solverParkId"]],
        "coordinate_step_blocked": coord_blocked_B,
        "coord_error": coord_err,
    }

    # ── 断言（实验结论的机器检查）────────────────────────────────────────────
    ok_A = (verdict_A["pass"] is True and verdict_A["reportedPass"] is False
            and rec_exists_A and coord_ok_A and n_mm_A >= 1)
    ok_B = (verdict_B["pass"] is False and verdict_B["reportedPass"] is True
            and not rec_exists_B and coord_blocked_B and n_mm_B >= 1)
    results["summary"] = {
        "scenario_A_defended": ok_A,
        "scenario_B_defended": ok_B,
        "conclusion": "verifier lying is powerless and leaves VERDICT_MISMATCH audit trail"
                      if (ok_A and ok_B) else "UNEXPECTED — check details",
    }

    # ── 导出 ─────────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path_json = f"{OUT_DIR}/EXP15_raw_{ts}.json"
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({"experiment": "EXP-1.5-VerifierLying",
                   "chaincode": "energy v2.6 (on-chain KKT recompute)",
                   "scheduleID": sid, "kkt_tol": 10000, "mask_scale": MASK_SCALE,
                   "results": results}, f, ensure_ascii=False, indent=2, default=str)
    pd.DataFrame([results["A"], results["B"]]).to_excel(
        f"{OUT_DIR}/EXP15_results_{ts}.xlsx", index=False)
    print(f"\n[保存] {path_json}")

    print(f"\n{'='*68}")
    print("EXP-1.5 验证方作恶实验汇总")
    print(f"{'='*68}")
    print(f"  场景A 陷害:   链上裁决 PASS（不受谎报影响）: {ok_A}")
    print(f"  场景B 洗白:   链上裁决 FAIL（不受谎报影响）: {ok_B}")
    print(f"  结论: {results['summary']['conclusion']}")
    print(f"{'='*68}")
    return results


if __name__ == "__main__":
    run()
