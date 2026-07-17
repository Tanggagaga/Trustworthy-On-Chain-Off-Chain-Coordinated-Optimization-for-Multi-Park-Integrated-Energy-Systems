package main

// ── Mask 化子问题信任机制（M4）相关的链上数据结构 ─────────────────────────
//
// 整体流程见 链下上传信任/第一版逻辑实现清单.md 的 M4 节。
// 设计原则：
//   1. KKT 校验留在链下（独立verifier，复用M3.5已验证过的定点逻辑），
//      链上只记录 PASS/FAIL 裁决，不在链码里跑大矩阵的KKT校验运算。
//   2. e_m 永远由链码自己用已注册的公开量 E_rows,x0_e 和提交的 y* 算出来，
//      不信任任何一方自报的 e_m。
//   3. 求解方(solver)、验证方(verifier)、数据方(parkID，子问题所有者)
//      三者身份必须两两不同，由链码强制校验，不能自证。

// MaskedSubproblem 是某个园区某次调度的 masked 子问题注册信息（一次性）。
// 所有数值字段均已按 MaskScale（M3.5确定为1,000,000）定点缩放为整数。
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

// SolverAssignment 记录某一轮某个园区的masked子问题，被分配给谁求解、谁验证。
// solverID、verifierID、parkID（数据方）三者必须两两不同。
type SolverAssignment struct {
	ScheduleID     string `json:"scheduleId"`
	Iter           int    `json:"iter"`
	ParkID         string `json:"parkId"` // 数据方（子问题所有者）
	SolverParkID   string `json:"solverParkId"`
	VerifierParkID string `json:"verifierParkId"`
	Timestamp      string `json:"timestamp"`
}

// MaskedSolution 是求解方提交的masked解。
type MaskedSolution struct {
	ScheduleID   string `json:"scheduleId"`
	Iter         int    `json:"iter"`
	ParkID       string `json:"parkId"` // 子问题所属的数据方
	SolverParkID string `json:"solverParkId"`

	YScaled  []int64 `json:"yScaled"`  // k0
	MuScaled []int64 `json:"muScaled"` // qRows

	SolutionHash string `json:"solutionHash"` // sha256(scheduleId,iter,parkId,y,mu)，链码自己算，不信任solver报的hash
	Timestamp    string `json:"timestamp"`
}

// Verdict 是验证方提交的KKT校验裁决。
type Verdict struct {
	ScheduleID     string `json:"scheduleId"`
	Iter           int    `json:"iter"`
	ParkID         string `json:"parkId"`
	VerifierParkID string `json:"verifierParkId"`

	SolutionHash string `json:"solutionHash"` // 必须跟MaskedSolution里存的一致，否则裁决的不是同一份数据

	Pass                  bool  `json:"pass"`
	StationarityResScaled int64 `json:"stationarityResScaled"`
	ComplementResScaled   int64 `json:"complementResScaled"`

	Timestamp string `json:"timestamp"`
}

// MaskedUploadMeta records an in-progress chunked upload for a large masked
// subproblem. It exists only to bypass local CLI/OS argument length limits; the
// final registered MaskedSubproblem is identical to the direct registration path.
type MaskedUploadMeta struct {
	ScheduleID string `json:"scheduleId"`
	ParkID     string `json:"parkId"`
	K0         int    `json:"k0"`
	QRows      int    `json:"qRows"`
	MaskScale  int64  `json:"maskScale"`
	StartedBy  string `json:"startedBy"`
	Timestamp  string `json:"timestamp"`
}

// MaskedUploadChunk stores one JSON string fragment for one field of the large
// masked subproblem upload.
type MaskedUploadChunk struct {
	ScheduleID  string `json:"scheduleId"`
	ParkID      string `json:"parkId"`
	PartName    string `json:"partName"`
	ChunkIndex  int    `json:"chunkIndex"`
	TotalChunks int    `json:"totalChunks"`
	Data        string `json:"data"`
	Timestamp   string `json:"timestamp"`
}
