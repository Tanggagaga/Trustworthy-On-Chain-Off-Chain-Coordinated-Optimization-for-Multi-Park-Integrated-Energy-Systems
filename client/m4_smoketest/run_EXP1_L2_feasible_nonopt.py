"""
EXP-1 Level-2：可行非最优解攻击
================================
攻击方法：
    Solver（Park2）忽略 ADMM 协调项（令 λ=0, z=0），
    只最小化本地成本 min 0.5 y'H_m y + c₀'y  s.t. Gy ≤ h，
    得到 y_L2（完全满足约束，原始可行），
    但对链上真实 λ/z/ρ 而言不是 ADMM 子问题最优解，
    链上 KKT 验证必然失败。

物理含义：
    Park2 不信任 / 无视协调器广播的对偶价格 λ，
    擅自按本地利益最小化成本后上报，
    试图让自己园区的用能成本最低而不承担能量平衡责任。

scheduleID 前缀：9994-M7D-（严格隔离，不得复用 M7A/M7B/M7C）
输出目录：results/L2/
"""

import json, math, os, sys, time, uuid
from datetime import datetime
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, CONTRACT_AUDIT, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

# ── 实验常量 ──────────────────────────────────────────────────────────────────
ATTACK_PARK      = "Park1"
ATTACK_SOLVER    = "Park2"
ATTACK_FROM_ITER = 10          # 稳定阶段触发
RNG_SEED         = 42

ALL_3      = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "results", "L2"))
CHUNK_CHARS = 50_000

os.makedirs(OUT_DIR, exist_ok=True)

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def chunks(text, size=CHUNK_CHARS):
    return [text[i:i+size] for i in range(0, len(text), size)]

def load_bundle(park_idx):
    path = os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json")
    with open(path, encoding="utf-8") as f:
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
        for idx, chunk in enumerate(cks):
            fc.retry(
                lambda idx=idx, chunk=chunk, cks=cks, pn=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=ALL_3,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[sid, park_id, pn, str(idx), str(len(cks)), chunk],
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

# ── 正常求解 ──────────────────────────────────────────────────────────────────
def solve_park_normal(park, k, mask_float, g_masked_float, h_vec_float, bundles,
                      lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
                      pre_assigned=False):
    idx = PARK_IDS.index(park)
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0]*24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0]*24
    lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star    = solve_masked_qp(h_masked, c_masked,
                                          g_masked_float[park], h_vec_float[park])
    y_scaled  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    bundle    = bundles[park]
    h_m_sc    = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_m_sc    = [int(round(v * MASK_SCALE)) for v in c_masked]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_m_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_scaled, mu_scaled)
    assert passed, f"正常路径 KKT 意外失败: iter={k} {park} stat={stat_res}"

    if not pre_assigned:
        fc.retry(
            lambda: fc.invoke(caller_org=park, endorsing_orgs=ALL_3,
                              contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                              args=[sid, str(k), park], wait_seconds=2.0),
            attempts=4, delay_seconds=2.0)

    assignment = fc.query_with_retry(park, CONTRACT_MASK,
                                     "QuerySolverAssignment", [sid, str(k), park],
                                     attempts=15, delay_seconds=2.0)
    solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]
    print(f"    {park}: solver={solver} verifier={verifier} kkt=PASS")

    fc.invoke(caller_org=solver, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_scaled), json.dumps(mu_scaled)],
              wait_seconds=1.0)
    msol = fc.query_with_retry(park, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    fc.invoke(caller_org=verifier, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, "true",
                    str(stat_res), str(compl_res), msol["solutionHash"]],
              wait_seconds=1.5)
    rec = fc.query_with_retry(park, CONTRACT_ADMM, "QueryIterationRecord",
                              [sid, str(k), park], attempts=8, delay_seconds=1.5)
    print(f"    {park}: IterationRecord 已确认 eE[0]={rec['eExScaled'][0]}")
    return {"park": park, "solver": solver, "verifier": verifier, "kkt_pass": True,
            "stat_res": stat_res, "compl_res": compl_res}


# ── L2 攻击：可行非最优解 ─────────────────────────────────────────────────────
def solve_park1_L2_attack(k, mask_float, g_masked_float, h_vec_float,
                           bundles, lam_e, lam_g, lam_h, lam_c,
                           z_e, z_g, z_h, z_c, rho, sid,
                           already_assigned=False):
    """
    L2 攻击：Solver（Park2）无视 ADMM 对偶价格 λ，令 λ=0,z=0 求本地最优解。
    y_L2 完全满足原始约束（可行），但对真实子问题而言非最优。
    链上 Verifier 用真实 λ/z 重建 c_masked 后验证 KKT → 必然失败。
    """
    park = ATTACK_PARK
    idx  = PARK_IDS.index(park)
    bundle = bundles[park]

    # ── 1. 真实最优解 y*（用链上真实 λ/z/ρ）──
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0]*24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0]*24
    lam_block_real = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block_real   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    h_real, c_real = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    y_star, mu_star = solve_masked_qp(h_real, c_real,
                                       g_masked_float[park], h_vec_float[park])
    y_star_sc  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_star_sc = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    h_m_sc     = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_real_sc  = [int(round(v * MASK_SCALE)) for v in c_real]

    pass_ref, stat_ref, compl_ref = check_kkt_scaled(
        h_m_sc, c_real_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_star_sc, mu_star_sc)
    assert pass_ref, f"基线 KKT 失败 stat={stat_ref}"

    lam_block_norm = float(np.linalg.norm(lam_block_real))
    z_block_norm   = float(np.linalg.norm(z_block_real))

    # ── 2. L2 攻击解：令 λ=0, z=0，只最小化本地成本 ──
    k0 = len(y_star)
    lam_zero = [0.0] * len(lam_block_real)
    z_zero   = [0.0] * len(z_block_real)
    h_L2, c_L2 = build_masked_round(mask_float[park], rho, lam_zero, z_zero)
    y_L2, mu_L2 = solve_masked_qp(h_L2, c_L2,
                                   g_masked_float[park], h_vec_float[park])
    y_L2_sc  = [int(round(v * MASK_SCALE)) for v in y_L2]
    mu_L2_sc = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_L2]

    # ── 3. 链下验证可行性 ──
    G_np     = np.array([[v/MASK_SCALE for v in row] for row in bundle["gMaskedScaled"]])
    h_np     = np.array([v/MASK_SCALE  for v in bundle["hVecScaled"]])
    y_L2_np  = np.array(y_L2)
    y_star_np= np.array(y_star)
    slack    = h_np - G_np @ y_L2_np
    n_viol   = int((slack < -1e-6).sum())   # 实际违约数（浮点舍入外）

    delta_y      = y_L2_np - y_star_np
    delta_norm   = float(np.linalg.norm(delta_y))
    star_norm    = float(np.linalg.norm(y_star_np))
    relative_err = delta_norm / (star_norm + 1e-12)

    print(f"\n  [L2-ATTACK] iter={k} {park} Solver={ATTACK_SOLVER}")
    print(f"    k0={k0}")
    print(f"    λ_block 范数={lam_block_norm:.4f}  z_block 范数={z_block_norm:.4f}")
    print(f"    y*[0]={y_star[0]:.4f}  y_L2[0]={y_L2[0]:.4f}  Δy[0]={delta_y[0]:.4f}")
    print(f"    ‖Δy‖={delta_norm:.4f}  ‖y*‖={star_norm:.4f}  相对误差={relative_err:.4%}")
    print(f"    可行性违约数={n_viol}（应=0，数值舍入除外）  min_slack={slack.min():.2e}")
    print(f"    基线 (y*,μ*): stat_ref={stat_ref}, compl_ref={compl_ref}")

    # ── 4. 链上 Assign（如未做）──
    if not already_assigned:
        fc.invoke(caller_org=park, endorsing_orgs=ALL_3,
                  contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                  args=[sid, str(k), park], wait_seconds=2.0)
    assignment = fc.query_with_retry(park, CONTRACT_MASK,
                                     "QuerySolverAssignment", [sid, str(k), park],
                                     attempts=15, delay_seconds=2.0)
    solver_id   = assignment["solverParkId"]
    verifier_id = assignment["verifierParkId"]
    assert solver_id == ATTACK_SOLVER, \
        f"本轮 solver={solver_id} ≠ {ATTACK_SOLVER}，跳过攻击"

    # ── 5. Solver 提交 y_L2 ──
    fc.invoke(caller_org=solver_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_L2_sc), json.dumps(mu_L2_sc)],
              wait_seconds=1.5)
    print(f"    → SubmitMaskedSolution(y_L2) 已上链，solutionHash 固化")

    # ── 6. Verifier 从链上下载，用真实 λ/z 重建 c_masked，运行 KKT ──
    msol = fc.query_with_retry(verifier_id, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    y_dl     = msol["yScaled"]
    mu_dl    = msol["muScaled"]
    sol_hash = msol["solutionHash"]

    # Verifier 重建 c_masked（用真实 lam/z，与链上 Verifier 行为一致）
    _, c_verify = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    c_verify_sc = [int(round(v * MASK_SCALE)) for v in c_verify]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_verify_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_dl, mu_dl)

    print(f"    → Verifier({verifier_id}) 链上 KKT 检验（真实 λ/z）：")
    print(f"      passed={passed}  stat_res={stat_res}  compl_res={compl_res}")
    print(f"      阈值 KKT_TOL=10000  超出倍数={stat_res/10000:.1f}×")

    # ── 7. RecordVerdict（如实提交）──
    verdict_str = "true" if passed else "false"
    fc.invoke(caller_org=verifier_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, verdict_str,
                    str(stat_res), str(compl_res), sol_hash],
              wait_seconds=1.5)
    print(f"    → RecordVerdict(pass={verdict_str}) 已上链")

    credit_after = query_solve_credit(solver_id)
    print(f"    → SOLVE_CREDIT[{solver_id}] 当前={credit_after}")

    return {
        "iter":              k,
        "attack_level":      "L2",
        "attack_type":       "feasible_nonopt_lambda0",
        "park":              park,
        "solver":            solver_id,
        "verifier":          verifier_id,
        "k0":                k0,
        "rho":               rho,
        # ADMM 状态
        "lam_block_norm":    round(lam_block_norm, 4),
        "z_block_norm":      round(z_block_norm, 4),
        # 真实最优解
        "y_star_0":          round(float(y_star[0]), 6),
        "y_star_norm":       round(star_norm, 6),
        "baseline_stat":     stat_ref,
        "baseline_compl":    compl_ref,
        "baseline_pass":     pass_ref,
        # L2 攻击解
        "y_L2_0":            round(float(y_L2[0]), 6),
        "y_L2_norm":         round(float(np.linalg.norm(y_L2_np)), 6),
        "delta_y_0":         round(float(delta_y[0]), 6),
        "delta_y_norm":      round(delta_norm, 6),
        "relative_err":      round(relative_err, 6),
        "n_constraint_viol": n_viol,
        "min_slack":         round(float(slack.min()), 8),
        # 链上 KKT 结果
        "kkt_pass":          passed,
        "stat_res":          stat_res,
        "compl_res":         compl_res,
        "stat_over_tol":     round(stat_res / 10000, 2),
        "solution_hash":     sol_hash,
        "solve_credit_after": credit_after,
    }


# ── 单轮实验（独立 scheduleID）────────────────────────────────────────────────
def run_l2_trial(bundles, mask_float, g_masked_float, h_vec_float, max_iters=60):
    date_str = f"9994-M7D-{datetime.now().strftime('%Y%m%d-%H%M%S')}-L2-{uuid.uuid4().hex[:4]}"
    sid = setup(date_str, max_iter=max_iters)
    print(f"\n{'='*65}")
    print(f"[TRIAL] L2 可行非最优解攻击  scheduleID={sid}")
    print(f"  隔离前缀: 9994-M7D-（不复用 M7A/M7B/M7C）")
    print(f"{'='*65}")

    print("\n[注册] 上传三个园区的 masked 结构")
    for i, p in enumerate(ALL_3, start=1):
        upload_large_mask(sid, p, bundles[p])
        print(f"  -> {p} 注册完成 (k0={bundles[p]['k0']})")

    credits_init = {p: query_solve_credit(p) for p in ALL_3}
    print(f"\n[基线] SOLVE_CREDIT: {credits_init}")

    normal_rows  = []
    attack_result = None

    for k in range(max_iters):
        print(f"\n--- iter {k} ---")
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
        print(f"  rho={rho:.4f}")

        fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                  contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                  args=[sid, str(k), "Park1"], wait_seconds=1.0)
        pre_assign = fc.query_with_retry(
            "Park1", CONTRACT_MASK, "QuerySolverAssignment", [sid, str(k), "Park1"])
        park1_solver = pre_assign["solverParkId"]
        print(f"  Park1 solver={park1_solver}")

        trigger = (k >= ATTACK_FROM_ITER and park1_solver == ATTACK_SOLVER)

        if trigger:
            credit_before = {p: query_solve_credit(p) for p in ALL_3}
            print(f"  [!] 触发 L2 攻击，SOLVE_CREDIT 前: {credit_before}")

            res = solve_park1_L2_attack(
                k, mask_float, g_masked_float, h_vec_float,
                bundles, lam_e, lam_g, lam_h, lam_c,
                z_e, z_g, z_h, z_c, rho, sid, already_assigned=True)
            res["credit_before"]    = credit_before
            res["credit_after_all"] = {p: query_solve_credit(p) for p in ALL_3}
            res["scheduleID"]       = sid
            res["credits_init"]     = credits_init
            attack_result = res

            # Park2/Park3 正常求解
            for p in ["Park2", "Park3"]:
                r = solve_park_normal(
                    p, k, mask_float, g_masked_float, h_vec_float, bundles,
                    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid)
                normal_rows.append({"iter": k, **r})

            # CoordinateStep：应被阻断
            print(f"\n  [验证] 尝试 CoordinateStep({k})...")
            try:
                fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                          contract=CONTRACT_ADMM, function="CoordinateStep",
                          args=[sid, str(k)], wait_seconds=2.0)
                print(f"  [!] 意外成功 —— 攻击未生效！")
                attack_result["coord_step_blocked"] = False
            except Exception as coord_err:
                err_str = str(coord_err)[:300]
                blocked = "missing" in err_str.lower()
                print(f"  -> CoordinateStep 被拒绝 ({'✓ 符合预期' if blocked else '?'})")
                print(f"     {err_str[:200]}")
                attack_result["coord_step_blocked"] = blocked
                attack_result["coord_error"]        = err_str

            # 确认 IterRecord 不存在
            try:
                fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                    [sid, str(k), "Park1"], attempts=3, delay_seconds=1.0)
                attack_result["iter_record_exists"] = True
            except Exception:
                attack_result["iter_record_exists"] = False
                print(f"  [✓] IterationRecord(iter={k}, Park1): 不存在")

            print(f"\n  [退出] 攻击完成，ADMM 状态已中断，退出迭代")
            break

        else:
            # 正常三园区求解
            r1 = solve_park_normal(
                "Park1", k, mask_float, g_masked_float, h_vec_float, bundles,
                lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
                pre_assigned=True)
            normal_rows.append({"iter": k, **r1})
            for p in ["Park2", "Park3"]:
                r = solve_park_normal(
                    p, k, mask_float, g_masked_float, h_vec_float, bundles,
                    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid)
                normal_rows.append({"iter": k, **r})

            try:
                fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                          contract=CONTRACT_ADMM, function="CoordinateStep",
                          args=[sid, str(k)], wait_seconds=2.0)
            except Exception as e:
                print(f"  [warn] CoordinateStep 失败: {e}")
                continue

            lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
            admm = pa.query_admm_state(sid)
            print(f"  status={admm.get('status')} "
                  f"primalSq={admm.get('primalResidSq',0)} "
                  f"dualSq={admm.get('dualResidSq',0)}")

    if attack_result is None:
        print(f"  [!!] {max_iters} 轮内未触发攻击")
    return attack_result, normal_rows


# ── 主函数 ─────────────────────────────────────────────────────────────────────
def run():
    print("\n[预热] 三个园区容器")
    for p in ALL_3:
        ready, msg = preheat_org(p)
        print(("  OK  " if ready else "  FAIL ") + msg)
        if not ready:
            raise RuntimeError(f"{p} 未就绪")

    bundles = {}
    mask_float = {}
    g_masked_float = {}
    h_vec_float = {}
    for i, p in enumerate(ALL_3, start=1):
        b = load_bundle(i)
        bundles[p]        = b
        mask_float[p]     = to_float_mask(b)
        g_masked_float[p] = [[v/MASK_SCALE for v in row] for row in b["gMaskedScaled"]]
        h_vec_float[p]    = [v/MASK_SCALE  for v in b["hVecScaled"]]

    res, normal_rows = run_l2_trial(
        bundles, mask_float, g_masked_float, h_vec_float, max_iters=60)

    # ── 保存数据 ──
    print(f"\n[保存数据]")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    path_json = f"{OUT_DIR}/L2_raw_{ts}.json"
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({
            "experiment":    "EXP-1-L2-FeasibleNonOpt",
            "attack_type":   "lambda0_local_only",
            "kkt_tol":       10000,
            "mask_scale":    MASK_SCALE,
            "attack_result": res,
            "normal_iters":  len(normal_rows),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"  原始 JSON: {path_json}")

    if res:
        df = pd.DataFrame([res])
        path_xlsx = f"{OUT_DIR}/L2_attack_result_{ts}.xlsx"
        df.to_excel(path_xlsx, index=False)
        print(f"  攻击结果: {path_xlsx}")

    df_norm = pd.DataFrame(normal_rows)
    path_norm = f"{OUT_DIR}/L2_normal_rounds_{ts}.xlsx"
    df_norm.to_excel(path_norm, index=False)
    print(f"  正常轮数据: {path_norm}")

    if res:
        print(f"\n{'='*65}")
        print(f"EXP-1 L2 可行非最优解攻击汇总")
        print(f"{'='*65}")
        print(f"  scheduleID : {res.get('scheduleID','?')}")
        print(f"  触发轮次   : iter={res['iter']}")
        print(f"  λ_block‖   : {res['lam_block_norm']:.4f}")
        print(f"  y*[0]      : {res['y_star_0']:.4f}")
        print(f"  y_L2[0]    : {res['y_L2_0']:.4f}")
        print(f"  ‖Δy‖       : {res['delta_y_norm']:.4f}  相对误差={res['relative_err']*100:.2f}%")
        print(f"  可行性     : 违约约束数={res['n_constraint_viol']}（=0 表示完全可行）")
        print(f"  基线 stat  : {res['baseline_stat']}")
        print(f"  攻击 stat  : {res['stat_res']}  超阈 {res['stat_over_tol']}×")
        print(f"  KKT 裁决   : {'PASS' if res['kkt_pass'] else 'FAIL'}")
        print(f"  CoordStep  : {'阻断 ✗' if res.get('coord_step_blocked') else '通过 ✓'}")
        bef = res.get('credit_before', {}).get(ATTACK_SOLVER, '?')
        aft = res.get('credit_after_all', {}).get(ATTACK_SOLVER, '?')
        print(f"  信用变化   : {ATTACK_SOLVER} {bef}→{aft}")
        print(f"{'='*65}")

    return res


if __name__ == "__main__":
    run()
