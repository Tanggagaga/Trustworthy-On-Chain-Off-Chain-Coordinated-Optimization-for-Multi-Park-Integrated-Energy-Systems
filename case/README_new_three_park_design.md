# 新_三个园区设计

本目录保存新版三园区综合能源算例数据。数据由 `shuju数据.xlsx` 的单园区数据改编得到，不是实测多园区数据。

## 设计定位

| 园区 | 角色 | 主要特征 |
|---|---|---|
| Park1 | 商业综合型基准园区 | 保留原始电/风/光/温度主形态，但修正气负荷尾段复制问题；冬季冷负荷按内部散热和局部工艺冷需求处理 |
| Park2 | 工业高负荷余热输出园区 | 三班制工业负荷；无新能源；电/气净受端；CHP和工业工艺余热使其成为热净供端 |
| Park3 | 风电富集P2G供气园区 | 无光伏、大风电；CHP缩小至Park1约30%；以风电直供和P2G制气外送为主，不接入热网 |

## 本轮修正

1. 修正 Park1 原始气负荷第16-24小时复制电负荷的问题，并保持日总气负荷基本不变。
2. 平滑 Park1/Park3 末小时热负荷，去除明显占位符。
3. 下调冬季冷负荷，Park1 冷负荷峰值控制在热负荷峰值的 80% 以内。
4. 明确 `waste_heat_available` 是 Park2 的工业工艺余热可用上限，不是 CHP 余热的一部分。四能模型接入时建议增加：`0 <= H_waste_used(t) <= waste_heat_available(t)`，并把 `H_waste_used` 加入 Park2 热平衡。
5. 热互联只保留 `H_12`，表示 Park2 工业余热向 Park1 供热；删除 `H_13` 和 `H_23`。
6. 冷互联采用近距离园区群公共冷冻水管网假设，并在 interconnection 表中设置 5% 损耗。

## 文件说明

- `park1_commercial_integrated_timeseries.csv`
- `park2_industrial_waste_heat_timeseries.csv`
- `park3_wind_p2g_export_timeseries.csv`
- `new_three_park_design.json`：结构化完整数据。
- `new_three_park_design.xlsx`：Excel版数据包，包含说明、时序、容量、互联和校验表。
- `new_three_park_design_summary.csv`：各园区负荷、新能源占比、峰谷比摘要。
- `new_four_energy_interconnection.csv`：电、气、热、冷互联容量与损耗。
- `new_device_capacity.csv`：设备容量设计。

CSV 中保留 `load_e/load_g/P_PV/P_WT/T_out` 字段，现有 MATLAB 读取逻辑可继续按这些列读取；同时新增 `load_h/load_c` 供后续四能互联模型使用。
