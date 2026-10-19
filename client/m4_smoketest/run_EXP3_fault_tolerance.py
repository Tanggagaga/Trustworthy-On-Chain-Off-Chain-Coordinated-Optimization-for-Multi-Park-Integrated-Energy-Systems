"""
EXP-3：单节点容错收敛实验。

实验设计：
  预处理：令 Park2 的 SOLVE_CREDIT 降至 40（< threshold=50），
          使其后续不再被分配为任何子问题的 solver/verifier。
  Phase-1（iter 0 ~ FAULT_ITER-1）：三园区全部在线，正常跑。
  Park2 peer 宕机（docker stop）。
  Phase-2（iter FAULT_ITER ~ 收敛）：两节点继续，
    - 背书改为 ["Park1","Park3"]。
    - Park2 自身子问题的 AssignSolverAndVerifier 由 Park1 代调
      （链码无 MSP 检查，合法）。
    - solver/verifier 均从 {Park1,Park3} 分配，不受宕机影响。
    - OSQP 求解仍在 Python 端进行，不依赖 peer。

证明结论：
  - OutOf(2,3) 背书：1 peer 宕机后 Park1+Park3 仍能背书所有交易。
  - ADMM 继续收敛，链不停服。
  - 恢复后 Park2 可通过 gossip 追块自动同步宕机期间数据。

scheduleID 前缀：9994-EXP3-（隔离）
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup, wait_state_after_coordinate
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

# ── 实验参数 ───────────────────────────────────────────────────────────────────
FAULT_ITER   = 10        # 在第 10 轮开始前停掉 Park2
DOWN_PARK    = "Park2"
DOWN_PEER    = "peer0.park2.energy.com"
ALL_3        = ["Park1", "Park2", "Park3"]
TWO_ALIVE    = ["Park1", "Park3"]
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
CHUNK_CHARS  = 50_000

schedule_id_ref = [None]   # 全局，供 solve_one_park 访问


# ── 工具函数 ───────────────────────────────────────────────────────────────────
def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    with open(os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(schedule_id, park_id, bundle, endorsers):
    fc.invoke(
        caller_org=park_id, endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, park_id,
              str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
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
        payload_chunks = chunks(payload)
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks,
                       pn=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=endorsers,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, park_id, pn,
                          str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(0.5)
    fc.retry(
        lambda: fc.invoke(
            caller_org=park_id, endorsing_orgs=endorsers,
            contract=CONTRACT_MASK, function="FinalizeMaskedSubproblemUpload",
            args=[schedule_id, park_id], wait_seconds=1.5,
        ),
        attempts=6, delay_seconds=2.0,
    )


def to_float_mask(bundle):
    return {
        "H_e0":  [[v / MASK_SCALE for v in row] for row in bundle["hE0Scaled"]],
        "C0":    [v / MASK_SCALE for v in bundle["c0Scaled"]],
        "E_rows":[[v / MASK_SCALE for v in row] for row in bundle["eRowsScaled"]],
        "x0_e":  [v / MASK_SCALE for v in bundle["x0eScaled"]],
    }


def safe_caller(caller, down_park=None, fallback="Park1"):
    """如果 caller 是已宕机的节点，换成 fallback 发起查询。"""
    if down_park and caller == down_park:
        return fallback
    return caller


# ── 预处理：扣除 Park2 的 SOLVE_CREDIT ────────────────────────────────────────
def pre_deduct_park2_credit(bundles, g_masked_float, h_vec_float, mask_float, main_sid):
    """
    在主实验的第 0 轮之前，利用主 scheduleID 的 iter=-1（实际上用 iter=998 作为哨兵轮）
    让 Park2 作为某个子问题的 solver 并提交一个故意错误的 y*（扰动后），
    使 RecordVerdict 返回 FAIL，SOLVE_CREDIT_Park2 从 50 扣到 40（< threshold 50）。

    之后主实验 FAULT_ITER 轮内 Park2 不会被分配为任何 solver/verifier。
    """
    print("\n[预处理] 扣除 Park2 的 SOLVE_CREDIT (50→40) 以排除其 solver 资格 ...")
    sid  = main_sid
    k    = 998   # 哨兵 iter，不干扰正式 iter 0~N
    park = "Park1"   # 让 Park1 的子问题被 Park2 求解
    endorsers = ALL_3

    # AssignSolverAndVerifier：若 hashToIndex 恰好不给 Park2，重试几次
    # 通常 iter=998 的哈希会命中 Park2 或 Park3；我们需要 solver==Park2
    for attempt in range(5):
        fc.invoke(
            caller_org=park, endorsing_orgs=endorsers,
            contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
            args=[sid, str(k + attempt), park], wait_seconds=1.0,
        )
        assignment = fc.query_with_retry(park, CONTRACT_MASK, "QuerySolverAssignment",
                                         [sid, str(k + attempt), park])
        solver   = assignment["solverParkId"]
        verifier = assignment["verifierParkId"]
        if solver == "Park2":
            k = k + attempt
            break
        print(f"  attempt {attempt}: solver={solver}，不是Park2，换轮次再试")
    else:
        print("  [警告] 5 次尝试均未让 Park2 被选为 solver；跳过预扣分，继续实验")
        return

    print(f"  iter={k}: solver=Park2  verifier={verifier} -> 提交扰动解触发 FAIL")

    # OSQP 正常求解，然后故意扰动 y*
    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
    idx      = PARK_IDS.index(park)
    lam_block = lam_e[idx] + lam_g[idx] + lam_h[idx] + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h[idx]   + z_c[idx]
    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star = solve_masked_qp(h_masked, c_masked,
                                      g_masked_float[park], h_vec_float[park])
    # 扰动：前 1/4 分量翻倍（破坏 KKT 稳定性）
    k0 = bundles[park]["k0"]
    y_bad = list(y_star)
    for i in range(k0 // 4):
        y_bad[i] = y_bad[i] * (-2.5)
    y_scaled  = [int(round(v * MASK_SCALE)) for v in y_bad]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]

    # KKT 验证（应该失败）
    bundle = bundles[park]
    passed, stat_res, compl_res = check_kkt_scaled(
        bundle["hE0Scaled"], bundle["c0Scaled"],
        bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_scaled, mu_scaled,
    )
    assert not passed, "预期 KKT 失败但意外通过，请调大扰动"

    # SubmitMaskedSolution（solver=Park2）
    fc.invoke(
        caller_org="Park2", endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[sid, str(k), park, json.dumps(y_scaled), json.dumps(mu_scaled)],
        wait_seconds=1.0,
    )
    msol = fc.query_with_retry("Park2", CONTRACT_MASK, "QueryMaskedSolution",
                               [sid, str(k), park])

    # RecordVerdict（verifier）→ FAIL → SOLVE_CREDIT_Park2 -10
    fc.invoke(
        caller_org=verifier, endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[sid, str(k), park,
              "false", str(stat_res), str(compl_res), msol["solutionHash"]],
        wait_seconds=1.0,
    )
    print(f"  -> FAIL 裁决提交成功（RecordVerdict FAIL → SOLVE_CREDIT_Park2 应为 40）")
    # 尝试查询信用分（若链码未暴露该函数则跳过）
    for qfunc in ["QuerySolveCreditScore", "GetSolveCreditScore", "QueryCreditScore"]:
        try:
            score_raw = fc.query_with_retry("Park1", CONTRACT_MASK, qfunc, ["Park2"])
            score = score_raw if isinstance(score_raw, (int, float)) else score_raw.get("score", "?")
            print(f"  -> {qfunc}: SOLVE_CREDIT_Park2 = {score}")
            break
        except Exception:
            pass
    else:
        print("  -> 信用分查询函数未找到，扣分逻辑依赖 RecordVerdict 链码内部执行")


# ── 单园区完整求解流程 ─────────────────────────────────────────────────────────
def solve_one_park(park, k, mask_float, g_masked_float, h_vec_float, bundles,
                   lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho,
                   endorsers, caller_for_assign, park2_down=False):
    """
    caller_for_assign：调用 AssignSolverAndVerifier 的身份；
        正常时 = park 自身；Park2 宕机时 Park2 子问题 = "Park1"。
    park2_down：True 时对 query 调用使用 Park1 作为 fallback caller。
    """
    idx = PARK_IDS.index(park)
    if park == "Park3":
        lam_h_row, z_h_row = [0.0] * 24, [0.0] * 24
    else:
        lam_h_row, z_h_row = lam_h[idx], z_h[idx]
    lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    t0 = time.time()
    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star = solve_masked_qp(h_masked, c_masked,
                                      g_masked_float[park], h_vec_float[park])
    t_solve = time.time() - t0

    y_scaled  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    bundle    = bundles[park]

    # h_masked = rho * H_e0；缩放到整数域：h_masked_scaled[i][j] = rho * hE0Scaled[i][j]
    h_masked_scaled  = [[int(round(rho * v)) for v in row] for row in bundle["hE0Scaled"]]
    c_masked_scaled  = [int(round(v * MASK_SCALE)) for v in c_masked]

    t_kkt0 = time.time()
    passed, stat_res, compl_res = check_kkt_scaled(
        h_masked_scaled, c_masked_scaled,
        bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_scaled, mu_scaled,
    )
    t_kkt = time.time() - t_kkt0

    sid = schedule_id_ref[0]

    # AssignSolverAndVerifier
    fc.invoke(
        caller_org=caller_for_assign, endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[sid, str(k), park], wait_seconds=1.0,
    )
    # Query 用存活节点（Park2 宕机时其 CORE_PEER_ADDRESS 无法使用）
    q_alive = safe_caller(caller_for_assign, DOWN_PARK if park2_down else None)
    assignment = fc.query_with_retry(q_alive, CONTRACT_MASK, "QuerySolverAssignment",
                                     [sid, str(k), park])
    solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]
    print(f"    assign: solver={solver} verifier={verifier}")

    # SubmitMaskedSolution
    # 即使 solver/verifier == Park2（宕机），invoke 时指定了 endorsing_orgs=alive peers，
    # MSP cert 仍在磁盘，CORE_PEER_ADDRESS 不参与 --peerAddresses 的背书流程。
    # 因此 caller_org=solver（含Park2）仍可正常完成 MSP 检查。
    fc.invoke(
        caller_org=solver, endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[sid, str(k), park, json.dumps(y_scaled), json.dumps(mu_scaled)],
        wait_seconds=1.0,
    )
    # Query 改用存活节点
    q_solver = safe_caller(solver, DOWN_PARK if park2_down else None)
    msol = fc.query_with_retry(q_solver, CONTRACT_MASK, "QueryMaskedSolution",
                               [sid, str(k), park])

    # RecordVerdict（verifier 可能是 Park2，invoke 不连 Park2 peer）
    fc.invoke(
        caller_org=verifier, endorsing_orgs=endorsers,
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[sid, str(k), park,
              "true" if passed else "false",
              str(stat_res), str(compl_res), msol["solutionHash"]],
        wait_seconds=1.0,
    )
    if not passed:
        raise RuntimeError(f"KKT 失败: iter={k} {park} stat={stat_res} compl={compl_res}")

    q_rec = safe_caller(solver, DOWN_PARK if park2_down else None)
    rec = fc.query_with_retry(q_rec, CONTRACT_ADMM, "QueryIterationRecord",
                              [sid, str(k), park])

    return {
        "park": park, "solver": solver, "verifier": verifier,
        "kkt_pass": passed, "stat_res": stat_res, "compl_res": compl_res,
        "t_solve_s": round(t_solve, 3), "t_kkt_s": round(t_kkt, 3),
        "caller_for_assign": caller_for_assign,
        "eE_0": rec["eExScaled"][0] / 1000,
    }


# ── docker 操作 ────────────────────────────────────────────────────────────────
def docker_stop_park2():
    print(f"\n[!] 停止 {DOWN_PEER} 容器（模拟节点宕机）...")
    result = subprocess.run(["docker", "stop", DOWN_PEER],
                            capture_output=True, text=True)
    status = "已停止" if result.returncode == 0 else f"警告 {result.stderr.strip()}"
    print(f"  -> {DOWN_PEER} {status}")
    time.sleep(3)


def docker_start_park2():
    print(f"\n[恢复] 重启 {DOWN_PEER} 容器...")
    result = subprocess.run(["docker", "start", DOWN_PEER],
                            capture_output=True, text=True)
    status = "已重启" if result.returncode == 0 else f"警告 {result.stderr.strip()}"
    print(f"  -> {DOWN_PEER} {status}")
    time.sleep(10)


# ── 主实验 ─────────────────────────────────────────────────────────────────────
def run(max_iters: int = 100):
    toy_date = "9994-EXP3-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    sid = setup(toy_date, max_iter=max_iters)
    schedule_id_ref[0] = sid
    print(f"scheduleID={sid}")
    print(f"设计：iter 0~{FAULT_ITER-1} 三节点；iter {FAULT_ITER}+ Park2宕机两节点继续")

    print("\n[预热] 三个园区容器")
    for park in PARK_IDS:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            raise RuntimeError(f"{park} 没就绪")

    print("\n[注册] 上传三个园区的 masked 结构（三节点背书）")
    bundles, g_masked_float, h_vec_float, mask_float = {}, {}, {}, {}
    for idx, park in enumerate(PARK_IDS, start=1):
        bundle = load_bundle(idx)
        bundles[park] = bundle
        upload_large_mask(sid, park, bundle, ALL_3)
        g_masked_float[park] = [[v / MASK_SCALE for v in row]
                                 for row in bundle["gMaskedScaled"]]
        h_vec_float[park]    = [v / MASK_SCALE for v in bundle["hVecScaled"]]
        mask_float[park]     = to_float_mask(bundle)
        print(f"  -> {park} 注册完成")

    # 预扣分：令 Park2 的 SOLVE_CREDIT 降至 40 < 50（threshold）
    pre_deduct_park2_credit(bundles, g_masked_float, h_vec_float, mask_float, sid)

    park2_is_down = False
    chain_rows, park_rows, sync_rows = [], [], []
    final_status = None

    for k in range(max_iters):
        # 在 FAULT_ITER 轮起，停掉 Park2
        if k == FAULT_ITER and not park2_is_down:
            docker_stop_park2()
            park2_is_down = True
            print(f"[状态] iter {k} 起使用两节点背书 {TWO_ALIVE}")

        endorsers = TWO_ALIVE if park2_is_down else ALL_3
        phase = "Phase-2(2节点)" if park2_is_down else "Phase-1(3节点)"
        print(f"\n--- EXP-3 iter {k} [{phase}] ---")

        t_iter = time.time()
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
        print(f"  rho={rho:.4f}  endorsers={endorsers}")

        for park in PARK_IDS:
            if park2_is_down and park == DOWN_PARK:
                caller_for_assign = "Park1"
                print(f"  {park}: AssignSolverAndVerifier 由 Park1 代调（无MSP检查）")
            else:
                caller_for_assign = park

            row = solve_one_park(
                park, k, mask_float, g_masked_float, h_vec_float, bundles,
                lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho,
                endorsers, caller_for_assign, park2_down=park2_is_down,
            )
            row["iter"] = k
            row["phase"] = phase
            row["park2_down"] = park2_is_down
            park_rows.append(row)
            print(f"  {park}: solver={row['solver']} verifier={row['verifier']} "
                  f"kkt={row['kkt_pass']} solve={row['t_solve_s']:.2f}s eE[0]={row['eE_0']:.4f}")

        # coordinate_step 固定背书 ["Park1","Park2"]，Park2宕机后需换成存活节点
        fc.retry(
            lambda: fc.invoke(
                caller_org="Park1", endorsing_orgs=endorsers,
                contract="ADMMCoordinationContract", function="CoordinateStep",
                args=[sid, str(k)], wait_seconds=1.0,
            ),
            attempts=5, delay_seconds=1.5,
        )
        state = wait_state_after_coordinate(sid, k)
        t_elapsed = round(time.time() - t_iter, 2)
        print(f"  chain: status={state['status']} "
              f"primalSq={state['primalResidSq']} dualSq={state['dualResidSq']} "
              f"t={t_elapsed}s")

        chain_rows.append({
            "iter": k, "phase": phase,
            "park2_down": park2_is_down,
            "status": state["status"],
            "primalResidSq": state["primalResidSq"],
            "dualResidSq": state["dualResidSq"],
            "rhoScaled": state["rhoScaled"],
            "t_iter_total_s": t_elapsed,
        })

        if state["status"] in ("CONVERGED", "FAILED"):
            final_status = state["status"]
            break
    else:
        final_status = pa.query_admm_state(sid)["status"]

    # 恢复 Park2 并验证追块同步
    if park2_is_down:
        docker_start_park2()
        print("\n[验证] 检查 Park2 gossip 追块同步...")
        check_iters = list(range(FAULT_ITER, min(FAULT_ITER + 5, len(chain_rows))))
        for ck in check_iters:
            for park in PARK_IDS:
                try:
                    rec = fc.query_with_retry("Park2", CONTRACT_ADMM,
                                              "QueryIterationRecord",
                                              [sid, str(ck), park],
                                              attempts=8, delay_seconds=2.0)
                    sync_rows.append({
                        "iter": ck, "park": park, "synced": True,
                        "eE_0": rec["eExScaled"][0] / 1000,
                    })
                    print(f"  ✓ iter={ck} {park}: 同步OK eE[0]={rec['eExScaled'][0]/1000:.4f}")
                except Exception as e:
                    sync_rows.append({"iter": ck, "park": park,
                                      "synced": False, "error": str(e)})
                    print(f"  ✗ iter={ck} {park}: 同步失败 {e}")

    # 保存 Excel
    out_wsl = os.path.join(os.path.dirname(__file__), "EXP3_fault_tolerance.xlsx")
    out_win = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "results", "EXP3_fault_tolerance.xlsx"))
    with pd.ExcelWriter(out_wsl, engine="openpyxl") as writer:
        pd.DataFrame(chain_rows).to_excel(writer, sheet_name="chain_per_iter", index=False)
        pd.DataFrame(park_rows).to_excel(writer, sheet_name="park_detail", index=False)
        if sync_rows:
            pd.DataFrame(sync_rows).to_excel(writer, sheet_name="sync_verification", index=False)
    subprocess.run(["cp", out_wsl, out_win], capture_output=True)

    # 终端汇总
    df = pd.DataFrame(chain_rows)
    p1 = df[~df["park2_down"]]
    p2 = df[df["park2_down"]]
    print("\n========== EXP-3 容错实验汇总 ==========")
    print(f"总轮数: {len(chain_rows)}  最终状态: {final_status}")
    print(f"Phase-1（3节点，iter 0~{FAULT_ITER-1}）: {len(p1)} 轮  "
          f"平均每轮 {p1['t_iter_total_s'].mean():.1f}s")
    print(f"Phase-2（2节点，iter {FAULT_ITER}+）: {len(p2)} 轮  "
          f"平均每轮 {p2['t_iter_total_s'].mean():.1f}s")
    if sync_rows:
        ok = sum(1 for r in sync_rows if r.get("synced"))
        print(f"Park2 账本同步: {ok}/{len(sync_rows)} 条验证通过")
    print(f"Excel: {out_win}")
    return sid, final_status


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iters", type=int, default=100)
    args = parser.parse_args()
    run(max_iters=args.max_iters)
