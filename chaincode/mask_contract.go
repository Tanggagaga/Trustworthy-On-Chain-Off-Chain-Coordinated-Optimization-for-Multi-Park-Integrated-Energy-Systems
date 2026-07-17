package main

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

// MaskCoordinationContract 实现 M4 阶段的mask化信任机制：
//
//	注册 → 分配求解/验证方 → 求解方提交masked解 → 验证方提交KKT裁决
//	→ 通过则链码自己反解e_m并复用现有容量/拓扑校验写入IterationRecord。
//
// KKT校验本身在链下完成（独立verifier，复用M3.5的定点逻辑），链码不做
// 矩阵运算，只做：维度/哈希校验、身份校验、e_m反解（轻量矩阵向量乘）。
type MaskCoordinationContract struct {
	contractapi.Contract
}

// MaskScale 必须跟 M3.5 阶段确定的定点缩放常数完全一致。
const MaskScale int64 = 1_000_000

const eBlockLen = 4 * horizonHours // eE+eG+eH+eC = 96

var maskedUploadParts = []string{"hE0", "gMasked", "hVec", "c0", "eRows", "x0e"}

// ── RegisterMaskedSubproblem ──────────────────────────────────────────────────
// 园区在调度开始时，一次性把本地算好的masked结构注册上链。
// 必须做维度校验和MaskScale校验——这条规则是因为M3阶段Park3那次
// 行数对不上的真实事故，专门加在这一关挡住，而不是等到后面才发现。
func (c *MaskCoordinationContract) RegisterMaskedSubproblem(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	parkID string,
	k0 int,
	qRows int,
	maskScale int64,
	hE0JSON, gMaskedJSON, hVecJSON, c0JSON, eRowsJSON, x0eJSON string,
) error {
	if err := requireParkMSP(ctx, parkID); err != nil {
		return err
	}
	return registerMaskedSubproblemFromJSON(ctx, scheduleID, parkID, k0, qRows, maskScale,
		hE0JSON, gMaskedJSON, hVecJSON, c0JSON, eRowsJSON, x0eJSON)
}

// BeginMaskedSubproblemUpload starts a chunked upload for real-size masked
// matrices. This avoids local peer CLI / OS argument length limits. It does not
// register the subproblem yet; FinalizeMaskedSubproblemUpload performs the same
// parsing, dimension checks, and hash computation as RegisterMaskedSubproblem.
func (c *MaskCoordinationContract) BeginMaskedSubproblemUpload(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	parkID string,
	k0 int,
	qRows int,
	maskScale int64,
) error {
	if err := requireParkMSP(ctx, parkID); err != nil {
		return err
	}
	if maskScale != MaskScale {
		return fmt.Errorf("maskScale %d does not match required %d", maskScale, MaskScale)
	}
	if k0 <= 0 || qRows <= 0 {
		return fmt.Errorf("invalid dimensions k0=%d qRows=%d", k0, qRows)
	}
	if exists, err := stateExists(ctx, maskKey(scheduleID, parkID)); err != nil {
		return err
	} else if exists {
		return fmt.Errorf("masked subproblem already registered for %s schedule %s", parkID, scheduleID)
	}
	key := uploadMetaKey(scheduleID, parkID)
	if exists, err := stateExists(ctx, key); err != nil {
		return err
	} else if exists {
		return fmt.Errorf("masked upload already started for %s schedule %s", parkID, scheduleID)
	}
	meta := MaskedUploadMeta{
		ScheduleID: scheduleID,
		ParkID:     parkID,
		K0:         k0,
		QRows:      qRows,
		MaskScale:  maskScale,
		StartedBy:  parkID,
		Timestamp:  txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &meta); err != nil {
		return err
	}
	return setEvent(ctx, "MaskUploadStarted", map[string]any{
		"scheduleId": scheduleID, "parkId": parkID, "k0": k0, "qRows": qRows,
	})
}

// UploadMaskedSubproblemChunk uploads one chunk of one JSON field.
func (c *MaskCoordinationContract) UploadMaskedSubproblemChunk(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	parkID string,
	partName string,
	chunkIndex int,
	totalChunks int,
	data string,
) error {
	if err := requireParkMSP(ctx, parkID); err != nil {
		return err
	}
	if _, err := readUploadMeta(ctx, scheduleID, parkID); err != nil {
		return err
	}
	if !isMaskedUploadPart(partName) {
		return fmt.Errorf("invalid upload part %s", partName)
	}
	if chunkIndex < 0 || totalChunks <= 0 || chunkIndex >= totalChunks {
		return fmt.Errorf("invalid chunk index %d/%d", chunkIndex, totalChunks)
	}
	if len(data) == 0 {
		return fmt.Errorf("empty chunk data")
	}
	key := uploadChunkKey(scheduleID, parkID, partName, chunkIndex)
	if exists, err := stateExists(ctx, key); err != nil {
		return err
	} else if exists {
		return fmt.Errorf("duplicate chunk %s[%d]", partName, chunkIndex)
	}
	chunk := MaskedUploadChunk{
		ScheduleID:  scheduleID,
		ParkID:      parkID,
		PartName:    partName,
		ChunkIndex:  chunkIndex,
		TotalChunks: totalChunks,
		Data:        data,
		Timestamp:   txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &chunk); err != nil {
		return err
	}
	return setEvent(ctx, "MaskUploadChunkStored", map[string]any{
		"scheduleId": scheduleID, "parkId": parkID, "partName": partName,
		"chunkIndex": chunkIndex, "totalChunks": totalChunks,
	})
}

// FinalizeMaskedSubproblemUpload assembles all chunks and registers the exact
// same MaskedSubproblem object as the direct RegisterMaskedSubproblem path.
func (c *MaskCoordinationContract) FinalizeMaskedSubproblemUpload(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	parkID string,
) error {
	if err := requireParkMSP(ctx, parkID); err != nil {
		return err
	}
	meta, err := readUploadMeta(ctx, scheduleID, parkID)
	if err != nil {
		return err
	}
	assembled := make(map[string]string)
	for _, part := range maskedUploadParts {
		value, err := assembleUploadPart(ctx, scheduleID, parkID, part)
		if err != nil {
			return err
		}
		assembled[part] = value
	}
	if err := registerMaskedSubproblemFromJSON(ctx, scheduleID, parkID, meta.K0, meta.QRows, meta.MaskScale,
		assembled["hE0"], assembled["gMasked"], assembled["hVec"],
		assembled["c0"], assembled["eRows"], assembled["x0e"]); err != nil {
		return err
	}
	_ = ctx.GetStub().DelState(uploadMetaKey(scheduleID, parkID))
	for _, part := range maskedUploadParts {
		if err := deleteUploadPart(ctx, scheduleID, parkID, part); err != nil {
			return err
		}
	}
	return setEvent(ctx, "MaskUploadFinalized", map[string]any{
		"scheduleId": scheduleID, "parkId": parkID,
	})
}

func registerMaskedSubproblemFromJSON(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	parkID string,
	k0 int,
	qRows int,
	maskScale int64,
	hE0JSON, gMaskedJSON, hVecJSON, c0JSON, eRowsJSON, x0eJSON string,
) error {
	if maskScale != MaskScale {
		return fmt.Errorf("maskScale %d does not match required %d", maskScale, MaskScale)
	}
	if k0 <= 0 || qRows <= 0 {
		return fmt.Errorf("invalid dimensions k0=%d qRows=%d", k0, qRows)
	}

	key := maskKey(scheduleID, parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return err
	}
	if exists {
		return fmt.Errorf("masked subproblem already registered for %s schedule %s", parkID, scheduleID)
	}

	hE0, err := parseInt64Matrix(hE0JSON, k0, k0, "H_e0")
	if err != nil {
		return err
	}
	gMasked, err := parseInt64Matrix(gMaskedJSON, qRows, k0, "G_masked")
	if err != nil {
		return err
	}
	hVec, err := parseInt64Array(hVecJSON, qRows, "h_masked")
	if err != nil {
		return err
	}
	c0, err := parseInt64Array(c0JSON, k0, "C0")
	if err != nil {
		return err
	}
	eRows, err := parseInt64Matrix(eRowsJSON, eBlockLen, k0, "E_rows")
	if err != nil {
		return err
	}
	x0e, err := parseInt64Array(x0eJSON, eBlockLen, "x0_e")
	if err != nil {
		return err
	}

	mask := MaskedSubproblem{
		ScheduleID:    scheduleID,
		ParkID:        parkID,
		K0:            k0,
		QRows:         qRows,
		HE0Scaled:     hE0,
		GMaskedScaled: gMasked,
		HVecScaled:    hVec,
		C0Scaled:      c0,
		ERowsScaled:   eRows,
		X0EScaled:     x0e,
		MaskScale:     maskScale,
		RegisteredBy:  parkID,
		Timestamp:     txTimestamp(ctx),
	}
	mask.MaskHash = hashStruct(struct {
		HE0   [][]int64
		G     [][]int64
		H     []int64
		C0    []int64
		ERows [][]int64
		X0E   []int64
	}{hE0, gMasked, hVec, c0, eRows, x0e})

	if err := putJSON(ctx, key, &mask); err != nil {
		return err
	}
	return setEvent(ctx, "MaskRegistered", map[string]any{
		"scheduleId": scheduleID,
		"parkId":     parkID,
		"maskHash":   mask.MaskHash,
	})
}

// ── AssignSolverAndVerifier ────────────────────────────────────────────────────
// 用 hash(scheduleID+iter+parkID+role) 做确定性分配，不依赖区块哈希
// （Fabric链码执行发生在交易排序进区块之前，拿不到"当前区块"的哈希，
//
//	这里只需要"不固定配对"，不需要抗强对抗的密码学随机性，
//	确定性哈希完全够用，任何人都能用同样的输入重算一遍核实分配没被操纵）。
//
// solver、verifier、parkID（数据方）三者强制两两不同：候选池是另外两个园区，
// solver从其中按哈希挑一个，verifier是剩下那一个——在只有3个园区的网络规模下，
// 一旦solver选定，verifier就没有别的选择了，这是诚实记录这个局限，
// 不是回避它（候选池更大时这一步会有更多随机性）。
func (c *MaskCoordinationContract) AssignSolverAndVerifier(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
	parkID string,
) (*SolverAssignment, error) {
	key := assignmentKey(scheduleID, iter, parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return nil, err
	}
	if exists {
		var existing SolverAssignment
		if err := getRequiredJSON(ctx, key, &existing); err != nil {
			return nil, err
		}
		return &existing, nil
	}

	others, err := otherTwoParks(parkID)
	if err != nil {
		return nil, err
	}

	// 候选池按"求解信用分"过滤：低于阈值的园区不参与本轮solver候选
	// （跟TriggerDispatch对"数据信用分"的现有门槛做法对称，只是换成
	// 求解信用分这本独立账本，互不影响）。极端情况下两个候选都低于
	// 阈值时退回到完整候选池，保证流程不会卡死，但记一笔异常。
	eligible := make([]string, 0, len(others))
	for _, p := range others {
		s, err := getSolveCreditScore(ctx, p)
		if err != nil {
			return nil, err
		}
		if s.Score >= minSolveCreditThreshold {
			eligible = append(eligible, p)
		}
	}
	if len(eligible) == 0 {
		_ = recordAnomaly(ctx, parkID, scheduleID, "SOLVER_POOL_DEGRADED",
			fmt.Sprintf("iter %d: all candidate solvers below solve-credit threshold %d, falling back to full pool",
				iter, minSolveCreditThreshold))
		eligible = others
	}

	seed := hashStruct(scheduleID + "_" + strconv.Itoa(iter) + "_" + parkID + "_solver")
	idx := hashToIndex(seed, len(eligible))
	solverID := eligible[idx]

	var verifierID string
	for _, p := range others {
		if p != solverID {
			verifierID = p
			break
		}
	}

	assignment := SolverAssignment{
		ScheduleID:     scheduleID,
		Iter:           iter,
		ParkID:         parkID,
		SolverParkID:   solverID,
		VerifierParkID: verifierID,
		Timestamp:      txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &assignment); err != nil {
		return nil, err
	}
	_ = setEvent(ctx, "SolverAssigned", map[string]any{
		"scheduleId": scheduleID, "iter": iter, "parkId": parkID,
		"solverParkId": solverID, "verifierParkId": verifierID,
	})
	return &assignment, nil
}

// ── SubmitMaskedSolution ───────────────────────────────────────────────────────
// 必须是被分配的solver（不是数据方自己）才能提交。
func (c *MaskCoordinationContract) SubmitMaskedSolution(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
	parkID string,
	yJSON, muJSON string,
) error {
	assignment, err := readAssignment(ctx, scheduleID, iter, parkID)
	if err != nil {
		return err
	}
	if err := requireParkMSP(ctx, assignment.SolverParkID); err != nil {
		return fmt.Errorf("caller is not the assigned solver %s: %w", assignment.SolverParkID, err)
	}

	mask, err := readMask(ctx, scheduleID, parkID)
	if err != nil {
		return err
	}

	y, err := parseInt64Array(yJSON, mask.K0, "y")
	if err != nil {
		return err
	}
	mu, err := parseInt64Array(muJSON, mask.QRows, "mu")
	if err != nil {
		return err
	}

	key := solutionKey(scheduleID, iter, parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return err
	}
	if exists {
		return fmt.Errorf("masked solution already submitted for %s iter %d", parkID, iter)
	}

	solutionHash := hashStruct(struct {
		ScheduleID string
		Iter       int
		ParkID     string
		Y          []int64
		Mu         []int64
	}{scheduleID, iter, parkID, y, mu})

	solution := MaskedSolution{
		ScheduleID:   scheduleID,
		Iter:         iter,
		ParkID:       parkID,
		SolverParkID: assignment.SolverParkID,
		YScaled:      y,
		MuScaled:     mu,
		SolutionHash: solutionHash,
		Timestamp:    txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &solution); err != nil {
		return err
	}
	return setEvent(ctx, "MaskedSolutionSubmitted", map[string]any{
		"scheduleId": scheduleID, "iter": iter, "parkId": parkID,
		"solverParkId": assignment.SolverParkID, "solutionHash": solutionHash,
	})
}

// ── RecordVerdict ──────────────────────────────────────────────────────────────
// 必须是被分配的verifier提交；solutionHash必须跟链上已存的提交一致
// （验证方不能凭空报一个裁决，裁决必须绑定到链上确实存在的那一份y,mu）；
// 不允许重复提交裁决。通过则反解e_m并复用现有容量/拓扑校验写入IterationRecord，
// 不通过则只记录裁决，不写IterationRecord——CoordinateStep本身已经要求
// 三园区记录齐全才能往下走（readIterationRecords用getRequiredJSON强制），
// 所以这里不需要额外再加"不允许CoordinateStep继续"的逻辑，现有约束已经满足。
func (c *MaskCoordinationContract) RecordVerdict(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
	parkID string,
	pass bool,
	stationarityResScaled int64,
	complementResScaled int64,
	solutionHash string,
) error {
	assignment, err := readAssignment(ctx, scheduleID, iter, parkID)
	if err != nil {
		return err
	}
	if err := requireParkMSP(ctx, assignment.VerifierParkID); err != nil {
		return fmt.Errorf("caller is not the assigned verifier %s: %w", assignment.VerifierParkID, err)
	}

	solution, err := readSolution(ctx, scheduleID, iter, parkID)
	if err != nil {
		return err
	}
	if solution.SolutionHash != solutionHash {
		return fmt.Errorf("solutionHash mismatch: verdict references %s, on-chain solution is %s",
			solutionHash, solution.SolutionHash)
	}

	vKey := verdictKey(scheduleID, iter, parkID)
	exists, err := stateExists(ctx, vKey)
	if err != nil {
		return err
	}
	if exists {
		return fmt.Errorf("verdict already recorded for %s iter %d", parkID, iter)
	}

	verdict := Verdict{
		ScheduleID:            scheduleID,
		Iter:                  iter,
		ParkID:                parkID,
		VerifierParkID:        assignment.VerifierParkID,
		SolutionHash:          solutionHash,
		Pass:                  pass,
		StationarityResScaled: stationarityResScaled,
		ComplementResScaled:   complementResScaled,
		Timestamp:             txTimestamp(ctx),
	}
	if err := putJSON(ctx, vKey, &verdict); err != nil {
		return err
	}

	// 求解信用分是独立账本，跟数据信用分(updateCreditScore)完全分开记账：
	// Park2帮Park1解题解错了，扣的是Park2的求解信用，绝不牵连Park1的数据信用。
	if !pass {
		_ = recordAnomaly(ctx, assignment.SolverParkID, scheduleID, "KKT_VERIFICATION_FAILED",
			fmt.Sprintf("iter %d park %d solver %s: stationarityRes=%d complementRes=%d",
				iter, mustParkIndex(parkID), assignment.SolverParkID, stationarityResScaled, complementResScaled))
		_ = updateSolveCreditScore(ctx, assignment.SolverParkID, solveFailPenalty)
		return setEvent(ctx, "VerdictRecorded", map[string]any{
			"scheduleId": scheduleID, "iter": iter, "parkId": parkID, "pass": false,
		})
	}

	if err := recoverAndRecordExchange(ctx, scheduleID, iter, parkID, solution.YScaled); err != nil {
		return err
	}
	// 通过KKT校验给求解方小额正向奖励——解题是有真实算力成本的，
	// 只罚不奖会导致长期没人愿意接求解任务（呼应第一版mask逻辑.md第8.3节
	// 的两条独立约束：奖励保证有人愿意干活，惩罚压制合谋，不能合并成一条）。
	_ = updateSolveCreditScore(ctx, assignment.SolverParkID, solvePassReward)
	return setEvent(ctx, "VerdictRecorded", map[string]any{
		"scheduleId": scheduleID, "iter": iter, "parkId": parkID, "pass": true,
	})
}

// recoverAndRecordExchange 用链上已注册的E_rows,x0_e和刚验证通过的y*，
// 按M3.5确定的定点公式（逐项乘完先用divRound除一次MaskScale再累加，
// 跟链码现有divRound约定保持一致）机械算出e_m，再复用
// validateExchangeBounds（跟SubmitIterationResult完全同一段校验逻辑）
// 写入IterationRecord。e_m不是任何一方自报的，链码自己算。
func recoverAndRecordExchange(
	ctx contractapi.TransactionContextInterface,
	scheduleID string,
	iter int,
	parkID string,
	yScaled []int64,
) error {
	mask, err := readMask(ctx, scheduleID, parkID)
	if err != nil {
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
		return fmt.Errorf("iteration mismatch: got %d want %d", iter, state.Iter)
	}

	key := iterationKey(scheduleID, iter, parkID)
	exists, err := stateExists(ctx, key)
	if err != nil {
		return err
	}
	if exists {
		return fmt.Errorf("iteration result already exists for %s iter %d", parkID, iter)
	}

	// eFull 算出来是 MaskScale(1e6) 量级的物理值（比如真实1.5584MW会算成约1558400），
	// 但 IterationRecord 现有字段(EExScaled等)用的是 EScale(1000) 量级
	// （1.5584MW对应1558）。两者缩放基准不同，必须先转换单位再写入，
	// 否则数值会差1000倍（MaskScale/EScale）——这是真实跑出来才暴露的bug，
	// 之前只测过E_rows/x0_e全零的占位数据，恰好0×任何换算都还是0，没暴露这个问题。
	const maskToEScale = MaskScale / EScale

	eFull := make([]int64, eBlockLen)
	for i := 0; i < eBlockLen; i++ {
		var sum int64
		row := mask.ERowsScaled[i]
		for j := 0; j < mask.K0; j++ {
			sum += divRound(row[j]*yScaled[j], MaskScale)
		}
		eFullMaskScale := sum + mask.X0EScaled[i]
		eFull[i] = divRound(eFullMaskScale, maskToEScale)
	}
	eEx := eFull[0:horizonHours]
	gEx := eFull[horizonHours : 2*horizonHours]
	hEx := eFull[2*horizonHours : 3*horizonHours]
	cEx := eFull[3*horizonHours : 4*horizonHours]

	if err := validateExchangeBounds(ctx, state, scheduleID, parkID, eEx, gEx, hEx, cEx); err != nil {
		return err
	}

	// localCost/localCarbon沿用园区自报（这两个量从来不是mask/KKT要解决的
	// 威胁范围，跟现状一致地留给原有信用/审计机制处理），这里先记0，
	// 真正接入真实求解流程时由Python客户端在SubmitMaskedSolution之外
	// 另行调用现有上报接口或在此函数签名中补充对应参数。
	record := IterationRecord{
		ScheduleID:        scheduleID,
		Iter:              iter,
		ParkID:            parkID,
		EExScaled:         eEx,
		GExScaled:         gEx,
		HExScaled:         hEx,
		CExScaled:         cEx,
		LocalCostScaled:   0,
		LocalCarbonScaled: 0,
		Timestamp:         txTimestamp(ctx),
	}
	if err := putJSON(ctx, key, &record); err != nil {
		return err
	}
	return setEvent(ctx, "MaskedSubmitConfirmed", map[string]any{
		"scheduleId": scheduleID, "iter": iter, "parkId": parkID,
	})
}

// ── 查询函数 ───────────────────────────────────────────────────────────────────

func (c *MaskCoordinationContract) QueryMaskedSubproblem(ctx contractapi.TransactionContextInterface, scheduleID, parkID string) (*MaskedSubproblem, error) {
	return readMask(ctx, scheduleID, parkID)
}

func (c *MaskCoordinationContract) QuerySolverAssignment(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*SolverAssignment, error) {
	return readAssignment(ctx, scheduleID, iter, parkID)
}

func (c *MaskCoordinationContract) QueryMaskedSolution(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*MaskedSolution, error) {
	return readSolution(ctx, scheduleID, iter, parkID)
}

func (c *MaskCoordinationContract) QueryVerdict(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*Verdict, error) {
	var v Verdict
	if err := getRequiredJSON(ctx, verdictKey(scheduleID, iter, parkID), &v); err != nil {
		return nil, err
	}
	return &v, nil
}

// ── 内部辅助函数 ───────────────────────────────────────────────────────────────

func maskKey(scheduleID, parkID string) string { return "MASK_" + scheduleID + "_" + parkID }
func uploadMetaKey(scheduleID, parkID string) string {
	return "MASK_UPLOAD_" + scheduleID + "_" + parkID
}
func uploadChunkKey(scheduleID, parkID, partName string, chunkIndex int) string {
	return "MASK_UPLOAD_CHUNK_" + scheduleID + "_" + parkID + "_" + partName + "_" + strconv.Itoa(chunkIndex)
}
func assignmentKey(scheduleID string, iter int, parkID string) string {
	return "ASSIGN_" + scheduleID + "_" + strconv.Itoa(iter) + "_" + parkID
}
func solutionKey(scheduleID string, iter int, parkID string) string {
	return "MSOL_" + scheduleID + "_" + strconv.Itoa(iter) + "_" + parkID
}
func verdictKey(scheduleID string, iter int, parkID string) string {
	return "VERDICT_" + scheduleID + "_" + strconv.Itoa(iter) + "_" + parkID
}

func readMask(ctx contractapi.TransactionContextInterface, scheduleID, parkID string) (*MaskedSubproblem, error) {
	var mask MaskedSubproblem
	if err := getRequiredJSON(ctx, maskKey(scheduleID, parkID), &mask); err != nil {
		return nil, fmt.Errorf("masked subproblem not registered for %s: %w", parkID, err)
	}
	return &mask, nil
}

func readUploadMeta(ctx contractapi.TransactionContextInterface, scheduleID, parkID string) (*MaskedUploadMeta, error) {
	var meta MaskedUploadMeta
	if err := getRequiredJSON(ctx, uploadMetaKey(scheduleID, parkID), &meta); err != nil {
		return nil, fmt.Errorf("masked upload not started for %s: %w", parkID, err)
	}
	return &meta, nil
}

func isMaskedUploadPart(partName string) bool {
	for _, p := range maskedUploadParts {
		if p == partName {
			return true
		}
	}
	return false
}

func assembleUploadPart(ctx contractapi.TransactionContextInterface, scheduleID, parkID, partName string) (string, error) {
	var first MaskedUploadChunk
	if err := getRequiredJSON(ctx, uploadChunkKey(scheduleID, parkID, partName, 0), &first); err != nil {
		return "", fmt.Errorf("missing first chunk for %s: %w", partName, err)
	}
	if first.TotalChunks <= 0 {
		return "", fmt.Errorf("invalid totalChunks for %s: %d", partName, first.TotalChunks)
	}

	var builder strings.Builder
	for i := 0; i < first.TotalChunks; i++ {
		var chunk MaskedUploadChunk
		if err := getRequiredJSON(ctx, uploadChunkKey(scheduleID, parkID, partName, i), &chunk); err != nil {
			return "", fmt.Errorf("missing chunk %s[%d/%d]: %w", partName, i, first.TotalChunks, err)
		}
		if chunk.PartName != partName || chunk.ChunkIndex != i || chunk.TotalChunks != first.TotalChunks {
			return "", fmt.Errorf("inconsistent chunk metadata for %s[%d]", partName, i)
		}
		builder.WriteString(chunk.Data)
	}
	return builder.String(), nil
}

func deleteUploadPart(ctx contractapi.TransactionContextInterface, scheduleID, parkID, partName string) error {
	var first MaskedUploadChunk
	if err := getRequiredJSON(ctx, uploadChunkKey(scheduleID, parkID, partName, 0), &first); err != nil {
		return err
	}
	for i := 0; i < first.TotalChunks; i++ {
		if err := ctx.GetStub().DelState(uploadChunkKey(scheduleID, parkID, partName, i)); err != nil {
			return err
		}
	}
	return nil
}

func readAssignment(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*SolverAssignment, error) {
	var a SolverAssignment
	if err := getRequiredJSON(ctx, assignmentKey(scheduleID, iter, parkID), &a); err != nil {
		return nil, fmt.Errorf("no solver assignment for %s iter %d: %w", parkID, iter, err)
	}
	return &a, nil
}

func readSolution(ctx contractapi.TransactionContextInterface, scheduleID string, iter int, parkID string) (*MaskedSolution, error) {
	var s MaskedSolution
	if err := getRequiredJSON(ctx, solutionKey(scheduleID, iter, parkID), &s); err != nil {
		return nil, fmt.Errorf("no masked solution submitted for %s iter %d: %w", parkID, iter, err)
	}
	return &s, nil
}

// otherTwoParks 返回除parkID之外的另外两个园区，固定顺序（按parkIDs()的声明顺序）。
func otherTwoParks(parkID string) ([]string, error) {
	if _, err := parkIndex(parkID); err != nil {
		return nil, err
	}
	var others []string
	for _, p := range parkIDs() {
		if p != parkID {
			others = append(others, p)
		}
	}
	return others, nil
}

// hashToIndex 把一个十六进制哈希字符串的前几位转成 [0,n) 范围内的索引。
func hashToIndex(hexHash string, n int) int {
	if n <= 0 {
		return 0
	}
	var v uint64
	for i := 0; i < 16 && i < len(hexHash); i++ {
		v = v*16 + uint64(hexDigit(hexHash[i]))
	}
	return int(v % uint64(n))
}

func hexDigit(b byte) uint64 {
	switch {
	case b >= '0' && b <= '9':
		return uint64(b - '0')
	case b >= 'a' && b <= 'f':
		return uint64(b-'a') + 10
	case b >= 'A' && b <= 'F':
		return uint64(b-'A') + 10
	default:
		return 0
	}
}

func mustParkIndex(parkID string) int {
	idx, err := parkIndex(parkID)
	if err != nil {
		return -1
	}
	return idx
}

// parseInt64Matrix 解析形如 [[...],[...],...] 的JSON二维数组，并校验行列数。
func parseInt64Matrix(raw string, expectedRows, expectedCols int, name string) ([][]int64, error) {
	var values [][]int64
	if err := json.Unmarshal([]byte(raw), &values); err != nil {
		return nil, fmt.Errorf("invalid %s JSON: %w", name, err)
	}
	if len(values) != expectedRows {
		return nil, fmt.Errorf("%s row count %d, want %d", name, len(values), expectedRows)
	}
	for i, row := range values {
		if len(row) != expectedCols {
			return nil, fmt.Errorf("%s row %d has %d cols, want %d", name, i, len(row), expectedCols)
		}
	}
	return values, nil
}
