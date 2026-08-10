// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ExploitGrader} from "../src/ExploitGrader.sol";

interface Vm {
    function startBroadcast() external;
    function stopBroadcast() external;
}

contract GradeScript {
    Vm private constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function run() external {
        VM.startBroadcast();
        ExploitGrader grader = new ExploitGrader{value: 1 ether}();
        grader.grade();
        VM.stopBroadcast();
        require(grader.passed(), "grade failed");
    }
}
