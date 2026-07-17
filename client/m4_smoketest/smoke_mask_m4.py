"""
M4 最小链路烧测：验证 mask 信任机制的5个链码函数能不能在真实网络上正确串起来。

★ 隔离原则：
  - 用专门的玩具日期 SMOKE-DATE（带时间戳），scheduleID 完全独立，
    绝不和后面真实算例(2024-03-01那种)的命名空间冲突。
  - 用极小的人工KKT数据(k0=2, qRows=2)，E_rows/x0_e 全零 → 反解出的 e_m 全零，
    天然通过容量/拓扑校验，专注测"管道通不通"，不掺真实尺寸的大数据。

测试链路：
  0. (前置) Observer PublishNetworkParams + Park1 TriggerDispatch 建独立玩具调度
  1. RegisterMaskedSubproblem(Park1)            注册玩具mask
  2. AssignSolverAndVerifier(iter=0, Park1)      链上分配 solver/verifier
  3. (链下) verifier 角色的python重算KKT          独立验证(复刻M3.5逻辑的极简版)
  4. SubmitMaskedSolution                        由solver身份提交 y*,μ*
  5. RecordVerdict(pass=true)                    由verifier身份提交裁决→触发反解e_m
  6. QueryIterationRecord                        确认链上反解出的 e_m == 期望(全零)

外加3个"应该被拒绝"的反例，确认身份/哈希约束真的生效：
  R1. 数据方(Park1)自己冒充solver提交 → 必须被拒
  R2. solver冒充verifier提交裁决       → 必须被拒
  R3. verdict带错误的solutionHash       → 必须被拒
"""

import sys
import json
import time
import uuid
from datetime import datetime

sys.path.insert(0, "..")  # 复用上层 client 的 fabric_client / config

import fabric_client as fc
from config import CONTRACT_ADMM, CONTRACT_MASK, CONTRACT_AUDIT
from preheat_chaincode_peers import preheat_org

MASK_SCALE = 1_000_000

# ── 玩具日期：保证 scheduleID 独立 ────────────────────────────────────────────
TOY_DATE = "9999-SMOKE-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
SCHEDULE_ID = "SCHEDULE_" + TOY_DATE
ITER = 0
DATA_OWNER = "Park1"   # 子问题所有者（数据方）

# ── 玩具KKT问题（已手算验证满足KKT）─────────────────────────────────────────
#   min ½yᵀH'y + c'ᵀy  s.t.  y[0]≤1, y[1]≤1
#   H'=diag(2,2), c'=[-4,-6] → 无约束极小在 y=[2,3]，被约束拉回 y*=[1,1]
#   平稳性: H'y*+c'+G'ᵀμ* = [2,2]+[-4,-6]+μ = 0 → μ*=[2,4] ≥0 ✓
#   互补松弛: G'y*-h'=[0,0] → 0 ✓   原始可行: [1,1]≤[1,1] ✓   对偶可行: μ≥0 ✓
K0 = 2
QROWS = 2
H_E0   = [[2 * MASK_SCALE, 0], [0, 2 * MASK_SCALE]]
G_MASK = [[1 * MASK_SCALE, 0], [0, 1 * MASK_SCALE]]
H_VEC  = [1 * MASK_SCALE, 1 * MASK_SCALE]
C0     = [-4 * MASK_SCALE, -6 * MASK_SCALE]
# E_rows: 96×2 全零；x0_e: 96 全零  → 反解 e_m = E_rows·y* + x0_e = 0（全部96维）
E_ROWS = [[0, 0] for _ in range(96)]
X0E    = [0 for _ in range(96)]

Y_STAR  = [1 * MASK_SCALE, 1 * MASK_SCALE]
MU_STAR = [2 * MASK_SCALE, 4 * MASK_SCALE]


def offchain_kkt_verify():
    """链下verifier：独立用定点整数重算KKT四条，返回(pass, statRes, complRes)。
    这是M3.5逻辑的极简python复刻（玩具尺寸下足够），代表'独立第三方重新验证'这一步。
    用四舍五入除法 divRound，跟链码/ M3.5 保持一致。"""
    def divround(num, den):
        if den == 0:
            raise ZeroDivisionError("den is zero")
        if den < 0:
            num, den = -num, -den
        q = int(num / den)  # truncate toward zero, matching Go int division
        r = num % den
        if r < 0:
            r = -r
        if r * 2 >= den:
            q += 1 if num > 0 else -1
        return q

    # H'y*
    Hy = [sum(divround(H_E0[i][j] * Y_STAR[j], MASK_SCALE) for j in range(K0)) for i in range(K0)]
    # G'ᵀμ*
    GTmu = [sum(divround(G_MASK[r][i] * MU_STAR[r], MASK_SCALE) for r in range(QROWS)) for i in range(K0)]
    stat = [Hy[i] + C0[i] + GTmu[i] for i in range(K0)]
    stat_res = round((sum(s * s for s in stat)) ** 0.5)

    # 互补松弛
    slack = [sum(divround(G_MASK[r][j] * Y_STAR[j], MASK_SCALE) for j in range(K0)) - H_VEC[r] for r in range(QROWS)]
    compl_res = max(abs(divround(MU_STAR[r] * slack[r], MASK_SCALE)) for r in range(QROWS))

    primal_ok = all(s <= 100 for s in slack)        # 容差(scaled)留宽
    dual_ok   = all(m >= -100 for m in MU_STAR)
    tol = int(0.01 * MASK_SCALE)
    passed = primal_ok and dual_ok and stat_res <= tol and compl_res <= tol
    return passed, stat_res, compl_res


def expect_ok(label, fn):
    try:
        fn()
        print(f"  ✅ {label}")
        return True
    except Exception as e:
        print(f"  ❌ {label}  —— 本应成功却失败：{e}")
        return False


def expect_reject(label, fn):
    try:
        fn()
        print(f"  ❌ {label}  —— 本应被拒绝却成功了！")
        return False
    except Exception:
        print(f"  ✅ {label}（已正确拒绝）")
        return True


def main():
    print("=" * 64)
    print(f"M4 mask链路烧测   scheduleID={SCHEDULE_ID}")
    print("=" * 64)
    ok = True

    # ── 预热：链码升级后，每个peer的新版容器镜像是首次运行时才懒构建的。
    # 让每个park各自查询一次（只读、无害），强制其peer构建energy链码容器，
    # 否则后续Park3作为solver提交时会因容器未建而背书失败。
    # 这是真实run_5b流程同样需要的系统性预热，不是测试专用的hack。
    print("\n[预热] 触发各peer构建新版链码容器")
    for park in ["Park1", "Park2", "Park3"]:
        ready, message = preheat_org(park)
        if ready:
            print(f"    {park} 容器就绪")
        else:
            print(f"    {message}")
            ok = False
    time.sleep(2.0)

    # ── 步骤0：建独立玩具调度（容量给足，反正反解出来的e全是0）─────────────
    print("\n[0] 建立独立玩具调度状态")
    expect_ok("PublishNetworkParams", lambda: fc.invoke(
        caller_org="Observer", endorsing_orgs=["Park1", "Park2", "Park3"],
        contract=CONTRACT_ADMM, function="PublishNetworkParams",
        args=[TOY_DATE, json.dumps([99000, 99000, 99000]),
              json.dumps([99000, 99000, 99000]), json.dumps([99000, 99000]),
              json.dumps([99000, 99000, 99000])], wait_seconds=2.0))
    expect_ok("TriggerDispatch", lambda: fc.retry(lambda: fc.invoke(
        caller_org="Park1", endorsing_orgs=["Park1", "Park2", "Park3"],
        contract=CONTRACT_ADMM, function="TriggerDispatch",
        args=[TOY_DATE, "10000", "10000", "10000", "100", "4500",
              json.dumps([430000, 530000, 240000])], wait_seconds=2.0)))

    # ── 步骤1：注册玩具mask ─────────────────────────────────────────────────
    print("\n[1] RegisterMaskedSubproblem")
    ok &= expect_ok("注册Park1的masked子问题", lambda: fc.invoke(
        caller_org=DATA_OWNER, endorsing_orgs=["Park1", "Park2", "Park3"],
        contract=CONTRACT_MASK, function="RegisterMaskedSubproblem",
        args=[SCHEDULE_ID, DATA_OWNER, str(K0), str(QROWS), str(MASK_SCALE),
              json.dumps(H_E0), json.dumps(G_MASK), json.dumps(H_VEC),
              json.dumps(C0), json.dumps(E_ROWS), json.dumps(X0E)],
        wait_seconds=2.0))

    # ── 步骤2：链上分配 solver/verifier ─────────────────────────────────────
    print("\n[2] AssignSolverAndVerifier")
    ok &= expect_ok("分配求解/验证方", lambda: fc.invoke(
        caller_org="Park1", endorsing_orgs=["Park1", "Park2", "Park3"],
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[SCHEDULE_ID, str(ITER), DATA_OWNER], wait_seconds=2.0))
    assignment = fc.query_with_retry(
        caller_org="Park1", contract=CONTRACT_MASK,
        function="QuerySolverAssignment", args=[SCHEDULE_ID, str(ITER), DATA_OWNER])
    solver = assignment["solverParkId"]
    verifier = assignment["verifierParkId"]
    print(f"    分配结果：数据方={DATA_OWNER}  solver={solver}  verifier={verifier}")
    # 确认三者两两不同
    if len({DATA_OWNER, solver, verifier}) == 3:
        print("    ✅ 数据方/solver/verifier 三者两两不同")
    else:
        print("    ❌ 三者身份有重叠！")
        ok = False

    def endorsers_for(caller):
        # 默认让三个Park peer都参与背书。链码策略只要求OutOf(2)，但烧测阶段
        # 主动覆盖Park3，避免容器/peer问题被隐藏到后面的真实ADMM实验里。
        return ["Park1", "Park2", "Park3"]

    # ── 步骤3：链下verifier重算KKT ──────────────────────────────────────────
    print("\n[3] 链下KKT验证（独立重算）")
    passed, stat_res, compl_res = offchain_kkt_verify()
    print(f"    KKT结果: pass={passed}  平稳性残差(scaled)={stat_res}  互补松弛(scaled)={compl_res}")

    # ── 反例R1：数据方自己冒充solver提交 ────────────────────────────────────
    print("\n[反例R1] 数据方Park1冒充solver提交masked解 → 应被拒")
    if DATA_OWNER != solver:
        ok &= expect_reject("Park1非solver不能提交", lambda: fc.invoke(
            caller_org=DATA_OWNER, endorsing_orgs=endorsers_for(DATA_OWNER),
            contract=CONTRACT_MASK, function="SubmitMaskedSolution",
            args=[SCHEDULE_ID, str(ITER), DATA_OWNER,
                  json.dumps(Y_STAR), json.dumps(MU_STAR)], wait_seconds=1.0))

    # ── 步骤4：solver正确提交 ───────────────────────────────────────────────
    print("\n[4] SubmitMaskedSolution（由solver身份提交）")
    ok &= expect_ok(f"{solver}作为solver提交y*,μ*", lambda: fc.invoke(
        caller_org=solver, endorsing_orgs=endorsers_for(solver),
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[SCHEDULE_ID, str(ITER), DATA_OWNER,
              json.dumps(Y_STAR), json.dumps(MU_STAR)], wait_seconds=2.0))
    # 拿链上记录的 solutionHash（链码自己算的，不是solver自报的）
    msol = fc.query_with_retry(caller_org="Park1", contract=CONTRACT_MASK,
        function="QueryMaskedSolution", args=[SCHEDULE_ID, str(ITER), DATA_OWNER])
    solution_hash = msol["solutionHash"]
    print(f"    链上solutionHash = {solution_hash[:16]}...")

    # ── 反例R2：solver冒充verifier记录裁决 ──────────────────────────────────
    print("\n[反例R2] solver冒充verifier提交裁决 → 应被拒")
    ok &= expect_reject(f"{solver}非verifier不能裁决", lambda: fc.invoke(
        caller_org=solver, endorsing_orgs=endorsers_for(solver),
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[SCHEDULE_ID, str(ITER), DATA_OWNER, "true",
              str(stat_res), str(compl_res), solution_hash], wait_seconds=1.0))

    # ── 反例R3：verifier带错误solutionHash ──────────────────────────────────
    print("\n[反例R3] verifier提交错误的solutionHash → 应被拒")
    ok &= expect_reject("错误solutionHash被拒", lambda: fc.invoke(
        caller_org=verifier, endorsing_orgs=endorsers_for(verifier),
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[SCHEDULE_ID, str(ITER), DATA_OWNER, "true",
              str(stat_res), str(compl_res), "deadbeef" * 8], wait_seconds=1.0))

    # ── 步骤5：verifier正确记录裁决(pass=true) → 触发反解e_m ─────────────────
    print("\n[5] RecordVerdict(pass=true)（由verifier身份提交）")
    ok &= expect_ok(f"{verifier}作为verifier记录PASS", lambda: fc.invoke(
        caller_org=verifier, endorsing_orgs=endorsers_for(verifier),
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[SCHEDULE_ID, str(ITER), DATA_OWNER, "true",
              str(stat_res), str(compl_res), solution_hash], wait_seconds=2.0))

    # ── 步骤6：确认链上反解出的 e_m == 期望(全零) ──────────────────────────
    print("\n[6] QueryIterationRecord 确认反解的e_m")
    rec = fc.query_with_retry(caller_org="Park1", contract=CONTRACT_ADMM,
        function="QueryIterationRecord", args=[SCHEDULE_ID, str(ITER), DATA_OWNER])
    eE = rec["eExScaled"]; eG = rec["gExScaled"]; eH = rec["hExScaled"]; eC = rec["cExScaled"]
    all_zero = all(v == 0 for v in eE + eG + eH + eC)
    if all_zero and len(eE) == 24:
        print(f"    ✅ 链上反解出的 e_m 全为0（与E_rows/x0_e全零的期望一致），写入IterationRecord成功")
    else:
        print(f"    ❌ e_m 不符合期望: eE[:3]={eE[:3]} eG[:3]={eG[:3]}")
        ok = False

    print("\n" + "=" * 64)
    print("烧测结论:", "✅ 全链路串通，身份/哈希约束全部生效" if ok else "❌ 存在失败项，见上")
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
