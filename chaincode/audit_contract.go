package main

import (
	"fmt"
	"strconv"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

type AuditCreditContract struct {
	contractapi.Contract
}

func (c *AuditCreditContract) QueryCreditScore(ctx contractapi.TransactionContextInterface, parkID string) (*CreditScore, error) {
	score, err := getCreditScore(ctx, parkID)
	if err != nil {
		return nil, err
	}
	return score, nil
}

// QuerySolveCreditScore 是独立于"数据信用分"的第二套账本，专门记录
// 某个园区被分配去当solver时，解出来的结果是否真的通过KKT校验。
// 两套信用分必须分开记录：Park2帮Park1解题解错了，扣的是Park2的求解信用，
// 不能牵连Park1的数据信用——这是M4设计文档里明确要求、之前一直没有真正
// 落地实现的一条（之前的RecordVerdict误把求解失败写进了共享的数据信用分池）。
func (c *AuditCreditContract) QuerySolveCreditScore(ctx contractapi.TransactionContextInterface, parkID string) (*CreditScore, error) {
	score, err := getSolveCreditScore(ctx, parkID)
	if err != nil {
		return nil, err
	}
	return score, nil
}

func (c *AuditCreditContract) QueryAuditTrail(ctx contractapi.TransactionContextInterface, parkID string) ([]AnomalyRecord, error) {
	if _, err := parkIndex(parkID); err != nil {
		return nil, err
	}
	counter, err := anomalyCounter(ctx, parkID)
	if err != nil {
		return nil, err
	}
	records := make([]AnomalyRecord, 0, counter)
	for i := 1; i <= counter; i++ {
		var record AnomalyRecord
		if err := getRequiredJSON(ctx, anomalyKey(parkID, i), &record); err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, nil
}

func recordAnomaly(ctx contractapi.TransactionContextInterface, parkID string, scheduleID string, anomalyType string, detail string) error {
	if parkID != "" {
		if _, err := parkIndex(parkID); err != nil {
			return err
		}
	}
	counterKey := anomalyCounterKey(parkID)
	counter, err := anomalyCounter(ctx, parkID)
	if err != nil {
		return err
	}
	counter++
	record := AnomalyRecord{
		AnomalyID:  strconv.Itoa(counter),
		ScheduleID: scheduleID,
		ParkID:     parkID,
		Type:       anomalyType,
		Detail:     detail,
		Timestamp:  txTimestamp(ctx),
	}
	if err := putJSON(ctx, anomalyKey(parkID, counter), &record); err != nil {
		return err
	}
	return ctx.GetStub().PutState(counterKey, []byte(strconv.Itoa(counter)))
}

func updateCreditScore(ctx contractapi.TransactionContextInterface, parkID string, delta int64) error {
	if parkID == "" {
		for _, id := range parkIDs() {
			if err := updateCreditScore(ctx, id, delta); err != nil {
				return err
			}
		}
		return nil
	}
	score, err := getCreditScore(ctx, parkID)
	if err != nil {
		return err
	}
	score.Score += delta
	if score.Score > 100 {
		score.Score = 100
	}
	if score.Score < 0 {
		score.Score = 0
	}
	if delta > 0 {
		score.NormalStreak++
	} else if delta < 0 {
		score.NormalStreak = 0
	}
	score.LastUpdated = txTimestamp(ctx)
	return putJSON(ctx, creditKey(parkID), score)
}

func getCreditScore(ctx contractapi.TransactionContextInterface, parkID string) (*CreditScore, error) {
	if _, err := parkIndex(parkID); err != nil {
		return nil, err
	}
	key := creditKey(parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return nil, err
	}
	if !exists {
		return &CreditScore{
			ParkID: parkID,
			Score:  defaultCreditScore,
		}, nil
	}
	var score CreditScore
	if err := getRequiredJSON(ctx, key, &score); err != nil {
		return nil, err
	}
	return &score, nil
}

// updateSolveCreditScore / getSolveCreditScore：求解信用分账本，跟数据信用分
// （updateCreditScore/getCreditScore）完全独立，存在不同的key前缀下，互不读写。
func updateSolveCreditScore(ctx contractapi.TransactionContextInterface, parkID string, delta int64) error {
	score, err := getSolveCreditScore(ctx, parkID)
	if err != nil {
		return err
	}
	score.Score += delta
	if score.Score > 100 {
		score.Score = 100
	}
	if score.Score < 0 {
		score.Score = 0
	}
	if delta > 0 {
		score.NormalStreak++
	} else if delta < 0 {
		score.NormalStreak = 0
	}
	score.LastUpdated = txTimestamp(ctx)
	return putJSON(ctx, solveCreditKey(parkID), score)
}

func getSolveCreditScore(ctx contractapi.TransactionContextInterface, parkID string) (*CreditScore, error) {
	if _, err := parkIndex(parkID); err != nil {
		return nil, err
	}
	key := solveCreditKey(parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return nil, err
	}
	if !exists {
		return &CreditScore{
			ParkID: parkID,
			Score:  defaultCreditScore,
		}, nil
	}
	var score CreditScore
	if err := getRequiredJSON(ctx, key, &score); err != nil {
		return nil, err
	}
	return &score, nil
}

func anomalyCounter(ctx contractapi.TransactionContextInterface, parkID string) (int, error) {
	raw, err := ctx.GetStub().GetState(anomalyCounterKey(parkID))
	if err != nil {
		return 0, err
	}
	if raw == nil {
		return 0, nil
	}
	value, err := strconv.Atoi(string(raw))
	if err != nil {
		return 0, fmt.Errorf("invalid anomaly counter for %s: %w", parkID, err)
	}
	return value, nil
}

func creditKey(parkID string) string {
	return "CREDIT_" + parkID
}

func solveCreditKey(parkID string) string {
	return "SOLVE_CREDIT_" + parkID
}

const (
	minSolveCreditThreshold = 50 // 跟minCreditThreshold同量级，低于此分的园区不会被分配求解任务
	solvePassReward         = 2  // 通过KKT校验的小额正向奖励
	solveFailPenalty        = -10
)

func anomalyCounterKey(parkID string) string {
	return "ANOMALY_COUNTER_" + parkID
}

func anomalyKey(parkID string, index int) string {
	return "ANOMALY_" + parkID + "_" + strconv.Itoa(index)
}
