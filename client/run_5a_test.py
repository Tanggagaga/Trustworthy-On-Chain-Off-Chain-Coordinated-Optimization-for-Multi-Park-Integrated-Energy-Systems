"""
阶段5A：三类可控测试，验证Python能自动走完完整链上流程（四能版本）。

测试1：全零e（四能） → 预期1轮收敛 (CONVERGED)
测试2：固定非零e（四能） → 验证lambda有更新，不强求收敛
测试3：maxIter=2，不平衡e → 预期触发FAILED/fallback路径

Park3 热能 eH 始终为0（不在热网），所有测试用例均遵守此约束。
"""

import json
import sys
import time
from datetime import datetime

import fabric_client as fc
import park_agent as pa
from config import (
    CONTRACT_ADMM, CONTRACT_SETTLEMENT, CONTRACT_AUDIT,
    DEFAULT_RHO_SCALED, DEFAULT_EPS_PRI_SQ, DEFAULT_EPS_DUAL_SQ,
    DEFAULT_CARBON_PRICE, CARBON_QUOTA,
    E_MAX_SCALED, G_MAX_SCALED, H_MAX_SCALED, C_MAX_SCALED,
    PARK_IDS,
)


# ── 辅助 ─────────────────────────────────────────────────────────────
def make_date(test_id: int) -> str:
    stamp = datetime.now().strftime("%H%M%S")
    return f"2024-02-{test_id:02d}-{stamp}"


def schedule_id_for(date: str) -> str:
    return f"SCHEDULE_{date}"


def print_state(schedule_id: str):
    state = pa.query_admm_state(schedule_id)
    print(f"  iter={state['iter']}  status={state['status']}"
          f"  converged={state['converged']}"
          f"  primalResidSq={state['primalResidSq']}"
          f"  dualResidSq={state['dualResidSq']}"
          f"  rhoScaled={state['rhoScaled']}")


def wait_state_after_coordinate(schedule_id: str, iter_k: int) -> dict:
    """等待CoordinateStep提交后的ADMMState可见。"""
    def predicate():
        state = pa.query_admm_state(schedule_id)
        ok = state["status"] in ("CONVERGED", "FAILED") or state["iter"] > iter_k
        return ok, state

    return fc.wait_until(
        description=f"ADMMState advanced after iter {iter_k}",
        predicate=predicate,
        attempts=10,
        delay_seconds=1.0,
    )


# ── 链上初始化 ────────────────────────────────────────────────────────
def setup(date: str, max_iter: int) -> str:
    """
    发布四能网络参数并初始化ADMMState。
    返回 schedule_id。
    """
    schedule_id = schedule_id_for(date)
    print(f"\n[setup] date={date}  schedule_id={schedule_id}")

    # Observer 发布四能网络容量参数
    # H_MAX_SCALED 是长度2（Park1, Park2），其余长度3
    print("  PublishNetworkParams ...")
    fc.invoke(
        caller_org     = "Observer",
        endorsing_orgs = ["Park1", "Park2"],
        contract       = CONTRACT_ADMM,
        function       = "PublishNetworkParams",
        args=[
            date,
            json.dumps(E_MAX_SCALED),   # [3]
            json.dumps(G_MAX_SCALED),   # [3]
            json.dumps(H_MAX_SCALED),   # [2]  Park1, Park2 only
            json.dumps(C_MAX_SCALED),   # [3]
        ],
        wait_seconds=2.0,
    )

    # Park1 触发调度，初始化ADMMState（四能lambda/z全零）
    print("  TriggerDispatch ...")
    fc.retry(
        lambda: fc.invoke(
            caller_org     = "Park1",
            endorsing_orgs = ["Park1", "Park2"],
            contract       = CONTRACT_ADMM,
            function       = "TriggerDispatch",
            args=[
                date,
                str(DEFAULT_RHO_SCALED),
                str(DEFAULT_EPS_PRI_SQ),
                str(DEFAULT_EPS_DUAL_SQ),
                str(max_iter),
                str(DEFAULT_CARBON_PRICE),
                json.dumps(CARBON_QUOTA),
            ],
            wait_seconds=1.0,
        ),
        attempts=3,
        delay_seconds=2.0,
    )

    print_state(schedule_id)
    return schedule_id


# ── 主迭代循环 ────────────────────────────────────────────────────────
def run_admm_loop(schedule_id: str, e_provider, max_iter: int) -> str:
    """
    e_provider(park_id, iter_k) → (eE[24], eG[24], eH[24], eC[24], cost, carbon)

    Park3 的 eH 必须始终为零（由 e_provider 保证）。
    """
    for k in range(max_iter):
        print(f"\n  --- iter {k} ---")

        for park_id in PARK_IDS:
            eE, eG, eH, eC, cost, carbon = e_provider(park_id, k)
            pa.submit_iteration_result(
                schedule_id, k, park_id,
                eE, eG, eH, eC,
                cost, carbon,
            )
            print(f"    {park_id} submitted")

        pa.coordinate_step(schedule_id, k)
        print(f"    CoordinateStep done")

        state = wait_state_after_coordinate(schedule_id, k)
        status = state["status"]
        print(f"    status={status}  primalSq={state['primalResidSq']}"
              f"  dualSq={state['dualResidSq']}"
              f"  iter={state['iter']}")

        if status in ("CONVERGED", "FAILED"):
            print(f"  → 终止：{status}")
            return status

    return "MAX_ITER"


# ── 结算和信用查询 ────────────────────────────────────────────────────
def settle_and_report(schedule_id: str):
    print("\n  SettleAfterConvergence ...")
    fc.invoke(
        caller_org     = "Park1",
        endorsing_orgs = ["Park1", "Park2"],
        contract       = CONTRACT_SETTLEMENT,
        function       = "SettleAfterConvergence",
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
            f"  {park_id}: "
            f"payE={result['energyPaymentEScaled']}  "
            f"payG={result['energyPaymentGScaled']}  "
            f"payH={result['energyPaymentHScaled']}  "
            f"payC={result['energyPaymentCScaled']}  "
            f"totalEnergy={result['totalEnergyPaymentScaled']}  "
            f"carbon={result['carbonPaymentScaled']}  "
            f"total={result['totalPaymentScaled']}  "
            f"creditScore={score}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 测试1：全零e（四能全零） → 预期1轮收敛
# ═══════════════════════════════════════════════════════════════════════
def test1_all_zeros():
    print("\n" + "="*60)
    print("测试1：全零e（四能）  预期：1轮后 CONVERGED")
    print("="*60)

    date = make_date(1)
    schedule_id = setup(date, max_iter=80)

    def zero_e(park_id, iter_k):
        # 四能全零：满足所有约束，z更新后z也为零，残差=0，立即收敛
        return [0.0]*24, [0.0]*24, [0.0]*24, [0.0]*24, 100.0, 5.0

    status = run_admm_loop(schedule_id, zero_e, max_iter=80)

    assert status == "CONVERGED", f"测试1失败：期望CONVERGED，实际{status}"
    print("\n✅ 测试1通过：四能全零，1轮收敛")

    settle_and_report(schedule_id)


# ═══════════════════════════════════════════════════════════════════════
# 测试2：固定非零e → 验证z/lambda有更新
# ═══════════════════════════════════════════════════════════════════════
def test2_fixed_nonzero():
    print("\n" + "="*60)
    print("测试2：固定非零e（四能）  验证z/lambda更新，不要求收敛")
    print("="*60)

    date = make_date(2)
    schedule_id = setup(date, max_iter=5)

    # 电力：Park1进口5MW，Park2进口3MW，Park3出口6MW（故意不平衡，总和=2MW）
    # 气：Park1进口2MW，Park2进口1MW，Park3出口3MW（平衡）
    # 热：Park1进口4MW，Park2出口4MW，Park3=0（Park3必须为0）
    # 冷：Park1出口1MW，Park2进口0.5MW，Park3进口0.5MW（平衡）
    fixed_eE = {"Park1": [5.0]*24,  "Park2": [3.0]*24,  "Park3": [-6.0]*24}
    fixed_eG = {"Park1": [2.0]*24,  "Park2": [1.0]*24,  "Park3": [-3.0]*24}
    fixed_eH = {"Park1": [4.0]*24,  "Park2": [-4.0]*24, "Park3": [0.0]*24}
    fixed_eC = {"Park1": [-1.0]*24, "Park2": [0.5]*24,  "Park3": [0.5]*24}

    def fixed_e_provider(park_id, iter_k):
        return (
            fixed_eE[park_id], fixed_eG[park_id],
            fixed_eH[park_id], fixed_eC[park_id],
            200.0, 10.0,
        )

    # 跑2轮，检查lambda是否更新
    for k in range(2):
        print(f"\n  --- iter {k} ---")
        for park_id in PARK_IDS:
            eE, eG, eH, eC, cost, carbon = fixed_e_provider(park_id, k)
            pa.submit_iteration_result(schedule_id, k, park_id, eE, eG, eH, eC, cost, carbon)
        pa.coordinate_step(schedule_id, k)
        state_after = wait_state_after_coordinate(schedule_id, k)
        print(f"    iter={state_after['iter']}  "
              f"primalSq={state_after['primalResidSq']}  "
              f"dualSq={state_after['dualResidSq']}")
        if state_after["status"] in ("CONVERGED", "FAILED"):
            break

    # 验证电力lambda有变化（非零e情况下lambda_E必须更新）
    lambda_e_after = state_after["lambdaEScaled"]
    any_nonzero = any(v != 0 for row in lambda_e_after for v in row)
    assert any_nonzero, "测试2失败：lambdaE全为零，说明协调计算没有执行"

    print("\n✅ 测试2通过：lambdaE已更新，CoordinateStep正常执行")


# ═══════════════════════════════════════════════════════════════════════
# 测试3：maxIter=2 → 验证FAILED/fallback路径
# ═══════════════════════════════════════════════════════════════════════
def test3_fallback():
    print("\n" + "="*60)
    print("测试3：maxIter=2，不平衡e  预期：FAILED + fallback")
    print("="*60)

    date = make_date(3)
    schedule_id = setup(date, max_iter=2)

    # 电力严重不平衡（总和=4MW），确保2轮内不收敛
    # 其他能源给平衡值（不影响不收敛结论，但满足Park3 eH=0约束）
    def unbalanced_e(park_id, iter_k):
        eE = {"Park1": [10.0]*24, "Park2": [0.0]*24,  "Park3": [-6.0]*24}
        eG = {"Park1": [0.0]*24,  "Park2": [0.0]*24,  "Park3": [0.0]*24}
        eH = {"Park1": [0.0]*24,  "Park2": [0.0]*24,  "Park3": [0.0]*24}
        eC = {"Park1": [0.0]*24,  "Park2": [0.0]*24,  "Park3": [0.0]*24}
        return eE[park_id], eG[park_id], eH[park_id], eC[park_id], 150.0, 8.0

    status = run_admm_loop(schedule_id, unbalanced_e, max_iter=2)

    assert status == "FAILED", f"测试3失败：期望FAILED，实际{status}"

    final = fc.query_with_retry(
        caller_org="Park1",
        contract=CONTRACT_ADMM,
        function="QueryFinalSchedule",
        args=[schedule_id],
    )
    assert final["isFallback"] is True, "测试3失败：isFallback应为True"
    print(f"\n✅ 测试3通过：FAILED路径，bestIter={final['convergedIter']}，fallback已写入")

    settle_and_report(schedule_id)


# ── 主入口 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = {
        "1": test1_all_zeros,
        "2": test2_fixed_nonzero,
        "3": test3_fallback,
    }

    if len(sys.argv) > 1:
        t = sys.argv[1]
        if t not in tests:
            print(f"未知测试编号: {t}，可选 1/2/3")
            sys.exit(1)
        tests[t]()
    else:
        for name, fn in tests.items():
            fn()

    print("\n\n✅ 5A 所有测试完成")
