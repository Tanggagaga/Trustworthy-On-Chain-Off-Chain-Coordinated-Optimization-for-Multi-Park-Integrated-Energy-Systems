#!/usr/bin/env bash
# upgrade_chaincode.sh
# Upgrades the 'energy' chaincode to a new version/sequence on dispatch-channel.
# Usage: bash upgrade_chaincode.sh
set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────────
PEER_BIN="$HOME/fabric-workspace/fabric-samples/bin/peer"
NET="$HOME/fabric-workspace/energy-dispatch/network"
CC_SRC="$HOME/fabric-workspace/energy-dispatch/chaincode"
export FABRIC_CFG_PATH="$HOME/fabric-workspace/energy-dispatch/config"
export ORDERER_CA="$NET/organizations/ordererOrganizations/example.com/tlsca/tlsca.example.com-cert.pem"

# ── Chaincode parameters ────────────────────────────────────────────────────────
CHANNEL="dispatch-channel"
CC_NAME="energy"
CC_VERSION="2.5"
CC_SEQUENCE="7"
CC_POLICY="OutOf(2,'Park1MSP.peer','Park2MSP.peer','Park3MSP.peer')"

# ── TLS certs ──────────────────────────────────────────────────────────────────
PARK1_TLS="$NET/organizations/peerOrganizations/park1.energy.com/tlsca/tlsca.park1.energy.com-cert.pem"
PARK2_TLS="$NET/organizations/peerOrganizations/park2.energy.com/tlsca/tlsca.park2.energy.com-cert.pem"
PARK3_TLS="$NET/organizations/peerOrganizations/park3.energy.com/tlsca/tlsca.park3.energy.com-cert.pem"
OBS_TLS="$NET/organizations/peerOrganizations/observer.energy.com/tlsca/tlsca.observer.energy.com-cert.pem"

# ── Helper: set env for an org ─────────────────────────────────────────────────
use_org() {
  local ORG=$1
  export CORE_PEER_TLS_ENABLED=true
  case $ORG in
    Park1)
      export CORE_PEER_LOCALMSPID=Park1MSP
      export CORE_PEER_ADDRESS=localhost:7051
      export CORE_PEER_MSPCONFIGPATH="$NET/organizations/peerOrganizations/park1.energy.com/users/Admin@park1.energy.com/msp"
      export CORE_PEER_TLS_ROOTCERT_FILE="$PARK1_TLS"
      ;;
    Park2)
      export CORE_PEER_LOCALMSPID=Park2MSP
      export CORE_PEER_ADDRESS=localhost:8051
      export CORE_PEER_MSPCONFIGPATH="$NET/organizations/peerOrganizations/park2.energy.com/users/Admin@park2.energy.com/msp"
      export CORE_PEER_TLS_ROOTCERT_FILE="$PARK2_TLS"
      ;;
    Park3)
      export CORE_PEER_LOCALMSPID=Park3MSP
      export CORE_PEER_ADDRESS=localhost:9051
      export CORE_PEER_MSPCONFIGPATH="$NET/organizations/peerOrganizations/park3.energy.com/users/Admin@park3.energy.com/msp"
      export CORE_PEER_TLS_ROOTCERT_FILE="$PARK3_TLS"
      ;;
    Observer)
      export CORE_PEER_LOCALMSPID=NetworkObserverMSP
      export CORE_PEER_ADDRESS=localhost:10051
      export CORE_PEER_MSPCONFIGPATH="$NET/organizations/peerOrganizations/observer.energy.com/users/Admin@observer.energy.com/msp"
      export CORE_PEER_TLS_ROOTCERT_FILE="$OBS_TLS"
      ;;
  esac
}

echo "============================================================"
echo " Step 1: Vendor Go dependencies"
echo "============================================================"
cd "$CC_SRC"
GO111MODULE=on go mod vendor
echo "Done vendoring."

echo ""
echo "============================================================"
echo " Step 2: Package chaincode  (${CC_NAME} v${CC_VERSION})"
echo "============================================================"
cd "$NET"
use_org Park1

"$PEER_BIN" lifecycle chaincode package "${CC_NAME}.tar.gz" \
  --path "$CC_SRC" \
  --lang golang \
  --label "${CC_NAME}_${CC_VERSION}"

PACKAGE_ID=$("$PEER_BIN" lifecycle chaincode calculatepackageid "${CC_NAME}.tar.gz")
echo "Package ID: $PACKAGE_ID"

echo ""
echo "============================================================"
echo " Step 3: Install on all four peers"
echo "============================================================"
for ORG in Park1 Park2 Park3 Observer; do
  use_org $ORG
  echo "  Installing on $ORG ..."
  "$PEER_BIN" lifecycle chaincode install "${CC_NAME}.tar.gz" 2>&1
  echo "  Done: $ORG"
done

echo ""
echo "============================================================"
echo " Step 4: Approve for Park1, Park2, Park3"
echo "============================================================"
for ORG in Park1 Park2 Park3; do
  use_org $ORG
  echo "  Approving for $ORG ..."
  "$PEER_BIN" lifecycle chaincode approveformyorg \
    -o localhost:7050 \
    --ordererTLSHostnameOverride orderer.example.com \
    --tls --cafile "$ORDERER_CA" \
    --channelID "$CHANNEL" \
    --name "$CC_NAME" \
    --version "$CC_VERSION" \
    --package-id "$PACKAGE_ID" \
    --sequence "$CC_SEQUENCE" \
    --signature-policy "$CC_POLICY" \
    2>&1
  echo "  Approved: $ORG"
done

echo ""
echo "============================================================"
echo " Step 5: Check commit readiness"
echo "============================================================"
use_org Park1
"$PEER_BIN" lifecycle chaincode checkcommitreadiness \
  --channelID "$CHANNEL" \
  --name "$CC_NAME" \
  --version "$CC_VERSION" \
  --sequence "$CC_SEQUENCE" \
  --signature-policy "$CC_POLICY" \
  --output json 2>&1

echo ""
echo "============================================================"
echo " Step 6: Commit chaincode definition"
echo "============================================================"
use_org Park1
"$PEER_BIN" lifecycle chaincode commit \
  -o localhost:7050 \
  --ordererTLSHostnameOverride orderer.example.com \
  --tls --cafile "$ORDERER_CA" \
  --channelID "$CHANNEL" \
  --name "$CC_NAME" \
  --version "$CC_VERSION" \
  --sequence "$CC_SEQUENCE" \
  --signature-policy "$CC_POLICY" \
  --peerAddresses localhost:7051 --tlsRootCertFiles "$PARK1_TLS" \
  --peerAddresses localhost:8051 --tlsRootCertFiles "$PARK2_TLS" \
  --peerAddresses localhost:9051 --tlsRootCertFiles "$PARK3_TLS" \
  2>&1

echo ""
echo "============================================================"
echo " Step 7: Verify committed"
echo "============================================================"
use_org Park1
"$PEER_BIN" lifecycle chaincode querycommitted \
  -C "$CHANNEL" --name "$CC_NAME" 2>&1

echo ""
echo "✅  Upgrade complete: ${CC_NAME} v${CC_VERSION} Sequence ${CC_SEQUENCE}"
