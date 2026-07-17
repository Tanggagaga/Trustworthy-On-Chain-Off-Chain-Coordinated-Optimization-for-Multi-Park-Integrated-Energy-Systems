"""
M4 真实数据闭环验证：链上反解出的 e_m 是否等于 MATLAB(M1a) 真实算出的 e_m。

跟之前的 smoke_mask_m4_real_payload.py 不同：那次用的是 E_rows/x0_e 全零的
占位数据（只测管道通不通）。这次用 Park1 真实的注册量(H_e0,G_masked,h_masked,
C0,E_rows,x0_e) 和 M1a/M3 缓存里真实的(y*,mu*)，比对链上反解结果跟MATLAB
真实算出的eE/eG/eH/eC是否一致——这才是"结果对不对"，不只是"管道通不通"。

★ 隔离原则：scheduleID 前缀用 9996-M4REALCHECK，跟：
   - 9999-SMOKE-...（最小烧测）
   - 9999-M4PAYLOAD-...（占位payload压力测试）
   - 真实 2024-03-01-... 算例
  都不冲突。
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fabric_client as fc
from config import CONTRACT_ADMM, CONTRACT_MASK
from preheat_chaincode_peers import preheat_org

MASK_SCALE = 1_000_000
E_SCALE = 1000
DATA_OWNER = "Park1"
ITER = 0
ALL_PARK_ENDORSERS = ["Park1", "Park2", "Park3"]
BUNDLE_PATH = ("/mnt/c/Users/Lenovo/Desktop/daima/综合能源/链下上传信任/"
               "M1a离线Mask验证/M4真实数据校验/m4_real_check_park1_iter1.json")
CHUNK_CHARS = 50_000


def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def invoke(label, **kwargs):
    print(f"  -> {label}")
    return fc.invoke(**kwargs)


def upload_large_mask(schedule_id, k0, qrows, h_e0, g_masked, h_vec, c0, e_rows, x0e):
    invoke(
        "BeginMaskedSubproblemUpload",
        caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, DATA_OWNER, str(k0), str(qrows), str(MASK_SCALE)],
        wait_seconds=3.0,
    )
    parts = {
        "hE0": json.dumps(h_e0, separators=(",", ":")),
        "gMasked": json.dumps(g_masked, separators=(",", ":")),
        "hVec": json.dumps(h_vec, separators=(",", ":")),
        "c0": json.dumps(c0, separators=(",", ":")),
        "eRows": json.dumps(e_rows, separators=(",", ":")),
        "x0e": json.dumps(x0e, separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        payload_chunks = chunks(payload)
        print(f"  -> Upload {part_name}: {len(payload)} chars in {len(payload_chunks)} chunks")
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks, part_name=part_name: fc.invoke(
                    caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, DATA_OWNER, part_name, str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(1.0)
    print("  -> FinalizeMaskedSubproblemUpload")
    fc.retry(
        lambda: fc.invoke(
            caller_org=DATA_OWNER, endorsing_orgs=ALL_PARK_ENDORSERS,
            contract=CONTRACT_MASK, function="FinalizeMaskedSubproblemUpload",
            args=[schedule_id, DATA_OWNER], wait_seconds=2.0,
        ),
        attempts=6, delay_seconds=2.0,
    )


def main():
    with open(BUNDLE_PATH, encoding="utf-8") as f:
        bundle = json.load(f)

    k0 = bundle["k0"]
    qrows = bundle["qRows"]
    print(f"载入真实数据包: Park1 iter1, k0={k0}, qRows={qrows}, rho={bundle['rho']}")
    print(f"期望的真实e_m(scaled): eE[0]={bundle['expectedEEScaled'][0]} "
          f"eG[0]={bundle['expectedEGScaled'][0]} eH[0]={bundle['expectedEHScaled'][0]} "
          f"eC[0]={bundle['expectedECScaled'][0]}")

    toy_date = "9996-M4REALCHECK-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    schedule_id = "SCHEDULE_" + toy_date
    print("=" * 72)
    print(f"M4 真实数据闭环验证   scheduleID={schedule_id}")
    print("=" * 72)

    for park in ["Park1", "Park2", "Park3"]:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            return 1

    invoke(
        "PublishNetworkParams",
        caller_org="Observer", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM, function="PublishNetworkParams",
        args=[toy_date, json.dumps([99000, 99000, 99000]), json.dumps([99000, 99000, 99000]),
              json.dumps([99000, 99000]), json.dumps([99000, 99000, 99000])],
        wait_seconds=2.0,
    )
    invoke(
        "TriggerDispatch",
        caller_org="Park1", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_ADMM, function="TriggerDispatch",
        args=[toy_date, "10000", "10000", "10000", "100", "4500", json.dumps([430000, 530000, 240000])],
        wait_seconds=2.0,
    )

    upload_large_mask(
        schedule_id, k0, qrows,
        bundle["hE0Scaled"], bundle["gMaskedScaled"], bundle["hVecScaled"],
        bundle["c0Scaled"], bundle["eRowsScaled"], bundle["x0eScaled"],
    )

    invoke(
        "AssignSolverAndVerifier",
        caller_org="Park1", endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[schedule_id, str(ITER), DATA_OWNER], wait_seconds=2.0,
    )
    assignment = fc.query_with_retry("Park1", CONTRACT_MASK, "QuerySolverAssignment",
                                      [schedule_id, str(ITER), DATA_OWNER])
    solver = assignment["solverParkId"]
    verifier = assignment["verifierParkId"]
    print(f"分配结果: 数据方={DATA_OWNER} solver={solver} verifier={verifier}")

    invoke(
        f"SubmitMaskedSolution(真实y*,mu*)",
        caller_org=solver, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[schedule_id, str(ITER), DATA_OWNER,
              json.dumps(bundle["yScaled"]), json.dumps(bundle["muScaled"])],
        wait_seconds=2.0,
    )
    msol = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                [schedule_id, str(ITER), DATA_OWNER])

    # 这组(y*,mu*)已经在M2/M3的离线KKT校验里验证过PASS（来自m3_raw_cases.mat的
    # 真实回归用例），这里直接以verifier身份记录PASS裁决，重点测的是链上
    # e_m反解的数值正确性，不是再重复一遍KKT校验本身（那是M2/M3已经做的事）。
    invoke(
        "RecordVerdict(pass=true，复用M2/M3已验证过的KKT结论)",
        caller_org=verifier, endorsing_orgs=ALL_PARK_ENDORSERS,
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[schedule_id, str(ITER), DATA_OWNER, "true", "0", "0", msol["solutionHash"]],
        wait_seconds=2.0,
    )

    rec = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                               [schedule_id, str(ITER), DATA_OWNER])

    def compare(name, actual, expected_scaled):
        diffs = [abs(a - e) for a, e in zip(actual, expected_scaled)]
        max_diff = max(diffs)
        ok = max_diff <= 2  # 容差2个scaled单位 = 0.002 MW，覆盖MaskScale定点舍入
        print(f"  {name}: 链上={actual[:3]}...  期望={expected_scaled[:3]}...  "
              f"最大差异={max_diff}(scaled)  {'PASS' if ok else 'FAIL'}")
        return ok

    print("\n[比对] 链上反解的e_m vs MATLAB真实算出的e_m")
    ok = True
    ok &= compare("eE", rec["eExScaled"], bundle["expectedEEScaled"])
    ok &= compare("eG", rec["gExScaled"], bundle["expectedEGScaled"])
    ok &= compare("eH", rec["hExScaled"], bundle["expectedEHScaled"])
    ok &= compare("eC", rec["cExScaled"], bundle["expectedECScaled"])

    print("\n" + "=" * 72)
    print("结论:", "✅ 链上反解的e_m与MATLAB真实值一致" if ok else "❌ 存在不一致，见上")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
