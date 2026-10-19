# Three-park integrated energy dispatch: research artifact

This repository contains the current chaincode, constructed three-park case data, analysis scripts, and saved results used in the manuscript *Trustworthy On-Chain/Off-Chain Coordinated Optimization for Multi-Park Integrated Energy Systems*. The earlier Fabric deployment files remain in `network/`; the research inputs and results were added for this release.

## What is included

| Directory | Contents | Scope |
|---|---|---|
| `case/` | Three-park hourly CSVs, design metadata, MATLAB ADMM and matched centralized programs | Offline numerical case study |
| `mask/` | Mask registration export program and three saved registration bundles | Input to masked optimization |
| `chaincode/` | Go chaincode, module files, and existing unit-test source | Ledger coordination, mask registration, on-chain KKT check, audit and settlement |
| `client/` | Python Fabric client, masked solver, M6 workflow and security experiment drivers | Fabric workflow; requires a configured network |
| `experiments/` | Scripts and saved evidence for sensitivity, KKT tolerance, and verifier-misreport checks | Supplemental analyses with different execution requirements |
| `results/` | Saved spreadsheets and JSON for the M6 and attack runs | Historical experiment outputs, not new runs made for this release |
| `config/`, `network/` | Fabric configuration and network deployment scripts | Generate identities locally; no private keys are supplied |

The three-park profiles were **constructed by adapting a single-park source workbook**; they are not measurements from three operating parks. The source workbook is not redistributed. `case/new_four_energy_interconnection.csv` describes design assumptions; its loss fields and the suggested waste-heat extension in the original case README should not be interpreted as constraints implemented in every released solver. The programs, rather than that metadata table, define each numerical experiment.

## Reproduction map

1. For the offline four-energy study, start with `case/distributed_admm/admm_four_energy_three_park.m` and the three hourly CSVs under `case/`. The code requires MATLAB, YALMIP, and Gurobi. See the source files for further toolbox requirements.
2. For a **matched centralized comparison**, read `case/centralized_comparison/README_结果与适用边界.md`, then run `centralized_matched_4e.m`, `replay47/admm_four_energy_replay.m`, and `audit_matched_comparison.m` in the sequence specified there. The saved `audited_comparison.csv` is the comparison record to inspect first. The original ADMM's 47th iteration is a tolerance-stopped iterate with a nonzero cross-park balance residual; the comparison note gives both cost and feasibility information. The `replay47` copy changes paths and its displayed reference cost, not the local ADMM updates.
3. For Mask bundles, run `mask/export_all_park_masks.m` with the offline case inputs, or inspect the saved `mask_registration_park{1,2,3}.json` bundles. The export script's input path was adapted to this folder.
4. For chaincode, use `chaincode/go.mod` (Go 1.23) and a compatible Hyperledger Fabric deployment. The `client/` scripts need peer/orderer addresses, Fabric binaries, channel/chaincode deployment, and locally generated identities. Set `ENERGY_DISPATCH_NETWORK_DIR`, `FABRIC_BIN`, `FABRIC_CFG_PATH`, and optionally `MASK_BUNDLE_DIR` as appropriate; see `network/README.md`. No network identities or deployment archive are included. The current KKT recomputation code is in `chaincode/kkt_onchain.go`; older scripts retained from the original repository are historical.
5. `client/m4_smoketest/run_M6_full_masked_admm.py` is the 47-iteration Fabric workflow driver. The L1–L4 and EXP2/EXP3 scripts are the historical security experiments; `run_EXP15_verifier_attack.py` addresses false verifier reports under on-chain KKT recomputation. These scripts can submit transactions and should be run only on a user-controlled test network. The related saved output is in `results/` or `experiments/EXP1_5/`.
6. `experiments/ADMM_sensitivity/` and `experiments/KKT_threshold/` are supplementary analyses. The latter includes an offline Go harness and saved results; its `prepare.py` now reads the local `chaincode/` copy. `experiments/EXP1_5/run_EXP15_recheck.py` is an environment-specific historical recheck and still needs a locally built deployment archive; it is not a one-command clean-network setup.

The offline MATLAB cost comparison and Fabric transaction/timing runs have different measurement boundaries. Do not present the offline cost figures as a cost independently recomputed by the Fabric client.

## Release notes

The source code was copied from the current three-park case folders and the active WSL Fabric project. The `chaincode/` files come from the WSL project, rather than an older Windows chaincode copy. Edits in the curated copy were limited to paths/configuration defaults needed to remove personal absolute paths; numerical algorithms and saved outputs were not intentionally changed. No experiment was rerun during packaging. `PROVENANCE.md` records source locations, and `MANIFEST.sha256` records repository file hashes for this revision.

The operating-cost objective retains the numerical units of the supplied model; its monetary-unit interpretation should be checked against the manuscript before economic claims are made. No license is declared here; contact the authors about reuse terms.
