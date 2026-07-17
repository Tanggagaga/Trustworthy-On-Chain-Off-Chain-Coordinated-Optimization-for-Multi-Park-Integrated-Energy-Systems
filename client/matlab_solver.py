"""
MATLAB/Gurobi bridge for 4-energy 3-park ADMM.

The Fabric client runs in WSL; MATLAB and Gurobi run on Windows.
JSON files are exchanged via a shared Windows path mounted in WSL.

Solver script: solve_park_once_4e.m
  Input:  park_id, lambda_{E,G,H,C}[24], z_{E,G,H,C}[24], rho
  Output: eE/eG/eH/eC[24], local_cost, local_carbon, C_energy, C_carbon
"""

import json
import os
import subprocess
import uuid
from pathlib import Path


MATLAB_EXE = "/mnt/d/matlab/bin/matlab.exe"

# Directory containing solve_park_once_4e.m and the park CSV data files.
ADMM_DIR = Path(
    "/mnt/c/Users/Lenovo/Desktop/daima/综合能源"
    "/三个园区数据/新_三个园区设计/分布式ADMM"
)


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_matlab_batch(runner_name: str, timeout_seconds: int = 600) -> None:
    env = os.environ.copy()
    cmd = [MATLAB_EXE, "-batch", runner_name]
    result = subprocess.run(
        cmd,
        cwd=str(ADMM_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"MATLAB batch failed: {runner_name}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def solve_all_parks(
    lambda_e_rows: list[list[float]],   # [3][24]
    lambda_g_rows: list[list[float]],   # [3][24]
    lambda_h_rows: list[list[float]],   # [2][24]  Park1=index0, Park2=index1
    lambda_c_rows: list[list[float]],   # [3][24]
    z_e_rows: list[list[float]],        # [3][24]
    z_g_rows: list[list[float]],        # [3][24]
    z_h_rows: list[list[float]],        # [2][24]
    z_c_rows: list[list[float]],        # [3][24]
    rho: float,
) -> list[dict]:
    """
    Solve all three park subproblems in one MATLAB process.

    Three calls to solve_park_once_4e are batched into a single MATLAB
    startup to avoid paying the startup cost three times per iteration.

    Park3 has no heat exchange (lambda_H and z_H are zero for Park3).
    This is enforced here: Park3's heat inputs are always sent as zeros.

    Returns list of 3 result dicts (one per park, in order Park1/Park2/Park3).
    Each dict contains: eE, eG, eH, eC [24], local_cost, local_carbon, etc.
    """
    zeros24 = [0.0] * 24

    token = uuid.uuid4().hex
    runner_name = f"matlab_bridge_runner_all_{token}"
    runner_path = ADMM_DIR / f"{runner_name}.m"
    input_paths = []
    output_paths = []

    lines = []
    for park_index in (1, 2, 3):
        m = park_index - 1  # 0-based index
        input_path  = ADMM_DIR / f"matlab_bridge_input_{token}_{park_index}.json"
        output_path = ADMM_DIR / f"matlab_bridge_output_{token}_{park_index}.json"
        input_paths.append(input_path)
        output_paths.append(output_path)

        # Park3 (m=2) is not in the heat network: always send zeros for H.
        if m < 2:
            lam_h = lambda_h_rows[m]
            z_h   = z_h_rows[m]
        else:
            lam_h = zeros24
            z_h   = zeros24

        _write_json(input_path, {
            "park_id":  park_index,
            "lambda_E": lambda_e_rows[m],
            "lambda_G": lambda_g_rows[m],
            "lambda_H": lam_h,
            "lambda_C": lambda_c_rows[m],
            "z_E":      z_e_rows[m],
            "z_G":      z_g_rows[m],
            "z_H":      z_h,
            "z_C":      z_c_rows[m],
            "rho":      rho,
        })
        lines.append(
            f"solve_park_once_4e('{input_path.name}', '{output_path.name}');"
        )

    runner_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        _run_matlab_batch(runner_name, timeout_seconds=1200)
        results = [_read_json(path) for path in output_paths]
        for i, result in enumerate(results, start=1):
            if int(result["problem"]) != 0:
                raise RuntimeError(
                    f"MATLAB/Gurobi solve failed for Park{i}: {result['info']}"
                )
        return results
    finally:
        for path in [*input_paths, *output_paths, runner_path]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
