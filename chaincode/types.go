package main

const (
	Park1ID           = "Park1"
	Park2ID           = "Park2"
	Park3ID           = "Park3"
	NetworkObserverID = "NetworkObserver"

	StatusDispatching = "DISPATCHING"
	StatusConverged   = "CONVERGED"
	StatusFailed      = "FAILED"
	StatusSettled     = "SETTLED"

	// Energy type indices used in 4-energy arrays.
	EnergyE = 0 // electricity
	EnergyG = 1 // natural gas
	EnergyH = 2 // heat  (Park1-Park2 only; Park3 always zero)
	EnergyC = 3 // cold
)

// ADMMState is the on-chain ADMM coordination state.
// All four energy types are tracked separately.
// Heat (H) only involves Park1 and Park2, so lambdaH and zH have shape [2][24].
// All other energy types have shape [3][24].
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

// IterationRecord stores one park's submission for one ADMM iteration.
// Each exchange field is a 24-element array (one value per hour).
type IterationRecord struct {
	ScheduleID string `json:"scheduleId"`
	Iter       int    `json:"iter"`
	ParkID     string `json:"parkId"`

	// Net exchange per energy type (MW * EScale, positive = import).
	EExScaled []int64 `json:"eExScaled"` // [24]
	GExScaled []int64 `json:"gExScaled"` // [24]
	HExScaled []int64 `json:"hExScaled"` // [24]  must be all-zero for Park3
	CExScaled []int64 `json:"cExScaled"` // [24]

	LocalCostScaled   int64  `json:"localCostScaled"`   // yuan * CostScale
	LocalCarbonScaled int64  `json:"localCarbonScaled"` // tCO2 * CarbonScale
	Timestamp         string `json:"timestamp"`
}

// FinalSchedule is written on-chain when ADMM converges (or falls back).
type FinalSchedule struct {
	ScheduleID    string `json:"scheduleId"`
	Date          string `json:"date"`
	ConvergedIter int    `json:"convergedIter"`
	IsFallback    bool   `json:"isFallback"`

	TotalCostScaled   int64 `json:"totalCostScaled"`
	TotalCarbonScaled int64 `json:"totalCarbonScaled"`

	// Final coordinator allocations z^α_m (used for settlement).
	FinalZEScaled [][]int64 `json:"finalZEScaled"` // [3][24]
	FinalZGScaled [][]int64 `json:"finalZGScaled"` // [3][24]
	FinalZHScaled [][]int64 `json:"finalZHScaled"` // [2][24]
	FinalZCScaled [][]int64 `json:"finalZCScaled"` // [3][24]

	// Final net exchanges submitted by each park.
	FinalEEScaled [][]int64 `json:"finalEEScaled"` // [3][24]
	FinalEGScaled [][]int64 `json:"finalEGScaled"` // [3][24]
	FinalEHScaled [][]int64 `json:"finalEHScaled"` // [3][24]
	FinalECScaled [][]int64 `json:"finalECScaled"` // [3][24]

	// Final dual multipliers (for audit and shadow-price settlement).
	FinalLambdaEScaled [][]int64 `json:"finalLambdaEScaled"` // [3][24]
	FinalLambdaGScaled [][]int64 `json:"finalLambdaGScaled"` // [3][24]
	FinalLambdaHScaled [][]int64 `json:"finalLambdaHScaled"` // [2][24]
	FinalLambdaCScaled [][]int64 `json:"finalLambdaCScaled"` // [3][24]

	// Clearing (average) shadow price per energy per hour (for settlement).
	ClearingLambdaEScaled []int64 `json:"clearingLambdaEScaled"` // [24]
	ClearingLambdaGScaled []int64 `json:"clearingLambdaGScaled"` // [24]
	ClearingLambdaHScaled []int64 `json:"clearingLambdaHScaled"` // [24]  mean of Park1+Park2
	ClearingLambdaCScaled []int64 `json:"clearingLambdaCScaled"` // [24]

	FinalCarbonScaled []int64 `json:"finalCarbonScaled"` // [3]
	ScheduleHash      string  `json:"scheduleHash"`
	Timestamp         string  `json:"timestamp"`
}

// NetworkParams published by the Observer before dispatching.
type NetworkParams struct {
	Date string `json:"date"`

	// Capacity limits per park per energy type (MW * EScale).
	EMaxScaled []int64 `json:"eMaxScaled"` // [3]
	GMaxScaled []int64 `json:"gMaxScaled"` // [3]
	HMaxScaled []int64 `json:"hMaxScaled"` // [2]  Park1, Park2 only
	CMaxScaled []int64 `json:"cMaxScaled"` // [3]

	PublishedBy string `json:"publishedBy"`
	Timestamp   string `json:"timestamp"`
}

// SettlementRecord stores per-park settlement result after convergence.
type SettlementRecord struct {
	ScheduleID string `json:"scheduleId"`
	ParkID     string `json:"parkId"`

	// Energy payment per type (yuan * CostScale).
	EnergyPaymentEScaled int64 `json:"energyPaymentEScaled"`
	EnergyPaymentGScaled int64 `json:"energyPaymentGScaled"`
	EnergyPaymentHScaled int64 `json:"energyPaymentHScaled"`
	EnergyPaymentCScaled int64 `json:"energyPaymentCScaled"`

	TotalEnergyPaymentScaled int64 `json:"totalEnergyPaymentScaled"`
	CarbonPaymentScaled      int64 `json:"carbonPaymentScaled"`
	TotalPaymentScaled       int64 `json:"totalPaymentScaled"`
	Timestamp                string `json:"timestamp"`
}

type ActualMeterRecord struct {
	ScheduleID string `json:"scheduleId"`
	ParkID     string `json:"parkId"`

	ActualEEScaled []int64 `json:"actualEEScaled"` // [24]
	ActualEGScaled []int64 `json:"actualEGScaled"` // [24]
	ActualEHScaled []int64 `json:"actualEHScaled"` // [24]
	ActualECScaled []int64 `json:"actualECScaled"` // [24]
	Timestamp      string  `json:"timestamp"`
}

type AnomalyRecord struct {
	AnomalyID  string `json:"anomalyId"`
	ScheduleID string `json:"scheduleId"`
	ParkID     string `json:"parkId"`
	Type       string `json:"type"`
	Detail     string `json:"detail"`
	Timestamp  string `json:"timestamp"`
}

type CreditScore struct {
	ParkID       string `json:"parkId"`
	Score        int64  `json:"score"`
	LastUpdated  string `json:"lastUpdated"`
	NormalStreak int    `json:"normalStreak"`
}
