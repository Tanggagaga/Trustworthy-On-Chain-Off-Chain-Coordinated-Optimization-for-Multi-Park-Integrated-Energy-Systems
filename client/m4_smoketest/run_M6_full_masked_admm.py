"""
M6：完整三园区、多轮真实ADMM收敛，全程走mask信任机制（注册→分配→OSQP独立
求解→KKT校验→裁决→链上反解e_m→CoordinateStep），跟M0/M1a的诚实基线对比。

跟之前所有M4测试的关键区别：
  - 三个园区都参与（不是只测Park1）
  - 每一轮的λ,z,ρ都是从链上当前ADMMState现场读出来的（不是固定/缓存值）
  - 每一轮solver都现场用OSQP重新解题，不借用任何缓存答案
  - 分配、KKT校验、信用分变化全部记录，逐轮导出Excel

localCost/localCarbon这次依然记0（mask_contract.go现有设计的已知简化，
不影响ADMM收敛动力学，只影响结算环节——这次的重点是验证收敛轨迹本身）。

★ 隔离原则：scheduleID 用 "9994-M6FULL-" 前缀，跟此前所有测试和真实
  2024-03-01算例都不冲突。
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
BUNDLE_DIR = "/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/M1a离线Mask验证/M6完整闭环"
CHUNK_CHARS = 50_000


def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    with open(os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json"), encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(schedule_id, park_id, bundle):
    fc.invoke(
        caller_org=park_id, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, park_id, str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
        wait_seconds=2.0,
    )
    parts = {
        "hE0": json.dumps(bundle["hE0Scaled"], separators=(",", ":")),
        "gMasked": json.dumps(bundle["gMaskedScaled"], separators=(",", ":")),
        "hVec": json.dumps(bundle["hVecScaled"], separators=(",", ":")),
        "c0": json.dumps(bundle["c0Scaled"], separators=(",", ":")),
        "eRows": json.dumps(bundle["eRowsScaled"], separators=(",", ":")),
        "x0e": json.dumps(bundle["x0eScaled"], separators=(",", ":")),
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
        "H_e0": [[v / MASK_SCALE for v in row] for row in bundle["hE0Scaled"]],
        "C0": [v / MASK_SCALE for v in bundle["c0Scaled"]],
        "E_rows": [[v / MASK_SCALE for v in row] for row in bundle["eRowsScaled"]],
        "x0_e": [v / MASK_SCALE for v in bundle["x0eScaled"]],
    }


def run(max_iters: int, log_tag: str = ""):
    toy_date = "9994-M6FULL-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    schedule_id = setup(toy_date, max_iter=max_iters)
    print(f"scheduleID={schedule_id}")

    print("\n[预热] 三个园区容器")
    for park in PARK_IDS:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            raise RuntimeError(f"{park} 容器没就绪，必须先修好才能继续")

    print("\n[注册] 上传三个园区的真实masked结构（一次性）")
    bundles = {}
    g_masked_float = {}
    h_vec_float = {}
    mask_float = {}
    for idx, park in enumerate(PARK_IDS, start=1):
        bundle = load_bundle(idx)
        bundles[park] = bundle
        print(f"  -> {park}: k0={bundle['k0']} qRows={bundle['qRows']}")
        upload_large_mask(schedule_id, park, bundle)
        g_masked_float[park] = [[v / MASK_SCALE for v in row] for row in bundle["gMaskedScaled"]]
        h_vec_float[park] = [v / MASK_SCALE for v in bundle["hVecScaled"]]
        mask_float[park] = to_float_mask(bundle)
        print(f"  -> {park} 注册完成")

    chain_rows = []
    park_rows = []
    final_status = None

    for k in range(max_iters):
        print(f"\n--- M6 iter {k} ---")
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(schedule_id)
        print(f"rho={rho:.6f}")

        for idx, park in enumerate(PARK_IDS):
            if park == "Park3":
                lam_h_row, z_h_row = [0.0] * 24, [0.0] * 24
            else:
                lam_h_row, z_h_row = lam_h[idx], z_h[idx]
            lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
            z_block = z_e[idx] + z_g[idx] + z_h_row + z_c[idx]

            h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
            t0 = time.time()
            y_star, mu_star = solve_masked_qp(h_masked, c_masked, g_masked_float[park], h_vec_float[park])
            solve_seconds = time.time() - t0

            y_scaled = [int(round(v * MASK_SCALE)) for v in y_star]
            mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
            bundle = bundles[park]
            h_masked_scaled = [[int(round(rho * x)) for x in row] for row in bundle["hE0Scaled"]]
            c_masked_scaled = [int(round(x * MASK_SCALE)) for x in c_masked]
            passed, stat_res, compl_res = check_kkt_scaled(
                h_masked_scaled, c_masked_scaled, bundle["gMaskedScaled"], bundle["hVecScaled"],
                y_scaled, mu_scaled,
            )

            fc.invoke(
                caller_org=park, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
                args=[schedule_id, str(k), park], wait_seconds=1.0,
            )
            assignment = fc.query_with_retry(park, CONTRACT_MASK, "QuerySolverAssignment",
                                              [schedule_id, str(k), park])
            solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]
            if solver == park:
                raise RuntimeError(f"严重错误：iter{k} {park}的solver分配到了自己！必须停下来排查")

            fc.invoke(
                caller_org=solver, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="SubmitMaskedSolution",
                args=[schedule_id, str(k), park, json.dumps(y_scaled), json.dumps(mu_scaled)],
                wait_seconds=1.0,
            )
            msol = fc.query_with_retry(park, CONTRACT_MASK, "QueryMaskedSolution", [schedule_id, str(k), park])

            fc.invoke(
                caller_org=verifier, endorsing_orgs=ALL_PARK_ENDORSERS,
                contract=CONTRACT_MASK, function="RecordVerdict",
                args=[schedule_id, str(k), park, "true" if passed else "false",
                      str(stat_res), str(compl_res), msol["solutionHash"]],
                wait_seconds=1.0,
            )
            if not passed:
                raise RuntimeError(f"严重错误：iter{k} {park}的KKT校验没通过(stat={stat_res},compl={compl_res})，必须停下来排查")

            rec = fc.query_with_retry(park, CONTRACT_ADMM, "QueryIterationRecord", [schedule_id, str(k), park])
            print(f"  {park}: solver={solver} verifier={verifier} pass={passed} "
                  f"solve={solve_seconds:.2f}s eE[0]={rec['eExScaled'][0]/1000:.4f}")

            park_rows.append({
                "iter": k, "park": park, "solver": solver, "verifier": verifier,
                "pass": passed, "stationarity_res": stat_res, "complement_res": compl_res,
                "solve_seconds": solve_seconds, "rho": rho,
                **{f"eE_{h:02d}": rec["eExScaled"][h] for h in range(24)},
                **{f"eG_{h:02d}": rec["gExScaled"][h] for h in range(24)},
                **{f"eH_{h:02d}": rec["hExScaled"][h] for h in range(24)},
                **{f"eC_{h:02d}": rec["cExScaled"][h] for h in range(24)},
            })

        pa.coordinate_step(schedule_id, k)
        state = wait_state_after_coordinate(schedule_id, k)
        print(f"  chain: iter={state['iter']} status={state['status']} "
              f"primalSq={state['primalResidSq']} dualSq={state['dualResidSq']} rhoScaled={state['rhoScaled']}")
        chain_rows.append({
            "iter": k, "status": state["status"], "converged": state.get("converged"),
            "primalResidSq": state["primalResidSq"], "dualResidSq": state["dualResidSq"],
            "rhoScaled": state["rhoScaled"],
        })

        if state["status"] in ("CONVERGED", "FAILED"):
            final_status = state["status"]
            break
    else:
        state = pa.query_admm_state(schedule_id)
        final_status = state["status"]

    out_path = f"M6_results{log_tag}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        pd.DataFrame(chain_rows).to_excel(writer, sheet_name="chain_state_per_iter", index=False)
        pd.DataFrame(park_rows).to_excel(writer, sheet_name="park_detail_per_iter", index=False)

    print(f"\n=== M6完成：status={final_status}，共{len(chain_rows)}轮，结果写入 {out_path} ===")
    return schedule_id, final_status, out_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iters", type=int, default=100)
    parser.add_argument("--tag", type=str, default="")
    args = parser.parse_args()
    run(max_iters=args.max_iters, log_tag=args.tag)
