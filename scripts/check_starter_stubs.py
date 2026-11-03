#!/usr/bin/env python3
"""Reject task images that give the agent anything but an empty Exploit stub."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_COMMENTS = {
    "// SPDX-License-Identifier: MIT",
    "// SPDX-License-Identifier: UNLICENSED",
    "// Write your exploit here.",
}


def compact(source: str) -> str:
    return re.sub(r"\s+", "", source)


PREFIX = "pragma solidity ^0.8.28; contract Exploit {"
SUFFIX = "}"
EMPTY_MEMBERS = {
    "function execute() external {}",
    "constructor() payable {} function execute() external {}",
    "receive() external payable {} function execute() external {}",
    "constructor() payable {} receive() external payable {} function execute() external {}",
    "constructor() payable {} function execute() external {} receive() external payable {}",
}
ALLOWED_CODE = {compact(PREFIX + members + SUFFIX) for members in EMPTY_MEMBERS}


def validate_stub(source: str) -> str | None:
    lines = source.splitlines()
    if not lines or lines[0].strip() not in ALLOWED_COMMENTS - {"// Write your exploit here."}:
        return "missing or unexpected SPDX header"
    code_lines = []
    for line in lines:
        if "/*" in line or "*/" in line:
            return "block comments are not allowed in the starter"
        if "//" in line:
            if line.strip() not in ALLOWED_COMMENTS:
                return "unexpected starter comment"
            continue
        code_lines.append(line)
    if compact("\n".join(code_lines)) not in ALLOWED_CODE:
        return "starter contains code beyond an empty Exploit.execute()"
    return None


def check_tasks(root: Path, task_id: str | None = None) -> list[str]:
    task_root = root / "tasks"
    task_ids = [task_id] if task_id else sorted(path.parent.name for path in task_root.glob("*/task.toml"))
    if not task_ids:
        return ["no task.toml files found"]
    errors = []
    for name in task_ids:
        task = task_root / name
        if not (task / "task.toml").is_file():
            errors.append(f"{name}: task.toml is missing")
            continue
        starter = task / "environment/project/src/Exploit.sol"
        if not starter.is_file():
            errors.append(f"{name}: agent-visible Exploit.sol is missing")
            continue
        problem = validate_stub(starter.read_text(encoding="utf-8"))
        if problem:
            errors.append(f"{name}: {problem}")
    if not task_id:
        listed = set(task_ids)
        for starter in task_root.glob("*/environment/project/src/Exploit.sol"):
            if starter.parents[3].name not in listed:
                errors.append(f"{starter.parents[3].name}: starter exists without task.toml")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="check only this task ID")
    args = parser.parse_args()
    errors = check_tasks(ROOT, args.task)
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print(f"validated empty Exploit.sol starters for {args.task or 'all tasks'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
