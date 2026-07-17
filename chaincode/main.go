package main

import (
	"log"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

func main() {
	chaincode, err := contractapi.NewChaincode(
		new(ADMMCoordinationContract),
		new(SettlementContract),
		new(AuditCreditContract),
		new(MaskCoordinationContract),
	)
	if err != nil {
		log.Panicf("failed to create energy dispatch chaincode: %v", err)
	}

	if err := chaincode.Start(); err != nil {
		log.Panicf("failed to start energy dispatch chaincode: %v", err)
	}
}
