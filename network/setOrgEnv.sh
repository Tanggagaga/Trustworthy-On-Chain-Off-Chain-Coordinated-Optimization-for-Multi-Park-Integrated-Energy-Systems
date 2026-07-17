#!/usr/bin/env bash
#
# SPDX-License-Identifier: Apache-2.0

# default to using Park1
ORG=${1:-Park1}

# Exit on first error, print all commands.
set -e
set -o pipefail

# Where am I?
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

ORDERER_CA=${DIR}/organizations/ordererOrganizations/example.com/tlsca/tlsca.example.com-cert.pem
PEER0_PARK1_CA=${DIR}/organizations/peerOrganizations/park1.energy.com/tlsca/tlsca.park1.energy.com-cert.pem
PEER0_PARK2_CA=${DIR}/organizations/peerOrganizations/park2.energy.com/tlsca/tlsca.park2.energy.com-cert.pem
PEER0_PARK3_CA=${DIR}/organizations/peerOrganizations/park3.energy.com/tlsca/tlsca.park3.energy.com-cert.pem
PEER0_OBSERVER_CA=${DIR}/organizations/peerOrganizations/observer.energy.com/tlsca/tlsca.observer.energy.com-cert.pem

if [[ ${ORG,,} == "park1" ]]; then

   CORE_PEER_LOCALMSPID=Park1MSP
   CORE_PEER_MSPCONFIGPATH=${DIR}/organizations/peerOrganizations/park1.energy.com/users/Admin@park1.energy.com/msp
   CORE_PEER_ADDRESS=localhost:7051
   CORE_PEER_TLS_ROOTCERT_FILE=${PEER0_PARK1_CA}

elif [[ ${ORG,,} == "park2" ]]; then

   CORE_PEER_LOCALMSPID=Park2MSP
   CORE_PEER_MSPCONFIGPATH=${DIR}/organizations/peerOrganizations/park2.energy.com/users/Admin@park2.energy.com/msp
   CORE_PEER_ADDRESS=localhost:8051
   CORE_PEER_TLS_ROOTCERT_FILE=${PEER0_PARK2_CA}

elif [[ ${ORG,,} == "park3" ]]; then

   CORE_PEER_LOCALMSPID=Park3MSP
   CORE_PEER_MSPCONFIGPATH=${DIR}/organizations/peerOrganizations/park3.energy.com/users/Admin@park3.energy.com/msp
   CORE_PEER_ADDRESS=localhost:9051
   CORE_PEER_TLS_ROOTCERT_FILE=${PEER0_PARK3_CA}

elif [[ ${ORG,,} == "observer" || ${ORG,,} == "networkobserver" ]]; then

   CORE_PEER_LOCALMSPID=NetworkObserverMSP
   CORE_PEER_MSPCONFIGPATH=${DIR}/organizations/peerOrganizations/observer.energy.com/users/Admin@observer.energy.com/msp
   CORE_PEER_ADDRESS=localhost:10051
   CORE_PEER_TLS_ROOTCERT_FILE=${PEER0_OBSERVER_CA}

else
   echo "Unknown \"$ORG\", please choose Park1, Park2, Park3, or Observer"
   echo "Usage: export \$(./setOrgEnv.sh Park1 | xargs)"
   exit 1
fi

# output the variables
echo "CORE_PEER_TLS_ENABLED=true"
echo "ORDERER_CA=${ORDERER_CA}"
echo "PEER0_PARK1_CA=${PEER0_PARK1_CA}"
echo "PEER0_PARK2_CA=${PEER0_PARK2_CA}"
echo "PEER0_PARK3_CA=${PEER0_PARK3_CA}"
echo "PEER0_OBSERVER_CA=${PEER0_OBSERVER_CA}"
echo "CORE_PEER_LOCALMSPID=${CORE_PEER_LOCALMSPID}"
echo "CORE_PEER_MSPCONFIGPATH=${CORE_PEER_MSPCONFIGPATH}"
echo "CORE_PEER_ADDRESS=${CORE_PEER_ADDRESS}"
echo "CORE_PEER_TLS_ROOTCERT_FILE=${CORE_PEER_TLS_ROOTCERT_FILE}"
