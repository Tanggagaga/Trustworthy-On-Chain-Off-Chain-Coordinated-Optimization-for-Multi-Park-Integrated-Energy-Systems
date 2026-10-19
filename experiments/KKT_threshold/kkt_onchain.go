package main

import (
	"fmt"
	"math"
)

// ── 链上 KKT 定点复算（M7）───────────────────────────────────────────────────
//
// 这里的每一步都必须与链下 verifier 参考实现（client/masked_solver.py 的
// build_masked_round + check_kkt_scaled）逐步对应：同一份 (y,mu) 在链上链下
// 必须得出同一个 PASS/FAIL 结论。链下用 float 重构系数后一次性取整，链上
// 全程 int64 定点、逐乘逐除（divRound），两者残差允许差几个最低位——
// 但裁决阈值是 10,000，正常提交的残差在 1e3 量级、作恶提交在 1e5 以上，
// 几个单位的取整差不可能翻转结论。
//
// 全程无浮点：背书节点各自执行本函数得到的读写集必须逐字节一致，
// 任何浮点运算都可能破坏这一点，这是把 KKT 搬上链的前提约束。

// KKTTolScaled 是链上 KKT 裁决阈值 = 0.01 × MaskScale。
// 必须与 masked_solver.py 的 KKT_TOL 保持同一数值。
const KKTTolScaled int64 = 10_000

// λ(×LambdaScale=1e4)、z(×EScale=1e3) 换算到 MaskScale(1e6) 的倍率。
const (
	lambdaToMask int64 = MaskScale / LambdaScale // 100
	eToMask      int64 = MaskScale / EScale      // 1000
)

// kktResult 是链码复算出的裁决。Saturated 表示中间量发生 int64 溢出：
// 这只会出现在恶意构造的极端数值上，此时残差按 MaxInt64 记、直接判 FAIL，
// 而不是让交易报错——垃圾提交应当得到一个 FAIL 裁决进账本，
// 不能反过来卡死本轮流程。
type kktResult struct {
	StatRes   int64
	ComplRes  int64
	PrimalOK  bool
	DualOK    bool
	Pass      bool
	Saturated bool
}

// computeKKTOnChain 用链上已注册的 mask 常量和当前 ADMMState 的 (ρ,λ,z)，
// 对提交的 (y,mu) 定点复算四条 KKT 条件。
//
// 定点公式（均 ×MaskScale）：
//
//	H'_ij   = divRound(ρs·HE0_ij, RhoScale)
//	d_i     = λ_i·100 − divRound(ρs·z_i, RhoScale)·1000
//	c'_j    = C0_j + divRound(ρs·Σ_i divRound(x0e_i·E_ij, 1e6), RhoScale)
//	          + Σ_i divRound(d_i·E_ij, 1e6)
//	stat_i  = Σ_j divRound(H'_ij·y_j, 1e6) + c'_i + Σ_r divRound(G_ri·μ_r, 1e6)
//	slack_r = Σ_j divRound(G_rj·y_j, 1e6) − h_r
//	stat_res = isqrt(Σ stat_i²)，compl_res = max_r |divRound(μ_r·slack_r, 1e6)|
//
// λ/z 块按 [电|气|热|冷]×24h 拼接，与客户端 lam_block/z_block 同序；
// 热网只有 Park1/Park2，Park3 的热行取零（同客户端约定）。
func computeKKTOnChain(
	mask *MaskedSubproblem,
	state *ADMMState,
	parkIdx int,
	y, mu []int64,
) (*kktResult, error) {
	k0, q := mask.K0, mask.QRows
	if len(y) != k0 || len(mu) != q {
		return nil, fmt.Errorf("kkt recompute: y/mu dimension mismatch (%d/%d, want %d/%d)",
			len(y), len(mu), k0, q)
	}
	if len(mask.HE0Scaled) != k0 || len(mask.GMaskedScaled) != q ||
		len(mask.HVecScaled) != q || len(mask.C0Scaled) != k0 ||
		len(mask.ERowsScaled) != eBlockLen || len(mask.X0EScaled) != eBlockLen {
		return nil, fmt.Errorf("kkt recompute: registered mask dimensions corrupted")
	}
	if parkIdx < 0 || parkIdx >= parkCount {
		return nil, fmt.Errorf("kkt recompute: invalid park index %d", parkIdx)
	}

	sat := false
	// mulDiv / add：溢出即置 sat，返回值不再可信，最终统一判 FAIL。
	mulDiv := func(a, b, den int64) int64 {
		p, err := checkedMul(a, b)
		if err != nil {
			sat = true
			return 0
		}
		return divRound(p, den)
	}
	add := func(a, b int64) int64 {
		s, err := checkedAdd(a, b)
		if err != nil {
			sat = true
			return a
		}
		return s
	}

	// ── λ/z 块拼接（×LambdaScale / ×EScale）──────────────────────────────
	lamBlock := make([]int64, eBlockLen)
	zBlock := make([]int64, eBlockLen)
	copyRow := func(dst []int64, src [][]int64, m int) error {
		if m >= len(src) || len(src[m]) != horizonHours {
			return fmt.Errorf("kkt recompute: ADMMState row shape invalid")
		}
		copy(dst, src[m])
		return nil
	}
	if err := copyRow(lamBlock[0:horizonHours], state.LambdaEScaled, parkIdx); err != nil {
		return nil, err
	}
	if err := copyRow(lamBlock[horizonHours:2*horizonHours], state.LambdaGScaled, parkIdx); err != nil {
		return nil, err
	}
	if parkIdx < heatParkCount { // Park3 不在热网：热块保持零
		if err := copyRow(lamBlock[2*horizonHours:3*horizonHours], state.LambdaHScaled, parkIdx); err != nil {
			return nil, err
		}
	}
	if err := copyRow(lamBlock[3*horizonHours:4*horizonHours], state.LambdaCScaled, parkIdx); err != nil {
		return nil, err
	}
	if err := copyRow(zBlock[0:horizonHours], state.ZEScaled, parkIdx); err != nil {
		return nil, err
	}
	if err := copyRow(zBlock[horizonHours:2*horizonHours], state.ZGScaled, parkIdx); err != nil {
		return nil, err
	}
	if parkIdx < heatParkCount {
		if err := copyRow(zBlock[2*horizonHours:3*horizonHours], state.ZHScaled, parkIdx); err != nil {
			return nil, err
		}
	}
	if err := copyRow(zBlock[3*horizonHours:4*horizonHours], state.ZCScaled, parkIdx); err != nil {
		return nil, err
	}

	// d_i = λ_i − ρ·z_i（×MaskScale）
	d := make([]int64, eBlockLen)
	for i := 0; i < eBlockLen; i++ {
		lam6, err := checkedMul(lamBlock[i], lambdaToMask)
		if err != nil {
			sat = true
			lam6 = 0
		}
		rhoZ := mulDiv(state.RhoScaled, zBlock[i], RhoScale) // ×EScale
		rhoZ6, err := checkedMul(rhoZ, eToMask)
		if err != nil {
			sat = true
			rhoZ6 = 0
		}
		d[i] = lam6 - rhoZ6
	}

	// c'_j = C0_j + ρ·(x0e·E)_j + (d·E)_j（×MaskScale）
	cPrime := make([]int64, k0)
	for j := 0; j < k0; j++ {
		var s1, s3 int64
		for i := 0; i < eBlockLen; i++ {
			eij := mask.ERowsScaled[i][j]
			s1 = add(s1, mulDiv(mask.X0EScaled[i], eij, MaskScale))
			s3 = add(s3, mulDiv(d[i], eij, MaskScale))
		}
		t2 := mulDiv(state.RhoScaled, s1, RhoScale)
		cPrime[j] = add(add(mask.C0Scaled[j], t2), s3)
	}

	// stat_i = (H'y)_i + c'_i + (G'^T μ)_i，stat_res = ‖stat‖₂
	var statSq int64
	for i := 0; i < k0; i++ {
		var hy int64
		row := mask.HE0Scaled[i]
		for j := 0; j < k0; j++ {
			hp := mulDiv(state.RhoScaled, row[j], RhoScale)
			hy = add(hy, mulDiv(hp, y[j], MaskScale))
		}
		var gt int64
		for r := 0; r < q; r++ {
			gt = add(gt, mulDiv(mask.GMaskedScaled[r][i], mu[r], MaskScale))
		}
		s := add(add(hy, cPrime[i]), gt)
		sq, err := addSquare(statSq, s)
		if err != nil {
			sat = true
		} else {
			statSq = sq
		}
	}
	statRes := isqrt(statSq)

	// 原始可行性 + 互补松弛
	primalOK := true
	var complRes int64
	for r := 0; r < q; r++ {
		var gy int64
		grow := mask.GMaskedScaled[r]
		for j := 0; j < k0; j++ {
			gy = add(gy, mulDiv(grow[j], y[j], MaskScale))
		}
		slack := gy - mask.HVecScaled[r]
		if slack > KKTTolScaled {
			primalOK = false
		}
		c := mulDiv(mu[r], slack, MaskScale)
		if c < 0 {
			c = -c
		}
		if c > complRes {
			complRes = c
		}
	}

	// 对偶可行性
	dualOK := true
	for r := 0; r < q; r++ {
		if mu[r] < -KKTTolScaled {
			dualOK = false
			break
		}
	}

	res := &kktResult{
		StatRes:   statRes,
		ComplRes:  complRes,
		PrimalOK:  primalOK,
		DualOK:    dualOK,
		Saturated: sat,
	}
	if sat {
		res.StatRes = math.MaxInt64
		res.ComplRes = math.MaxInt64
		res.Pass = false
		return res, nil
	}
	res.Pass = primalOK && dualOK && statRes <= KKTTolScaled && complRes <= KKTTolScaled
	return res, nil
}
