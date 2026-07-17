"""
EXP-1 Level-3：错误对偶变量攻击（变体 B）
==========================================
攻击方法（变体 B——最高对抗强度）：
    提交 y = y_L2（可行非最优，与 L2 相同构造：令 λ=0,z=0 的本地最优解），
    同时精心伪造 μ_fake：通过非负最小二乘反解平稳性方程
        G_m^T μ = -(H_m y_L2 + c_masked)
    寻找使平稳性残差最小的非负 μ_fake，试图"救活"坏的原始解。

攻击意图：
    平稳性条件可能被 μ_fake 部分补偿，但互补松弛条件会暴露矛盾——
    y_L2 ≠ y* 导致活跃约束集不同，μ_fake 在错误位置有非零值。
    严格凸 QP 的 KKT 解唯一，不存在任何 μ 能让非最优 y 通过全部四条件。

验证目标：
    记录哪一条 KKT 条件最终失败（stationarity / complementary slackness / 两者），
    以及失败量级，证明"单条件洗白"策略对四条联合约束无效。

scheduleID 前缀：9994-M7E-（严格隔离，不复用 M7A/B/C/D）
输出目录：/mnt/c/Users/Lenovo/Desktop/daima/综合能源/作恶MaskKKT专属/L3/
"""

import json, os, sys, time, uuid
from datetime import datetime
import numpy as np
import pandas as pd
from scipy.optimize import nnls

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
ATTACK_FROM_ITER = 10

ALL_3      = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/M1a离线Mask验证/M6完整闭环"
OUT_DIR    = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/作恶MaskKKT专属/L3"
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
    return {"park": park, "solver": solver, "verifier": verifier,
            "kkt_pass": True, "stat_res": stat_res, "compl_res": compl_res}


# ── L3-B 攻击：错误对偶（NNLS 反解平稳性）────────────────────────────────────
def solve_park1_L3B_attack(k, mask_float, g_masked_float, h_vec_float,
                            bundles, lam_e, lam_g, lam_h, lam_c,
                            z_e, z_g, z_h, z_c, rho, sid,
                            already_assigned=False):
    """
    L3-B 攻击流程：
    1. 用真实 λ/z 计算 y*（基线）
    2. 用 λ=0,z=0 计算 y_L2（可行非最优，与 L2 相同）
    3. 通过 NNLS 反解平稳性方程构造 μ_fake（非负最小二乘）
    4. 检验 μ_fake 的各项 KKT 表现，逐条记录
    5. 链上提交 (y_L2, μ_fake)，Verifier 复算，RecordVerdict(false)
    """
    park   = ATTACK_PARK
    idx    = PARK_IDS.index(park)
    bundle = bundles[park]

    # ── 1. 真实最优解 y* ──
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0]*24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0]*24
    lam_block_real = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block_real   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    h_real, c_real = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    y_star, mu_star = solve_masked_qp(h_real, c_real,
                                       g_masked_float[park], h_vec_float[park])

    H_m = np.array([[v * rho for v in row] for row in bundle["hE0Scaled"]]) / MASK_SCALE
    G_m = np.array([[v / MASK_SCALE for v in row] for row in bundle["gMaskedScaled"]])
    h_m = np.array([v / MASK_SCALE for v in bundle["hVecScaled"]])
    c_real_np = np.array(c_real)

    y_star_np  = np.array(y_star)
    mu_star_np = np.array([max(v, 0.0) for v in mu_star])

    h_m_sc    = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_real_sc = [int(round(v * MASK_SCALE)) for v in c_real]
    y_star_sc = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_star_sc= [int(round(v * MASK_SCALE)) for v in mu_star_np]

    pass_ref, stat_ref, compl_ref = check_kkt_scaled(
        h_m_sc, c_real_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_star_sc, mu_star_sc)
    assert pass_ref, f"基线 KKT 失败 stat={stat_ref}"

    # ── 2. y_L2：λ=0,z=0 本地最优（可行，非最优）──
    lam_zero = [0.0] * len(lam_block_real)
    z_zero   = [0.0] * len(z_block_real)
    h_L2, c_L2 = build_masked_round(mask_float[park], rho, lam_zero, z_zero)
    y_L2, _    = solve_masked_qp(h_L2, c_L2, g_masked_float[park], h_vec_float[park])
    y_L2_np    = np.array(y_L2)

    # 验证 y_L2 原始可行性
    slack_L2  = h_m - G_m @ y_L2_np
    n_viol_L2 = int((slack_L2 < -1e-6).sum())

    # ── 3. 构造 μ_fake：NNLS 反解平稳性方程 G_m^T μ = -(H_m y_L2 + c_real) ──
    # 平稳性要求：H_m y_L2 + c_real + G_m^T μ = 0
    # 即 G_m^T μ = -(H_m y_L2 + c_real) = rhs_stat
    rhs_stat = -(H_m @ y_L2_np + c_real_np)       # shape (k0,)
    # G_m^T 的 shape 是 (k0, qRows)，μ 的 shape 是 (qRows,)
    # NNLS: min ‖G_m^T μ - rhs_stat‖²  s.t. μ ≥ 0
    GT = G_m.T                                      # (k0, qRows)
    mu_fake_np, nnls_residual = nnls(GT, rhs_stat)  # μ_fake ≥ 0 by construction

    # ── 4. 逐条 KKT 检验（浮点层，用于诊断）──
    # (a) 原始可行性：G_m y_L2 ≤ h_m
    primal_slack_min = float(slack_L2.min())
    primal_ok = (primal_slack_min >= -1e-6)

    # (b) 对偶可行性：μ_fake ≥ 0
    dual_min    = float(mu_fake_np.min())
    dual_ok     = (dual_min >= -1e-9)

    # (c) 平稳性残差（用 μ_fake）
    stat_vec   = H_m @ y_L2_np + c_real_np + G_m.T @ mu_fake_np
    stat_float = float(np.linalg.norm(stat_vec))

    # (d) 互补松弛：μ_fake_j * (G_m y_L2 - h_m)_j
    constraint_slack = G_m @ y_L2_np - h_m          # ≤ 0 for feasible
    compl_vec  = mu_fake_np * constraint_slack
    compl_float= float(np.max(np.abs(compl_vec)))

    # 对比：μ_star 的 NNLS 残差（验证 y* 满足平稳性）
    stat_vec_star = H_m @ y_star_np + c_real_np + G_m.T @ mu_star_np
    stat_float_star = float(np.linalg.norm(stat_vec_star))

    print(f"\n  [L3-B ATTACK] iter={k} {park} Solver={ATTACK_SOLVER}")
    print(f"    === 攻击解构造 ===")
    print(f"    y_L2[0]={y_L2[0]:.4f}  y*[0]={y_star[0]:.4f}")
    print(f"    ‖Δy‖={np.linalg.norm(y_L2_np-y_star_np):.4f}（相对 {np.linalg.norm(y_L2_np-y_star_np)/np.linalg.norm(y_star_np)*100:.2f}%）")
    print(f"    y_L2 原始可行性：违约数={n_viol_L2}  min_slack={primal_slack_min:.2e}")
    print(f"    平稳性方程 NNLS 残差：‖G^T μ_fake - rhs‖={nnls_residual:.4f}")
    print(f"    μ_fake 非零数：{(mu_fake_np > 1e-8).sum()}  μ*非零数：{(mu_star_np > 1e-8).sum()}")
    print(f"    μ_fake[0:5]={[round(v,4) for v in mu_fake_np[:5]]}")
    print(f"    μ*[0:5]    ={[round(v,4) for v in mu_star_np[:5]]}")
    print(f"    === 浮点 KKT 逐条诊断 ===")
    print(f"    (a) 原始可行：{primal_ok}  min_slack={primal_slack_min:.2e}")
    print(f"    (b) 对偶可行：{dual_ok}   min(μ_fake)={dual_min:.2e}")
    print(f"    (c) 平稳性  ：‖残差‖={stat_float:.4f}  "
          f"（vs y*残差={stat_float_star:.4f}，KKT_TOL/SCALE={10000/MASK_SCALE:.4f}）")
    print(f"    (d) 互补松弛：max|μ·slack|={compl_float:.4f}  "
          f"（KKT_TOL/SCALE²={10000/MASK_SCALE**2:.2e}）")

    # ── 5. 换算为 scaled 整数（与链码一致）──
    y_L2_sc    = [int(round(v * MASK_SCALE)) for v in y_L2_np]
    mu_fake_sc = [int(round(v * MASK_SCALE)) for v in mu_fake_np]

    # scaled KKT 检验（与 Verifier 链上逻辑完全一致）
    passed_sc, stat_sc, compl_sc = check_kkt_scaled(
        h_m_sc, c_real_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_L2_sc, mu_fake_sc)
    print(f"    === Scaled KKT（链上逻辑）===")
    print(f"    passed={passed_sc}  stat_res={stat_sc}  compl_res={compl_sc}")
    print(f"    超阈倍数：stat={stat_sc/10000:.1f}×  compl={compl_sc/10000:.1f}×")

    # ── 6. 链上 Assign（如未做）──
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

    # ── 7. 提交 (y_L2, μ_fake) ──
    fc.invoke(caller_org=solver_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_L2_sc), json.dumps(mu_fake_sc)],
              wait_seconds=1.5)
    print(f"    → SubmitMaskedSolution(y_L2, μ_fake) 已上链")

    # ── 8. Verifier 链上下载，用真实 λ/z 重建 c_masked，运行 KKT ──
    msol = fc.query_with_retry(verifier_id, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    y_dl     = msol["yScaled"]
    mu_dl    = msol["muScaled"]
    sol_hash = msol["solutionHash"]

    _, c_verify = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    c_verify_sc = [int(round(v * MASK_SCALE)) for v in c_verify]

    passed_chain, stat_chain, compl_chain = check_kkt_scaled(
        h_m_sc, c_verify_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_dl, mu_dl)

    print(f"    → Verifier({verifier_id}) 链上 KKT：")
    print(f"      passed={passed_chain}  stat_res={stat_chain}  compl_res={compl_chain}")
    print(f"      超阈：stat={stat_chain/10000:.1f}×  compl={compl_chain/10000:.1f}×")

    # ── 9. RecordVerdict ──
    verdict_str = "true" if passed_chain else "false"
    fc.invoke(caller_org=verifier_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, verdict_str,
                    str(stat_chain), str(compl_chain), sol_hash],
              wait_seconds=1.5)
    print(f"    → RecordVerdict(pass={verdict_str}) 已上链")
    credit_after = query_solve_credit(solver_id)
    print(f"    → SOLVE_CREDIT[{solver_id}] 当前={credit_after}")

    return {
        "iter":               k,
        "attack_level":       "L3",
        "attack_type":        "wrong_dual_nnls_variantB",
        "park":               park,
        "solver":             solver_id,
        "verifier":           verifier_id,
        "k0":                 len(y_L2),
        "rho":                rho,
        # ADMM 状态
        "lam_block_norm":     round(float(np.linalg.norm(lam_block_real)), 4),
        "z_block_norm":       round(float(np.linalg.norm(z_block_real)), 4),
        # 解向量对比
        "y_star_0":           round(float(y_star[0]), 6),
        "y_star_norm":        round(float(np.linalg.norm(y_star_np)), 6),
        "y_L2_0":             round(float(y_L2[0]), 6),
        "y_L2_norm":          round(float(np.linalg.norm(y_L2_np)), 6),
        "delta_y_norm":       round(float(np.linalg.norm(y_L2_np - y_star_np)), 6),
        "relative_err":       round(float(np.linalg.norm(y_L2_np - y_star_np)
                                          / np.linalg.norm(y_star_np)), 6),
        "n_primal_viol":      n_viol_L2,
        # 对偶变量对比
        "mu_star_nnz":        int((mu_star_np > 1e-8).sum()),
        "mu_fake_nnz":        int((mu_fake_np > 1e-8).sum()),
        "mu_fake_min":        round(float(mu_fake_np.min()), 8),
        "nnls_residual":      round(float(nnls_residual), 6),
        # 浮点 KKT 逐条诊断
        "stat_float_star":    round(stat_float_star, 6),
        "stat_float_fake":    round(stat_float, 6),
        "compl_float_fake":   round(compl_float, 6),
        "primal_ok":          primal_ok,
        "dual_ok":            dual_ok,
        # 基线
        "baseline_stat":      stat_ref,
        "baseline_compl":     compl_ref,
        "baseline_pass":      pass_ref,
        # 链上结果
        "kkt_pass":           passed_chain,
        "stat_res":           stat_chain,
        "compl_res":          compl_chain,
        "stat_over_tol":      round(stat_chain / 10000, 2),
        "compl_over_tol":     round(compl_chain / 10000, 2),
        "solution_hash":      sol_hash,
        "solve_credit_after": credit_after,
    }


# ── 单轮实验 ──────────────────────────────────────────────────────────────────
def run_l3_trial(bundles, mask_float, g_masked_float, h_vec_float, max_iters=60):
    date_str = (f"9994-M7E-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-L3B-{uuid.uuid4().hex[:4]}")
    sid = setup(date_str, max_iter=max_iters)
    print(f"\n{'='*65}")
    print(f"[TRIAL] L3-B 错误对偶攻击  scheduleID={sid}")
    print(f"  隔离前缀: 9994-M7E-（不复用 M7A/B/C/D）")
    print(f"{'='*65}")

    print("\n[注册] 上传三个园区的 masked 结构")
    for i, p in enumerate(ALL_3, start=1):
        upload_large_mask(sid, p, bundles[p])
        print(f"  -> {p} 注册完成 (k0={bundles[p]['k0']})")

    credits_init = {p: query_solve_credit(p) for p in ALL_3}
    print(f"\n[基线] SOLVE_CREDIT: {credits_init}")

    normal_rows   = []
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
            print(f"  [!] 触发 L3-B 攻击，SOLVE_CREDIT 前: {credit_before}")

            res = solve_park1_L3B_attack(
                k, mask_float, g_masked_float, h_vec_float,
                bundles, lam_e, lam_g, lam_h, lam_c,
                z_e, z_g, z_h, z_c, rho, sid, already_assigned=True)
            res["credit_before"]    = credit_before
            res["credit_after_all"] = {p: query_solve_credit(p) for p in ALL_3}
            res["scheduleID"]       = sid
            res["credits_init"]     = credits_init
            attack_result = res

            for p in ["Park2", "Park3"]:
                r = solve_park_normal(
                    p, k, mask_float, g_masked_float, h_vec_float, bundles,
                    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid)
                normal_rows.append({"iter": k, **r})

            print(f"\n  [验证] 尝试 CoordinateStep({k})...")
            try:
                fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                          contract=CONTRACT_ADMM, function="CoordinateStep",
                          args=[sid, str(k)], wait_seconds=2.0)
                print(f"  [!] 意外成功")
                attack_result["coord_step_blocked"] = False
            except Exception as coord_err:
                err_str = str(coord_err)[:300]
                blocked = "missing" in err_str.lower()
                print(f"  -> CoordinateStep 被拒绝 ({'✓ 符合预期' if blocked else '?'})")
                print(f"     {err_str[:200]}")
                attack_result["coord_step_blocked"] = blocked
                attack_result["coord_error"]        = err_str

            try:
                fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                    [sid, str(k), "Park1"], attempts=3, delay_seconds=1.0)
                attack_result["iter_record_exists"] = True
            except Exception:
                attack_result["iter_record_exists"] = False
                print(f"  [✓] IterationRecord(iter={k}, Park1): 不存在")

            print(f"\n  [退出] 攻击完成，退出迭代")
            break

        else:
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

    return attack_result, normal_rows


# ── 主函数 ─────────────────────────────────────────────────────────────────────
def run():
    print("\n[预热] 三个园区容器")
    for p in ALL_3:
        ready, msg = preheat_org(p)
        print(("  OK  " if ready else "  FAIL ") + msg)
        if not ready:
            raise RuntimeError(f"{p} 未就绪")

    bundles        = {}
    mask_float     = {}
    g_masked_float = {}
    h_vec_float    = {}
    for i, p in enumerate(ALL_3, start=1):
        b = load_bundle(i)
        bundles[p]        = b
        mask_float[p]     = to_float_mask(b)
        g_masked_float[p] = [[v/MASK_SCALE for v in row] for row in b["gMaskedScaled"]]
        h_vec_float[p]    = [v/MASK_SCALE  for v in b["hVecScaled"]]

    res, normal_rows = run_l3_trial(bundles, mask_float, g_masked_float, h_vec_float)

    print(f"\n[保存数据]")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    path_json = f"{OUT_DIR}/L3_raw_{ts}.json"
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({
            "experiment":    "EXP-1-L3B-WrongDual-NNLS",
            "attack_type":   "y_L2_feasible_nonopt_mu_nnls_fake",
            "kkt_tol":       10000,
            "mask_scale":    MASK_SCALE,
            "attack_result": res,
            "normal_iters":  len(normal_rows),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"  原始 JSON: {path_json}")

    if res:
        df = pd.DataFrame([res])
        path_xlsx = f"{OUT_DIR}/L3_attack_result_{ts}.xlsx"
        df.to_excel(path_xlsx, index=False)
        print(f"  攻击结果: {path_xlsx}")

    df_norm   = pd.DataFrame(normal_rows)
    path_norm = f"{OUT_DIR}/L3_normal_rounds_{ts}.xlsx"
    df_norm.to_excel(path_norm, index=False)
    print(f"  正常轮数据: {path_norm}")

    if res:
        print(f"\n{'='*65}")
        print(f"EXP-1 L3-B 错误对偶攻击汇总")
        print(f"{'='*65}")
        print(f"  scheduleID  : {res.get('scheduleID','?')}")
        print(f"  触发轮次    : iter={res['iter']}  rho={res['rho']}")
        print(f"  y_L2 vs y*  : ‖Δy‖={res['delta_y_norm']:.4f}  "
              f"相对误差={res['relative_err']*100:.2f}%")
        print(f"  原始可行    : 违约数={res['n_primal_viol']}")
        print(f"  NNLS 残差   : {res['nnls_residual']:.4f}  "
              f"（μ_fake 非零数={res['mu_fake_nnz']}  μ* 非零数={res['mu_star_nnz']}）")
        print(f"  浮点平稳性  : y*残差={res['stat_float_star']:.4f}  "
              f"μ_fake残差={res['stat_float_fake']:.4f}")
        print(f"  浮点互补松弛: {res['compl_float_fake']:.4f}")
        print(f"  基线 stat   : {res['baseline_stat']}")
        print(f"  链上 stat   : {res['stat_res']}  超阈 {res['stat_over_tol']}×")
        print(f"  链上 compl  : {res['compl_res']}  超阈 {res['compl_over_tol']}×")
        print(f"  KKT 裁决    : {'PASS' if res['kkt_pass'] else 'FAIL'}")
        print(f"  CoordStep   : {'阻断' if res.get('coord_step_blocked') else '通过'}")
        bef = res.get('credit_before',{}).get(ATTACK_SOLVER,'?')
        aft = res.get('credit_after_all',{}).get(ATTACK_SOLVER,'?')
        print(f"  信用变化    : {ATTACK_SOLVER} {bef}→{aft}")
        print(f"{'='*65}")

    return res


if __name__ == "__main__":
    run()
