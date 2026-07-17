"""
Stage 5B：Fabric 链上 ADMM 协调 + MATLAB/Gurobi 链下四能子问题求解。

完整闭环：
  链上 lambda/z/rho (四能)
  → MATLAB solve_park_once_4e（三园区，每园区输出 eE/eG/eH/eC）
  → 提交四能交换量上链
  → CoordinateStep（链上 z/lambda 更新、残差、判敛）
  → 收敛后结算

Park3 的热能交换量 eH 由 MATLAB 约束为零（eH_3=0），
链上 SubmitIterationResult 会二次校验。
"""

import argparse
import time
from datetime import datetime

import matlab_solver
import park_agent as pa
from config import PARK_IDS, DEFAULT_MAX_ITER
from run_5a_test import setup, settle_and_report, wait_state_after_coordinate


def _date_tag() -> str:
    return "2024-03-01-" + datetime.now().strftime("%H%M%S")


def run(max_iters: int, settle: bool) -> str:
    date = _date_tag()
    schedule_id = setup(date, max_iter=max_iters)

    for k in range(max_iters):
        print(f"\n--- ADMM iter {k} ---")

        # ── 1. 读取链上当前协调变量（四能） ─────────────────────────────
        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = \
            pa.get_lambda_z_rho(schedule_id)
        print(f"rho={rho:.6f}")

        # ── 2. 链下：MATLAB/Gurobi 求解三个园区子问题 ──────────────────
        # lambda_h, z_h 是 [2][24]（Park1, Park2）；
        # matlab_solver 内部对 Park3 自动补零。
        t0 = time.time()
        results = matlab_solver.solve_all_parks(
            lam_e, lam_g, lam_h, lam_c,
            z_e,   z_g,   z_h,   z_c,
            rho,
        )
        print(f"MATLAB solved 3 parks in {time.time() - t0:.1f}s")

        # ── 3. 三园区分别提交本轮四能交换量 ────────────────────────────
        for park_id, result in zip(PARK_IDS, results):
            eE   = result["eE"]
            eG   = result["eG"]
            eH   = result["eH"]
            eC   = result["eC"]
            cost   = float(result["local_cost"])
            carbon = float(result["local_carbon"])

            pa.submit_iteration_result(
                schedule_id, k, park_id,
                eE, eG, eH, eC,
                cost, carbon,
            )
            print(
                f"  {park_id}: cost={cost:.4f}  carbon={carbon:.4f}"
                f"  eE[0]={eE[0]:.4f}  eG[0]={eG[0]:.4f}"
                f"  eH[0]={eH[0]:.4f}  eC[0]={eC[0]:.4f}"
            )

        # ── 4. 链上协调步骤 ──────────────────────────────────────────────
        pa.coordinate_step(schedule_id, k)
        state = wait_state_after_coordinate(schedule_id, k)
        print(
            f"  chain: iter={state['iter']}  status={state['status']}"
            f"  primalSq={state['primalResidSq']}  dualSq={state['dualResidSq']}"
            f"  rhoScaled={state['rhoScaled']}"
        )

        # ── 5. 判断终止 ──────────────────────────────────────────────────
        if state["status"] in ("CONVERGED", "FAILED"):
            if settle:
                settle_and_report(schedule_id)
            return state["status"]

    # 达到 max_iters 但链上未判定（理论上不会，因为链上有 maxIter 保护）
    state = pa.query_admm_state(schedule_id)
    if settle and state["status"] in ("CONVERGED", "FAILED"):
        settle_and_report(schedule_id)
    return state["status"]


def main():
    parser = argparse.ArgumentParser(
        description="Run 4-energy blockchain ADMM with MATLAB/Gurobi local solvers."
    )
    parser.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITER,
                        help="Maximum ADMM iterations (default: config.DEFAULT_MAX_ITER)")
    parser.add_argument("--settle", action="store_true",
                        help="Run settlement after convergence")
    args = parser.parse_args()

    status = run(max_iters=args.max_iters, settle=args.settle)
    print(f"\n5B finished with status={status}")


if __name__ == "__main__":
    main()
