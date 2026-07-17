#!/usr/bin/env bash
#
# Copyright IBM Corp All Rights Reserved
#
# SPDX-License-Identifier: Apache-2.0
#

TEST_NETWORK_HOME=${TEST_NETWORK_HOME:-${PWD}}
. ${TEST_NETWORK_HOME}/scripts/utils.sh

export CORE_PEER_TLS_ENABLED=true
export ORDERER_CA=${TEST_NETWORK_HOME}/organizations/ordererOrganizations/example.com/tlsca/tlsca.example.com-cert.pem
export PEER0_PARK1_CA=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park1.energy.com/tlsca/tlsca.park1.energy.com-cert.pem
export PEER0_PARK2_CA=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park2.energy.com/tlsca/tlsca.park2.energy.com-cert.pem
export PEER0_PARK3_CA=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park3.energy.com/tlsca/tlsca.park3.energy.com-cert.pem
export PEER0_OBSERVER_CA=${TEST_NETWORK_HOME}/organizations/peerOrganizations/observer.energy.com/tlsca/tlsca.observer.energy.com-cert.pem

# setGlobals <org>
# 切换当前 shell 身份到指定组织，接受 park1/park2/park3/observer
setGlobals() {
  local USING_ORG=""
  if [ -z "$OVERRIDE_ORG" ]; then
    USING_ORG=$1
  else
    USING_ORG="${OVERRIDE_ORG}"
  fi
  infoln "Using organization ${USING_ORG}"

  if [[ ${USING_ORG,,} == "park1" ]]; then
    export CORE_PEER_LOCALMSPID=Park1MSP
    export CORE_PEER_TLS_ROOTCERT_FILE=$PEER0_PARK1_CA
    export CORE_PEER_MSPCONFIGPATH=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park1.energy.com/users/Admin@park1.energy.com/msp
    export CORE_PEER_ADDRESS=localhost:7051

  elif [[ ${USING_ORG,,} == "park2" ]]; then
    export CORE_PEER_LOCALMSPID=Park2MSP
    export CORE_PEER_TLS_ROOTCERT_FILE=$PEER0_PARK2_CA
    export CORE_PEER_MSPCONFIGPATH=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park2.energy.com/users/Admin@park2.energy.com/msp
    export CORE_PEER_ADDRESS=localhost:8051

  elif [[ ${USING_ORG,,} == "park3" ]]; then
    export CORE_PEER_LOCALMSPID=Park3MSP
    export CORE_PEER_TLS_ROOTCERT_FILE=$PEER0_PARK3_CA
    export CORE_PEER_MSPCONFIGPATH=${TEST_NETWORK_HOME}/organizations/peerOrganizations/park3.energy.com/users/Admin@park3.energy.com/msp
    export CORE_PEER_ADDRESS=localhost:9051

  elif [[ ${USING_ORG,,} == "observer" ]]; then
    export CORE_PEER_LOCALMSPID=NetworkObserverMSP
    export CORE_PEER_TLS_ROOTCERT_FILE=$PEER0_OBSERVER_CA
    export CORE_PEER_MSPCONFIGPATH=${TEST_NETWORK_HOME}/organizations/peerOrganizations/observer.energy.com/users/Admin@observer.energy.com/msp
    export CORE_PEER_ADDRESS=localhost:10051

  else
    errorln "ORG Unknown: ${USING_ORG}. Choose park1, park2, park3, or observer."
  fi

  if [ "$VERBOSE" = "true" ]; then
    env | grep CORE
  fi
}

# parsePeerConnectionParameters <org1> <org2> ...
# 拼接多个 peer 的连接参数，供 chaincode invoke 时指定 endorsing peers
parsePeerConnectionParameters() {
  PEER_CONN_PARMS=()
  PEERS=""
  while [ "$#" -gt 0 ]; do
    setGlobals $1
    PEER="peer0.$1"
    if [ -z "$PEERS" ]; then
      PEERS="$PEER"
    else
      PEERS="$PEERS $PEER"
    fi
    PEER_CONN_PARMS=("${PEER_CONN_PARMS[@]}" --peerAddresses $CORE_PEER_ADDRESS)
    PEER_CONN_PARMS=("${PEER_CONN_PARMS[@]}" --tlsRootCertFiles $CORE_PEER_TLS_ROOTCERT_FILE)
    shift
  done
}

verifyResult() {
  if [ $1 -ne 0 ]; then
    fatalln "$2"
  fi
}
