"""
EXP-1：KKT 拦截非最优解实验

实验场景：
  Park2 被链码哈希分配为 Park1 电力子问题的 Solver（iter ≥ 20）。
  Park2 的真实动机：通过歪曲 Park1 的能量交换量 e_E，
    拉偏共识变量 z_E，使后续各轮 ADMM 以对自己有利的基点迭代，
    降低自身电力采购/出售成本。

作恶手段：
  正常 OSQP 求解得 y* ∈ ℝ^k0，然后扰动：
    y_bad[i] = y*[i] × (−2.5),  i < k0//4   (前 1/4 分量翻转放大)
  其余分量保持不变。提交 (y_bad, μ*) 至链上。

KKT 拦截：
  Verifier（Park3 或另一方）从链上独立下载 y_bad，
  重建当前轮的 h_masked_scaled / c_masked_scaled，
  调用 check_kkt_scaled → passed=False, stat_res ≫ 0。
  RecordVerdict(pass=false) → e_m ✗ 不写 IterationRecord
                            → SOLVE_CREDIT[Park2] −10 (50→40)

连锁效果：
  CoordinateStep 需三园区全部有 IterationRecord，
  Park1 缺失 → 报错 "missing iteration record for Park1 iter K"
  → ADMM 链上状态锁死在 iter=K，z/λ 不更新，攻击无效。

scheduleID 前缀：9994-M7A-（严格隔离，禁用 2024-03-01-）
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, CONTRACT_AUDIT, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup, wait_state_after_coordinate
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

# ── 实验参数 ───────────────────────────────────────────────────────────────────
ATTACK_PARK       = "Park1"   # 受害方：谁的子问题被作恶 Solver 求解
ATTACK_SOLVER     = "Park2"   # 作恶方：被分配为 Solver 后提交扰动 y*
ATTACK_FROM_ITER  = 20        # 从第几轮开始等待攻击时机
PERTURB_SCALE     = -2.5      # 扰动倍率（翻转+放大，破坏 KKT 平稳性）

ALL_3       = ["Park1", "Park2", "Park3"]
BUNDLE_DIR  = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/M1a离线Mask验证/M6完整闭环"
CHUNK_CHARS = 50_000


# ── 工具函数 ───────────────────────────────────────────────────────────────────
def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    path = os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(sid, park_id, bundle):
    fc.invoke(
        caller_org=park_id, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[sid, park_id, str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
        wait_seconds=2.0,
    )
    parts = {
        "hE0":     json.dumps(bundle["hE0Scaled"],     separators=(",", ":")),
        "gMasked": json.dumps(bundle["gMaskedScaled"], separators=(",", ":")),
        "hVec":    json.dumps(bundle["hVecScaled"],    separators=(",", ":")),
        "c0":      json.dumps(bundle["c0Scaled"],      separators=(",", ":")),
        "eRows":   json.dumps(bundle["eRowsScaled"],   separators=(",", ":")),
        "x0e":     json.dumps(bundle["x0eScaled"],     separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        cks = chunks(payload)
        for idx, chunk in enumerate(cks):
            fc.retry(
                lambda idx=idx, chunk=chunk, cks=cks, pn=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=ALL_3,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[sid, park_id, pn, str(idx), str(len(cks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(0.5)
    fc.retry(
        lambda: fc.invoke(
            caller_org=park_id, endorsing_orgs=ALL_3,
            contract=CONTRACT_MASK, function="FinalizeMaskedSubproblemUpload",
            args=[sid, park_id], wait_seconds=1.5,
        ),
        attempts=6, delay_seconds=2.0,
    )


def to_float_mask(bundle):
    return {
        "H_e0":   [[v / MASK_SCALE for v in row] for row in bundle["hE0Scaled"]],
        "C0":     [v / MASK_SCALE for v in bundle["c0Scaled"]],
        "E_rows": [[v / MASK_SCALE for v in row] for row in bundle["eRowsScaled"]],
        "x0_e":   [v / MASK_SCALE for v in bundle["x0eScaled"]],
    }


def query_solve_credit(park_id):
    """查询 SOLVE_CREDIT，返回整数分值。"""
    try:
        raw = fc.query_with_retry("Park1", CONTRACT_AUDIT,
                                  "QuerySolveCreditScore", [park_id])
        if isinstance(raw, (int, float)):
            return int(raw)
        return int(raw.get("score", raw.get("Score", -1)))
    except Exception as e:
        return f"查询失败: {e}"


# ── 正常子问题求解（单园区单轮）─────────────────────────────────────────────────
def solve_park_normal(park, k, mask_float, g_masked_float, h_vec_float, bundles,
                      lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
                      pre_assigned=False):
    """
    正常路径：OSQP → KKT PASS → Assign → Submit → Verdict → 确认 IterationRecord。
    pre_assigned=True 时跳过 AssignSolverAndVerifier invoke（已在外部调用过）。
    """
    idx = PARK_IDS.index(park)
    lam_h_row = lam_h[idx] if park != "Park3" else [0.0] * 24
    z_h_row   = z_h[idx]   if park != "Park3" else [0.0] * 24
    lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star    = solve_masked_qp(h_masked, c_masked,
                                          g_masked_float[park], h_vec_float[park])
    y_scaled  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    bundle    = bundles[park]

    h_m_sc = [[int(round(rho * v)) for v in r_] for r_ in bundle["hE0Scaled"]]
    c_m_sc = [int(round(v * MASK_SCALE)) for v in c_masked]
    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_m_sc, bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_scaled, mu_scaled,
    )
    assert passed, f"正常路径 KKT 意外失败: iter={k} {park} stat={stat_res} compl={compl_res}"

    if not pre_assigned:
        fc.retry(
            lambda: fc.invoke(caller_org=park, endorsing_orgs=ALL_3,
                              contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                              args=[sid, str(k), park], wait_seconds=2.0),
            attempts=4, delay_seconds=2.0,
        )

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

    # 确认 IterationRecord 已写入（与 EXP-3 保持一致）
    rec = fc.query_with_retry(park, CONTRACT_ADMM, "QueryIterationRecord",
                              [sid, str(k), park], attempts=8, delay_seconds=1.5)
    print(f"    {park}: IterationRecord 已确认 eE[0]={rec['eExScaled'][0]}")
    return {"park": park, "solver": solver, "verifier": verifier, "kkt_pass": True}


# ── 攻击子问题求解（Park1 by Park2）─────────────────────────────────────────────
def solve_park1_attack(k, mask_float, g_masked_float, h_vec_float, bundles,
                       lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid):
    """
    攻击路径：
      Solver(Park2) 正常求解后扰动 y*，提交 y_bad。
      Verifier 从链上独立下载 y_bad，重建 c_masked，运行 KKT → FAIL。
      RecordVerdict(pass=false)。
    返回完整攻击元数据。
    """
    park = ATTACK_PARK   # "Park1"
    idx  = PARK_IDS.index(park)
    lam_block = lam_e[idx] + lam_g[idx] + lam_h[idx] + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h[idx]   + z_c[idx]
    bundle    = bundles[park]

    # ── Solver (Park2) 本地求解 ──────────────────────────────────────────────
    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star    = solve_masked_qp(h_masked, c_masked,
                                          g_masked_float[park], h_vec_float[park])
    k0 = bundle["k0"]

    # 扰动：前 k0//4 个分量 × PERTURB_SCALE（翻转并放大，破坏 KKT 平稳性）
    y_bad = list(y_star)
    perturb_count = k0 // 4
    for i in range(perturb_count):
        y_bad[i] = y_bad[i] * PERTURB_SCALE

    y_good_scaled = [int(round(v * MASK_SCALE)) for v in y_star]
    y_bad_scaled  = [int(round(v * MASK_SCALE)) for v in y_bad]
    mu_scaled     = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]

    print(f"\n  [ATTACK] iter={k} {park}: Solver=Park2 提交扰动 y*")
    print(f"    k0={k0}, 扰动前 {perturb_count} 个分量 × {PERTURB_SCALE}")
    print(f"    y*[0]={y_star[0]:.4f} → y_bad[0]={y_bad[0]:.4f}")

    # AssignSolverAndVerifier（已在外层调用并确认 solver=Park2）
    fc.invoke(caller_org=park, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
              args=[sid, str(k), park], wait_seconds=1.0)
    assignment = fc.query_with_retry(park, CONTRACT_MASK,
                                     "QuerySolverAssignment", [sid, str(k), park])
    solver_id   = assignment["solverParkId"]
    verifier_id = assignment["verifierParkId"]
    assert solver_id == ATTACK_SOLVER, (
        f"预期 solver=Park2，实际={solver_id}，调用方应先确认再调本函数")

    # ── Solver 提交扰动后的 y_bad ────────────────────────────────────────────
    fc.invoke(caller_org=solver_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="SubmitMaskedSolution",
              args=[sid, str(k), park, json.dumps(y_bad_scaled), json.dumps(mu_scaled)],
              wait_seconds=1.0)
    print(f"    → SubmitMaskedSolution(y_bad) 成功（链上 solutionHash 已固化）")

    # ── Verifier 独立下载 y_bad，重建 c_masked，运行 KKT ────────────────────
    msol = fc.query_with_retry(verifier_id, CONTRACT_MASK,
                               "QueryMaskedSolution", [sid, str(k), park])
    y_downloaded = msol["yScaled"]       # 从链上取回的 y_bad_scaled
    mu_downloaded = msol["muScaled"]
    solution_hash = msol["solutionHash"]

    # Verifier 用链上当前 rho/lam/z 重建 h_masked_scaled 和 c_masked_scaled
    h_m_sc = [[int(round(rho * v)) for v in row] for row in bundle["hE0Scaled"]]
    _, c_masked_verifier = build_masked_round(mask_float[park], rho, lam_block, z_block)
    c_m_sc = [int(round(v * MASK_SCALE)) for v in c_masked_verifier]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_m_sc, c_m_sc,
        bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_downloaded, mu_downloaded,
    )
    print(f"    → Verifier({verifier_id}) 独立 KKT 验证：")
    print(f"      passed={passed}  stat_res={stat_res}  compl_res={compl_res}")
    assert not passed, (
        f"预期 KKT FAIL，但意外通过。扰动不足？stat={stat_res} compl={compl_res}")

    # ── Verifier 提交 FAIL 裁决 ──────────────────────────────────────────────
    fc.invoke(caller_org=verifier_id, endorsing_orgs=ALL_3,
              contract=CONTRACT_MASK, function="RecordVerdict",
              args=[sid, str(k), park, "false",
                    str(stat_res), str(compl_res), solution_hash],
              wait_seconds=1.0)
    print(f"    → RecordVerdict(pass=false) 提交成功")
    print(f"      SOLVE_CREDIT[Park2] 应 −10")

    return {
        "attack_iter":    k,
        "attack_park":    park,
        "solver":         solver_id,
        "verifier":       verifier_id,
        "k0":             k0,
        "perturb_count":  perturb_count,
        "perturb_scale":  PERTURB_SCALE,
        "y_star_0":       round(y_star[0], 6),
        "y_bad_0":        round(y_bad[0],  6),
        "y_star_norm":    round(sum(v**2 for v in y_star)**0.5, 4),
        "y_bad_norm":     round(sum(v**2 for v in y_bad)**0.5,  4),
        "kkt_pass":       passed,
        "stat_res":       stat_res,
        "compl_res":      compl_res,
        "solution_hash":  solution_hash,
        "y_good_scaled_0": y_good_scaled[0],
        "y_bad_scaled_0":  y_bad_scaled[0],
    }


# ── 主实验 ─────────────────────────────────────────────────────────────────────
def run(max_iters: int = 100):
    sid = setup(
        "9994-M7A-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        max_iter=max_iters,
    )
    print(f"\nscheduleID = {sid}")

    print("\n[预热] 三个园区容器")
    for park in PARK_IDS:
        ready, msg = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + msg)
        if not ready:
            raise RuntimeError(f"{park} 没就绪")

    print("\n[注册] 上传三个园区的 masked 结构")
    bundles, g_masked_float, h_vec_float, mask_float = {}, {}, {}, {}
    for park_idx, park in enumerate(PARK_IDS, start=1):
        bundle = load_bundle(park_idx)
        bundles[park] = bundle
        upload_large_mask(sid, park, bundle)
        g_masked_float[park] = [[v / MASK_SCALE for v in row]
                                 for row in bundle["gMaskedScaled"]]
        h_vec_float[park]    = [v / MASK_SCALE for v in bundle["hVecScaled"]]
        mask_float[park]     = to_float_mask(bundle)
        print(f"  -> {park} 注册完成 (k0={bundle['k0']})")

    # 攻击前信用分快照
    credit_before = {p: query_solve_credit(p) for p in PARK_IDS}
    print(f"\n[基线] SOLVE_CREDIT: {credit_before}")

    normal_rows = []   # iter=0~attack-1 的链上状态
    attack_meta = None
    evidence    = []
    attack_iter = None

    for k in range(max_iters):
        print(f"\n--- iter {k} ---")
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
        print(f"  rho={rho:.4f}")

        # ── 先做一次预查询（不提交），确认 Park1 的 solver 是否是 Park2 ────────
        # 注意：AssignSolverAndVerifier 若已存在则直接返回，不会重复写入
        # 所以先调一次 invoke 再 query 是安全的
        fc.invoke(caller_org=ATTACK_PARK, endorsing_orgs=ALL_3,
                  contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                  args=[sid, str(k), ATTACK_PARK], wait_seconds=1.0)
        pre_assign = fc.query_with_retry(
            ATTACK_PARK, CONTRACT_MASK, "QuerySolverAssignment",
            [sid, str(k), ATTACK_PARK])
        park1_solver = pre_assign["solverParkId"]

        attack_this_iter = (k >= ATTACK_FROM_ITER and park1_solver == ATTACK_SOLVER
                            and attack_meta is None)

        if attack_this_iter:
            print(f"  [!] iter={k}: Park1 的 Solver={park1_solver} == Park2 → 攻击启动！")
            attack_iter = k
            credit_before_attack = {p: query_solve_credit(p) for p in PARK_IDS}
            print(f"  攻击前 SOLVE_CREDIT: {credit_before_attack}")

            # Park1 子问题：攻击路径（AssignSolverAndVerifier 已在上面调用）
            attack_meta = solve_park1_attack(
                k, mask_float, g_masked_float, h_vec_float, bundles,
                lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid)

            # Park2 和 Park3 子问题正常跑
            for park in [p for p in PARK_IDS if p != ATTACK_PARK]:
                print(f"  {park}: 正常求解")
                solve_park_normal(park, k, mask_float, g_masked_float, h_vec_float,
                                  bundles, lam_e, lam_g, lam_h, lam_c,
                                  z_e, z_g, z_h, z_c, rho, sid)

            # CoordinateStep → 预期失败（Park1 IterationRecord 缺失）
            print(f"\n  [验证] 尝试 CoordinateStep({k}) —— 预期因缺少 Park1 记录而报错...")
            coord_error = None
            try:
                fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                          contract=CONTRACT_ADMM, function="CoordinateStep",
                          args=[sid, str(k)], wait_seconds=1.0)
                coord_error = "(意外成功！)"
                print(f"  !! CoordinateStep 意外成功")
            except RuntimeError as e:
                coord_error = str(e)[:300]
                blocked = "missing iteration record" in coord_error.lower() or \
                          "missing" in coord_error.lower()
                print(f"  -> CoordinateStep 被拒绝 ({'✓ 符合预期' if blocked else '✗ 不符预期'})")
                print(f"     {coord_error[:200]}")

            # 收集所有证据
            credit_after = {p: query_solve_credit(p) for p in PARK_IDS}
            print(f"\n  攻击后 SOLVE_CREDIT: {credit_after}")

            # Park1 IterationRecord 应不存在
            iter_rec_exists = False
            iter_rec_error  = ""
            try:
                fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                    [sid, str(k), ATTACK_PARK], attempts=3, delay_seconds=1.0)
                iter_rec_exists = True
            except Exception as e:
                iter_rec_error = str(e)[:200]

            # ADMM 状态（应仍在 iter=k，未推进）
            admm_state = pa.query_admm_state(sid)

            evidence = [
                {
                    "检查项": f"IterationRecord(iter={k}, Park1)",
                    "预期": "不存在（e_m 未上链）",
                    "实际": "不存在 ✓" if not iter_rec_exists else f"存在！{iter_rec_error}",
                    "通过": not iter_rec_exists,
                },
                {
                    "检查项": "SOLVE_CREDIT[Park2]",
                    "预期": f"{credit_before_attack.get('Park2', '?')} → {credit_before_attack.get('Park2', 50) - 10}",
                    "实际": str(credit_after.get("Park2", "?")),
                    "通过": (credit_after.get("Park2", 999) ==
                             credit_before_attack.get("Park2", 50) - 10),
                },
                {
                    "检查项": f"CoordinateStep(iter={k})",
                    "预期": "missing iteration record 报错",
                    "实际": coord_error[:120],
                    "通过": "missing" in coord_error.lower() or "iteration record" in coord_error.lower(),
                },
                {
                    "检查项": "ADMM 链上 iter",
                    "预期": f"仍在 iter={k}（未推进）",
                    "实际": f"iter={admm_state.get('iter', '?')} status={admm_state.get('status', '?')}",
                    "通过": admm_state.get("iter", -1) == k,
                },
            ]

            for ev in evidence:
                sym = "✓" if ev["通过"] else "✗"
                print(f"  [{sym}] {ev['检查项']}: {ev['实际']}")

            break   # 实验结束，不再继续 ADMM

        else:
            # 正常轮：Park1 的 Assign 已在上面调用（pre_assigned=True），其余两园区内部 Assign
            row = {"iter": k, "park1_solver": park1_solver, "attack": False}
            # Park1（pre_assigned=True：跳过 AssignSolverAndVerifier invoke）
            solve_park_normal(ATTACK_PARK, k, mask_float, g_masked_float, h_vec_float,
                              bundles, lam_e, lam_g, lam_h, lam_c,
                              z_e, z_g, z_h, z_c, rho, sid, pre_assigned=True)
            # Park2, Park3
            for park in [p for p in PARK_IDS if p != ATTACK_PARK]:
                solve_park_normal(park, k, mask_float, g_masked_float, h_vec_float,
                                  bundles, lam_e, lam_g, lam_h, lam_c,
                                  z_e, z_g, z_h, z_c, rho, sid, pre_assigned=False)

            fc.retry(
                lambda: fc.invoke(caller_org="Park1", endorsing_orgs=ALL_3,
                                  contract=CONTRACT_ADMM, function="CoordinateStep",
                                  args=[sid, str(k)], wait_seconds=1.0),
                attempts=5, delay_seconds=1.5,
            )
            state = wait_state_after_coordinate(sid, k)
            print(f"  status={state['status']} primalSq={state['primalResidSq']} "
                  f"dualSq={state['dualResidSq']} park1_solver={park1_solver}")
            row.update({
                "status":         state["status"],
                "primalResidSq":  state["primalResidSq"],
                "dualResidSq":    state["dualResidSq"],
                "rhoScaled":      state["rhoScaled"],
            })
            normal_rows.append(row)

            if state["status"] in ("CONVERGED", "FAILED"):
                print("  [注意] 已收敛，但尚未遇到攻击时机！增大 max_iters 或调整 scheduleID")
                break

    # ── 保存结果 ────────────────────────────────────────────────────────────────
    if attack_meta is None:
        print("\n[警告] 实验结束但未找到攻击时机（Park2 未被分配为 Park1 的 Solver）")
        print("       请重新运行（不同 scheduleID 哈希不同）")
        return

    attack_meta["schedule_id"] = sid
    attack_meta["credit_before"] = str(credit_before)

    out_wsl = os.path.join(os.path.dirname(__file__), "EXP1_kkt_attack.xlsx")
    out_win = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/作恶/EXP1_kkt_attack.xlsx"

    with pd.ExcelWriter(out_wsl, engine="openpyxl") as writer:
        pd.DataFrame(normal_rows).to_excel(writer, sheet_name="normal_iters", index=False)
        pd.DataFrame([attack_meta]).to_excel(writer, sheet_name="attack_detail", index=False)
        pd.DataFrame(evidence).to_excel(writer, sheet_name="evidence", index=False)

    import subprocess
    subprocess.run(["cp", out_wsl, out_win], capture_output=True)
    print(f"\n数据已保存: {out_win}")

    print("\n" + "="*60)
    all_pass = all(ev["通过"] for ev in evidence)
    print(f"EXP-1 结论：KKT 拦截 {'全部验证通过 ✓' if all_pass else '部分未通过 ✗'}")
    print(f"  攻击轮次: iter={attack_iter}")
    print(f"  KKT stat_res: {attack_meta['stat_res']}（阈值 10000，差 "
          f"{attack_meta['stat_res'] // 10000}x）")
    print("="*60)


if __name__ == "__main__":
    run(max_iters=100)
