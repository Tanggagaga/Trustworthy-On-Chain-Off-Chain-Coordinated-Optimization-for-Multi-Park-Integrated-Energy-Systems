package main
import("math";"errors")
const(MaskScale int64=1000000; EScale int64=1000; LambdaScale int64=10000; RhoScale int64=10000; MaxSqrtInt64 int64=3037000499; horizonHours=24;parkCount=3;heatParkCount=2;eBlockLen=96)
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
func absInt64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}
type ADMMState struct {
	ScheduleID string `json:"scheduleId"`
	Date       string `json:"date"`
	Iter       int    `json:"iter"`

	RhoScaled int64 `json:"rhoScaled"` // rho * RhoScale

	// Dual multipliers: λ^α_m per energy type, per park, per hour.
	LambdaEScaled [][]int64 `json:"lambdaEScaled"` // [3][24]
	LambdaGScaled [][]int64 `json:"lambdaGScaled"` // [3][24]
	LambdaHScaled [][]int64 `json:"lambdaHScaled"` // [2][24]  Park1=index0, Park2=index1
	LambdaCScaled [][]int64 `json:"lambdaCScaled"` // [3][24]

	// Coordinator allocation: z^α_m per energy type, per park, per hour.
	ZEScaled [][]int64 `json:"zEScaled"` // [3][24]
	ZGScaled [][]int64 `json:"zGScaled"` // [3][24]
	ZHScaled [][]int64 `json:"zHScaled"` // [2][24]
	ZCScaled [][]int64 `json:"zCScaled"` // [3][24]

	// Residuals: combined Frobenius norm across all 4 energy types.
	PrimalResidSq   int64 `json:"primalResidSq"`   // (MW * EScale)^2, summed over all energies
	DualResidSq     int64 `json:"dualResidSq"`
	EpsPriSqScaled  int64 `json:"epsPriSqScaled"`
	EpsDualSqScaled int64 `json:"epsDualSqScaled"`

	MaxIter     int `json:"maxIter"`
	BestIter    int `json:"bestIter"`
	BestResidSq int64 `json:"bestResidSq"`

	// Best-so-far snapshots (same shapes as live arrays).
	BestZEScaled      [][]int64 `json:"bestZEScaled"`
	BestZGScaled      [][]int64 `json:"bestZGScaled"`
	BestZHScaled      [][]int64 `json:"bestZHScaled"` // [2][24]
	BestZCScaled      [][]int64 `json:"bestZCScaled"`
	BestLambdaEScaled [][]int64 `json:"bestLambdaEScaled"`
	BestLambdaGScaled [][]int64 `json:"bestLambdaGScaled"`
	BestLambdaHScaled [][]int64 `json:"bestLambdaHScaled"` // [2][24]
	BestLambdaCScaled [][]int64 `json:"bestLambdaCScaled"`

	Converged  bool   `json:"converged"`
	IsFallback bool   `json:"isFallback"`
	Status     string `json:"status"`

	CarbonPriceScaled int64   `json:"carbonPriceScaled"`
	CarbonQuotaScaled []int64 `json:"carbonQuotaScaled"` // [3]

	// Network capacity limits per park per energy type (MW * EScale).
	EMaxScaled []int64 `json:"eMaxScaled"` // [3] electric
	GMaxScaled []int64 `json:"gMaxScaled"` // [3] gas
	HMaxScaled []int64 `json:"hMaxScaled"` // [2] heat  (Park1, Park2 only)
	CMaxScaled []int64 `json:"cMaxScaled"` // [3] cold
}
type MaskedSubproblem struct {
	ScheduleID string `json:"scheduleId"`
	ParkID     string `json:"parkId"` // 子问题所有者（数据方）

	K0    int `json:"k0"`    // masked变量y的维度
	QRows int `json:"qRows"` // 不等式约束G_masked的行数

	HE0Scaled     [][]int64 `json:"hE0Scaled"`     // k0 x k0，H_masked^(k)=rho^(k)*HE0
	GMaskedScaled [][]int64 `json:"gMaskedScaled"` // qRows x k0
	HVecScaled    []int64   `json:"hVecScaled"`    // qRows，对应h_masked（命名避开跟HE0混淆）
	C0Scaled      []int64   `json:"c0Scaled"`      // k0
	ERowsScaled   [][]int64 `json:"eRowsScaled"`   // 96 x k0，e-block对应的行
	X0EScaled     []int64   `json:"x0EScaled"`     // 96，x0的e-block分量

	MaskScale int64  `json:"maskScale"` // 必须等于本链码版本约定的MaskScale
	MaskHash  string `json:"maskHash"`  // sha256(以上6个量)，后续所有任务绑定这个hash

	RegisteredBy string `json:"registeredBy"`
	Timestamp    string `json:"timestamp"`
}
