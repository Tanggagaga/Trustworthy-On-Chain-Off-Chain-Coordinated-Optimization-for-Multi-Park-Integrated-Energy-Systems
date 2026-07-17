"""
M0 基线实验：跑一次完整的链上四能 ADMM + MATLAB/Gurobi 收敛流程，
逐轮记录所有中间数据（lambda/z/rho/e/cost/carbon/残差），导出为 Excel。

用途：
  1. 作为后续 mask 机制改造的回归对比基线。
  2. 作为后续"作恶场景"实验的诚实基准（对比攻击前后的差异）。

输出文件：M0_baseline_<schedule_id>.xlsx，包含4个sheet：
  - chain_state_per_iter   每轮链上状态（rho, 残差, status）
  - park_iteration_summary 每轮每园区提交摘要（cost, carbon, e_m每小时值）
  - lambda_z_detail        每轮每能源每园区每小时的 lambda/z 明细（长表）
  - settlement             收敛后结算与信用分
"""

import time
from datetime import datetime

import matlab_solver
import park_agent as pa
from config import PARK_IDS, DEFAULT_MAX_ITER
from run_5a_test import setup, wait_state_after_coordinate

import pandas as pd


ENERGY_TYPES = ["E", "G", "H", "C"]


def _date_tag() -> str:
    return "2024-03-01-" + datetime.now().strftime("%H%M%S")


def run_and_log(max_iters: int = DEFAULT_MAX_ITER):
    date = _date_tag()
    schedule_id = setup(date, max_iter=max_iters)

    chain_rows = []
    park_rows = []
    lambda_z_rows = []

    final_status = None

    for k in range(max_iters):
        print(f"\n--- ADMM iter {k} ---")

        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = \
            pa.get_lambda_z_rho(schedule_id)
        print(f"rho={rho:.6f}")

        # 记录 lambda/z 明细（长表：iter, energy, park_idx, hour, lambda, z）
        for energy, lam_mat, z_mat in [
            ("E", lam_e, z_e), ("G", lam_g, z_g),
            ("H", lam_h, z_h), ("C", lam_c, z_c),
        ]:
            for park_idx, (lam_row, z_row) in enumerate(zip(lam_mat, z_mat)):
                for hour, (lv, zv) in enumerate(zip(lam_row, z_row)):
                    lambda_z_rows.append({
                        "iter": k, "energy": energy, "park_idx": park_idx,
                        "hour": hour, "lambda": lv, "z": zv, "rho": rho,
                    })

        t0 = time.time()
        results = matlab_solver.solve_all_parks(
            lam_e, lam_g, lam_h, lam_c,
            z_e, z_g, z_h, z_c,
            rho,
        )
        solve_seconds = time.time() - t0
        print(f"MATLAB solved 3 parks in {solve_seconds:.1f}s")

        for park_id, result in zip(PARK_IDS, results):
            eE, eG, eH, eC = result["eE"], result["eG"], result["eH"], result["eC"]
            cost = float(result["local_cost"])
            carbon = float(result["local_carbon"])

            pa.submit_iteration_result(
                schedule_id, k, park_id, eE, eG, eH, eC, cost, carbon,
            )
            print(
                f"  {park_id}: cost={cost:.4f}  carbon={carbon:.4f}"
                f"  eE[0]={eE[0]:.4f}  eG[0]={eG[0]:.4f}"
                f"  eH[0]={eH[0]:.4f}  eC[0]={eC[0]:.4f}"
            )

            row = {
                "iter": k, "park_id": park_id,
                "local_cost": cost, "local_carbon": carbon,
                "rho": rho, "solve_seconds": solve_seconds,
            }
            for prefix, arr in [("eE", eE), ("eG", eG), ("eH", eH), ("eC", eC)]:
                for hour, v in enumerate(arr):
                    row[f"{prefix}_{hour:02d}"] = v
            park_rows.append(row)

        pa.coordinate_step(schedule_id, k)
        state = wait_state_after_coordinate(schedule_id, k)
        print(
            f"  chain: iter={state['iter']}  status={state['status']}"
            f"  primalSq={state['primalResidSq']}  dualSq={state['dualResidSq']}"
            f"  rhoScaled={state['rhoScaled']}"
        )
        chain_rows.append({
            "iter": k,
            "status": state["status"],
            "converged": state.get("converged"),
            "primalResidSq": state["primalResidSq"],
            "dualResidSq": state["dualResidSq"],
            "rhoScaled": state["rhoScaled"],
            "solve_seconds": solve_seconds,
        })

        if state["status"] in ("CONVERGED", "FAILED"):
            final_status = state["status"]
            break
    else:
        state = pa.query_admm_state(schedule_id)
        final_status = state["status"]

    settlement_rows = []
    if final_status == "CONVERGED":
        print("\n  SettleAfterConvergence ...")
        import fabric_client as fc
        from config import CONTRACT_SETTLEMENT
        fc.invoke(
            caller_org="Park1",
            endorsing_orgs=["Park1", "Park2"],
            contract=CONTRACT_SETTLEMENT,
            function="SettleAfterConvergence",
            args=[schedule_id],
        )
        for park_id in PARK_IDS:
            result = fc.query_with_retry(
                caller_org="Park1",
                contract=CONTRACT_SETTLEMENT,
                function="QuerySettlement",
                args=[schedule_id, park_id],
            )
            score = pa.query_credit_score(park_id)
            print(
                f"  {park_id}: payE={result['energyPaymentEScaled']}  "
                f"payG={result['energyPaymentGScaled']}  "
                f"payH={result['energyPaymentHScaled']}  "
                f"payC={result['energyPaymentCScaled']}  "
                f"totalEnergy={result['totalEnergyPaymentScaled']}  "
                f"carbon={result['carbonPaymentScaled']}  "
                f"total={result['totalPaymentScaled']}  "
                f"creditScore={score}"
            )
            row = dict(result)
            row["park_id"] = park_id
            row["creditScore"] = score
            settlement_rows.append(row)

    out_path = f"M0_baseline_{schedule_id}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        pd.DataFrame(chain_rows).to_excel(writer, sheet_name="chain_state_per_iter", index=False)
        pd.DataFrame(park_rows).to_excel(writer, sheet_name="park_iteration_summary", index=False)
        pd.DataFrame(lambda_z_rows).to_excel(writer, sheet_name="lambda_z_detail", index=False)
        pd.DataFrame(settlement_rows).to_excel(writer, sheet_name="settlement", index=False)

    print(f"\n✅ M0基线完成，status={final_status}，结果写入 {out_path}")
    return out_path, final_status


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITER)
    args = parser.parse_args()
    run_and_log(max_iters=args.max_iters)
