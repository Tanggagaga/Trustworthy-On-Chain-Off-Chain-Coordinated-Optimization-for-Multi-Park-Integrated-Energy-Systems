# Trustworthy On-Chain/Off-Chain Coordinated Optimization for Multi-Park Integrated Energy Systems

This repository contains the implementation and deployment materials for coordinated multi-park integrated-energy-system optimization using ADMM, Hyperledger Fabric chaincode, and Mask-KKT verification.

## Repository layout

- `chaincode/`: Go smart contracts for ADMM coordination, masking, auditing, and settlement.
- `client/`: Python clients, solvers, experiments, and selected input data.
- `config/`: Fabric orderer and channel configuration.
- `network/`: Fabric network scripts and deployment configuration.

## Security note

Fabric-generated identities, MSP keystores, TLS private keys, runtime ledgers, logs, and packaged archives are intentionally excluded from version control. Generate credentials locally before starting the network; never commit private keys or production credentials.
