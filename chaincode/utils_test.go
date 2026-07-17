package main

import (
	"reflect"
	"testing"
)

func TestDivRoundSigned(t *testing.T) {
	tests := []struct {
		num  int64
		den  int64
		want int64
	}{
		{1000, 3, 333},
		{1001, 3, 334},
		{-1000, 3, -333},
		{-1001, 3, -334},
		{5, 2, 3},
		{-5, 2, -3},
	}

	for _, tt := range tests {
		if got := divRound(tt.num, tt.den); got != tt.want {
			t.Fatalf("divRound(%d,%d)=%d want %d", tt.num, tt.den, got, tt.want)
		}
	}
}

func TestComputeZUpdateUsesCorrectLambdaRhoScale(t *testing.T) {
	e := [][]int64{
		{0},
		{0},
		{0},
	}
	lambda := [][]int64{
		{10000}, // 1 yuan/MW
		{0},
		{0},
	}

	z, err := computeZUpdate(e, lambda, 10000) // rho=1
	if err != nil {
		t.Fatal(err)
	}

	// aux=[1000,0,0], mean=333, z=[667,-333,-333].
	// This test catches the common bug e + lambdaScaled/rhoScaled,
	// which would produce approximately [1,0,0] before mean removal.
	want := [][]int64{{667}, {-333}, {-333}}
	if !reflect.DeepEqual(z, want) {
		t.Fatalf("z=%v want %v", z, want)
	}

	if sum := z[0][0] + z[1][0] + z[2][0]; absInt64(sum) > 3 {
		t.Fatalf("sum z = %d, want close to zero", sum)
	}
}

func TestComputeZUpdateTwoTimeSteps(t *testing.T) {
	e := [][]int64{
		{1000, 2000},
		{0, 1000},
		{-1000, -3000},
	}
	lambda := [][]int64{
		{10000, 0},
		{10000, 0},
		{10000, 0},
	}

	z, err := computeZUpdate(e, lambda, 10000)
	if err != nil {
		t.Fatal(err)
	}

	want := [][]int64{
		{1000, 2000},
		{0, 1000},
		{-1000, -3000},
	}
	if !reflect.DeepEqual(z, want) {
		t.Fatalf("z=%v want %v", z, want)
	}
}

func TestComputeLambdaUpdateScale(t *testing.T) {
	lambda := [][]int64{
		{0},
		{0},
		{0},
	}
	e := [][]int64{
		{1000},
		{0},
		{-1000},
	}
	z := [][]int64{
		{0},
		{0},
		{0},
	}

	got, err := computeLambdaUpdate(lambda, e, z, 10000)
	if err != nil {
		t.Fatal(err)
	}

	want := [][]int64{{10000}, {0}, {-10000}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("lambda=%v want %v", got, want)
	}
}

func TestComputePrimalResidSq(t *testing.T) {
	e := [][]int64{
		{1000, 500},
		{0, 0},
	}
	z := [][]int64{
		{0, 500},
		{1000, -500},
	}

	got, err := computePrimalResidSq(e, z)
	if err != nil {
		t.Fatal(err)
	}

	// diffs: 1000,0,-1000,500 => 1e6+0+1e6+250000
	want := int64(2250000)
	if got != want {
		t.Fatalf("primal=%d want %d", got, want)
	}
}

func TestComputeDualResidSqIncludesRhoScale(t *testing.T) {
	zOld := [][]int64{{0}, {0}}
	zNew := [][]int64{{1000}, {-1000}}
	// sumDeltaZSq = 1000² + 1000² = 2,000,000
	// isqrt(2,000,000) = 1414  (floor of sqrt(2)×1000 = 1414.21...)
	// rho=1: dualResid = 10000×1414/10000 = 1414 → dualResidSq = 1414² = 1,999,396
	// rho=2: dualResid = 20000×1414/10000 = 2828 → dualResidSq = 2828² = 7,997,584
	// (small difference from exact 2,000,000 / 8,000,000 is integer-sqrt truncation)

	gotRho1, err := computeDualResidSq(zNew, zOld, 10000)
	if err != nil {
		t.Fatal(err)
	}
	// allow ±1% tolerance for integer sqrt rounding
	if gotRho1 < 1979000 || gotRho1 > 2020000 {
		t.Fatalf("dual rho=1 got %d, expected ~1999396", gotRho1)
	}

	gotRho2, err := computeDualResidSq(zNew, zOld, 20000)
	if err != nil {
		t.Fatal(err)
	}
	if gotRho2 < 7917000 || gotRho2 > 8080000 {
		t.Fatalf("dual rho=2 got %d, expected ~7997584", gotRho2)
	}
}

func TestDualResidSmallRho(t *testing.T) {
	zOld := [][]int64{{0}, {0}}
	zNew := [][]int64{{1000}, {-1000}}
	// rho=0.5 (rhoScaled=5000) — was zero with the old code due to integer underflow.
	// Fix: isqrt(2,000,000)=1414, dualResid=5000×1414/10000=707, dualResidSq=707²=499849
	got, err := computeDualResidSq(zNew, zOld, 5000)
	if err != nil {
		t.Fatal(err)
	}
	if got == 0 {
		t.Fatal("dualResidSq is zero for rho=0.5: integer underflow bug not fixed")
	}
	// real r_dual = 0.5×sqrt(2) ≈ 0.7071 → scaled ≈ 707 → sq ≈ 499849
	if got < 490000 || got > 510000 {
		t.Fatalf("dualResidSq rho=0.5 got %d, expected ~499849", got)
	}
}

func TestDualResidVerySmallRho(t *testing.T) {
	zOld := [][]int64{{0}, {0}}
	zNew := [][]int64{{1000}, {-1000}}
	// rho=0.0625 (rhoScaled=625) — smallest value seen in MATLAB iteration history.
	// isqrt(2,000,000)=1414, dualResid=625×1414/10000=88, dualResidSq=88²=7744
	got, err := computeDualResidSq(zNew, zOld, 625)
	if err != nil {
		t.Fatal(err)
	}
	if got == 0 {
		t.Fatal("dualResidSq is zero for rho=0.0625: integer underflow bug not fixed")
	}
	// real r_dual = 0.0625×sqrt(2) ≈ 0.0884 → scaled ≈ 88 → sq ≈ 7744
	if got < 7000 || got > 8500 {
		t.Fatalf("dualResidSq rho=0.0625 got %d, expected ~7744", got)
	}
}

func TestIsqrt(t *testing.T) {
	cases := []struct {
		n    int64
		want int64
	}{
		{0, 0},
		{1, 1},
		{4, 2},
		{9, 3},
		{2000000, 1414},  // isqrt(2×10^6): floor(sqrt(2)×1000)=1414
		{9300000000, 96436}, // near max primalResidSq for sanity
	}
	for _, c := range cases {
		got := isqrt(c.n)
		if got != c.want {
			t.Fatalf("isqrt(%d) = %d, want %d", c.n, got, c.want)
		}
	}
}

func TestAdaptRho(t *testing.T) {
	if got := adaptRho(10000, 10001, 100); got != 20000 {
		t.Fatalf("rho increase got %d want 20000", got)
	}
	if got := adaptRho(10000, 100, 10001); got != 5000 {
		t.Fatalf("rho decrease got %d want 5000", got)
	}
	if got := adaptRho(10000, 1000, 1000); got != 10000 {
		t.Fatalf("rho unchanged got %d want 10000", got)
	}
	if got := adaptRho(RhoMaxScaled, 10001, 100); got != RhoMaxScaled {
		t.Fatalf("rho max cap got %d want %d", got, RhoMaxScaled)
	}
	if got := adaptRho(RhoMinScaled, 100, 10001); got != RhoMinScaled {
		t.Fatalf("rho min cap got %d want %d", got, RhoMinScaled)
	}
}

func TestConvergenceThreshold(t *testing.T) {
	eps := epsSqScaled(0.05)
	if eps != 2500 {
		t.Fatalf("epsSqScaled(0.05)=%d want 2500", eps)
	}
	if !hasConverged(2500, 2499, eps, eps) {
		t.Fatal("expected converged")
	}
	if hasConverged(2501, 2499, eps, eps) {
		t.Fatal("expected not converged")
	}
}

func TestValidateShapeRejectsMismatch(t *testing.T) {
	a := [][]int64{{1, 2}, {3, 4}}
	b := [][]int64{{1}, {2}}
	if _, _, err := validateSameShape(a, b); err == nil {
		t.Fatal("expected shape mismatch error")
	}
}

func TestOverflowGuard(t *testing.T) {
	if _, err := addSquare(0, MaxSqrtInt64+1); err == nil {
		t.Fatal("expected square overflow error")
	}
}
