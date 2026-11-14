"""Compile and run a minimal contract on an in-memory EVM."""

from pathlib import Path
import os

os.environ["SOLCX_BINARY_PATH"] = str(Path(__file__).resolve().parents[1] / ".solcx")

import solcx
from eth_tester import EthereumTester, PyEVMBackend
from web3 import EthereumTesterProvider, Web3


def main():
    version = "0.8.20"
    source = """// SPDX-License-Identifier: MIT
    pragma solidity ^0.8.20;
    contract EnvironmentCheck {
        uint256 public value;
        function setValue(uint256 newValue) external { value = newValue; }
    }
    """
    compiled = solcx.compile_source(source, output_values=["abi", "bin"], solc_version=version)
    contract_data = compiled["<stdin>:EnvironmentCheck"]
    web3 = Web3(EthereumTesterProvider(EthereumTester(backend=PyEVMBackend())))
    account = web3.eth.accounts[0]
    factory = web3.eth.contract(abi=contract_data["abi"], bytecode=contract_data["bin"])
    deployment = web3.eth.wait_for_transaction_receipt(factory.constructor().transact({"from": account}))
    assert deployment.status == 1 and deployment.contractAddress
    contract = web3.eth.contract(address=deployment.contractAddress, abi=contract_data["abi"])
    receipt = web3.eth.wait_for_transaction_receipt(contract.functions.setValue(42).transact({"from": account}))
    assert receipt.status == 1 and contract.functions.value().call() == 42
    print(f"Solidity {version}; Py-EVM backend: OK")
    print(f"Contract: {deployment.contractAddress}")
    print(f"Transaction: {receipt.transactionHash.hex()}; gasUsed: {receipt.gasUsed}")
    print("Contract call and state check: PASS")


if __name__ == "__main__":
    main()
