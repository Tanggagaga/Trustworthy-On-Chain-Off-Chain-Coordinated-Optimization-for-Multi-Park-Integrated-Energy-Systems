"""
单个园区的链上操作逻辑（四能版本）。

每个园区负责：
  1. 读取链上当前四种能源的 lambda / z / rho
  2. 提交本轮四种能源的 e_m 结果
"""

import json

import fabric_client as fc
from config import (
    CONTRACT_ADMM, CONTRACT_SETTLEMENT, CONTRACT_AUDIT,
    E_SCALE, LAMBDA_SCALE, RHO_SCALE, COST_SCALE, CARBON_SCALE,
    PARK_IDS,
)


# ── 辅助：背书组合 ────────────────────────────────────────────────────
def _endorsers_for(park_id: str) -> list:
    """
    Park1 → [Park1, Park2]
    Park2 → [Park1, Park2]
    Park3 → [Park2, Park3]
    """
    if park_id == "Park3":
        return ["Park2", "Park3"]
    return ["Park1", "Park2"]


# ── 链上查询 ──────────────────────────────────────────────────────────
def query_admm_state(schedule_id: str) -> dict:
    """读取链上 ADMMState，返回原始 dict（整数缩放）。"""
    return fc.query_with_retry(
        caller_org="Park1",
        contract=CONTRACT_ADMM,
        function="QueryADMMState",
        args=[schedule_id],
    )


def get_lambda_z_rho(schedule_id: str):
    """
    读链上状态并反缩放为浮点，供本地优化器使用。

    返回:
        lambda_e [3][24], lambda_g [3][24],
        lambda_h [2][24],  (Park1=index0, Park2=index1; Park3 不在热网)
        lambda_c [3][24],
        z_e [3][24], z_g [3][24],
        z_h [2][24],
        z_c [3][24],
        rho (float)
    """
    state = query_admm_state(schedule_id)

    def descale_matrix(key, scale):
        return [[v / scale for v in row] for row in state[key]]

    lambda_e = descale_matrix("lambdaEScaled", LAMBDA_SCALE)  # [3][24]
    lambda_g = descale_matrix("lambdaGScaled", LAMBDA_SCALE)  # [3][24]
    lambda_h = descale_matrix("lambdaHScaled", LAMBDA_SCALE)  # [2][24]
    lambda_c = descale_matrix("lambdaCScaled", LAMBDA_SCALE)  # [3][24]

    z_e = descale_matrix("zEScaled", E_SCALE)  # [3][24]
    z_g = descale_matrix("zGScaled", E_SCALE)  # [3][24]
    z_h = descale_matrix("zHScaled", E_SCALE)  # [2][24]
    z_c = descale_matrix("zCScaled", E_SCALE)  # [3][24]

    rho = state["rhoScaled"] / RHO_SCALE

    return lambda_e, lambda_g, lambda_h, lambda_c, z_e, z_g, z_h, z_c, rho


# ── 链上写入 ──────────────────────────────────────────────────────────
def submit_iteration_result(
    schedule_id: str,
    iter_k: int,
    park_id: str,
    e_e_float: list[float],   # 电力净交换，MW，长度24
    e_g_float: list[float],   # 天然气净交换，MW_gas，长度24
    e_h_float: list[float],   # 热能净交换，MW_th，长度24（Park3 必须全为0）
    e_c_float: list[float],   # 冷能净交换，MW_c，长度24
    local_cost_float: float,
    local_carbon_float: float,
) -> None:
    """
    将本轮四能优化结果提交上链。
    自动做浮点→整数缩放，自动选择正确的背书 Peer。
    """
    def scale24(vals):
        return [int(round(v * E_SCALE)) for v in vals]

    ee_scaled = scale24(e_e_float)
    eg_scaled = scale24(e_g_float)
    eh_scaled = scale24(e_h_float)
    ec_scaled = scale24(e_c_float)
    cost_scaled   = int(round(local_cost_float   * COST_SCALE))
    carbon_scaled = int(round(local_carbon_float * CARBON_SCALE))

    fc.invoke(
        caller_org     = park_id,
        endorsing_orgs = _endorsers_for(park_id),
        contract       = CONTRACT_ADMM,
        function       = "SubmitIterationResult",
        args=[
            schedule_id,
            str(iter_k),
            park_id,
            json.dumps(ee_scaled),
            json.dumps(eg_scaled),
            json.dumps(eh_scaled),
            json.dumps(ec_scaled),
            str(cost_scaled),
            str(carbon_scaled),
        ],
        wait_seconds=0.5,
    )


def coordinate_step(schedule_id: str, iter_k: int) -> None:
    """触发链上 CoordinateStep，背书用 Park1+Park2。"""
    fc.retry(
        lambda: fc.invoke(
            caller_org     = "Park1",
            endorsing_orgs = ["Park1", "Park2"],
            contract       = CONTRACT_ADMM,
            function       = "CoordinateStep",
            args=[schedule_id, str(iter_k)],
            wait_seconds   = 1.0,
        ),
        attempts=5,
        delay_seconds=1.0,
    )


def query_credit_score(park_id: str) -> int:
    result = fc.query(
        caller_org="Park1",
        contract=CONTRACT_AUDIT,
        function="QueryCreditScore",
        args=[park_id],
    )
    return result["score"]
