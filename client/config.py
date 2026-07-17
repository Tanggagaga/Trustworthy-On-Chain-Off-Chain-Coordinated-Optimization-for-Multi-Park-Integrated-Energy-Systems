"""
所有路径、组织配置、链码参数集中在这里。
修改网络时只需改这一个文件。
"""

import os

# ── 基础路径 ─────────────────────────────────────────────────────────
HOME         = os.path.expanduser("~")
NETWORK_DIR  = os.path.join(HOME, "fabric-workspace", "energy-dispatch", "network")
FAB_BIN      = os.path.join(HOME, "fabric-workspace", "fabric-samples", "bin")
CFG_PATH     = os.path.join(HOME, "fabric-workspace", "energy-dispatch", "config")

ORDERER_CA   = os.path.join(
    NETWORK_DIR,
    "organizations", "ordererOrganizations", "example.com",
    "tlsca", "tlsca.example.com-cert.pem"
)

# ── 链码 / 通道 ───────────────────────────────────────────────────────
CHANNEL_NAME    = "dispatch-channel"
CHAINCODE_NAME  = "energy"

# 合约名（多合约注册时函数名必须带前缀）
CONTRACT_ADMM       = "ADMMCoordinationContract"
CONTRACT_SETTLEMENT = "SettlementContract"
CONTRACT_AUDIT      = "AuditCreditContract"
CONTRACT_MASK       = "MaskCoordinationContract"  # M4 mask信任机制（v2.1 Seq3新增）

# ── 组织配置 ──────────────────────────────────────────────────────────
def _org(domain, msp_id, port):
    base = os.path.join(
        NETWORK_DIR, "organizations", "peerOrganizations", domain
    )
    return {
        "msp_id":       msp_id,
        "peer_address": f"localhost:{port}",
        "tls_cert":     os.path.join(base, "tlsca", f"tlsca.{domain}-cert.pem"),
        "msp_path":     os.path.join(base, "users", f"Admin@{domain}", "msp"),
    }

ORG_CONFIG = {
    "Park1":    _org("park1.energy.com",    "Park1MSP",           7051),
    "Park2":    _org("park2.energy.com",    "Park2MSP",           8051),
    "Park3":    _org("park3.energy.com",    "Park3MSP",           9051),
    "Observer": _org("observer.energy.com", "NetworkObserverMSP", 10051),
}

PARK_IDS = ["Park1", "Park2", "Park3"]

# ── ADMM 参数（与链码 TriggerDispatch 对应）──────────────────────────
DEFAULT_RHO_SCALED  = 10000   # rho = 1.0
# eps = 0.10 MW → eps_sq_scaled = (0.10 × 1000)² = 10000
DEFAULT_EPS_PRI_SQ  = 10000
DEFAULT_EPS_DUAL_SQ = 10000
DEFAULT_MAX_ITER    = 100
DEFAULT_CARBON_PRICE = 4500   # 0.45 元/tCO2，同旧版缩放

# 各园区碳配额（tCO2 × CARBON_SCALE），来自新算例 MATLAB get_parameters()
# p.E_quota = [430.0, 530.0, 240.0] tCO2
CARBON_QUOTA = [430_000, 530_000, 240_000]

# ── 四能网络容量（MW × E_SCALE），来自新算例 get_parameters() ─────────
# Electric: eE_max = [11, 14, 9] MW  (Park1, Park2, Park3)
E_MAX_SCALED = [11_000, 14_000, 9_000]

# Gas: eG_max = [7, 13, 14] MW
G_MAX_SCALED = [7_000, 13_000, 14_000]

# Heat: eH_max = [9, 9] MW  (Park1, Park2 only — Park3 not in heat network)
H_MAX_SCALED = [9_000, 9_000]

# Cold: eC_max = [2, 4, 2] MW
C_MAX_SCALED = [2_000, 4_000, 2_000]

# ── 缩放常数（与 Go 链码一致）────────────────────────────────────────
E_SCALE      = 1000
LAMBDA_SCALE = 10000
RHO_SCALE    = 10000
COST_SCALE   = 100
CARBON_SCALE = 1000
