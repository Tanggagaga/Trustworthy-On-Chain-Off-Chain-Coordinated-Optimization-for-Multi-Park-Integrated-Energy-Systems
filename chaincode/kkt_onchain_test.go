package main

import (
	"testing"
)

// ── 测试脚手架 ────────────────────────────────────────────────────────────────

// kktTestState 返回一个 λ=z=0、ρ=1.0 的最小 ADMMState，形状与 TriggerDispatch
// 初始化一致（E/G/C 为 [3][24]，H 为 [2][24]）。
func kktTestState() *ADMMState {
	return &ADMMState{
		RhoScaled:     RhoScale, // ρ = 1.0
		LambdaEScaled: makeMatrix(parkCount, horizonHours),
		LambdaGScaled: makeMatrix(parkCount, horizonHours),
		LambdaHScaled: makeMatrix(heatParkCount, horizonHours),
		LambdaCScaled: makeMatrix(parkCount, horizonHours),
		ZEScaled:      makeMatrix(parkCount, horizonHours),
		ZGScaled:      makeMatrix(parkCount, horizonHours),
		ZHScaled:      makeMatrix(heatParkCount, horizonHours),
		ZCScaled:      makeMatrix(parkCount, horizonHours),
	}
}

// kktTestMask 返回一个 k0×q 的全零 mask 骨架（E_rows/x0e 均为零），
// 调用方按需填充 HE0/G/h/C0。
func kktTestMask(k0, q int) *MaskedSubproblem {
	m := &MaskedSubproblem{
		K0: k0, QRows: q,
		HE0Scaled:     makeMatrix(k0, k0),
		GMaskedScaled: makeMatrix(q, k0),
		HVecScaled:    make([]int64, q),
		C0Scaled:      make([]int64, k0),
		ERowsScaled:   makeMatrix(eBlockLen, k0),
		X0EScaled:     make([]int64, eBlockLen),
		MaskScale:     MaskScale,
	}
	return m
}

// tinyQP 构造手工可验算的二维 QP（×MaskScale 定点）：
//
//	min ½y'Iy + c'y   s.t.  y0 ≤ 1, y1 ≤ 1, −y0 ≤ 1
//
// c = (−1, 0) 时无约束最优 y* = (1, 0) 恰在可行域内，
// 且第一行约束恰好活跃（slack=0），μ* = 0 满足全部四条 KKT。
func tinyQP() *MaskedSubproblem {
	m := kktTestMask(2, 3)
	m.HE0Scaled[0][0] = MaskScale // H = I
	m.HE0Scaled[1][1] = MaskScale
	m.C0Scaled[0] = -MaskScale // c = (−1, 0)
	m.GMaskedScaled[0][0] = MaskScale
	m.GMaskedScaled[1][1] = MaskScale
	m.GMaskedScaled[2][0] = -MaskScale
	m.HVecScaled[0] = MaskScale
	m.HVecScaled[1] = MaskScale
	m.HVecScaled[2] = MaskScale
	return m
}

// ── 正确性 ───────────────────────────────────────────────────────────────────

func TestKKTOnChainOptimalPasses(t *testing.T) {
	res, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{MaskScale, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Pass || res.StatRes != 0 || res.ComplRes != 0 {
		t.Fatalf("optimal solution should pass with zero residuals, got %+v", res)
	}
}

func TestKKTOnChainPerturbedPrimalFailsStationarity(t *testing.T) {
	// y0 从 1.0 扰动到 1.1：stat_0 = y0 + c0 = 0.1 → 1e5，超阈 10 倍
	res, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{1_100_000, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if res.Pass {
		t.Fatalf("perturbed y should fail, got %+v", res)
	}
	if res.StatRes < 90_000 || res.StatRes > 110_000 {
		t.Fatalf("statRes should be ≈1e5, got %d", res.StatRes)
	}
	if !res.PrimalOK { // y0=1.1 违反 y0≤1 恰好 0.1=1e5 > tol，也应触发原始可行性
		t.Log("primal violation also detected (expected: slack=1e5 > tol)")
	}
}

func TestKKTOnChainPrimalViolationDetected(t *testing.T) {
	res, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{2_500_000, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if res.PrimalOK || res.Pass {
		t.Fatalf("y0=2.5 violates y0≤1, primalOK must be false, got %+v", res)
	}
}

func TestKKTOnChainDualViolationDetected(t *testing.T) {
	res, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{MaskScale, 0}, []int64{0, -20_000, 0})
	if err != nil {
		t.Fatal(err)
	}
	if res.DualOK || res.Pass {
		t.Fatalf("μ1=−0.02 < −tol must fail dual feasibility, got %+v", res)
	}
}

func TestKKTOnChainComplementarityDetected(t *testing.T) {
	// 非活跃约束 −y0 ≤ 1（y0=1 时 slack = −2）上放正乘子 μ2 = 0.05：
	// 互补乘积 |0.05×(−2)| = 0.1 → compl_res = 1e5 超阈；
	// 同时 G 第三行的 −μ2 项也会打破平稳性——两条防线同时触发，
	// 正是论文 L3 实验的微缩版。
	res, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{MaskScale, 0}, []int64{0, 0, 50_000})
	if err != nil {
		t.Fatal(err)
	}
	if res.Pass {
		t.Fatalf("positive multiplier on inactive constraint must fail, got %+v", res)
	}
	if res.ComplRes < 90_000 || res.ComplRes > 110_000 {
		t.Fatalf("complRes should be ≈1e5, got %d", res.ComplRes)
	}
}

func TestKKTOnChainLambdaShiftsCPrime(t *testing.T) {
	// E_rows[0][0]=1.0，λ_E[park0][hour0]=1.0：
	// d_0 = 1e6，c'_0 = C0_0 + (d·E)_0 = −1e6 + 1e6 = 0，
	// 新最优点变成 y = (0,0)。验证 λ 路径的符号与缩放换算（×100）。
	mask := tinyQP()
	mask.ERowsScaled[0][0] = MaskScale
	state := kktTestState()
	state.LambdaEScaled[0][0] = LambdaScale // λ = 1.0

	res, err := computeKKTOnChain(mask, state, 0, []int64{0, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Pass || res.StatRes != 0 {
		t.Fatalf("with λ=1 the optimum moves to y=0, got %+v", res)
	}
}

func TestKKTOnChainRhoZCancelsLambda(t *testing.T) {
	// 在上一个测试基础上再加 z=1（ρ=1）：d_0 = λ−ρz = 0，
	// c' 退回 (−1,0)，最优点回到 y=(1,0)。验证 ρz 路径的符号与缩放换算（×1000）。
	mask := tinyQP()
	mask.ERowsScaled[0][0] = MaskScale
	state := kktTestState()
	state.LambdaEScaled[0][0] = LambdaScale // λ = 1.0
	state.ZEScaled[0][0] = EScale           // z = 1.0

	res, err := computeKKTOnChain(mask, state, 0, []int64{MaskScale, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Pass || res.StatRes != 0 {
		t.Fatalf("λ−ρz=0 should restore optimum y=(1,0), got %+v", res)
	}
}

func TestKKTOnChainPark3HeatBlockZero(t *testing.T) {
	// Park3（idx=2）不在热网：λ/z 热块必须按零处理，不能越界读 [2][24] 数组。
	mask := tinyQP()
	res, err := computeKKTOnChain(mask, kktTestState(), 2,
		[]int64{MaskScale, 0}, []int64{0, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Pass {
		t.Fatalf("Park3 with zero heat block should pass, got %+v", res)
	}
}

func TestKKTOnChainDimensionMismatchRejected(t *testing.T) {
	if _, err := computeKKTOnChain(tinyQP(), kktTestState(), 0,
		[]int64{MaskScale}, []int64{0, 0, 0}); err == nil {
		t.Fatal("wrong y dimension must be rejected")
	}
}

func TestKKTOnChainOverflowSaturatesToFail(t *testing.T) {
	// 极端数值触发 int64 溢出：必须饱和判 FAIL，而不是 panic 或返回错误。
	mask := tinyQP()
	const huge = int64(4_000_000_000_000_000_000)
	res, err := computeKKTOnChain(mask, kktTestState(), 0,
		[]int64{huge, huge}, []int64{huge, 0, 0})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Saturated || res.Pass {
		t.Fatalf("overflow input must saturate and fail, got %+v", res)
	}
}

// ── 性能 ─────────────────────────────────────────────────────────────────────

// BenchmarkKKTOnChainRealSize 用真实问题规模（k0=220, q=633）测单次复算耗时。
// 数据用确定性 LCG 填充（数值范围贴近真实 mask 常量量级），结果本身无意义，
// 只看耗时。
func BenchmarkKKTOnChainRealSize(b *testing.B) {
	const k0, q = 220, 633
	seed := int64(20260709)
	next := func(bound int64) int64 {
		seed = (seed*6364136223846793005 + 1442695040888963407) & 0x7fffffffffffffff
		return seed%(2*bound+1) - bound
	}

	mask := kktTestMask(k0, q)
	for i := 0; i < k0; i++ {
		for j := 0; j < k0; j++ {
			mask.HE0Scaled[i][j] = next(MaskScale)
		}
		mask.C0Scaled[i] = next(1_000 * MaskScale)
	}
	for r := 0; r < q; r++ {
		for j := 0; j < k0; j++ {
			mask.GMaskedScaled[r][j] = next(5 * MaskScale)
		}
		mask.HVecScaled[r] = next(10_000 * MaskScale)
	}
	for i := 0; i < eBlockLen; i++ {
		for j := 0; j < k0; j++ {
			mask.ERowsScaled[i][j] = next(MaskScale)
		}
		mask.X0EScaled[i] = next(10 * MaskScale)
	}
	state := kktTestState()
	state.RhoScaled = RhoScale / 2 // ρ = 0.5，主实验常见值
	for m := 0; m < parkCount; m++ {
		for t := 0; t < horizonHours; t++ {
			state.LambdaEScaled[m][t] = next(10 * LambdaScale)
			state.ZEScaled[m][t] = next(10 * EScale)
		}
	}
	y := make([]int64, k0)
	mu := make([]int64, q)
	for j := 0; j < k0; j++ {
		y[j] = next(50 * MaskScale)
	}
	for r := 0; r < q; r++ {
		v := next(1_000 * MaskScale)
		if v < 0 {
			v = -v
		}
		mu[r] = v
	}

	b.ResetTimer()
	for n := 0; n < b.N; n++ {
		if _, err := computeKKTOnChain(mask, state, 0, y, mu); err != nil {
			b.Fatal(err)
		}
	}
}
