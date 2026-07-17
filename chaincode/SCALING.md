# ADMM 定点整数缩放推导

本文档对应 MATLAB 源码：

`C:\Users\Lenovo\Desktop\daima\综合能源\三个园区数据\ADMM功率平衡\admm_power_balance_three_park.m`

核心目标：把 MATLAB 浮点 ADMM 协调层公式转换为 Fabric Go 链码可执行的 `int64` 定点整数公式。链上只实现协调层，不实现各园区本地 MILP 求解。

---

## 1. MATLAB 中的 ADMM 协调公式

MATLAB 主循环中的核心公式为：

```matlab
v = e + lambda/rho;
z = v - mean(v);

r_pri  = norm(e - z, 'fro');
r_dual = rho * norm(z - z_old, 'fro');

lambda = lambda + rho*(e - z);

if r_pri > mu * r_dual
    rho = min(rho_max, omega * rho);
elseif r_dual > mu * r_pri
    rho = max(rho_min, rho / omega);
end
```

参数来自 MATLAB 代码：

| 参数 | MATLAB 值 | 说明 |
|---|---:|---|
| `rho` | `1.0` | 初始 ADMM 惩罚参数 |
| `mu` | `10` | 残差平衡阈值 |
| `omega` | `2.0` | rho 放大/缩小倍率 |
| `rho_min` | `0.05` | rho 下限 |
| `rho_max` | `100.0` | rho 上限 |
| `eps_pri` | `0.05 MW` | 原始残差收敛阈值 |
| `eps_dual` | `0.05` | 对偶残差收敛阈值 |
| `maxIter` | `80` | 最大迭代轮数 |

---

## 2. 缩放约定

链码中所有参与状态更新和判敛的数值均使用 `int64`。

| 变量 | 真实单位 | 缩放倍数 | 链上含义 |
|---|---|---:|---|
| `e` | MW | `E_SCALE = 1000` | `eScaled = round(e * 1000)`，等价于 kW 整数 |
| `z` | MW | `E_SCALE = 1000` | `zScaled = round(z * 1000)` |
| `lambda` | 元/MW | `LAMBDA_SCALE = 10000` | `lambdaScaled = round(lambda * 10000)` |
| `rho` | 元/MW^2 | `RHO_SCALE = 10000` | `rhoScaled = round(rho * 10000)` |
| 成本 | 元 | `COST_SCALE = 100` | 保留 0.01 元 |
| 碳排 | tCO2 | `CARBON_SCALE = 1000` | 保留 0.001 tCO2 |

Go 常量建议：

```go
const (
    EScale      int64 = 1000
    LambdaScale int64 = 10000
    RhoScale    int64 = 10000
    CostScale   int64 = 100
    CarbonScale int64 = 1000
)
```

---

## 3. z 更新缩放推导

真实公式：

```text
aux_m(t) = e_m(t) + lambda_m(t) / rho
z_m(t)   = aux_m(t) - mean_m(aux_m(t))
```

缩放关系：

```text
e_real      = eScaled / E_SCALE
lambda_real = lambdaScaled / LAMBDA_SCALE
rho_real    = rhoScaled / RHO_SCALE
```

因为当前约定中 `LAMBDA_SCALE = RHO_SCALE = 10000`，所以：

```text
lambda_real / rho_real
= (lambdaScaled / 10000) / (rhoScaled / 10000)
= lambdaScaled / rhoScaled
```

该值的真实单位为 MW。为了和 `eScaled` 相加，需要再乘以 `E_SCALE`：

```text
(lambda/rho)_ScaledE = lambdaScaled * E_SCALE / rhoScaled
```

因此链上整数公式为：

```go
auxScaled[m][t] = eScaled[m][t] + divRound(lambdaScaled[m][t]*EScale, rhoScaled)
meanAuxScaled   = divRound(sum_m auxScaled[m][t], int64(M))
zScaled[m][t]   = auxScaled[m][t] - meanAuxScaled
```

注意：

1. 不应直接写 `eScaled + lambdaScaled/rhoScaled`，否则 `lambda/rho` 少乘了 `E_SCALE`，量纲错误。
2. `meanAuxScaled` 建议使用确定性四舍五入 `divRound`，不要用浮点。
3. 由于整数除法会产生截断误差，`sum_m zScaled[m][t]` 可能不是严格 0，通常误差不超过园区数级别的 kW 整数。单测中应允许 `abs(sumZ) <= M`，不要强制等于 0。

---

## 4. lambda 更新缩放推导

真实公式：

```text
lambda_m(t) = lambda_m(t) + rho * (e_m(t) - z_m(t))
```

代入缩放：

```text
lambdaNewScaled / LAMBDA_SCALE
= lambdaScaled / LAMBDA_SCALE
  + (rhoScaled / RHO_SCALE) * ((eScaled - zScaled) / E_SCALE)
```

两边乘以 `LAMBDA_SCALE`：

```text
lambdaNewScaled
= lambdaScaled
  + rhoScaled * (eScaled - zScaled) * LAMBDA_SCALE
    / (RHO_SCALE * E_SCALE)
```

由于 `LAMBDA_SCALE = RHO_SCALE`，公式简化为：

```go
lambdaNewScaled[m][t] =
    lambdaScaled[m][t] + divRound(rhoScaled*(eScaled[m][t]-zScaled[m][t]), EScale)
```

注意：

1. 这里也不能直接写 `rhoScaled*(e-z)`，否则会多出 `E_SCALE` 倍。
2. `eScaled - zScaled` 的单位是 kW 整数；除以 `EScale` 后才回到 MW 量纲对应的 lambda 缩放。

---

## 5. 原始残差缩放推导

MATLAB 公式：

```text
r_pri = ||e - z||_F
```

链上避免开方，直接比较平方：

```text
r_pri^2 = sum_m sum_t (e_m(t) - z_m(t))^2
```

因为 `eScaled` 和 `zScaled` 都是 `MW * 1000`：

```go
primalResidSqScaled = sum((eScaled[m][t] - zScaled[m][t])^2)
```

它的缩放单位为：

```text
MW^2 * E_SCALE^2 = MW^2 * 1,000,000
```

收敛阈值：

```text
eps_pri = 0.05 MW
epsPriSqScaled = (eps_pri * E_SCALE)^2 = (0.05 * 1000)^2 = 50^2 = 2500
```

Go 建议：

```go
epsPriSqScaled := int64(2500)
```

---

## 6. 对偶残差缩放推导

MATLAB 公式：

```text
r_dual = rho * ||z_new - z_old||_F
```

链上同样比较平方：

```text
r_dual^2 = rho^2 * sum_m sum_t (z_new - z_old)^2
```

真实量纲：

```text
rho_real^2 * (deltaZ_real)^2
```

链上希望与 MATLAB 的 `r_dual^2` 在同一数量级下按平方阈值比较。由于：

```text
rho_real = rhoScaled / RHO_SCALE
deltaZ_real = deltaZScaled / E_SCALE
```

所以：

```text
r_dual^2
= (rhoScaled^2 / RHO_SCALE^2) * sum(deltaZScaled^2 / E_SCALE^2)
```

如果要和 `eps_dual^2` 使用同一套整数平方缩放，定义：

```text
dualResidSqScaled = r_dual^2 * E_SCALE^2
```

则：

```text
dualResidSqScaled
= rhoScaled^2 * sum(deltaZScaled^2) / RHO_SCALE^2
```

因为 `RHO_SCALE = 10000`：

```go
dualResidSqScaled =
    divRound(rhoScaled*rhoScaled*sumDeltaZSq, RhoScale*RhoScale)
```

收敛阈值：

```text
eps_dual = 0.05
epsDualSqScaled = (eps_dual * E_SCALE)^2 = 2500
```

注意：

1. 清单中若写成 `rhoScaled^2 * sum(deltaZScaled^2)`，会多出 `RHO_SCALE^2` 倍，无法与 `epsDualSqScaled=2500` 比较。
2. 必须除以 `RhoScale*RhoScale`，或等价地把阈值也乘上 `RhoScale^2`。建议采用本节公式，让原始残差和对偶残差都落在 `E_SCALE^2` 缩放下。

---

## 7. rho 自适应缩放推导

MATLAB 使用未平方残差判断：

```matlab
if r_pri > mu * r_dual
    rho = min(rho_max, omega * rho);
elseif r_dual > mu * r_pri
    rho = max(rho_min, rho / omega);
end
```

链上存的是平方残差，因此比较式应改为：

```text
r_pri^2  > mu^2 * r_dual^2
r_dual^2 > mu^2 * r_pri^2
```

当前 `mu=10`，所以 `mu^2=100`。

Go 整数公式：

```go
const (
    MuSq          int64 = 100
    Omega         int64 = 2
    RhoMinScaled  int64 = 500    // 0.05 * 10000
    RhoMaxScaled  int64 = 1000000 // 100.0 * 10000
)

if primalSq > MuSq*dualSq {
    rho = min(RhoMaxScaled, Omega*rho)
} else if dualSq > MuSq*primalSq {
    rho = max(RhoMinScaled, rho/Omega)
}
```

注意：

1. `rho_min=0.05`，按 `RHO_SCALE=10000` 应为 `500`，不是 `100`。
2. `rho_max=100.0`，按 `RHO_SCALE=10000` 应为 `1,000,000`，不是 `100,000`。
3. 如果后续希望使用 `rho_min=0.01`、`rho_max=10`，可以改成 `100` 和 `100000`，但这与当前 MATLAB 代码不一致。

---

## 8. 整数除法策略

Fabric 背书要求所有 endorsing peer 得到完全一致的读写集，因此必须避免浮点。

建议实现确定性四舍五入：

```go
func divRound(num, den int64) int64 {
    if den == 0 {
        panic("division by zero")
    }
    if num >= 0 {
        return (num + den/2) / den
    }
    return -((-num + den/2) / den)
}
```

如果希望更保守，也可以全程使用截断除法 `/`，但 Go 与 MATLAB 浮点结果会有更大偏差。无论选择哪种方式，必须在 `SCALING.md`、`utils.go` 和 `utils_test.go` 中保持一致。

本文建议使用 `divRound`，因为：

1. 与 MATLAB 浮点计算更接近；
2. 对正负数对称；
3. 仍然完全确定性。

---

## 9. int64 溢出检查

以最大交换功率 `20 MW` 为估算：

```text
eScaled = 20 * 1000 = 20000
delta = eScaled - zScaled
```

即使保守估计 `abs(delta) <= 40000`：

```text
delta^2 = 1.6e9
M*T = 3*24 = 72
sum(delta^2) ≈ 1.152e11
```

原始残差平方约 `1.2e11`，远低于 `int64` 上限 `9.22e18`。

对偶残差计算中最危险的是：

```text
rhoScaled^2 * sumDeltaZSq
```

若 `rhoScaled = 1,000,000`，则：

```text
rhoScaled^2 * sumDeltaZSq ≈ 1e12 * 1.152e11 = 1.152e23
```

这会超过 `int64`。因此 `computeDualResidSq` 不能先直接计算完整乘积再除以 `RhoScale^2`。

推荐实现：

```go
// 先把 rhoScaled/RhoScale 约分或分步缩放，避免中间乘积溢出。
rhoRatioScaled := divRound(rhoScaled*rhoScaled, RhoScale*RhoScale)
dualSq := rhoRatioScaled * sumDeltaZSq
```

但当 `rhoScaled*rhoScaled` 最大为 `1e12`，仍在 int64 内，安全。`rhoRatioScaled` 最大为：

```text
(1,000,000^2)/(10,000^2) = 10,000
```

因此：

```text
dualSq <= 10000 * 1.152e11 = 1.152e15
```

仍低于 `int64` 上限。

Go 建议：

```go
rhoSqRatio := divRound(rhoScaled*rhoScaled, RhoScale*RhoScale)
dualResidSqScaled := rhoSqRatio * sumDeltaZSq
```

---

## 10. Go 工具函数建议签名

```go
func computeZUpdate(eScaled, lambdaScaled [][]int64, rhoScaled int64) [][]int64

func computeLambdaUpdate(lambdaScaled, eScaled, zScaled [][]int64, rhoScaled int64) [][]int64

func computePrimalResidSq(eScaled, zScaled [][]int64) int64

func computeDualResidSq(zNewScaled, zOldScaled [][]int64, rhoScaled int64) int64

func adaptRho(rhoScaled, primalSq, dualSq int64) int64
```

---

## 11. 单元测试必须覆盖

1. `lambda/rho` 量纲测试：验证 `lambdaScaled * EScale / rhoScaled`，不能误写为 `lambdaScaled/rhoScaled`。
2. `lambda` 更新量纲测试：验证增量为 `rhoScaled*(e-z)/EScale`。
3. `sum z` 平衡测试：每个时刻 `abs(sum_m z_m(t)) <= M`。
4. 原始残差平方测试：与手算值一致。
5. 对偶残差平方测试：确认除以 `RhoScale^2` 后再比较阈值。
6. `rho` 三分支测试：
   - `primalSq > 100*dualSq`，rho 翻倍；
   - `dualSq > 100*primalSq`，rho 减半；
   - 否则 rho 不变。
7. `rho_min/rho_max` 测试：当前 MATLAB 对应 `500` 和 `1,000,000`。
8. 溢出边界测试：`eMax <= 20 MW`、`rho <= 100` 下所有中间量不超过 `int64`。

---

## 12. 与 MATLAB 结果对照

已导出的结果文件可作为后续测试参考：

```text
admmpb_iteration_history.csv
admmpb_timeseries.csv
admmpb_lambda_timeseries.csv
admmpb_summary.csv
```

其中 `admmpb_summary.csv` 显示：

```text
iters = 20
r_pri = 0.0251221549956585
r_dual = 0.000132071152626803
gap_pct = 0.00190998791728369
```

后续 Go 单测建议优先使用一组人工小样本验证公式，再使用 MATLAB 导出的某轮数据做回归测试。不要直接用最终时序数据反推完整迭代，因为 CSV 中主要是最终结果和历史摘要，并不包含每一轮完整的 `e/z/lambda` 矩阵。
