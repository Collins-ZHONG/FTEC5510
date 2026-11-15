// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/// @notice Test ledger: balances are accounting entries, not transferable tokens.
contract SettlementLedger {
    struct Session { address owner; address gateway; uint256 budget; uint256 spent; }
    address public immutable administrator;
    mapping(address => uint256) public balances;
    mapping(bytes32 => Session) public sessions;
    mapping(bytes32 => mapping(bytes32 => bool)) public processedBatches;

    event SessionCreated(bytes32 indexed sessionId, address indexed owner, address indexed gateway, uint256 budget);
    event PaymentSettled(bytes32 indexed sessionId, bytes32 indexed batchId, address indexed payee, uint256 amount);
    event BatchSettled(bytes32 indexed sessionId, bytes32 indexed batchId, uint256 total, uint256 count);

    constructor() { administrator = msg.sender; }

    function credit(address owner, uint256 amount) external {
        require(msg.sender == administrator, "administrator only");
        require(owner != address(0) && amount > 0, "invalid credit");
        balances[owner] += amount;
    }

    function createSession(bytes32 sessionId, address gateway, uint256 budget) external {
        require(sessionId != bytes32(0) && sessions[sessionId].owner == address(0), "invalid session");
        require(gateway != address(0) && budget > 0, "invalid parameters");
        sessions[sessionId] = Session(msg.sender, gateway, budget, 0);
        emit SessionCreated(sessionId, msg.sender, gateway, budget);
    }

    function settleBatch(bytes32 sessionId, bytes32 batchId, address[] calldata payees, uint256[] calldata amounts) external {
        Session storage session = sessions[sessionId];
        require(session.owner != address(0), "unknown session");
        require(msg.sender == session.gateway, "gateway only");
        require(batchId != bytes32(0) && !processedBatches[sessionId][batchId], "invalid or repeated batch");
        require(payees.length > 0 && payees.length == amounts.length, "invalid payments");
        uint256 total;
        for (uint256 i; i < payees.length; ++i) {
            require(payees[i] != address(0) && amounts[i] > 0, "invalid payment");
            total += amounts[i];
        }
        require(session.spent + total <= session.budget, "budget exceeded");
        require(balances[session.owner] >= total, "insufficient balance");
        processedBatches[sessionId][batchId] = true;
        session.spent += total;
        balances[session.owner] -= total;
        for (uint256 i; i < payees.length; ++i) {
            balances[payees[i]] += amounts[i];
            emit PaymentSettled(sessionId, batchId, payees[i], amounts[i]);
        }
        emit BatchSettled(sessionId, batchId, total, payees.length);
    }
}
