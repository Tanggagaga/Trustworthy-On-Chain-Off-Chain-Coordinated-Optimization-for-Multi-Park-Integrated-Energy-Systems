"""
EXP-2：不可篡改性验证实验。

三层防御，三次攻击，一次完整证明：

  ① Attack-A 【重复写入 Solution】
    已提交的 (iter=0, Park1) Solution → 再次调用 SubmitMaskedSolution（篡改Y）
    预期错误："masked solution already submitted for Park1 iter 0"
    对应链码：mask_contract.go solutionKey 存在性检查

  ② Attack-B 【哈希绑定】
    iter=1 Park1：先正常提交 Solution（链上存 solutionHash=H_real），
    再以FAKE_HASH调用 RecordVerdict。
    预期错误："solutionHash mismatch: verdict references FAKE, on-chain solution is H_real"
    核心价值：即使绕过重复写入检查，伪造 y* 对应的 hash 也无法通过验证，
              solver 无法事后用"正确 hash"提交不同的解。

  ③ Attack-C 【重复写入 Verdict】
    已记录的 (iter=0, Park1) Verdict → 再次调用 RecordVerdict（即使 hash 正确）
    预期错误："verdict already recorded for Park1 iter 0"
    对应链码：mask_contract.go verdictKey 存在性检查

  完整性验证：每次攻击后查询原始数据，确认链上记录分毫未动。

scheduleID 前缀：9994-EXP2-（与其他实验完全隔离，禁止使用 2024-03-01-）
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
from config import CONTRACT_ADMM, CONTRACT_MASK, PARK_IDS
from run_5a_test import setup, wait_state_after_coordinate
from preheat_chaincode_peers import preheat_org
from masked_solver import build_masked_round, solve_masked_qp, check_kkt_scaled, MASK_SCALE

# ── 常量 ───────────────────────────────────────────────────────────────────────
ALL_3      = ["Park1", "Park2", "Park3"]
BUNDLE_DIR = os.environ.get("MASK_BUNDLE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mask")))
CHUNK_CHARS = 50_000

FAKE_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


# ── 工具函数 ───────────────────────────────────────────────────────────────────
def chunks(text, size=CHUNK_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def load_bundle(park_idx):
    with open(os.path.join(BUNDLE_DIR, f"mask_registration_park{park_idx}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def upload_large_mask(schedule_id, park_id, bundle):
    fc.invoke(
        caller_org=park_id, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="BeginMaskedSubproblemUpload",
        args=[schedule_id, park_id,
              str(bundle["k0"]), str(bundle["qRows"]), str(MASK_SCALE)],
        wait_seconds=2.0,
    )
    parts = {
        "hE0":     json.dumps(bundle["hE0Scaled"],     separators=(",", ":")),
        "gMasked": json.dumps(bundle["gMaskedScaled"], separators=(",", ":")),
        "hVec":    json.dumps(bundle["hVecScaled"],    separators=(",", ":")),
        "c0":      json.dumps(bundle["c0Scaled"],      separators=(",", ":")),
        "eRows":   json.dumps(bundle["eRowsScaled"],   separators=(",", ":")),
        "x0e":     json.dumps(bundle["x0eScaled"],     separators=(",", ":")),
    }
    for part_name, payload in parts.items():
        payload_chunks = chunks(payload)
        for idx, chunk in enumerate(payload_chunks):
            fc.retry(
                lambda idx=idx, chunk=chunk, payload_chunks=payload_chunks,
                       pn=part_name: fc.invoke(
                    caller_org=park_id, endorsing_orgs=ALL_3,
                    contract=CONTRACT_MASK, function="UploadMaskedSubproblemChunk",
                    args=[schedule_id, park_id, pn,
                          str(idx), str(len(payload_chunks)), chunk],
                    wait_seconds=0.0,
                ),
                attempts=4, delay_seconds=1.5,
            )
        time.sleep(0.5)
    fc.retry(
        lambda: fc.invoke(
            caller_org=park_id, endorsing_orgs=ALL_3,
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


def try_attack(name, caller_org, endorsing_orgs, contract, function, args,
               expected_keyword, wait_seconds=1.0):
    """
    发起一次预期失败的 invoke，捕获错误消息。
    返回结果 dict：name / blocked / error_msg / contains_expected。
    """
    print(f"\n[Attack-{name}] invoke {contract}:{function}")
    try:
        fc.invoke(
            caller_org=caller_org, endorsing_orgs=endorsing_orgs,
            contract=contract, function=function, args=args,
            wait_seconds=wait_seconds,
        )
        print(f"  !! 意外成功（预期被拒绝）")
        return {
            "attack": name, "blocked": False,
            "error_msg": "(unexpected success)", "contains_expected": False,
        }
    except RuntimeError as e:
        msg = str(e)
        ok  = expected_keyword.lower() in msg.lower()
        print(f"  -> 被拒绝 ({'✓ 符合预期' if ok else '✗ 错误不符预期'})")
        print(f"     预期关键词: {expected_keyword!r}")
        print(f"     实际错误  : {msg[:300]}")
        return {
            "attack": name, "blocked": True,
            "error_msg": msg[:400], "contains_expected": ok,
        }


def solve_park_one_iter(park, k, mask_float, g_masked_float, h_vec_float, bundles,
                        lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid):
    """完整跑一个 park 的一次迭代（Assign → Submit → Verdict），返回元数据。"""
    idx = PARK_IDS.index(park)
    if park == "Park3":
        lam_h_row, z_h_row = [0.0] * 24, [0.0] * 24
    else:
        lam_h_row, z_h_row = lam_h[idx], z_h[idx]
    lam_block = lam_e[idx] + lam_g[idx] + lam_h_row + lam_c[idx]
    z_block   = z_e[idx]   + z_g[idx]   + z_h_row   + z_c[idx]

    h_masked, c_masked = build_masked_round(mask_float[park], rho, lam_block, z_block)
    y_star, mu_star = solve_masked_qp(h_masked, c_masked,
                                      g_masked_float[park], h_vec_float[park])

    y_scaled  = [int(round(v * MASK_SCALE)) for v in y_star]
    mu_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu_star]
    bundle    = bundles[park]

    h_masked_scaled = [[int(round(rho * v)) for v in row] for row in bundle["hE0Scaled"]]
    c_masked_scaled = [int(round(v * MASK_SCALE)) for v in c_masked]

    passed, stat_res, compl_res = check_kkt_scaled(
        h_masked_scaled, c_masked_scaled,
        bundle["gMaskedScaled"], bundle["hVecScaled"],
        y_scaled, mu_scaled,
    )
    assert passed, f"KKT 失败 iter={k} {park} stat={stat_res} compl={compl_res}"

    fc.invoke(
        caller_org=park, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[sid, str(k), park], wait_seconds=1.0,
    )
    assignment = fc.query_with_retry(park, CONTRACT_MASK, "QuerySolverAssignment",
                                     [sid, str(k), park])
    solver, verifier = assignment["solverParkId"], assignment["verifierParkId"]

    fc.invoke(
        caller_org=solver, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[sid, str(k), park, json.dumps(y_scaled), json.dumps(mu_scaled)],
        wait_seconds=1.0,
    )
    msol = fc.query_with_retry(park, CONTRACT_MASK, "QueryMaskedSolution",
                               [sid, str(k), park])
    real_hash = msol["solutionHash"]

    fc.invoke(
        caller_org=verifier, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="RecordVerdict",
        args=[sid, str(k), park,
              "true", str(stat_res), str(compl_res), real_hash],
        wait_seconds=1.0,
    )

    return {
        "park": park, "iter": k,
        "solver": solver, "verifier": verifier,
        "y_scaled": y_scaled, "mu_scaled": mu_scaled,
        "real_hash": real_hash,
        "stat_res": stat_res, "compl_res": compl_res,
    }


# ── 主实验 ─────────────────────────────────────────────────────────────────────
def run():
    toy_date = "9994-EXP2-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    sid = setup(toy_date, max_iter=10)
    print(f"\nscheduleID = {sid}")

    print("\n[预热] 三个园区容器")
    for park in PARK_IDS:
        ready, message = preheat_org(park)
        print(("  OK  " if ready else "  FAIL ") + message)
        if not ready:
            raise RuntimeError(f"{park} 没就绪")

    print("\n[注册] 上传三个园区的 masked 结构")
    bundles, g_masked_float, h_vec_float, mask_float = {}, {}, {}, {}
    for park_idx, park in enumerate(PARK_IDS, start=1):
        bundle = load_bundle(park_idx)
        bundles[park] = bundle
        upload_large_mask(sid, park, bundle)
        g_masked_float[park] = [[v / MASK_SCALE for v in row]
                                 for row in bundle["gMaskedScaled"]]
        h_vec_float[park]    = [v / MASK_SCALE for v in bundle["hVecScaled"]]
        mask_float[park]     = to_float_mask(bundle)
        print(f"  -> {park} 注册完成")

    # ── 正式 iter=0：三园区完整跑一轮，建立基线数据 ────────────────────────────
    print("\n[基线] 运行 iter=0（全三园区）")
    lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho = pa.get_lambda_z_rho(sid)
    meta_iter0 = {}
    for park in PARK_IDS:
        meta = solve_park_one_iter(
            park, 0, mask_float, g_masked_float, h_vec_float, bundles,
            lam_e, lam_g, lam_h, lam_c, z_e, z_g, z_h, z_c, rho, sid,
        )
        meta_iter0[park] = meta
        print(f"  {park}: solver={meta['solver']} verifier={meta['verifier']} "
              f"hash={meta['real_hash'][:16]}...")

    fc.retry(
        lambda: fc.invoke(
            caller_org="Park1", endorsing_orgs=ALL_3,
            contract="ADMMCoordinationContract", function="CoordinateStep",
            args=[sid, "0"], wait_seconds=1.0,
        ),
        attempts=5, delay_seconds=1.5,
    )
    wait_state_after_coordinate(sid, 0)
    print("  iter=0 完成，链上状态推进到 iter=1")

    # 快照基线数据（攻击前）
    baseline_sol  = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                        [sid, "0", "Park1"])
    baseline_rec  = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                        [sid, "0", "Park1"])
    p1_meta       = meta_iter0["Park1"]

    print(f"\n  基线 solutionHash : {baseline_sol['solutionHash'][:32]}...")
    print(f"  基线 eExScaled[0] : {baseline_rec['eExScaled'][0]}")

    attack_rows   = []
    integrity_rows = []

    # ── Attack-A：重复提交 Solution（篡改 y*）────────────────────────────────────
    print("\n" + "="*60)
    print("Attack-A: 重复写入 Solution（iter=0, Park1, 篡改Y）")
    print("="*60)
    y_tampered = [int(v * -3) for v in p1_meta["y_scaled"]]
    row = try_attack(
        name="A",
        caller_org=p1_meta["solver"],
        endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK,
        function="SubmitMaskedSolution",
        args=[sid, "0", "Park1", json.dumps(y_tampered), json.dumps(p1_meta["mu_scaled"])],
        expected_keyword="masked solution already submitted",
    )
    attack_rows.append(row)

    sol_after_A = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                      [sid, "0", "Park1"])
    unchanged_A = (sol_after_A["solutionHash"] == baseline_sol["solutionHash"])
    integrity_rows.append({
        "check": "Attack-A 后 solutionHash 未变",
        "expected": baseline_sol["solutionHash"][:32],
        "actual":   sol_after_A["solutionHash"][:32],
        "unchanged": unchanged_A,
    })
    print(f"  完整性: solutionHash {'未变 ✓' if unchanged_A else '已变 ✗'}")

    # ── Attack-B：哈希绑定（iter=1 Park1 提交真实 Solution，然后以 FAKE_HASH 裁决）──
    print("\n" + "="*60)
    print("Attack-B: 哈希篡改（iter=1, Park1 — 以伪造 hash 发起 RecordVerdict）")
    print("="*60)
    lam_e1, lam_g1, lam_h1, lam_c1, z_e1, z_g1, z_h1, z_c1, rho1 = pa.get_lambda_z_rho(sid)
    idx1 = PARK_IDS.index("Park1")
    lam_block1 = lam_e1[idx1] + lam_g1[idx1] + lam_h1[idx1] + lam_c1[idx1]
    z_block1   = z_e1[idx1]   + z_g1[idx1]   + z_h1[idx1]   + z_c1[idx1]
    h_m1, c_m1 = build_masked_round(mask_float["Park1"], rho1, lam_block1, z_block1)
    y1, mu1    = solve_masked_qp(h_m1, c_m1,
                                 g_masked_float["Park1"], h_vec_float["Park1"])
    y1_scaled  = [int(round(v * MASK_SCALE)) for v in y1]
    mu1_scaled = [int(round(max(v, 0.0) * MASK_SCALE)) for v in mu1]

    fc.invoke(
        caller_org="Park1", endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="AssignSolverAndVerifier",
        args=[sid, "1", "Park1"], wait_seconds=1.0,
    )
    assign1 = fc.query_with_retry("Park1", CONTRACT_MASK, "QuerySolverAssignment",
                                  [sid, "1", "Park1"])
    solver1, verifier1 = assign1["solverParkId"], assign1["verifierParkId"]
    print(f"  iter=1 Park1: solver={solver1} verifier={verifier1}")

    fc.invoke(
        caller_org=solver1, endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK, function="SubmitMaskedSolution",
        args=[sid, "1", "Park1", json.dumps(y1_scaled), json.dumps(mu1_scaled)],
        wait_seconds=1.0,
    )
    msol1     = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                    [sid, "1", "Park1"])
    real_hash1 = msol1["solutionHash"]
    print(f"  链上存储的 solutionHash: {real_hash1[:32]}...")
    print(f"  攻击使用的 FAKE_HASH   : {FAKE_HASH[:32]}...")

    row = try_attack(
        name="B",
        caller_org=verifier1,
        endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK,
        function="RecordVerdict",
        args=[sid, "1", "Park1", "true", "0", "0", FAKE_HASH],
        expected_keyword="solutionHash mismatch",
    )
    attack_rows.append(row)

    # 验证：原始 Solution 未改变，Verdict 未被写入
    sol1_after_B = fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                                       [sid, "1", "Park1"])
    unchanged_B_hash = (sol1_after_B["solutionHash"] == real_hash1)
    integrity_rows.append({
        "check": "Attack-B 后 iter=1 solutionHash 未变",
        "expected": real_hash1[:32],
        "actual":   sol1_after_B["solutionHash"][:32],
        "unchanged": unchanged_B_hash,
    })
    # Verdict 不应存在
    verdict_written = False
    try:
        fc.query_with_retry("Park1", CONTRACT_MASK, "QueryMaskedSolution",
                            [sid, "1", "Park1"])
        # RecordVerdict writes to a different key (verdictKey); check via iteration record
        fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                            [sid, "1", "Park1"])
        verdict_written = True
    except Exception:
        verdict_written = False
    integrity_rows.append({
        "check": "Attack-B 后 iter=1 IterationRecord 未写入",
        "expected": "(不存在)",
        "actual":   "(不存在)" if not verdict_written else "(存在！)",
        "unchanged": not verdict_written,
    })
    print(f"  完整性: solutionHash {'未变 ✓' if unchanged_B_hash else '已变 ✗'}")
    print(f"  完整性: IterationRecord {'未写入 ✓' if not verdict_written else '已写入 ✗'}")

    # ── Attack-C：重复写入 Verdict（iter=0 Park1，hash 正确）────────────────────
    print("\n" + "="*60)
    print("Attack-C: 重复写入 Verdict（iter=0, Park1 — 即使 hash 正确也被拒）")
    print("="*60)
    row = try_attack(
        name="C",
        caller_org=p1_meta["verifier"],
        endorsing_orgs=ALL_3,
        contract=CONTRACT_MASK,
        function="RecordVerdict",
        args=[sid, "0", "Park1",
              "true", str(p1_meta["stat_res"]), str(p1_meta["compl_res"]),
              p1_meta["real_hash"]],
        expected_keyword="verdict already recorded",
    )
    attack_rows.append(row)

    rec_after_C = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryIterationRecord",
                                      [sid, "0", "Park1"])
    unchanged_C = (rec_after_C["eExScaled"][0] == baseline_rec["eExScaled"][0])
    integrity_rows.append({
        "check": "Attack-C 后 eExScaled[0] 未变",
        "expected": str(baseline_rec["eExScaled"][0]),
        "actual":   str(rec_after_C["eExScaled"][0]),
        "unchanged": unchanged_C,
    })
    print(f"  完整性: eExScaled[0] {'未变 ✓' if unchanged_C else '已变 ✗'}")

    # ── 汇总输出 ────────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("实验结论汇总")
    print("="*60)
    all_blocked   = all(r["blocked"] for r in attack_rows)
    all_confirmed = all(r["contains_expected"] for r in attack_rows)
    all_unchanged = all(r["unchanged"] for r in integrity_rows)
    print(f"  三次攻击全部被链码拒绝: {'✓' if all_blocked else '✗'}")
    print(f"  错误消息与预期一致     : {'✓' if all_confirmed else '✗'}")
    print(f"  原始数据全部未被修改   : {'✓' if all_unchanged else '✗'}")

    for r in attack_rows:
        sym = "✓" if r["blocked"] and r["contains_expected"] else "✗"
        print(f"  [{sym}] Attack-{r['attack']}: blocked={r['blocked']} "
              f"expected_keyword_found={r['contains_expected']}")
    for r in integrity_rows:
        sym = "✓" if r["unchanged"] else "✗"
        print(f"  [{sym}] {r['check']}")

    # 生成 iter=0 每园区数据行
    iter0_rows = []
    for park in PARK_IDS:
        m = meta_iter0[park]
        iter0_rows.append({
            "park": park, "iter": 0,
            "solver": m["solver"], "verifier": m["verifier"],
            "solutionHash": m["real_hash"],
            "stat_res": m["stat_res"], "compl_res": m["compl_res"],
        })

    out_wsl = os.path.join(os.path.dirname(__file__), "EXP2_immutability.xlsx")
    out_win = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "results", "EXP2_immutability.xlsx"))
    with pd.ExcelWriter(out_wsl, engine="openpyxl") as writer:
        pd.DataFrame(attack_rows).to_excel(writer, sheet_name="attack_results", index=False)
        pd.DataFrame(integrity_rows).to_excel(writer, sheet_name="integrity_check", index=False)
        pd.DataFrame(iter0_rows).to_excel(writer, sheet_name="iter0_baseline", index=False)

    import subprocess
    subprocess.run(["cp", out_wsl, out_win], capture_output=True)
    print(f"\n数据已保存: {out_win}")
    return {
        "all_blocked": all_blocked,
        "all_confirmed": all_confirmed,
        "all_unchanged": all_unchanged,
        "attack_rows": attack_rows,
        "integrity_rows": integrity_rows,
    }


if __name__ == "__main__":
    run()
