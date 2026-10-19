"""
M6-Timing：性能开销量化实验。

与 run_M6_full_masked_admm.py 逻辑完全一致，
在每个关键操作前后加 time.time() 计时，输出详细的每轮耗时分解。

计时说明：
  fc.invoke 内部在命令执行完之后会 sleep(wait_seconds)。
  本脚本记录的是包含该 sleep 的总墙钟时间。
  各操作的 wait_seconds 如下（在代码注释中标注）：
    AssignSolverAndVerifier : wait_seconds=1.0
    SubmitMaskedSolution    : wait_seconds=1.0
    RecordVerdict           : wait_seconds=1.0
    coordinate_step         : wait_seconds=1.0（内部retry，最多5次）
  纯网络+共识时间 ≈ 记录值 - wait_seconds（下限估计）。

输出 Excel 三个 sheet：
  registration_timing   — 三园区注册的一次性耗时
  chain_state_per_iter  — 每轮链上状态 + 本轮总墙钟时间 + coordinate耗时
  park_detail_per_iter  — 每轮每园区的全部操作耗时分解

隔离原则：scheduleID 用 "9994-M6TIMING-" 前缀。
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

ALL_PARK_ENDORSERS = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
CHUNK_CHARS = 50_000


def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    with open(os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json"), encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(schedule_id, park_id, bundle):
    """与M6原版完全相同的分块上传逻辑。"""
    fc.invoke(
        caller_org=park_id, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, park_id, str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
        wait_seconds=2.0,
    )
    parts = {
        "hE0":    json.dumps(bundle["hE0Scaled"],    separators=(",", ":")),
        "gMasked":json.dumps(bundle["gMaskedScaled"],separators=(",", ":")),
        "hVec":   json.dumps(bundle["hVecScaled"],   separators=(",", ":")),
        "c0":     json.dumps(bundle["c0Scaled"],     separators=(",", ":")),
        "eRows":  json.dumps(bundle["eRowsScaled"],  separators=(",", ":")),
        "x0e":    json.dumps(bundle["x0eScaled"],    separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        payload_chunks = chunks(payload)
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks, part_name=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=ALL_PARK_ENDORSERS,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, park_id, part_name, str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(0.5)
    fc.retry(
        lambda: fc.invoke(
            caller_org=park_id, endorsing_orgs=ALL_PARK_ENDORSERS,
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


def run(max_iters: int, log_tag: str = ""):
    toy_date = "9994-M6TIMING-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    schedule_id = setup(toy_date, max_iter=max_iters)
    print(f"scheduleID={schedule_id}")

    print("\n[预热] 三个园区容器")
    for park in PARK_IDS:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            raise RuntimeError(f"{park} 容器没就绪，必须先修好才能继续")

    # ── 注册阶段计时（一次性） ─────────────────────────────────────────
    print("\n[注册] 上传三个园区的masked结构（一次性）")
    reg_rows = []
    bundles = {}
    g_masked_float = {}
    h_vec_float = {}
    mask_float = {}

    for idx, park in enumerate(PARK_IDS, start=1):
        bundle = load_bundle(idx)
        bundles[park] = bundle
        print(f"  -> {park}: k0={bundle['k0']} qRows={bundle['qRows']}")

        t_reg0 = time.time()
        upload_large_mask(schedule_id, park, bundle)
        t_reg = time.time() - t_reg0

        g_masked_float[park] = [[v / MASK_SCALE for v in row] for row in bundle["gMaskedScaled"]]
        h_vec_float[park]    = [v / MASK_SCALE for v in bundle["hVecScaled"]]
        mask_float[park]     = to_float_mask(bundle)

        reg_rows.append({
            "park": park,
            "k0": bundle["k0"],
            "qRows": bundle["qRows"],
            "t_registration_s": round(t_reg, 3),
        })
        print(f"  -> {park} 注册完成，耗时 {t_reg:.2f}s")

    # ── 迭代阶段 ───────────────────────────────────────────────────────
    chain_rows = []
    park_rows  = []
    final_status = None

    for k in range(max_iters):
        print(f"\n--- M6-Timing iter {k} ---")
        t_iter_start = time.time()

        # 读链上 λ,z,ρ（每轮一次，三园区共享）
        t_get0 = time.time()
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(schedule_id)
        t_get_lambda = time.time() - t_get0
        print(f"  get_lambda_z_rho: {t_get_lambda:.3f}s  rho={rho:.6f}")

        for idx, park in enumerate(PARK_IDS):
            # ── 构建本轮 masked QP 系数 ──────────────────────────────
            if park == "Park3":
                lam_h_row, z_h_row = [0.0] * 24, [0.0] * 24
            else:
                lam_h_row, z_h_row = lam_h[idx], z_h[idx]
            lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
            z_block   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

            t_build0 = time.time()
            h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
            t_build = time.time() - t_build0

            # ── OSQP 求解 ────────────────────────────────────────────
            t_solve0 = time.time()
            y_star, mu_star = solve_masked_qp(h_masked, c_masked,
                                               g_masked_float[park], h_vec_float[park])
            t_solve = time.time() - t_solve0

            # ── 缩放 ─────────────────────────────────────────────────
            y_scaled  = [int(round(v * MASK_SCALE)) for v in y_star]
            mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
            bundle    = bundles[park]
            h_masked_scaled = [[int(round(rho * x)) for x in row] for row in bundle["hE0Scaled"]]
            c_masked_scaled = [int(round(x * MASK_SCALE)) for x in c_masked]

            # ── KKT 校验 ─────────────────────────────────────────────
            t_kkt0 = time.time()
            passed, stat_res, compl_res = check_kkt_scaled(
                h_masked_scaled, c_masked_scaled,
                bundle["gMaskedScaled"], bundle["hVecScaled"],
                y_scaled, mu_scaled,
            )
            t_kkt = time.time() - t_kkt0

            # ── invoke: AssignSolverAndVerifier (wait_seconds=1.0) ───
            t_assign0 = time.time()
            fc.invoke(
                caller_org=park, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                args=[schedule_id, str(k), park], wait_seconds=1.0,
            )
            t_assign = time.time() - t_assign0  # 含 1.0s sleep

            # ── query: QuerySolverAssignment ─────────────────────────
            t_qassign0 = time.time()
            assignment = fc.query_with_retry(park, CONTRACT_MASK, "QuerySolverAssignment",
                                             [schedule_id, str(k), park])
            t_qassign = time.time() - t_qassign0
            solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]
            if solver == park:
                raise RuntimeError(f"严重错误：iter{k} {park}的solver分配到了自己！")

            # ── invoke: SubmitMaskedSolution (wait_seconds=1.0) ──────
            t_submit0 = time.time()
            fc.invoke(
                caller_org=solver, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="SubmitMaskedSolution",
                args=[schedule_id, str(k), park,
                      json.dumps(y_scaled), json.dumps(mu_scaled)],
                wait_seconds=1.0,
            )
            t_submit = time.time() - t_submit0  # 含 1.0s sleep

            # ── query: QueryMaskedSolution ───────────────────────────
            t_qsol0 = time.time()
            msol = fc.query_with_retry(park, CONTRACT_MASK, "QueryMaskedSolution",
                                       [schedule_id, str(k), park])
            t_qsol = time.time() - t_qsol0

            # ── invoke: RecordVerdict (wait_seconds=1.0) ─────────────
            t_verdict0 = time.time()
            fc.invoke(
                caller_org=verifier, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="RecordVerdict",
                args=[schedule_id, str(k), park,
                      "true" if passed else "false",
                      str(stat_res), str(compl_res), msol["solutionHash"]],
                wait_seconds=1.0,
            )
            t_verdict = time.time() - t_verdict0  # 含 1.0s sleep

            if not passed:
                raise RuntimeError(
                    f"严重错误：iter{k} {park}的KKT校验没通过"
                    f"(stat={stat_res},compl={compl_res})"
                )

            # ── query: QueryIterationRecord ──────────────────────────
            t_qrec0 = time.time()
            rec = fc.query_with_retry(park, CONTRACT_ADMM, "QueryIterationRecord",
                                      [schedule_id, str(k), park])
            t_qrec = time.time() - t_qrec0

            # 三次 invoke 合计（含 sleep）
            t_chain_invokes = t_assign + t_submit + t_verdict
            # 三次 query 合计
            t_queries = t_qassign + t_qsol + t_qrec
            # 纯计算合计
            t_compute = t_build + t_solve + t_kkt

            print(
                f"  {park}: solver={solver} verifier={verifier} pass={passed} "
                f"solve={t_solve:.2f}s kkt={t_kkt:.3f}s "
                f"chain_invokes={t_chain_invokes:.2f}s eE[0]={rec['eExScaled'][0]/1000:.4f}"
            )

            park_rows.append({
                "iter":   k,
                "park":   park,
                "solver": solver,
                "verifier": verifier,
                "kkt_pass": passed,
                "stat_res": stat_res,
                "compl_res": compl_res,
                "rho": rho,
                # ── 耗时分解（秒）────────────────────────────────────
                "t_build_s":          round(t_build, 4),   # build_masked_round（纯计算）
                "t_solve_s":          round(t_solve, 4),   # OSQP求解（纯计算）
                "t_kkt_s":            round(t_kkt,   4),   # KKT校验（纯计算）
                "t_assign_s":         round(t_assign, 3),  # AssignSolverAndVerifier（含1s sleep）
                "t_query_assign_s":   round(t_qassign,3),  # QuerySolverAssignment
                "t_submit_s":         round(t_submit, 3),  # SubmitMaskedSolution（含1s sleep）
                "t_query_solution_s": round(t_qsol,   3),  # QueryMaskedSolution
                "t_verdict_s":        round(t_verdict, 3), # RecordVerdict（含1s sleep）
                "t_query_record_s":   round(t_qrec,   3),  # QueryIterationRecord
                # ── 汇总 ─────────────────────────────────────────────
                "t_compute_total_s":       round(t_compute, 4),       # 纯计算合计
                "t_chain_invokes_total_s": round(t_chain_invokes, 3), # 三次invoke合计（含sleep）
                "t_queries_total_s":       round(t_queries, 3),       # 三次query合计
                # ── e_m 结果 ─────────────────────────────────────────
                **{f"eE_{h:02d}": rec["eExScaled"][h] for h in range(24)},
                **{f"eG_{h:02d}": rec["gExScaled"][h] for h in range(24)},
                **{f"eH_{h:02d}": rec["hExScaled"][h] for h in range(24)},
                **{f"eC_{h:02d}": rec["cExScaled"][h] for h in range(24)},
            })

        # ── coordinate_step（触发z/λ更新） ────────────────────────────
        t_coord0 = time.time()
        pa.coordinate_step(schedule_id, k)
        t_coord = time.time() - t_coord0  # 含 retry 和 wait_seconds=1.0

        # ── wait_state_after_coordinate ───────────────────────────────
        t_wait0 = time.time()
        state = wait_state_after_coordinate(schedule_id, k)
        t_wait = time.time() - t_wait0

        t_iter_total = time.time() - t_iter_start

        print(
            f"  chain: iter={state['iter']} status={state['status']} "
            f"primalSq={state['primalResidSq']} dualSq={state['dualResidSq']} "
            f"rhoScaled={state['rhoScaled']}"
        )
        print(
            f"  耗时: get_λz={t_get_lambda:.2f}s "
            f"coord={t_coord:.2f}s wait={t_wait:.2f}s "
            f"iter_total={t_iter_total:.2f}s"
        )

        chain_rows.append({
            "iter":          k,
            "status":        state["status"],
            "converged":     state.get("converged"),
            "primalResidSq": state["primalResidSq"],
            "dualResidSq":   state["dualResidSq"],
            "rhoScaled":     state["rhoScaled"],
            # ── 本轮级别耗时 ──────────────────────────────────────────
            "t_get_lambda_z_s":  round(t_get_lambda, 3),  # 读链上λ,z,ρ
            "t_coordinate_s":    round(t_coord, 3),        # CoordinateStep invoke（含sleep）
            "t_wait_state_s":    round(t_wait, 3),         # 等待状态确认
            "t_iter_total_s":    round(t_iter_total, 3),   # 整轮墙钟时间
        })

        if state["status"] in ("CONVERGED", "FAILED"):
            final_status = state["status"]
            break
    else:
        state = pa.query_admm_state(schedule_id)
        final_status = state["status"]

    # ── 输出 Excel ────────────────────────────────────────────────────
    out_path = f"M6_timing{log_tag}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        pd.DataFrame(reg_rows).to_excel(writer, sheet_name="registration_timing", index=False)
        pd.DataFrame(chain_rows).to_excel(writer, sheet_name="chain_state_per_iter", index=False)
        pd.DataFrame(park_rows).to_excel(writer, sheet_name="park_detail_per_iter", index=False)

    # ── 终端汇总 ──────────────────────────────────────────────────────
    if park_rows:
        df = pd.DataFrame(park_rows)
        print("\n========== 性能开销汇总 ==========")
        print(f"总轮数: {len(chain_rows)}")
        print(f"平均每园区每轮耗时（秒）:")
        for col in ["t_build_s","t_solve_s","t_kkt_s",
                    "t_assign_s","t_submit_s","t_verdict_s",
                    "t_query_assign_s","t_query_solution_s","t_query_record_s",
                    "t_compute_total_s","t_chain_invokes_total_s","t_queries_total_s"]:
            print(f"  {col:<30s}: {df[col].mean():.3f}s")
        df_chain = pd.DataFrame(chain_rows)
        print(f"平均每轮总墙钟时间: {df_chain['t_iter_total_s'].mean():.2f}s")
        print(f"注: chain_invokes 含固定 sleep(1.0s)×3，纯共识延迟 ≈ chain_invokes - 3.0s")

    print(f"\n=== M6-Timing完成：status={final_status}，共{len(chain_rows)}轮，结果写入 {out_path} ===")
    return schedule_id, final_status, out_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iters", type=int, default=100)
    parser.add_argument("--tag", type=str, default="")
    args = parser.parse_args()
    run(max_iters=args.max_iters, log_tag=args.tag)
