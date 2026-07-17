package main

import (
	"errors"
	"fmt"
	"math"
)

const (
	EScale      int64 = 1000
	LambdaScale int64 = 10000
	RhoScale    int64 = 10000
	CostScale   int64 = 100
	CarbonScale int64 = 1000

	// Adaptive rho parameters (μ=10, ω=2, matching MATLAB admm_four_energy_three_park.m)
	DefaultMuSq  int64 = 100 // μ² = 10² = 100
	DefaultOmega int64 = 2

	RhoMinScaled int64 = 500     // 0.05 * RhoScale
	RhoMaxScaled int64 = 1000000 // 100.0 * RhoScale

	MaxSqrtInt64 int64 = 3037000499
)

// computeZUpdate3Park computes the z-update for energy types with 3-park coupling:
// electricity (E), gas (G), and cold (C).
//
// Coordinator update: v_m = e_m + λ_m/ρ,  z_m = v_m - mean_m(v)
// This projects onto the hyperplane Σ z_m = 0 (energy conservation).
// Input shapes: eScaled [3][24], lambdaScaled [3][24].
func computeZUpdate3Park(eScaled, lambdaScaled [][]int64, rhoScaled int64) ([][]int64, error) {
	if rhoScaled <= 0 {
		return nil, errors.New("rhoScaled must be positive")
	}
	rows, cols, err := validateSameShape(eScaled, lambdaScaled)
	if err != nil {
		return nil, err
	}

	aux := makeMatrix(rows, cols)
	z := makeMatrix(rows, cols)

	for m := 0; m < rows; m++ {
		for t := 0; t < cols; t++ {
			// v_m = e_m + λ_m/ρ  (integer: (λ * EScale) / rho)
			lambdaOverRho, err := checkedMul(lambdaScaled[m][t], EScale)
			if err != nil {
				return nil, fmt.Errorf("lambda/rho numerator overflow at [%d][%d]: %w", m, t, err)
			}
			aux[m][t] = eScaled[m][t] + divRound(lambdaOverRho, rhoScaled)
		}
	}

	for t := 0; t < cols; t++ {
		var sum int64
		for m := 0; m < rows; m++ {
			sum, err = checkedAdd(sum, aux[m][t])
			if err != nil {
				return nil, fmt.Errorf("aux sum overflow at time %d: %w", t, err)
			}
		}
		mean := divRound(sum, int64(rows))
		for m := 0; m < rows; m++ {
			z[m][t] = aux[m][t] - mean
		}
	}

	return z, nil
}

// computeZUpdateHeat computes the z-update for heat energy (H),
// which only couples Park1 and Park2 (indices 0 and 1).
//
// Coordinator update (2-park binary projection onto z_H1 + z_H2 = 0):
//   v_H1 = e_H1 + λ_H1/ρ
//   v_H2 = e_H2 + λ_H2/ρ
//   z_H1 = (v_H1 - v_H2) / 2
//   z_H2 = -z_H1
//
// Input shapes: eHScaled [2][24], lambdaHScaled [2][24].
// Returns zHScaled [2][24].
func computeZUpdateHeat(eHScaled, lambdaHScaled [][]int64, rhoScaled int64) ([][]int64, error) {
	if rhoScaled <= 0 {
		return nil, errors.New("rhoScaled must be positive")
	}
	if len(eHScaled) != 2 || len(lambdaHScaled) != 2 {
		return nil, errors.New("heat z-update requires exactly 2 parks (Park1, Park2)")
	}
	cols := len(eHScaled[0])
	if len(eHScaled[1]) != cols || len(lambdaHScaled[0]) != cols || len(lambdaHScaled[1]) != cols {
		return nil, errors.New("heat arrays have inconsistent time dimension")
	}

	zH := makeMatrix(2, cols)

	for t := 0; t < cols; t++ {
		// v_H1 = e_H1 + λ_H1/ρ
		lam1OverRho, err := checkedMul(lambdaHScaled[0][t], EScale)
		if err != nil {
			return nil, fmt.Errorf("heat lambda1/rho overflow at t=%d: %w", t, err)
		}
		vH1 := eHScaled[0][t] + divRound(lam1OverRho, rhoScaled)

		// v_H2 = e_H2 + λ_H2/ρ
		lam2OverRho, err := checkedMul(lambdaHScaled[1][t], EScale)
		if err != nil {
			return nil, fmt.Errorf("heat lambda2/rho overflow at t=%d: %w", t, err)
		}
		vH2 := eHScaled[1][t] + divRound(lam2OverRho, rhoScaled)

		// z_H1 = (v_H1 - v_H2) / 2,  z_H2 = -z_H1
		zH[0][t] = divRound(vH1-vH2, 2)
		zH[1][t] = -zH[0][t]
	}

	return zH, nil
}

// computeLambdaUpdate updates dual multipliers for a 3-park energy type.
// λ_m ← λ_m + ρ·(e_m - z_m)
// Input shapes: [3][24].
func computeLambdaUpdate(lambdaScaled, eScaled, zScaled [][]int64, rhoScaled int64) ([][]int64, error) {
	if rhoScaled <= 0 {
		return nil, errors.New("rhoScaled must be positive")
	}
	rows, cols, err := validateSameShape(lambdaScaled, eScaled, zScaled)
	if err != nil {
		return nil, err
	}

	out := makeMatrix(rows, cols)
	for m := 0; m < rows; m++ {
		for t := 0; t < cols; t++ {
			diff := eScaled[m][t] - zScaled[m][t]
			num, err := checkedMul(rhoScaled, diff)
			if err != nil {
				return nil, fmt.Errorf("lambda increment overflow at [%d][%d]: %w", m, t, err)
			}
			inc := divRound(num, EScale)
			out[m][t], err = checkedAdd(lambdaScaled[m][t], inc)
			if err != nil {
				return nil, fmt.Errorf("lambda update overflow at [%d][%d]: %w", m, t, err)
			}
		}
	}
	return out, nil
}

// computeLambdaUpdateHeat updates dual multipliers for the heat energy type.
// Only Park1 and Park2 participate; inputs have shape [2][24].
// Identical formula to computeLambdaUpdate — separate function for clarity.
func computeLambdaUpdateHeat(lambdaHScaled, eHScaled, zHScaled [][]int64, rhoScaled int64) ([][]int64, error) {
	return computeLambdaUpdate(lambdaHScaled, eHScaled, zHScaled, rhoScaled)
}

// computePrimalResidSq computes the combined primal residual squared across
// all four energy types:
//   r_pri² = Σ_α ‖e^α - z^α‖_F²
//
// Each input pair is (eScaled, zScaled) for one energy type.
// E, G, C have shape [3][24]; H has shape [2][24].
func computePrimalResidSq(
	eEScaled, zEScaled [][]int64,
	eGScaled, zGScaled [][]int64,
	eHScaled, zHScaled [][]int64, // [2][24]
	eCScaled, zCScaled [][]int64,
) (int64, error) {
	var total int64
	var err error

	for _, pair := range [][2][][]int64{
		{eEScaled, zEScaled},
		{eGScaled, zGScaled},
		{eHScaled, zHScaled},
		{eCScaled, zCScaled},
	} {
		e, z := pair[0], pair[1]
		rows := len(e)
		for m := 0; m < rows; m++ {
			for t := 0; t < len(e[m]); t++ {
				diff := e[m][t] - z[m][t]
				total, err = addSquare(total, diff)
				if err != nil {
					return 0, fmt.Errorf("primal residual overflow: %w", err)
				}
			}
		}
	}
	return total, nil
}

// computeDualResidSq computes the combined dual residual squared across
// all four energy types:
//   r_dual = ρ · √(Σ_α ‖z^α_new - z^α_old‖_F²)
//
// E, G, C have shape [3][24]; H has shape [2][24].
func computeDualResidSq(
	zENew, zEOld [][]int64,
	zGNew, zGOld [][]int64,
	zHNew, zHOld [][]int64, // [2][24]
	zCNew, zCOld [][]int64,
	rhoScaled int64,
) (int64, error) {
	if rhoScaled <= 0 {
		return 0, errors.New("rhoScaled must be positive")
	}

	var sumDeltaZSq int64
	var err error

	for _, pair := range [][2][][]int64{
		{zENew, zEOld},
		{zGNew, zGOld},
		{zHNew, zHOld},
		{zCNew, zCOld},
	} {
		zNew, zOld := pair[0], pair[1]
		rows := len(zNew)
		for m := 0; m < rows; m++ {
			for t := 0; t < len(zNew[m]); t++ {
				diff := zNew[m][t] - zOld[m][t]
				sumDeltaZSq, err = addSquare(sumDeltaZSq, diff)
				if err != nil {
					return 0, fmt.Errorf("dual delta-z overflow: %w", err)
				}
			}
		}
	}

	// dualResid = rhoScaled × isqrt(sumDeltaZSq) / RhoScale
	// keeps the same unit as primalResid (×EScale = ×1000)
	deltaZNorm := isqrt(sumDeltaZSq)
	intermediate, err := checkedMul(rhoScaled, deltaZNorm)
	if err != nil {
		return 0, fmt.Errorf("dual residual intermediate overflow: %w", err)
	}
	dualResid := divRound(intermediate, RhoScale)

	dualResidSq, err := checkedMul(dualResid, dualResid)
	if err != nil {
		return 0, fmt.Errorf("dual residual square overflow: %w", err)
	}
	return dualResidSq, nil
}

// adaptRho adjusts the penalty parameter using the standard adaptive rule
// (Ghadimi et al. 2015, same as MATLAB script):
//   if r_pri > μ·r_dual  → ρ ← min(ρ_max, ω·ρ)
//   if r_dual > μ·r_pri  → ρ ← max(ρ_min, ρ/ω)
func adaptRho(rhoScaled, primalSq, dualSq int64) int64 {
	if rhoScaled <= 0 {
		return rhoScaled
	}
	upperTrigger := saturatingMul(DefaultMuSq, dualSq)
	lowerTrigger := saturatingMul(DefaultMuSq, primalSq)

	switch {
	case primalSq > upperTrigger:
		return minInt64(RhoMaxScaled, saturatingMul(DefaultOmega, rhoScaled))
	case dualSq > lowerTrigger:
		return maxInt64(RhoMinScaled, rhoScaled/DefaultOmega)
	default:
		return rhoScaled
	}
}

func hasConverged(primalSq, dualSq, epsPriSq, epsDualSq int64) bool {
	return primalSq <= epsPriSq && dualSq <= epsDualSq
}

func epsSqScaled(epsMW float64) int64 {
	scaled := epsMW * float64(EScale)
	return int64(math.Round(scaled * scaled))
}

// isqrt returns floor(√n) for n ≥ 0.
func isqrt(n int64) int64 {
	if n <= 0 {
		return 0
	}
	x := int64(math.Sqrt(float64(n)))
	for x > 0 && x*x > n {
		x--
	}
	for (x+1)*(x+1) <= n {
		x++
	}
	return x
}

// ── Shape helpers ─────────────────────────────────────────────────────────────

func validateSameShape(mats ...[][]int64) (int, int, error) {
	if len(mats) == 0 {
		return 0, 0, errors.New("no matrices")
	}
	rows := len(mats[0])
	if rows == 0 {
		return 0, 0, errors.New("matrix has no rows")
	}
	cols := len(mats[0][0])
	if cols == 0 {
		return 0, 0, errors.New("matrix has no columns")
	}
	for i, mat := range mats {
		if len(mat) != rows {
			return 0, 0, fmt.Errorf("matrix %d row mismatch: got %d want %d", i, len(mat), rows)
		}
		for r := range mat {
			if len(mat[r]) != cols {
				return 0, 0, fmt.Errorf("matrix %d row %d column mismatch: got %d want %d", i, r, len(mat[r]), cols)
			}
		}
	}
	return rows, cols, nil
}

func makeMatrix(rows, cols int) [][]int64 {
	out := make([][]int64, rows)
	for i := range out {
		out[i] = make([]int64, cols)
	}
	return out
}

// ── Arithmetic helpers ────────────────────────────────────────────────────────

func divRound(num, den int64) int64 {
	if den == 0 {
		panic("division by zero")
	}
	if den < 0 {
		num = -num
		den = -den
	}
	q := num / den
	r := num % den
	if r == 0 {
		return q
	}
	if r < 0 {
		r = -r
	}
	if r*2 >= den {
		if num > 0 {
			q++
		} else {
			q--
		}
	}
	return q
}

func addSquare(total, x int64) (int64, error) {
	if x > MaxSqrtInt64 || x < -MaxSqrtInt64 {
		return 0, errors.New("square exceeds int64")
	}
	sq := x * x
	return checkedAdd(total, sq)
}

func checkedAdd(a, b int64) (int64, error) {
	if (b > 0 && a > math.MaxInt64-b) || (b < 0 && a < math.MinInt64-b) {
		return 0, errors.New("int64 addition overflow")
	}
	return a + b, nil
}

func checkedMul(a, b int64) (int64, error) {
	if a == 0 || b == 0 {
		return 0, nil
	}
	if a == math.MinInt64 || b == math.MinInt64 {
		return 0, errors.New("int64 multiplication overflow")
	}
	absA := absInt64(a)
	absB := absInt64(b)
	if absA > math.MaxInt64/absB {
		return 0, errors.New("int64 multiplication overflow")
	}
	return a * b, nil
}

func saturatingMul(a, b int64) int64 {
	v, err := checkedMul(a, b)
	if err != nil {
		if (a < 0) != (b < 0) {
			return math.MinInt64
		}
		return math.MaxInt64
	}
	return v
}

func absInt64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}

func minInt64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
