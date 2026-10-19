"""
EXP-1 Level-1：小扰动敏感度测试
==============================
目的：验证 KKT 阈值对"看起来接近最优但实际偏离"的解的检测能力。
      分三档噪声（1% / 3% / 5%）依次在链上提交，记录每档的 stat_res / compl_res，
      测量 KKT 拦截的灵敏度边界。

攻击方法：
    y_L1 = y* + noise_pct × |y*| × sign(rand)   (逐分量相对噪声)
    μ 保持 μ*（正确对偶，使 stat 残差只来自原始变量误差）

链上流程与 EXP-1 完全一致：
    AssignSolverAndVerifier → SubmitMaskedSolution(y_L1, μ*)
    → Verifier 链上下载 → check_kkt_scaled → RecordVerdict(pass=False)
    → SOLVE_CREDIT[Park2] -10 / CoordinateStep 被阻断

scheduleID 前缀：9994-M7C-（严格隔离，不得复用 9994-M7A- / 9994-M7B-）
输出目录：results/L1/
"""

import json, math, os, sys, time, uuid, random
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, CONTRACT_AUDIT, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup           # signature: setup(date_str, max_iter) -> sid
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

# ── 实验常量 ──────────────────────────────────────────────────────────────────
ATTACK_PARK      = "Park1"
ATTACK_SOLVER    = "Park2"
ATTACK_FROM_ITER = 10          # 稳定阶段才触发，避免早期收敛前噪声掩盖
NOISE_LEVELS     = [0.01, 0.03, 0.05]   # 三档：1% / 3% / 5%
RNG_SEED         = 42

ALL_3      = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "results", "L1"))
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

# ── 正常子问题求解 ─────────────────────────────────────────────────────────────
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


# ── L1 攻击：小扰动 ───────────────────────────────────────────────────────────
def solve_park1_noise_attack(k, noise_pct, mask_float, g_masked_float, h_vec_float,
                              bundles, lam_e, lam_g, lam_h, lam_c,
                              z_e, z_g, z_h, z_c, rho, sid, rng,
                              already_assigned=False):
    """
    Level-1 攻击：y_L1 = y* + noise_pct × |y*| × sign(rand)
    μ 保持最优 μ*，使 stat 残差纯粹来自原始变量扰动。
    全程在链上执行：Submit → Verifier下载 → KKT → RecordVerdict(pass=False)。
    """
    park = ATTACK_PARK
    idx  = PARK_IDS.index(park)
    lam_block = lam_e[idx] + lam_g[idx] + lam_h[idx] + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h[idx]   + z_c[idx]
    bundle    = bundles[park]

    # ── 1. Solver(Park2) 本地求出真实最优解 ──
    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star    = solve_masked_qp(h_masked, c_masked,
                                          g_masked_float[park], h_vec_float[park])
    k0 = len(y_star)

    # 先验证最优解确实通过 KKT（基线核查）
    y_star_sc = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_star_sc = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    h_m_sc = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_m_sc = [int(round(v * MASK_SCALE)) for v in c_masked]
    pass_ref, stat_ref, compl_ref = check_kkt_scaled(
        h_m_sc, c_m_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_star_sc, mu_star_sc)
    assert pass_ref, f"基线 KKT 失败 stat={stat_ref} compl={compl_ref}"

    # ── 2. 构造噪声扰动 y_L1 ──
    y_arr   = np.array(y_star)
    noise   = rng.choice([-1.0, 1.0], size=k0) * noise_pct * np.abs(y_arr)
    y_noisy = y_arr + noise

    # ── 3. 记录扰动统计（链下，供论文使用）──
    y_delta_norm = float(np.linalg.norm(noise))
    y_star_norm  = float(np.linalg.norm(y_arr))
    relative_err = y_delta_norm / (y_star_norm + 1e-12)

    y_noisy_sc = [int(round(v * MASK_SCALE)) for v in y_noisy]
    # μ 保持最优（不扰动对偶）
    mu_sc_used = mu_star_sc

    print(f"\n  [L1-ATTACK {noise_pct*100:.0f}%] iter={k} {park} Solver={ATTACK_SOLVER}")
    print(f"    k0={k0}, ‖Δy‖={y_delta_norm:.6f}, ‖y*‖={y_star_norm:.4f}, 相对误差={relative_err:.4%}")
    print(f"    y*[0]={y_arr[0]:.6f}  y_noisy[0]={y_noisy[0]:.6f}  Δy[0]={noise[0]:.6f}")
    print(f"    基线 (y*,μ*): stat_ref={stat_ref}, compl_ref={compl_ref}")

    # ── 4. AssignSolverAndVerifier ──
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
        f"本轮 solver={solver_id} ≠ Park2，跳过攻击（等下一轮）"

    # ── 5. Solver(Park2) 在链上提交 y_noisy ──
    fc.invoke(caller_org=solver_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_noisy_sc), json.dumps(mu_sc_used)],
              wait_seconds=1.5)
    print(f"    → SubmitMaskedSolution(y_noisy) 已上链，solutionHash 固化")

    # ── 6. Verifier 从链上独立下载 y_noisy，重建参数，运行 KKT ──
    msol = fc.query_with_retry(verifier_id, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    y_dl   = msol["yScaled"]
    mu_dl  = msol["muScaled"]
    sol_hash = msol["solutionHash"]

    _, c_masked_v = build_masked_round(mask_float[park], rho, lam_block, z_block)
    c_m_sc_v = [int(round(v * MASK_SCALE)) for v in c_masked_v]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_m_sc_v, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_dl, mu_dl)

    print(f"    → Verifier({verifier_id}) 链上 KKT 检验：")
    print(f"      passed={passed}  stat_res={stat_res}  compl_res={compl_res}")
    print(f"      阈值 KKT_TOL=10000  超出倍数={stat_res/10000:.1f}×")

    # ── 7. RecordVerdict（无论 pass/fail 均如实提交，记录真实结果）──
    verdict_str = "true" if passed else "false"
    fc.invoke(caller_org=verifier_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, verdict_str,
                    str(stat_res), str(compl_res), sol_hash],
              wait_seconds=1.5)
    print(f"    → RecordVerdict(pass={verdict_str}) 已上链")

    if not passed:
        credit_after = query_solve_credit(solver_id)
        print(f"    → SOLVE_CREDIT[{solver_id}] 当前={credit_after}")
    else:
        credit_after = query_solve_credit(solver_id)
        print(f"    !! 注意：{noise_pct*100:.0f}% 噪声通过了 KKT 检验（接近阈值边界）")

    return {
        "iter":              k,
        "attack_level":      "L1",
        "noise_pct":         noise_pct,
        "noise_pct_str":     f"{noise_pct*100:.0f}%",
        "park":              park,
        "solver":            solver_id,
        "verifier":          verifier_id,
        "k0":                k0,
        # 扰动统计
        "y_star_norm":       round(y_star_norm, 6),
        "delta_y_norm":      round(y_delta_norm, 6),
        "relative_err":      round(relative_err, 6),
        "y_star_0":          round(float(y_arr[0]),   6),
        "y_noisy_0":         round(float(y_noisy[0]), 6),
        "delta_y_0":         round(float(noise[0]),   6),
        # 基线 KKT（y* 应通过）
        "baseline_stat":     stat_ref,
        "baseline_compl":    compl_ref,
        "baseline_pass":     pass_ref,
        # 攻击 KKT 结果（链上真实数据）
        "kkt_pass":          passed,
        "stat_res":          stat_res,
        "compl_res":         compl_res,
        "stat_over_tol":     round(stat_res / 10000, 2),
        "solution_hash":     sol_hash,
        "solve_credit_after": credit_after,
        "rho":               rho,
    }


# ── 单噪声档实验（独立 scheduleID）─────────────────────────────────────────────
def run_one_noise_level(noise_pct: float, rng, bundles, mask_float,
                         g_masked_float, h_vec_float, max_iters: int = 60):
    """
    每档噪声用独立 scheduleID，攻击完成后立即 break。
    攻击后 ADMM 状态被"毒化"（Park1 IterRecord 缺失），无法继续迭代，
    因此隔离为独立 trial 是唯一正确的设计。
    """
    noise_tag = f"{noise_pct*100:.0f}pct"
    date_str = f"9994-M7C-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{noise_tag}-{uuid.uuid4().hex[:4]}"
    sid = setup(date_str, max_iter=max_iters)
    print(f"\n{'='*65}")
    print(f"[TRIAL] noise={noise_pct*100:.0f}%  scheduleID={sid}")
    print(f"{'='*65}")
    print(f"  隔离前缀: 9994-M7C-（不复用 M7A/M7B）")

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

        # 先调用 AssignSolverAndVerifier（幂等），再查 Park1 的分配结果
        fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                  contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                  args=[sid, str(k), "Park1"], wait_seconds=1.0)
        pre_assign = fc.query_with_retry(
            "Park1", CONTRACT_MASK, "QuerySolverAssignment",
            [sid, str(k), "Park1"])
        park1_solver = pre_assign["solverParkId"]
        print(f"  Park1 solver={park1_solver}")

        trigger = (k >= ATTACK_FROM_ITER and park1_solver == ATTACK_SOLVER)

        if trigger:
            credit_before = {p: query_solve_credit(p) for p in ALL_3}
            print(f"  [!] 触发 L1 攻击 noise={noise_pct*100:.0f}%，SOLVE_CREDIT 前: {credit_before}")

            res = solve_park1_noise_attack(
                k, noise_pct, mask_float, g_masked_float, h_vec_float,
                bundles, lam_e, lam_g, lam_h, lam_c,
                z_e, z_g, z_h, z_c, rho, sid, rng,
                already_assigned=True)
            res["credit_before"]    = credit_before
            res["credit_after_all"] = {p: query_solve_credit(p) for p in ALL_3}
            res["scheduleID"]       = sid
            res["credits_init"]     = credits_init
            attack_result = res
            print(f"  [攻击记录] noise={noise_pct*100:.0f}% "
                  f"stat_res={res['stat_res']} compl_res={res['compl_res']} "
                  f"pass={res['kkt_pass']}")

            # Park2/Park3 正常求解（Park1 IterRecord 不会写入）
            for p in ["Park2", "Park3"]:
                r = solve_park_normal(
                    p, k, mask_float, g_masked_float, h_vec_float, bundles,
                    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid)
                normal_rows.append({"iter": k, **r})

            # CoordinateStep：Park1 IterRecord 缺失，应报错
            print(f"\n  [验证] 尝试 CoordinateStep({k})，预期因 Park1 记录缺失而失败...")
            try:
                fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                          contract=CONTRACT_ADMM, function="CoordinateStep",
                          args=[sid, str(k)], wait_seconds=2.0)
                print(f"  [!] 意外成功 —— 攻击未生效！")
                attack_result["coord_step_blocked"] = False
            except Exception as coord_err:
                err_str = str(coord_err)[:300]
                blocked = "missing" in err_str.lower()
                print(f"  -> CoordinateStep 被拒绝 ({'✓ 符合预期' if blocked else '✗'})")
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

            print(f"\n  [退出] 攻击完成，scheduleID {sid} 不再继续（ADMM 已中断）")
            break   # ADMM 状态被毒化，立即退出，换下一个 trial

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
                  f"dualSq={admm.get('dualResidSq',0)} "
                  f"converged={admm.get('converged',False)}")

    if attack_result is None:
        print(f"  [!!] {max_iters} 轮内未触发攻击（条件未满足）")

    return attack_result, normal_rows


# ── 主函数 ─────────────────────────────────────────────────────────────────────
def run(max_iters: int = 60):
    rng = np.random.default_rng(RNG_SEED)

    # ── 预热（只做一次）──
    print("\n[预热] 三个园区容器")
    for p in ALL_3:
        ready, msg = preheat_org(p)
        print(("  OK  " if ready else "  FAIL ") + msg)
        if not ready:
            raise RuntimeError(f"{p} 未就绪")

    # ── 加载 Mask bundle（只做一次）──
    bundles = {}
    mask_float = {}
    g_masked_float = {}
    h_vec_float = {}
    for i, p in enumerate(ALL_3, start=1):
        b = load_bundle(i)
        bundles[p]        = b
        mask_float[p]     = to_float_mask(b)
        g_masked_float[p] = [[v/MASK_SCALE for v in row] for row in b["gMaskedScaled"]]
        h_vec_float[p]    = [v/MASK_SCALE for v in b["hVecScaled"]]

    attack_results  = []
    all_normal_rows = []

    # ── 三档噪声，各自独立 scheduleID ──
    for noise_pct in NOISE_LEVELS:
        res, norm_rows = run_one_noise_level(
            noise_pct, rng, bundles, mask_float,
            g_masked_float, h_vec_float, max_iters=max_iters)
        if res is not None:
            attack_results.append(res)
        all_normal_rows.extend(norm_rows)

    # ── 保存数据 ──
    print(f"\n[保存数据]")
    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")

    df_atk  = pd.DataFrame(attack_results)
    path_atk = f"{OUT_DIR}/L1_attack_results_{ts_file}.xlsx"
    df_atk.to_excel(path_atk, index=False)
    print(f"  攻击结果: {path_atk}")

    df_norm   = pd.DataFrame(all_normal_rows)
    path_norm = f"{OUT_DIR}/L1_normal_rounds_{ts_file}.xlsx"
    df_norm.to_excel(path_norm, index=False)
    print(f"  正常轮数据: {path_norm}")

    path_json = f"{OUT_DIR}/L1_raw_{ts_file}.json"
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({
            "experiment":     "EXP-1-L1-SmallNoise",
            "noise_levels":   NOISE_LEVELS,
            "kkt_tol":        10000,
            "mask_scale":     MASK_SCALE,
            "attack_results": attack_results,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"  原始 JSON: {path_json}")

    print(f"\n{'='*65}")
    print(f"EXP-1 L1 小扰动实验汇总")
    print(f"{'='*65}")
    print(f"{'噪声档':>8} {'iter':>5} {'基线stat':>10} {'攻击stat':>10} "
          f"{'超阈倍数':>8} {'KKT通过':>8} {'信用变化':>10}")
    print(f"{'-'*65}")
    for r in attack_results:
        credit_chg = ""
        if "credit_after_all" in r and "credit_before" in r:
            bef = r["credit_before"].get(ATTACK_SOLVER, "?")
            aft = r["credit_after_all"].get(ATTACK_SOLVER, "?")
            credit_chg = f"{bef}→{aft}"
        print(f"{r['noise_pct_str']:>8} {r['iter']:>5} "
              f"{r['baseline_stat']:>10} {r['stat_res']:>10} "
              f"{r['stat_over_tol']:>7.1f}× "
              f"{'通过' if r['kkt_pass'] else '拦截':>8} "
              f"{credit_chg:>10}")
    print(f"{'='*65}")

    return attack_results


if __name__ == "__main__":
    run()
