# Source map

The paths below identify the source trees used to assemble the newly added research files. They are relative to the authors' development workspace except for the WSL Fabric project. The SHA-256 manifest describes this repository revision, including path-only edits.

| Release path | Source |
|---|---|
| `case/` hourly CSVs and design metadata | `三个园区数据/新_三个园区设计/` |
| `case/distributed_admm/` | `三个园区数据/新_三个园区设计/分布式ADMM/` |
| `case/centralized_comparison/` | `三个园区数据/新_三个园区设计/分布式ADMM/centralized_comparison_same_model_20261018/` |
| `mask/` | Three-park Mask export and saved registration bundles in the case/experiment workspace |
| `chaincode/`, `client/`, `config/` | Active WSL project `~/fabric-workspace/energy-dispatch_KKT/` |
| `experiments/EXP1_5/` | Verifier-misreport recheck and archived evidence in the revision workspace |
| `experiments/ADMM_sensitivity/`, `experiments/KKT_threshold/` | Existing supplementary parameter experiment folders in the revision workspace |
| `results/` | Saved M6 and attack outputs from `作恶MaskKKT专属/` and `作恶/` |

The Windows `chaincode/` directory at the workspace root was **not** used because it differs from the active WSL chaincode. Fabric private material and the original `shuju数据.xlsx` workbook were not copied. The repository retains older deployment scripts and historical experiment files from its initial July 2026 upload; the current on-chain KKT implementation is `chaincode/kkt_onchain.go`.

Packaging edits: replaced personal absolute paths in the Mask exporter and selected Python drivers with package-relative paths; made Fabric network, binary, configuration, and Mask paths configurable; made the KKT-threshold preparation script read the packaged chaincode. These edits have not been used to regenerate the archived results.
