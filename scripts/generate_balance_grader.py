#!/usr/bin/env python3
"""Generate the common same-asset outflow grader for a reviewed task.

This is an authoring helper only. It deliberately accepts an explicit token and
protected source; choosing those values still requires incident-specific review.
"""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--chain-id", required=True, type=int)
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--minimum", required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--starting-value", default="1 ether")
    parser.add_argument("--receipt-minimum")
    parser.add_argument("--fund-exploit", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    path = ROOT / "tasks" / args.task_id / "tests" / "ExploitGrader.sol"
    if path.exists() and not args.overwrite:
        raise SystemExit(f"refusing to overwrite {path}")

    receipt_minimum = args.receipt_minimum or args.minimum
    deployment = "new Exploit{value: msg.value}()" if args.fund_exploit else "new Exploit()"
    source_message = f"source did not lose {args.asset}"
    receipt_message = f"Exploit did not receive {args.asset}"
    path.write_text(
        f'''// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {{Exploit}} from "./Exploit.sol";
import {{IERC20}} from "./interfaces/IERC20.sol";

contract ExploitGrader {{
    IERC20 private constant ASSET = IERC20({args.token});
    address private constant SOURCE = {args.source};
    uint256 private constant MINIMUM_OUTFLOW = {args.minimum};
    uint256 private constant MINIMUM_RECEIPT = {receipt_minimum};
    uint256 private immutable SOURCE_BEFORE_DEPLOYMENT;
    Exploit private immutable EXPLOIT;
    bool public passed;

    constructor() payable {{
        require(block.chainid == {args.chain_id}, "wrong chain");
        require(block.number >= {args.block:_} && block.number <= {args.block + 4:_}, "wrong fork");
        require(msg.value == {args.starting_value}, "wrong starting value");
        SOURCE_BEFORE_DEPLOYMENT = ASSET.balanceOf(SOURCE);
        EXPLOIT = {deployment};
    }}

    function grade() external {{
        require(!passed, "already graded");
        uint256 exploitBefore = ASSET.balanceOf(address(EXPLOIT));
        EXPLOIT.execute();
        require(SOURCE_BEFORE_DEPLOYMENT >= ASSET.balanceOf(SOURCE) + MINIMUM_OUTFLOW, "{source_message}");
        require(ASSET.balanceOf(address(EXPLOIT)) >= exploitBefore + MINIMUM_RECEIPT, "{receipt_message}");
        passed = true;
    }}
}}
'''
    )


if __name__ == "__main__":
    main()
