package main

import (
	"fmt"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

type SettlementContract struct {
	contractapi.Contract
}

// SettleAfterConvergence computes per-park settlement for all four energy types.
//
// Energy payment formula (per energy type α, per park m):
//   payment^α_m = Σ_t  z^α_m(t) × clearingLambda^α(t)
//
// Heat special case: Park3 has no heat exchange (z^H_3 = 0), so its heat
// payment is always zero. No special-casing needed — the loop handles it.
//
// Carbon payment:
//   carbonPayment_m = (carbon_m - quota_m) × carbonPrice
func (c *SettlementContract) SettleAfterConvergence(ctx contractapi.TransactionContextInterface, scheduleID string) error {
	state, err := readADMMState(ctx, scheduleID)
	if err != nil {
		return err
	}
	if state.Status != StatusConverged && state.Status != StatusFailed {
		return fmt.Errorf("schedule %s status %s is not ready for settlement", scheduleID, state.Status)
	}

	var final FinalSchedule
	if err := getRequiredJSON(ctx, finalScheduleKey(scheduleID), &final); err != nil {
		return err
	}

	// Validate final schedule shapes.
	if len(final.FinalZEScaled) != parkCount || len(final.FinalZGScaled) != parkCount ||
		len(final.FinalZHScaled) != heatParkCount || len(final.FinalZCScaled) != parkCount {
		return fmt.Errorf("final schedule z-arrays have wrong length")
	}
	if len(final.ClearingLambdaEScaled) != horizonHours ||
		len(final.ClearingLambdaGScaled) != horizonHours ||
		len(final.ClearingLambdaHScaled) != horizonHours ||
		len(final.ClearingLambdaCScaled) != horizonHours {
		return fmt.Errorf("final schedule clearing-lambda arrays have wrong length")
	}

	for m, parkID := range parkIDs() {
		// ── Electric payment ──────────────────────────────────────────────────
		payE, err := computeEnergyPaymentScaled(final.FinalZEScaled[m], final.ClearingLambdaEScaled)
		if err != nil {
			return fmt.Errorf("%s electric payment: %w", parkID, err)
		}

		// ── Gas payment ───────────────────────────────────────────────────────
		payG, err := computeEnergyPaymentScaled(final.FinalZGScaled[m], final.ClearingLambdaGScaled)
		if err != nil {
			return fmt.Errorf("%s gas payment: %w", parkID, err)
		}

		// ── Heat payment ──────────────────────────────────────────────────────
		// FinalZHScaled is [2][24]. Park3 (m=2) has no heat exchange.
		var payH int64
		if m < heatParkCount {
			payH, err = computeEnergyPaymentScaled(final.FinalZHScaled[m], final.ClearingLambdaHScaled)
			if err != nil {
				return fmt.Errorf("%s heat payment: %w", parkID, err)
			}
		}
		// Park3: payH = 0 (not in heat network)

		// ── Cold payment ──────────────────────────────────────────────────────
		payC, err := computeEnergyPaymentScaled(final.FinalZCScaled[m], final.ClearingLambdaCScaled)
		if err != nil {
			return fmt.Errorf("%s cold payment: %w", parkID, err)
		}

		// ── Total energy payment ──────────────────────────────────────────────
		totalEnergy := int64(0)
		for _, p := range []int64{payE, payG, payH, payC} {
			totalEnergy, err = checkedAdd(totalEnergy, p)
			if err != nil {
				return fmt.Errorf("%s total energy payment overflow: %w", parkID, err)
			}
		}

		// ── Carbon payment ────────────────────────────────────────────────────
		carbonPay, err := computeCarbonPaymentScaled(
			final.FinalCarbonScaled[m],
			state.CarbonQuotaScaled[m],
			state.CarbonPriceScaled,
		)
		if err != nil {
			return fmt.Errorf("%s carbon payment: %w", parkID, err)
		}

		total, err := checkedAdd(totalEnergy, carbonPay)
		if err != nil {
			return fmt.Errorf("%s total payment overflow: %w", parkID, err)
		}

		record := SettlementRecord{
			ScheduleID:               scheduleID,
			ParkID:                   parkID,
			EnergyPaymentEScaled:     payE,
			EnergyPaymentGScaled:     payG,
			EnergyPaymentHScaled:     payH,
			EnergyPaymentCScaled:     payC,
			TotalEnergyPaymentScaled: totalEnergy,
			CarbonPaymentScaled:      carbonPay,
			TotalPaymentScaled:       total,
			Timestamp:                txTimestamp(ctx),
		}
		if err := putJSON(ctx, settlementKey(scheduleID, parkID), &record); err != nil {
			return err
		}
	}

	state.Status = StatusSettled
	if err := updateCreditScore(ctx, "", 2); err != nil {
		return err
	}
	if err := putJSON(ctx, admmStateKey(scheduleID), state); err != nil {
		return err
	}
	return setEvent(ctx, "SettlementComplete", map[string]any{
		"scheduleId": scheduleID,
		"status":     StatusSettled,
	})
}

// SubmitActualMeter records actual measured exchanges for post-settlement audit.
// The Observer submits all four energy type measurements.
func (c *SettlementContract) SubmitActualMeter(
	ctx contractapi.TransactionContextInterface,
	scheduleID, parkID string,
	actualEJSON, actualGJSON, actualHJSON, actualCJSON string,
) error {
	if err := requireMSP(ctx, "NetworkObserverMSP"); err != nil {
		return err
	}
	if _, err := parkIndex(parkID); err != nil {
		return err
	}
	actualE, err := parseInt64Array(actualEJSON, horizonHours, "actualE")
	if err != nil {
		return err
	}
	actualG, err := parseInt64Array(actualGJSON, horizonHours, "actualG")
	if err != nil {
		return err
	}
	actualH, err := parseInt64Array(actualHJSON, horizonHours, "actualH")
	if err != nil {
		return err
	}
	actualC, err := parseInt64Array(actualCJSON, horizonHours, "actualC")
	if err != nil {
		return err
	}

	record := ActualMeterRecord{
		ScheduleID:     scheduleID,
		ParkID:         parkID,
		ActualEEScaled: actualE,
		ActualEGScaled: actualG,
		ActualEHScaled: actualH,
		ActualECScaled: actualC,
		Timestamp:      txTimestamp(ctx),
	}
	if err := putJSON(ctx, actualMeterKey(scheduleID, parkID), &record); err != nil {
		return err
	}
	return setEvent(ctx, "ActualMeterSubmitted", map[string]any{
		"scheduleId": scheduleID,
		"parkId":     parkID,
	})
}

func (c *SettlementContract) QuerySettlement(ctx contractapi.TransactionContextInterface, scheduleID, parkID string) (*SettlementRecord, error) {
	var record SettlementRecord
	if err := getRequiredJSON(ctx, settlementKey(scheduleID, parkID), &record); err != nil {
		return nil, err
	}
	return &record, nil
}

// computeEnergyPaymentScaled computes Σ_t z(t)·λ(t) for one energy type, one park.
// Both inputs are in scaled integer units. Result is in yuan * CostScale.
func computeEnergyPaymentScaled(zScaled []int64, lambdaScaled []int64) (int64, error) {
	if len(zScaled) != horizonHours || len(lambdaScaled) != horizonHours {
		return 0, fmt.Errorf("payment input length mismatch")
	}
	var total int64
	for t := 0; t < horizonHours; t++ {
		product, err := checkedMul(zScaled[t], lambdaScaled[t])
		if err != nil {
			return 0, err
		}
		product, err = checkedMul(product, CostScale)
		if err != nil {
			return 0, err
		}
		term := divRound(product, EScale*LambdaScale)
		total, err = checkedAdd(total, term)
		if err != nil {
			return 0, err
		}
	}
	return total, nil
}

func computeCarbonPaymentScaled(carbonScaled, quotaScaled, carbonPriceScaled int64) (int64, error) {
	diff := carbonScaled - quotaScaled
	product, err := checkedMul(diff, carbonPriceScaled)
	if err != nil {
		return 0, err
	}
	return divRound(product, CarbonScale), nil
}

func settlementKey(scheduleID, parkID string) string {
	return "SETTLEMENT_" + scheduleID + "_" + parkID
}

func actualMeterKey(scheduleID, parkID string) string {
	return "METER_" + scheduleID + "_" + parkID
}
