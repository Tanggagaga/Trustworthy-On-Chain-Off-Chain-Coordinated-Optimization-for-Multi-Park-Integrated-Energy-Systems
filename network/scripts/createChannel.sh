#!/usr/bin/env bash

. scripts/envVar.sh

CHANNEL_NAME="$1"
DELAY="$2"
MAX_RETRY="$3"
VERBOSE="$4"
: ${CHANNEL_NAME:="dispatch-channel"}
: ${DELAY:="3"}
: ${MAX_RETRY:="5"}
: ${VERBOSE:="false"}

: ${CONTAINER_CLI:="docker"}
if command -v ${CONTAINER_CLI}-compose > /dev/null 2>&1; then
    : ${CONTAINER_CLI_COMPOSE:="${CONTAINER_CLI}-compose"}
else
    : ${CONTAINER_CLI_COMPOSE:="${CONTAINER_CLI} compose"}
fi
infoln "Using ${CONTAINER_CLI} and ${CONTAINER_CLI_COMPOSE}"

if [ ! -d "channel-artifacts" ]; then
  mkdir channel-artifacts
fi

# 生成通道创世块，使用 DispatchChannel profile
createChannelGenesisBlock() {
  which configtxgen
  if [ "$?" -ne 0 ]; then
    fatalln "configtxgen tool not found."
  fi
  set -x
  configtxgen -profile DispatchChannel \
    -outputBlock ./channel-artifacts/${CHANNEL_NAME}.block \
    -channelID $CHANNEL_NAME
  res=$?
  { set +x; } 2>/dev/null
  verifyResult $res "Failed to generate channel genesis block."
}

# 把创世块提交给 Orderer，创建通道
createChannel() {
  local rc=1
  local COUNTER=1
  infoln "Submitting genesis block to orderer..."
  while [ $rc -ne 0 -a $COUNTER -lt $MAX_RETRY ]; do
    sleep $DELAY
    set -x
    . scripts/orderer.sh ${CHANNEL_NAME} > /dev/null 2>&1
    res=$?
    { set +x; } 2>/dev/null
    let rc=$res
    COUNTER=$(expr $COUNTER + 1)
  done
  cat log.txt
  verifyResult $res "Channel creation failed."
}

# 指定组织的 Peer 加入通道
joinChannel() {
  ORG=$1
  FABRIC_CFG_PATH=$PWD/../config/
  setGlobals $ORG
  local rc=1
  local COUNTER=1
  while [ $rc -ne 0 -a $COUNTER -lt $MAX_RETRY ]; do
    sleep $DELAY
    set -x
    peer channel join -b $BLOCKFILE >&log.txt
    res=$?
    { set +x; } 2>/dev/null
    let rc=$res
    COUNTER=$(expr $COUNTER + 1)
  done
  cat log.txt
  verifyResult $res "After $MAX_RETRY attempts, peer0.${ORG} has failed to join channel '$CHANNEL_NAME'."
}

# 设置锚节点
setAnchorPeer() {
  ORG=$1
  . scripts/setAnchorPeer.sh $ORG $CHANNEL_NAME
}

## 主流程

FABRIC_CFG_PATH=${PWD}/configtx
BLOCKFILE="./channel-artifacts/${CHANNEL_NAME}.block"

infoln "Generating channel genesis block '${CHANNEL_NAME}.block'"
createChannelGenesisBlock

infoln "Creating channel ${CHANNEL_NAME}"
createChannel
successln "Channel '${CHANNEL_NAME}' created"

infoln "Joining park1 peer to the channel..."
joinChannel park1
infoln "Joining park2 peer to the channel..."
joinChannel park2
infoln "Joining park3 peer to the channel..."
joinChannel park3
infoln "Joining observer peer to the channel..."
joinChannel observer

infoln "Setting anchor peer for park1..."
setAnchorPeer park1
infoln "Setting anchor peer for park2..."
setAnchorPeer park2
infoln "Setting anchor peer for park3..."
setAnchorPeer park3
infoln "Setting anchor peer for observer..."
setAnchorPeer observer

successln "Channel '${CHANNEL_NAME}' joined by all 4 organizations"
