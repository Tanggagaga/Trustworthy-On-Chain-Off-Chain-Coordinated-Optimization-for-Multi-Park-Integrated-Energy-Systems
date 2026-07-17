"""Export a converged chain schedule and compare it with offline MATLAB ADMM."""

import csv
import json
import math
import sys
from pathlib import Path

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM, E_SCALE, LAMBDA_SCALE, COST_SCALE, CARBON_SCALE


OFFLINE_DIR = Path("/mnt/c/Users/Lenovo/Desktop/daima/综合能源/三个园区数据/ADMM功率平衡")
OUTPUT_DIR = Path("/mnt/c/Users/Lenovo/Desktop/daima/综合能源/阶段五结果")

PARK_Z_COLUMNS = [
    "z_park1_base",
    "z_park2_industrial_high_load",
    "z_park3_renewable_rich",
]


def _load_offline_timeseries() -> list[dict]:
    path = OFFLINE_DIR / "admmpb_timeseries.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _load_offline_summary() -> dict:
    path = OFFLINE_DIR / "admmpb_summary.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[0]


def _scaled_matrix(matrix, scale: float) -> list[list[float]]:
    return [[v / scale for v in row] for row in matrix]


def _write_chain_csv(schedule_id: str, final: dict, state: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{schedule_id}_chain_timeseries.csv"

    z = _scaled_matrix(final["finalZScaled"], E_SCALE)
    e = _scaled_matrix(final["finalEExchangeScaled"], E_SCALE)
    lam = _scaled_matrix(final["finalLambdaScaled"], LAMBDA_SCALE)
    clearing = [v / LAMBDA_SCALE for v in final["clearingLambdaScaled"]]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "hour",
            "z_park1", "z_park2", "z_park3",
            "e_park1", "e_park2", "e_park3",
            "lambda_park1", "lambda_park2", "lambda_park3",
            "clearing_lambda",
            "sum_z", "sum_e",
        ])
        for t in range(24):
            writer.writerow([
                t + 1,
                z[0][t], z[1][t], z[2][t],
                e[0][t], e[1][t], e[2][t],
                lam[0][t], lam[1][t], lam[2][t],
                clearing[t],
                z[0][t] + z[1][t] + z[2][t],
                e[0][t] + e[1][t] + e[2][t],
            ])
    return path


def _write_comparison_csv(schedule_id: str, final: dict, offline_rows: list[dict]) -> tuple[Path, dict]:
    path = OUTPUT_DIR / f"{schedule_id}_compare_offline_admm.csv"
    chain_z = _scaled_matrix(final["finalZScaled"], E_SCALE)

    max_abs_error = 0.0
    sum_abs_error = 0.0
    sum_sq_error = 0.0
    n = 0

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "hour",
            "chain_z_park1", "offline_z_park1", "diff_park1",
            "chain_z_park2", "offline_z_park2", "diff_park2",
            "chain_z_park3", "offline_z_park3", "diff_park3",
        ])
        for t in range(24):
            row = [t + 1]
            for m, col in enumerate(PARK_Z_COLUMNS):
                chain_value = chain_z[m][t]
                offline_value = float(offline_rows[t][col])
                diff = chain_value - offline_value
                row.extend([chain_value, offline_value, diff])
                max_abs_error = max(max_abs_error, abs(diff))
                sum_abs_error += abs(diff)
                sum_sq_error += diff * diff
                n += 1
            writer.writerow(row)

    metrics = {
        "max_abs_z_error_mw": max_abs_error,
        "mean_abs_z_error_mw": sum_abs_error / n,
        "rmse_z_error_mw": math.sqrt(sum_sq_error / n),
    }
    return path, metrics


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 export_compare_5b.py SCHEDULE_ID")

    schedule_id = sys.argv[1]
    state = pa.query_admm_state(schedule_id)
    final = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryFinalSchedule", [schedule_id])

    offline_rows = _load_offline_timeseries()
    offline_summary = _load_offline_summary()

    chain_csv = _write_chain_csv(schedule_id, final, state)
    compare_csv, compare_metrics = _write_comparison_csv(schedule_id, final, offline_rows)

    chain_cost = final["totalCostScaled"] / COST_SCALE
    chain_carbon = final["totalCarbonScaled"] / CARBON_SCALE
    offline_cost = float(offline_summary["admm_obj"])

    summary = {
        "schedule_id": schedule_id,
        "chain_status": state["status"],
        "converged": state["converged"],
        "converged_iter": final["convergedIter"],
        "is_fallback": final["isFallback"],
        "primal_resid_sq": state["primalResidSq"],
        "dual_resid_sq": state["dualResidSq"],
        "rho_scaled": state["rhoScaled"],
        "chain_total_cost": chain_cost,
        "chain_total_carbon": chain_carbon,
        "offline_admm_cost": offline_cost,
        "cost_abs_error": chain_cost - offline_cost,
        "cost_rel_error_pct": (chain_cost - offline_cost) / abs(offline_cost) * 100,
        **compare_metrics,
        "chain_csv": str(chain_csv),
        "compare_csv": str(compare_csv),
    }

    summary_path = OUTPUT_DIR / f"{schedule_id}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
