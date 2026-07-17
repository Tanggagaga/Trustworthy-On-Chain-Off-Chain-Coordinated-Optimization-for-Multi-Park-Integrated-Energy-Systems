"""
EXP-1 Level-4：互补松弛定向违反攻击
======================================
两个子实验在同一脚本内依次运行，各用独立 scheduleID（前缀 9994-M7F-）：

L4-A：零对偶攻击（μ = 0）
    提交 (y_L2, μ=0)
    CS 恒成立（0×任意=0），对偶可行恒通过，但平稳性暴露 y_L2 的非协调性。
    攻击意图：通过"归零对偶"彻底规避 CS 检测。
    预期：stationarity FAIL（stat_res 与 L2 量级相同），CS PASS。

L4-B：活跃集截断对偶攻击
    1. 计算 y_L2 的活跃约束集 A = {j : (G_m y_L2 - h_m)_j > -ε}
    2. 仅在活跃行上做 NNLS：min ‖G_{m,A}^T μ_A - rhs_A‖² s.t. μ_A ≥ 0
    3. 其余分量置零，构造 μ_CS（保证 CS 完全满足）
    4. 提交 (y_L2, μ_CS)
    攻击意图：知道 L3-B 被 CS 拦截后，精确修补 CS；
              但仅凭活跃行无法满足完整平稳性方程。
    预期：CS PASS，stationarity FAIL。

scheduleID 前缀：9994-M7F-（严格隔离，不复用 M7A~M7E）
输出目录：/mnt/c/Users/Lenovo/Desktop/daima/综合能源/作恶MaskKKT专属/L4/
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

# ── 常量 ─────────────────────────────────────────────────────────────────────
ATTACK_PARK      = "Park1"
ATTACK_SOLVER    = "Park2"
ATTACK_FROM_ITER = 10

ALL_3      = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/M1a离线Mask验证/M6完整闭环"
OUT_DIR    = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/作恶MaskKKT专属/L4"
CHUNK_CHARS = 50_000
ACTIVE_TOL  = 1e-4   # 活跃约束判定阈值（scaled 单位）

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
    assert passed, f"正常路径 KKT 意外失败: {park} iter={k} stat={stat_res}"

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


# ── L2 解构造（共用）─────────────────────────────────────────────────────────
def build_y_L2(park, k, mask_float, g_masked_float, h_vec_float, bundles,
               lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho):
    idx = PARK_IDS.index(park)
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0]*24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0]*24
    lam_block_real = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block_real   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    # 真实最优 y*
    h_real, c_real = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    y_star, mu_star = solve_masked_qp(h_real, c_real,
                                       g_masked_float[park], h_vec_float[park])
    # y_L2：λ=0，z=0
    n = len(lam_block_real)
    h_L2, c_L2 = build_masked_round(mask_float[park], rho, [0.0]*n, [0.0]*n)
    y_L2, _    = solve_masked_qp(h_L2, c_L2, g_masked_float[park], h_vec_float[park])

    bundle  = bundles[park]
    H_m     = np.array([[v * rho / MASK_SCALE for v in row] for row in bundle["hE0Scaled"]])
    G_m     = np.array([[v / MASK_SCALE       for v in row] for row in bundle["gMaskedScaled"]])
    h_m     = np.array([v / MASK_SCALE        for v in bundle["hVecScaled"]])
    c_real_np = np.array(c_real)

    return (y_star, mu_star, y_L2, H_m, G_m, h_m, c_real_np,
            lam_block_real, z_block_real)


# ── 攻击核心：链上提交+验证 ───────────────────────────────────────────────────
def submit_and_verify(label, k, park, y_atk, mu_atk,
                      bundles, mask_float, rho,
                      lam_block_real, z_block_real,
                      sid, already_assigned=False):
    """通用：提交 (y_atk, mu_atk) 到链上，Verifier 用真实 λ/z 复算 KKT"""
    bundle    = bundles[park]
    h_m_sc    = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    _, c_verify = build_masked_round(mask_float[park], rho, lam_block_real, z_block_real)
    c_verify_sc = [int(round(v * MASK_SCALE)) for v in c_verify]

    y_sc  = [int(round(v * MASK_SCALE)) for v in y_atk]
    mu_sc = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_atk]

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
        f"[{label}] solver={solver_id} ≠ {ATTACK_SOLVER}，跳过"

    fc.invoke(caller_org=solver_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_sc), json.dumps(mu_sc)],
              wait_seconds=1.5)

    msol = fc.query_with_retry(verifier_id, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    y_dl, mu_dl, sol_hash = msol["yScaled"], msol["muScaled"], msol["solutionHash"]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_verify_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_dl, mu_dl)

    verdict_str = "true" if passed else "false"
    fc.invoke(caller_org=verifier_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, verdict_str,
                    str(stat_res), str(compl_res), sol_hash],
              wait_seconds=1.5)

    credit = query_solve_credit(solver_id)
    print(f"    [{label}] solver={solver_id} verifier={verifier_id}")
    print(f"      stat_res={stat_res}  compl_res={compl_res}  passed={passed}")
    print(f"      RecordVerdict({verdict_str})  CREDIT[{solver_id}]={credit}")
    return solver_id, verifier_id, passed, stat_res, compl_res, sol_hash, credit


# ── L4-A：零对偶攻击 ──────────────────────────────────────────────────────────
def run_l4a_attack(k, mask_float, g_masked_float, h_vec_float, bundles,
                   lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
                   already_assigned=False):
    park = ATTACK_PARK
    (y_star, mu_star, y_L2, H_m, G_m, h_m, c_real_np,
     lam_block_real, z_block_real) = build_y_L2(
        park, k, mask_float, g_masked_float, h_vec_float, bundles,
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho)

    y_L2_np = np.array(y_L2)
    mu_zero = np.zeros(len(h_m))

    # 浮点诊断
    slack       = h_m - G_m @ y_L2_np
    stat_float  = float(np.linalg.norm(H_m @ y_L2_np + c_real_np))  # μ=0
    compl_float = 0.0  # 0*任意=0

    print(f"\n  [L4-A ATTACK] iter={k} μ=0 攻击")
    print(f"    y_L2 原始可行：违约数={int((slack<-1e-6).sum())}  min_slack={float(slack.min()):.2e}")
    print(f"    浮点平稳性‖H y+c‖={stat_float:.4f}  浮点CS=0（恒成立）")

    solver_id, verifier_id, passed, stat_res, compl_res, sol_hash, credit = \
        submit_and_verify("L4-A", k, park, y_L2_np, mu_zero,
                          bundles, mask_float, rho,
                          lam_block_real, z_block_real, sid,
                          already_assigned=already_assigned)

    return {
        "sub_exp":            "L4-A",
        "attack_type":        "zero_dual_mu0",
        "iter":               k, "rho": rho,
        "park":               park, "solver": solver_id, "verifier": verifier_id,
        "k0":                 len(y_L2), "q_rows": len(h_m),
        "n_primal_viol":      int((slack < -1e-6).sum()),
        "min_slack":          round(float(slack.min()), 8),
        "delta_y_norm":       round(float(np.linalg.norm(y_L2_np - np.array(y_star))), 4),
        "relative_err":       round(float(np.linalg.norm(y_L2_np - np.array(y_star))
                                          / np.linalg.norm(np.array(y_star))), 4),
        "stat_float":         round(stat_float, 4),
        "compl_float":        0.0,
        "mu_nnz":             0,
        "kkt_pass":           passed,
        "stat_res":           stat_res,
        "compl_res":          compl_res,
        "stat_over_tol":      round(stat_res / 10000, 2),
        "compl_over_tol":     round(compl_res / 10000, 2),
        "solution_hash":      sol_hash,
        "solve_credit_after": credit,
    }


# ── L4-B：活跃集截断对偶攻击 ─────────────────────────────────────────────────
def run_l4b_attack(k, mask_float, g_masked_float, h_vec_float, bundles,
                   lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
                   already_assigned=False):
    park = ATTACK_PARK
    (y_star, mu_star, y_L2, H_m, G_m, h_m, c_real_np,
     lam_block_real, z_block_real) = build_y_L2(
        park, k, mask_float, g_masked_float, h_vec_float, bundles,
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho)

    y_L2_np = np.array(y_L2)

    # 活跃约束集：(G_m y_L2 - h_m)_j > -ACTIVE_TOL  即 slack < ACTIVE_TOL
    slack        = h_m - G_m @ y_L2_np             # slack_j = h_j - G_j y
    active_mask  = (slack < ACTIVE_TOL)             # True 为活跃
    active_idx   = np.where(active_mask)[0]
    n_active     = int(active_mask.sum())
    n_inactive   = int((~active_mask).sum())

    print(f"\n  [L4-B ATTACK] iter={k} 活跃集截断对偶攻击")
    print(f"    y_L2 活跃约束数={n_active}  非活跃={n_inactive}  总={len(h_m)}")

    # 仅在活跃行上做 NNLS
    rhs_stat = -(H_m @ y_L2_np + c_real_np)        # 完整平稳性 rhs，shape=(k0,)
    G_active = G_m[active_idx, :]                   # shape=(n_active, k0)
    # G_active^T μ_A = rhs_stat  →  NNLS on (G_active^T, rhs_stat)
    if n_active > 0:
        mu_active, nnls_res_active = nnls(G_active.T, rhs_stat)
    else:
        mu_active = np.zeros(0)
        nnls_res_active = float(np.linalg.norm(rhs_stat))

    # 构造 mu_CS：活跃处放 μ_A，其余置零
    mu_CS = np.zeros(len(h_m))
    if n_active > 0:
        mu_CS[active_idx] = mu_active

    # 浮点诊断
    stat_vec    = H_m @ y_L2_np + c_real_np + G_m.T @ mu_CS
    stat_float  = float(np.linalg.norm(stat_vec))
    compl_vec   = mu_CS * (G_m @ y_L2_np - h_m)
    compl_float = float(np.max(np.abs(compl_vec)))   # 应≈0

    # 参考：L3-B 全量 NNLS 残差（对比）
    mu_full, nnls_res_full = nnls(G_m.T, rhs_stat)
    stat_full = float(np.linalg.norm(H_m @ y_L2_np + c_real_np + G_m.T @ mu_full))

    print(f"    活跃行 NNLS 残差={nnls_res_active:.4f}  全量 NNLS 残差=0.0000（L3-B）")
    print(f"    浮点平稳性‖残差‖={stat_float:.4f}  浮点CS={compl_float:.6f}（应≈0）")
    print(f"    mu_CS 非零数={int((mu_CS>1e-8).sum())}  mu_CS[0:5]={[round(v,4) for v in mu_CS[:5]]}")

    solver_id, verifier_id, passed, stat_res, compl_res, sol_hash, credit = \
        submit_and_verify("L4-B", k, park, y_L2_np, mu_CS,
                          bundles, mask_float, rho,
                          lam_block_real, z_block_real, sid,
                          already_assigned=already_assigned)

    return {
        "sub_exp":              "L4-B",
        "attack_type":          "active_set_truncated_dual",
        "iter":                 k, "rho": rho,
        "park":                 park, "solver": solver_id, "verifier": verifier_id,
        "k0":                   len(y_L2), "q_rows": len(h_m),
        "n_active":             n_active,
        "n_inactive":           n_inactive,
        "n_primal_viol":        int((slack < -1e-6).sum()),
        "delta_y_norm":         round(float(np.linalg.norm(y_L2_np - np.array(y_star))), 4),
        "relative_err":         round(float(np.linalg.norm(y_L2_np - np.array(y_star))
                                             / np.linalg.norm(np.array(y_star))), 4),
        "nnls_active_residual": round(float(nnls_res_active), 6),
        "nnls_full_residual":   round(float(nnls_res_full), 6),
        "stat_float_cs":        round(stat_float, 4),
        "stat_float_full_nnls": round(stat_full, 6),
        "compl_float_cs":       round(compl_float, 8),
        "mu_cs_nnz":            int((mu_CS > 1e-8).sum()),
        "kkt_pass":             passed,
        "stat_res":             stat_res,
        "compl_res":            compl_res,
        "stat_over_tol":        round(stat_res / 10000, 2),
        "compl_over_tol":       round(compl_res / 10000, 2),
        "solution_hash":        sol_hash,
        "solve_credit_after":   credit,
    }


# ── 单轮迭代驱动（两个攻击共用）──────────────────────────────────────────────
def run_trial(label, attack_fn, bundles, mask_float, g_masked_float, h_vec_float,
              max_iters=60):
    ts_tag   = datetime.now().strftime('%Y%m%d-%H%M%S')
    uid      = uuid.uuid4().hex[:4]
    date_str = f"9994-M7F-{ts_tag}-{label}-{uid}"
    sid      = setup(date_str, max_iter=max_iters)

    print(f"\n{'='*65}")
    print(f"[TRIAL] {label}  scheduleID={sid}")
    print(f"  隔离前缀: 9994-M7F-（不复用 M7A~M7E）")
    print(f"{'='*65}")

    for i, p in enumerate(ALL_3, start=1):
        upload_large_mask(sid, p, bundles[p])
        print(f"  -> {p} 注册完成")

    credits_init = {p: query_solve_credit(p) for p in ALL_3}
    print(f"[基线] SOLVE_CREDIT: {credits_init}")

    normal_rows   = []
    attack_result = None

    for k in range(max_iters):
        print(f"\n--- iter {k} ---")
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
        print(f"  rho={rho:.4f}")

        fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                  contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                  args=[sid, str(k), "Park1"], wait_seconds=1.0)
        pre = fc.query_with_retry("Park1", CONTRACT_MASK,
                                  "QuerySolverAssignment", [sid, str(k), "Park1"])
        park1_solver = pre["solverParkId"]
        print(f"  Park1 solver={park1_solver}")

        trigger = (k >= ATTACK_FROM_ITER and park1_solver == ATTACK_SOLVER)

        if trigger:
            credit_before = {p: query_solve_credit(p) for p in ALL_3}
            print(f"  [!] 触发 {label}  SOLVE_CREDIT 前: {credit_before}")

            res = attack_fn(
                k, mask_float, g_masked_float, h_vec_float,
                bundles, lam_e, lam_g, lam_h, lam_c,
                z_e, z_g, z_h, z_c, rho, sid, already_assigned=True)
            res["scheduleID"]       = sid
            res["credits_init"]     = credits_init
            res["credit_before"]    = credit_before
            res["credit_after_all"] = {p: query_solve_credit(p) for p in ALL_3}
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
                attack_result["coord_step_blocked"] = False
                print(f"  [!] CoordinateStep 意外通过")
            except Exception as e:
                err_str = str(e)[:300]
                blocked = "missing" in err_str.lower()
                attack_result["coord_step_blocked"] = blocked
                attack_result["coord_error"]        = err_str
                print(f"  -> CoordStep 被拒绝 ({'✓ 预期' if blocked else '?'}): {err_str[:150]}")

            try:
                fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                    [sid, str(k), "Park1"], attempts=3, delay_seconds=1.0)
                attack_result["iter_record_exists"] = True
            except Exception:
                attack_result["iter_record_exists"] = False

            print(f"\n  [退出] {label} 完成，退出迭代")
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
                print(f"  [warn] CoordStep 失败: {e}")
                continue
            admm = pa.query_admm_state(sid)
            print(f"  status={admm.get('status')} primalSq={admm.get('primalResidSq',0)}")

    return attack_result, normal_rows


# ── 主函数 ────────────────────────────────────────────────────────────────────
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

    all_results  = []
    all_normals  = []

    # ── 子实验 L4-A ──
    res_a, norm_a = run_trial(
        "L4-A-ZeroDual",
        lambda k, mf, gf, hf, bd, le, lg, lh, lc, ze, zg, zh, zc, rho, sid, already_assigned=False:
            run_l4a_attack(k, mf, gf, hf, bd, le, lg, lh, lc, ze, zg, zh, zc, rho, sid, already_assigned),
        bundles, mask_float, g_masked_float, h_vec_float)
    all_results.append(res_a)
    all_normals.extend(norm_a)

    # ── 子实验 L4-B ──
    res_b, norm_b = run_trial(
        "L4-B-ActiveSetTrunc",
        lambda k, mf, gf, hf, bd, le, lg, lh, lc, ze, zg, zh, zc, rho, sid, already_assigned=False:
            run_l4b_attack(k, mf, gf, hf, bd, le, lg, lh, lc, ze, zg, zh, zc, rho, sid, already_assigned),
        bundles, mask_float, g_masked_float, h_vec_float)
    all_results.append(res_b)
    all_normals.extend(norm_b)

    # ── 保存 ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path_json = f"{OUT_DIR}/L4_raw_{ts}.json"
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({
            "experiment":  "EXP-1-L4-CSViolation",
            "sub_exps":    ["L4-A: zero dual", "L4-B: active set truncated dual"],
            "kkt_tol":     10000,
            "mask_scale":  MASK_SCALE,
            "results":     all_results,
            "normal_iters": len(all_normals),
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  原始 JSON: {path_json}")

    df_atk = pd.DataFrame([r for r in all_results if r])
    df_atk.to_excel(f"{OUT_DIR}/L4_attack_results_{ts}.xlsx", index=False)
    pd.DataFrame(all_normals).to_excel(f"{OUT_DIR}/L4_normal_rounds_{ts}.xlsx", index=False)
    print(f"  攻击结果 XLSX 和正常轮 XLSX 已保存至 {OUT_DIR}")

    # ── 汇总打印 ──
    print(f"\n{'='*65}")
    print(f"EXP-1 L4 互补松弛定向违反攻击汇总")
    print(f"{'='*65}")
    for res in all_results:
        if not res:
            continue
        print(f"\n  [{res['sub_exp']}]  scheduleID={res.get('scheduleID','?')}")
        print(f"    触发轮次  : iter={res['iter']}  rho={res['rho']}")
        print(f"    ‖Δy‖      : {res['delta_y_norm']}  相对={res['relative_err']*100:.2f}%")
        print(f"    stat_res  : {res['stat_res']}  超阈 {res['stat_over_tol']}×")
        print(f"    compl_res : {res['compl_res']}  超阈 {res['compl_over_tol']}×")
        print(f"    KKT 裁决  : {'PASS' if res['kkt_pass'] else 'FAIL'}")
        print(f"    CoordStep : {'阻断' if res.get('coord_step_blocked') else '通过'}")
    print(f"{'='*65}")

    return all_results


if __name__ == "__main__":
    run()
