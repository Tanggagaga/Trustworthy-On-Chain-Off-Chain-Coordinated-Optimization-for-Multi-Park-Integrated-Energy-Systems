package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"strconv"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

const (
	horizonHours       = 24
	parkCount          = 3
	heatParkCount      = 2 // only Park1 (idx 0) and Park2 (idx 1) are in the heat network
	minCreditThreshold = 50
	defaultCreditScore = 80
)

type ADMMCoordinationContract struct {
	contractapi.Contract
}

type dispatchEvent struct {
	ScheduleID    string `json:"scheduleId"`
	Date          string `json:"date"`
	Iter          int    `json:"iter"`
	RhoScaled     int64  `json:"rhoScaled"`
	PrimalResidSq int64  `json:"primalResidSq,omitempty"`
	DualResidSq   int64  `json:"dualResidSq,omitempty"`
	Status        string `json:"status"`
}

// ── PublishNetworkParams ───────────────────────────────────────────────────────
// Called by NetworkObserver before any dispatch.
// eMaxJSON, gMaxJSON, cMaxJSON: JSON arrays of length 3 (one per park, MW*EScale).
// hMaxJSON: JSON array of length 2 (Park1, Park2 only, MW*EScale). Park3 has no heat line.
func (c *ADMMCoordinationContract) PublishNetworkParams(
	ctx contractapi.TransactionContextInterface,
	date, eMaxJSON, gMaxJSON, hMaxJSON, cMaxJSON string,
) error {
	if err := requireMSP(ctx, "NetworkObserverMSP"); err != nil {
		return err
	}
	eMax, err := parseInt64Array(eMaxJSON, parkCount, "eMax")
	if err != nil {
		return err
	}
	gMax, err := parseInt64Array(gMaxJSON, parkCount, "gMax")
	if err != nil {
		return err
	}
	hMax, err := parseInt64Array(hMaxJSON, heatParkCount, "hMax")
	if err != nil {
		return err
	}
	cMax, err := parseInt64Array(cMaxJSON, parkCount, "cMax")
	if err != nil {
		return err
	}

	params := NetworkParams{
		Date:        date,
		EMaxScaled:  eMax,
		GMaxScaled:  gMax,
		HMaxScaled:  hMax,
		CMaxScaled:  cMax,
		PublishedBy: "NetworkObserverMSP",
		Timestamp:   txTimestamp(ctx),
	}
	if err := putJSON(ctx, networkParamsKey(date), &params); err != nil {
		return err
	}
	return setEvent(ctx, "NetworkParamsPublished", params)
}

// ── TriggerDispatch ───────────────────────────────────────────────────────────
// Initialises a new ADMMState for the given date.
// All four energy types start with zero multipliers and zero coordinator allocations.
func (c *ADMMCoordinationContract) TriggerDispatch(
	ctx contractapi.TransactionContextInterface,
	date string,
	rhoScaled int64,
	epsPriSqScaled int64,
	epsDualSqScaled int64,
	maxIter int,
	carbonPriceScaled int64,
	carbonQuotaJSON string,
) error {
	if rhoScaled <= 0 {
		return fmt.Errorf("rhoScaled must be positive")
	}
	if maxIter <= 0 {
		return fmt.Errorf("maxIter must be positive")
	}

	var params NetworkParams
	if err := getRequiredJSON(ctx, networkParamsKey(date), &params); err != nil {
		return fmt.Errorf("network params for date %s not ready: %w", date, err)
	}
	if len(params.EMaxScaled) != parkCount || len(params.GMaxScaled) != parkCount ||
		len(params.HMaxScaled) != heatParkCount || len(params.CMaxScaled) != parkCount {
		return fmt.Errorf("network params capacity arrays have wrong length")
	}

	for _, parkID := range parkIDs() {
		score, err := getCreditScore(ctx, parkID)
		if err != nil {
			return err
		}
		if score.Score < minCreditThreshold {
			return fmt.Errorf("%s credit score %d below threshold %d", parkID, score.Score, minCreditThreshold)
		}
	}

	active, err := ctx.GetStub().GetState(dateActiveKey(date))
	if err != nil {
		return err
	}
	if active != nil {
		return fmt.Errorf("date %s already has active schedule %s", date, string(active))
	}

	carbonQuota, err := parseInt64Array(carbonQuotaJSON, parkCount, "carbonQuota")
	if err != nil {
		return err
	}

	scheduleID := scheduleIDForDate(date)
	state := ADMMState{
		ScheduleID: scheduleID,
		Date:       date,
		Iter:       0,
		RhoScaled:  rhoScaled,

		// All multipliers and coordinator allocations initialised to zero.
		LambdaEScaled: makeMatrix(parkCount, horizonHours),
		LambdaGScaled: makeMatrix(parkCount, horizonHours),
		LambdaHScaled: makeMatrix(heatParkCount, horizonHours), // Park1, Park2 only
		LambdaCScaled: makeMatrix(parkCount, horizonHours),
		ZEScaled:      makeMatrix(parkCount, horizonHours),
		ZGScaled:      makeMatrix(parkCount, horizonHours),
		ZHScaled:      makeMatrix(heatParkCount, horizonHours),
		ZCScaled:      makeMatrix(parkCount, horizonHours),

		PrimalResidSq:   0,
		DualResidSq:     0,
		EpsPriSqScaled:  epsPriSqScaled,
		EpsDualSqScaled: epsDualSqScaled,

		MaxIter:           maxIter,
		BestIter:          -1,
		BestResidSq:       math.MaxInt64,
		BestZEScaled:      makeMatrix(parkCount, horizonHours),
		BestZGScaled:      makeMatrix(parkCount, horizonHours),
		BestZHScaled:      makeMatrix(heatParkCount, horizonHours),
		BestZCScaled:      makeMatrix(parkCount, horizonHours),
		BestLambdaEScaled: makeMatrix(parkCount, horizonHours),
		BestLambdaGScaled: makeMatrix(parkCount, horizonHours),
		BestLambdaHScaled: makeMatrix(heatParkCount, horizonHours),
		BestLambdaCScaled: makeMatrix(parkCount, horizonHours),

		Status:            StatusDispatching,
		CarbonPriceScaled: carbonPriceScaled,
		CarbonQuotaScaled: carbonQuota,

		EMaxScaled: append([]int64(nil), params.EMaxScaled...),
		GMaxScaled: append([]int64(nil), params.GMaxScaled...),
		HMaxScaled: append([]int64(nil), params.HMaxScaled...),
		CMaxScaled: append([]int64(nil), params.CMaxScaled...),
	}
	if err := putJSON(ctx, admmStateKey(scheduleID), &state); err != nil {
		return err
	}
	if err := ctx.GetStub().PutState(dateActiveKey(date), []byte(scheduleID)); err != nil {
		return err
	}

	return setEvent(ctx, "DispatchInitialized", dispatchEvent{
		ScheduleID: scheduleID,
		Date:       date,
		Iter:       0,
		RhoScaled:  rhoScaled,
		Status:     StatusDispatching,
	})
}

// ── SubmitIterationResult ─────────────────────────────────────────────────────
// Each park submits its local subproblem solution for one ADMM iteration.
// eExJSON, gExJSON, hExJSON, cExJSON: JSON arrays of length 24 (MW*EScale).
//
// Validation rules:
//   - Park3 must submit hExJSON = all zeros (not connected to heat network).
//   - Each energy type exchange must not exceed the published capacity limit.
func (c *ADMMCoordinationContract) SubmitIterationResult(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
	parkID string,
	eExJSON, gExJSON, hExJSON, cExJSON string,
	localCostScaled int64,
	localCarbonScaled int64,
) error {
	if err := requireParkMSP(ctx, parkID); err != nil {
		return err
	}

	state, err := readADMMState(ctx, scheduleID)
	if err != nil {
		return err
	}
	if state.Status != StatusDispatching {
		return fmt.Errorf("schedule %s status is %s, not %s", scheduleID, state.Status, StatusDispatching)
	}
	if iter != state.Iter {
		_ = recordAnomaly(ctx, parkID, scheduleID, "ITER_MISMATCH",
			fmt.Sprintf("submit iter %d, expected %d", iter, state.Iter))
		_ = updateCreditScore(ctx, parkID, -5)
		return fmt.Errorf("iteration mismatch: got %d want %d", iter, state.Iter)
	}

	key := iterationKey(scheduleID, iter, parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return err
	}
	if exists {
		_ = recordAnomaly(ctx, parkID, scheduleID, "DUPLICATE_SUBMIT", key)
		_ = updateCreditScore(ctx, parkID, -5)
		return fmt.Errorf("iteration result already exists for %s iter %d", parkID, iter)
	}

	// Parse all four energy exchange arrays.
	eEx, err := parseInt64Array(eExJSON, horizonHours, "eEx")
	if err != nil {
		return err
	}
	gEx, err := parseInt64Array(gExJSON, horizonHours, "gEx")
	if err != nil {
		return err
	}
	hEx, err := parseInt64Array(hExJSON, horizonHours, "hEx")
	if err != nil {
		return err
	}
	cEx, err := parseInt64Array(cExJSON, horizonHours, "cEx")
	if err != nil {
		return err
	}

	if err := validateExchangeBounds(ctx, state, scheduleID, parkID, eEx, gEx, hEx, cEx); err != nil {
		return err
	}

	record := IterationRecord{
		ScheduleID:        scheduleID,
		Iter:              iter,
		ParkID:            parkID,
		EExScaled:         eEx,
		GExScaled:         gEx,
		HExScaled:         hEx,
		CExScaled:         cEx,
		LocalCostScaled:   localCostScaled,
		LocalCarbonScaled: localCarbonScaled,
		Timestamp:         txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &record); err != nil {
		return err
	}
	return setEvent(ctx, "SubmitConfirmed", map[string]any{
		"scheduleId": scheduleID,
		"iter":       iter,
		"parkId":     parkID,
	})
}

// ── CoordinateStep ────────────────────────────────────────────────────────────
// After all three parks have submitted, the coordinator runs one ADMM step:
//   1. z-update  (E/G/C: 3-park mean projection; H: 2-park binary projection)
//   2. λ-update  (gradient ascent on dual variables)
//   3. Combined primal/dual residual across all 4 energy types
//   4. Adaptive ρ
//   5. Convergence check or iteration advance
func (c *ADMMCoordinationContract) CoordinateStep(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
) error {
	state, err := readADMMState(ctx, scheduleID)
	if err != nil {
		return err
	}
	if state.Status != StatusDispatching {
		return fmt.Errorf("schedule %s status is %s, not %s", scheduleID, state.Status, StatusDispatching)
	}
	if iter != state.Iter {
		return fmt.Errorf("coordinate iter mismatch: got %d want %d", iter, state.Iter)
	}

	// Read all three parks' submissions for this iteration.
	records, err := readIterationRecords(ctx, scheduleID, iter)
	if err != nil {
		return err
	}

	// Assemble exchange matrices.
	// E, G, C: shape [3][24].  H: shape [2][24] (Park1=0, Park2=1).
	eE := makeMatrix(parkCount, horizonHours)
	eG := makeMatrix(parkCount, horizonHours)
	eH := makeMatrix(heatParkCount, horizonHours) // only Park1 and Park2
	eC := makeMatrix(parkCount, horizonHours)
	for m, rec := range records {
		copy(eE[m], rec.EExScaled)
		copy(eG[m], rec.GExScaled)
		copy(eC[m], rec.CExScaled)
		if m < heatParkCount { // Park1 (m=0), Park2 (m=1) — skip Park3
			copy(eH[m], rec.HExScaled)
		}
		// Park3 HExScaled is guaranteed zero by SubmitIterationResult validation.
	}

	// Snapshot old z for dual residual.
	zEOld := cloneMatrix(state.ZEScaled)
	zGOld := cloneMatrix(state.ZGScaled)
	zHOld := cloneMatrix(state.ZHScaled)
	zCOld := cloneMatrix(state.ZCScaled)

	// ── z-update ──────────────────────────────────────────────────────────────
	zENew, err := computeZUpdate3Park(eE, state.LambdaEScaled, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("z-update E: %w", err)
	}
	zGNew, err := computeZUpdate3Park(eG, state.LambdaGScaled, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("z-update G: %w", err)
	}
	// Heat: binary projection onto z_H1 + z_H2 = 0
	zHNew, err := computeZUpdateHeat(eH, state.LambdaHScaled, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("z-update H: %w", err)
	}
	zCNew, err := computeZUpdate3Park(eC, state.LambdaCScaled, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("z-update C: %w", err)
	}

	// ── λ-update ──────────────────────────────────────────────────────────────
	lambdaENew, err := computeLambdaUpdate(state.LambdaEScaled, eE, zENew, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("lambda-update E: %w", err)
	}
	lambdaGNew, err := computeLambdaUpdate(state.LambdaGScaled, eG, zGNew, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("lambda-update G: %w", err)
	}
	// Heat multipliers: only Park1 and Park2. λ^H_3 stays zero (not stored).
	lambdaHNew, err := computeLambdaUpdateHeat(state.LambdaHScaled, eH, zHNew, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("lambda-update H: %w", err)
	}
	lambdaCNew, err := computeLambdaUpdate(state.LambdaCScaled, eC, zCNew, state.RhoScaled)
	if err != nil {
		return fmt.Errorf("lambda-update C: %w", err)
	}

	// ── Combined residuals ────────────────────────────────────────────────────
	primalSq, err := computePrimalResidSq(
		eE, zENew,
		eG, zGNew,
		eH, zHNew,
		eC, zCNew,
	)
	if err != nil {
		return fmt.Errorf("primal residual: %w", err)
	}
	dualSq, err := computeDualResidSq(
		zENew, zEOld,
		zGNew, zGOld,
		zHNew, zHOld,
		zCNew, zCOld,
		state.RhoScaled,
	)
	if err != nil {
		return fmt.Errorf("dual residual: %w", err)
	}

	rhoNew := adaptRho(state.RhoScaled, primalSq, dualSq)

	// ── Update state ──────────────────────────────────────────────────────────
	state.ZEScaled = zENew
	state.ZGScaled = zGNew
	state.ZHScaled = zHNew
	state.ZCScaled = zCNew
	state.LambdaEScaled = lambdaENew
	state.LambdaGScaled = lambdaGNew
	state.LambdaHScaled = lambdaHNew
	state.LambdaCScaled = lambdaCNew
	state.PrimalResidSq = primalSq
	state.DualResidSq = dualSq
	state.RhoScaled = rhoNew

	// Track best-so-far snapshot.
	combinedResid, err := checkedAdd(primalSq, dualSq)
	if err != nil {
		combinedResid = math.MaxInt64
	}
	if combinedResid < state.BestResidSq {
		state.BestResidSq = combinedResid
		state.BestIter = iter
		state.BestZEScaled = cloneMatrix(zENew)
		state.BestZGScaled = cloneMatrix(zGNew)
		state.BestZHScaled = cloneMatrix(zHNew)
		state.BestZCScaled = cloneMatrix(zCNew)
		state.BestLambdaEScaled = cloneMatrix(lambdaENew)
		state.BestLambdaGScaled = cloneMatrix(lambdaGNew)
		state.BestLambdaHScaled = cloneMatrix(lambdaHNew)
		state.BestLambdaCScaled = cloneMatrix(lambdaCNew)
	}

	// ── Convergence / termination ─────────────────────────────────────────────
	switch {
	case hasConverged(primalSq, dualSq, state.EpsPriSqScaled, state.EpsDualSqScaled):
		state.Converged = true
		state.Status = StatusConverged
		if err := finalizeSchedule(ctx, state, iter, false); err != nil {
			return err
		}
		if err := putJSON(ctx, admmStateKey(scheduleID), &state); err != nil {
			return err
		}
		return setEvent(ctx, "ConvergenceAchieved", dispatchEvent{
			ScheduleID:    scheduleID,
			Date:          state.Date,
			Iter:          iter,
			RhoScaled:     rhoNew,
			PrimalResidSq: primalSq,
			DualResidSq:   dualSq,
			Status:        state.Status,
		})

	case iter+1 >= state.MaxIter:
		state.IsFallback = true
		state.Status = StatusFailed
		if err := handleFallback(ctx, state); err != nil {
			return err
		}
		_ = recordAnomaly(ctx, "", scheduleID, "CONVERGENCE_FAIL",
			fmt.Sprintf("maxIter %d reached", state.MaxIter))
		_ = updateCreditScore(ctx, "", -3)
		if err := putJSON(ctx, admmStateKey(scheduleID), &state); err != nil {
			return err
		}
		return setEvent(ctx, "ConvergenceFailed", dispatchEvent{
			ScheduleID:    scheduleID,
			Date:          state.Date,
			Iter:          iter,
			RhoScaled:     rhoNew,
			PrimalResidSq: primalSq,
			DualResidSq:   dualSq,
			Status:        state.Status,
		})

	default:
		state.Iter++
		if err := putJSON(ctx, admmStateKey(scheduleID), &state); err != nil {
			return err
		}
		return setEvent(ctx, "IterationComplete", dispatchEvent{
			ScheduleID:    scheduleID,
			Date:          state.Date,
			Iter:          state.Iter,
			RhoScaled:     rhoNew,
			PrimalResidSq: primalSq,
			DualResidSq:   dualSq,
			Status:        state.Status,
		})
	}
}

// ── Query functions ───────────────────────────────────────────────────────────

func (c *ADMMCoordinationContract) QueryADMMState(ctx contractapi.TransactionContextInterface, scheduleID string) (*ADMMState, error) {
	return readADMMState(ctx, scheduleID)
}

func (c *ADMMCoordinationContract) QueryIterationRecord(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*IterationRecord, error) {
	var record IterationRecord
	if err := getRequiredJSON(ctx, iterationKey(scheduleID, iter, parkID), &record); err != nil {
		return nil, err
	}
	return &record, nil
}

func (c *ADMMCoordinationContract) QueryFinalSchedule(ctx contractapi.TransactionContextInterface, scheduleID string) (*FinalSchedule, error) {
	var schedule FinalSchedule
	if err := getRequiredJSON(ctx, finalScheduleKey(scheduleID), &schedule); err != nil {
		return nil, err
	}
	return &schedule, nil
}

// ── Internal helpers ──────────────────────────────────────────────────────────

func finalizeSchedule(ctx contractapi.TransactionContextInterface, state *ADMMState, iter int, fallback bool) error {
	records, err := readIterationRecords(ctx, state.ScheduleID, iter)
	if err != nil {
		return err
	}

	// Build final exchange matrices from the last submitted iteration.
	// E/G/C: [3][24]; H: [3][24] with Park3 row = zeros (for uniform indexing in settlement).
	finalEE := makeMatrix(parkCount, horizonHours)
	finalEG := makeMatrix(parkCount, horizonHours)
	finalEH := makeMatrix(parkCount, horizonHours) // Park3 row stays zero
	finalEC := makeMatrix(parkCount, horizonHours)
	finalCarbon := make([]int64, parkCount)
	var totalCost, totalCarbon int64

	for m, rec := range records {
		copy(finalEE[m], rec.EExScaled)
		copy(finalEG[m], rec.GExScaled)
		copy(finalEH[m], rec.HExScaled) // Park3 row is zeros (validated at submit)
		copy(finalEC[m], rec.CExScaled)
		finalCarbon[m] = rec.LocalCarbonScaled
		totalCost, err = checkedAdd(totalCost, rec.LocalCostScaled)
		if err != nil {
			return err
		}
		totalCarbon, err = checkedAdd(totalCarbon, rec.LocalCarbonScaled)
		if err != nil {
			return err
		}
	}

	// Choose z and lambda: converged state or best fallback snapshot.
	zE := state.ZEScaled
	zG := state.ZGScaled
	zH := state.ZHScaled // [2][24]
	zC := state.ZCScaled
	lambdaE := state.LambdaEScaled
	lambdaG := state.LambdaGScaled
	lambdaH := state.LambdaHScaled // [2][24]
	lambdaC := state.LambdaCScaled
	if fallback && state.BestIter >= 0 {
		zE = state.BestZEScaled
		zG = state.BestZGScaled
		zH = state.BestZHScaled
		zC = state.BestZCScaled
		lambdaE = state.BestLambdaEScaled
		lambdaG = state.BestLambdaGScaled
		lambdaH = state.BestLambdaHScaled
		lambdaC = state.BestLambdaCScaled
	}

	// Clearing (average) shadow prices per energy type per hour.
	// E/G/C: mean over 3 parks.  H: mean over Park1 and Park2 only.
	clearE := make([]int64, horizonHours)
	clearG := make([]int64, horizonHours)
	clearH := make([]int64, horizonHours)
	clearC := make([]int64, horizonHours)
	for t := 0; t < horizonHours; t++ {
		var sumE, sumG, sumC int64
		for m := 0; m < parkCount; m++ {
			sumE, err = checkedAdd(sumE, lambdaE[m][t])
			if err != nil {
				return err
			}
			sumG, err = checkedAdd(sumG, lambdaG[m][t])
			if err != nil {
				return err
			}
			sumC, err = checkedAdd(sumC, lambdaC[m][t])
			if err != nil {
				return err
			}
		}
		clearE[t] = divRound(sumE, int64(parkCount))
		clearG[t] = divRound(sumG, int64(parkCount))
		clearC[t] = divRound(sumC, int64(parkCount))

		// Heat: only Park1 (idx 0) and Park2 (idx 1).
		sumH, err := checkedAdd(lambdaH[0][t], lambdaH[1][t])
		if err != nil {
			return err
		}
		clearH[t] = divRound(sumH, int64(heatParkCount))
	}

	schedule := FinalSchedule{
		ScheduleID:    state.ScheduleID,
		Date:          state.Date,
		ConvergedIter: iter,
		IsFallback:    fallback,

		TotalCostScaled:   totalCost,
		TotalCarbonScaled: totalCarbon,

		FinalZEScaled: cloneMatrix(zE),
		FinalZGScaled: cloneMatrix(zG),
		FinalZHScaled: cloneMatrix(zH),
		FinalZCScaled: cloneMatrix(zC),

		FinalEEScaled: finalEE,
		FinalEGScaled: finalEG,
		FinalEHScaled: finalEH,
		FinalECScaled: finalEC,

		FinalLambdaEScaled: cloneMatrix(lambdaE),
		FinalLambdaGScaled: cloneMatrix(lambdaG),
		FinalLambdaHScaled: cloneMatrix(lambdaH),
		FinalLambdaCScaled: cloneMatrix(lambdaC),

		ClearingLambdaEScaled: clearE,
		ClearingLambdaGScaled: clearG,
		ClearingLambdaHScaled: clearH,
		ClearingLambdaCScaled: clearC,

		FinalCarbonScaled: finalCarbon,
		Timestamp:         txTimestamp(ctx),
	}
	schedule.ScheduleHash = hashStruct(schedule)
	return putJSON(ctx, finalScheduleKey(state.ScheduleID), &schedule)
}

func handleFallback(ctx contractapi.TransactionContextInterface, state *ADMMState) error {
	if state.BestIter < 0 {
		return fmt.Errorf("fallback requested but no best iteration recorded")
	}
	return finalizeSchedule(ctx, state, state.BestIter, true)
}

// validateExchangeBounds runs the topology and capacity checks against a park's
// four-energy exchange vectors. Shared by SubmitIterationResult (park self-reports
// e_m directly) and the masked-submission path in mask_contract.go (e_m is
// mechanically recovered on-chain from E_rows*y*+x0_e instead of self-reported).
// Both paths must apply the exact same checks against the exchange vector they
// end up with — the verification step does not change, only where e_m came from.
func validateExchangeBounds(
	ctx contractapi.TransactionContextInterface,
	state *ADMMState,
	scheduleID string,
	parkID string,
	eEx, gEx, hEx, cEx []int64,
) error {
	idx, err := parkIndex(parkID)
	if err != nil {
		return err
	}

	// Park3 is not connected to the heat network: eH must be zero for every hour.
	if parkID == Park3ID {
		for t, v := range hEx {
			if v != 0 {
				_ = recordAnomaly(ctx, parkID, scheduleID, "TOPOLOGY_VIOLATION",
					fmt.Sprintf("Park3 hEx[%d]=%d must be 0 (not in heat network)", t, v))
				_ = updateCreditScore(ctx, parkID, -10)
				return fmt.Errorf("Park3 is not connected to heat network: hEx[%d]=%d must be 0", t, v)
			}
		}
	}

	// Capacity bound checks for each energy type.
	type energyCheck struct {
		values []int64
		cap    int64
		name   string
	}

	// Heat capacity index: Park1→0, Park2→1. Park3 is already validated as zero above.
	hCapIdx := idx
	if idx >= heatParkCount {
		hCapIdx = 0 // Park3: cap is 0 but hEx already verified to be 0
	}

	checks := []energyCheck{
		{eEx, state.EMaxScaled[idx], "eEx"},
		{gEx, state.GMaxScaled[idx], "gEx"},
		{hEx, state.HMaxScaled[hCapIdx], "hEx"},
		{cEx, state.CMaxScaled[idx], "cEx"},
	}
	for _, chk := range checks {
		for t, v := range chk.values {
			if absInt64(v) > chk.cap {
				_ = recordAnomaly(ctx, parkID, scheduleID, "BOUNDS_VIOLATION",
					fmt.Sprintf("hour %d %s=%d exceeds cap=%d", t+1, chk.name, v, chk.cap))
				_ = updateCreditScore(ctx, parkID, -10)
				return fmt.Errorf("%s hour %d %s=%d exceeds cap=%d", parkID, t+1, chk.name, v, chk.cap)
			}
		}
	}
	return nil
}

func readADMMState(ctx contractapi.TransactionContextInterface, scheduleID string) (*ADMMState, error) {
	var state ADMMState
	if err := getRequiredJSON(ctx, admmStateKey(scheduleID), &state); err != nil {
		return nil, err
	}
	return &state, nil
}

func readIterationRecords(ctx contractapi.TransactionContextInterface, scheduleID string, iter int) ([]IterationRecord, error) {
	records := make([]IterationRecord, parkCount)
	for i, parkID := range parkIDs() {
		if err := getRequiredJSON(ctx, iterationKey(scheduleID, iter, parkID), &records[i]); err != nil {
			return nil, fmt.Errorf("missing iteration record for %s iter %d: %w", parkID, iter, err)
		}
		if len(records[i].EExScaled) != horizonHours {
			return nil, fmt.Errorf("%s EExScaled length %d, want %d", parkID, len(records[i].EExScaled), horizonHours)
		}
	}
	return records, nil
}

func parseInt64Array(raw string, expectedLen int, name string) ([]int64, error) {
	var values []int64
	if err := json.Unmarshal([]byte(raw), &values); err != nil {
		return nil, fmt.Errorf("invalid %s JSON: %w", name, err)
	}
	if len(values) != expectedLen {
		return nil, fmt.Errorf("%s length %d, want %d", name, len(values), expectedLen)
	}
	return values, nil
}

func requireMSP(ctx contractapi.TransactionContextInterface, expected string) error {
	mspID, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return err
	}
	if mspID != expected {
		return fmt.Errorf("MSP %s is not allowed, expected %s", mspID, expected)
	}
	return nil
}

func requireParkMSP(ctx contractapi.TransactionContextInterface, parkID string) error {
	expected, err := parkMSPID(parkID)
	if err != nil {
		return err
	}
	return requireMSP(ctx, expected)
}

func parkMSPID(parkID string) (string, error) {
	switch parkID {
	case Park1ID:
		return "Park1MSP", nil
	case Park2ID:
		return "Park2MSP", nil
	case Park3ID:
		return "Park3MSP", nil
	default:
		return "", fmt.Errorf("unknown parkId %s", parkID)
	}
}

func parkIndex(parkID string) (int, error) {
	switch parkID {
	case Park1ID:
		return 0, nil
	case Park2ID:
		return 1, nil
	case Park3ID:
		return 2, nil
	default:
		return -1, fmt.Errorf("unknown parkId %s", parkID)
	}
}

func parkIDs() []string {
	return []string{Park1ID, Park2ID, Park3ID}
}

func scheduleIDForDate(date string) string { return "SCHEDULE_" + date }
func admmStateKey(scheduleID string) string { return "ADMM_STATE_" + scheduleID }
func networkParamsKey(date string) string   { return "NETWORK_PARAMS_" + date }
func dateActiveKey(date string) string      { return "DATE_ACTIVE_" + date }
func iterationKey(scheduleID string, iter int, parkID string) string {
	return "ITER_" + scheduleID + "_" + strconv.Itoa(iter) + "_" + parkID
}
func finalScheduleKey(scheduleID string) string { return "FINAL_" + scheduleID }

func putJSON(ctx contractapi.TransactionContextInterface, key string, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return ctx.GetStub().PutState(key, payload)
}

func getRequiredJSON(ctx contractapi.TransactionContextInterface, key string, value any) error {
	payload, err := ctx.GetStub().GetState(key)
	if err != nil {
		return err
	}
	if payload == nil {
		return fmt.Errorf("state %s not found", key)
	}
	return json.Unmarshal(payload, value)
}

func stateExists(ctx contractapi.TransactionContextInterface, key string) (bool, error) {
	payload, err := ctx.GetStub().GetState(key)
	if err != nil {
		return false, err
	}
	return payload != nil, nil
}

func setEvent(ctx contractapi.TransactionContextInterface, name string, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return ctx.GetStub().SetEvent(name, payload)
}

func txTimestamp(ctx contractapi.TransactionContextInterface) string {
	ts, err := ctx.GetStub().GetTxTimestamp()
	if err != nil || ts == nil {
		return ""
	}
	return fmt.Sprintf("%d.%09d", ts.Seconds, ts.Nanos)
}

func hashStruct(value any) string {
	payload, _ := json.Marshal(value)
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func cloneMatrix(in [][]int64) [][]int64 {
	out := make([][]int64, len(in))
	for i := range in {
		out[i] = append([]int64(nil), in[i]...)
	}
	return out
}
