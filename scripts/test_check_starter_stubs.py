#!/usr/bin/env python3
"""Regression tests for the agent-visible starter-code guard."""

from __future__ import annotations

import unittest

from check_starter_stubs import validate_stub


class StarterStubTests(unittest.TestCase):
    def test_noop_variants(self) -> None:
        for body in (
            "function execute() external {}",
            "constructor() payable {} receive() external payable {} function execute() external {}",
            "constructor() payable {} function execute() external {\n// Write your exploit here.\n } receive() external payable {}",
        ):
            source = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.28;\ncontract Exploit {" + body + "}"
            with self.subTest(body=body):
                self.assertIsNone(validate_stub(source))

    def test_reference_code_is_rejected(self) -> None:
        source = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.28; contract Exploit { function execute() external { payable(msg.sender).transfer(1); } }"
        self.assertIsNotNone(validate_stub(source))

    def test_solution_hint_comment_is_rejected(self) -> None:
        source = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.28; contract Exploit { function execute() external {} }\n// Call withdraw(42)"
        self.assertIsNotNone(validate_stub(source))

    def test_extra_helper_is_rejected(self) -> None:
        source = "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.28; contract Exploit { function execute() external {} function drain() external {} }"
        self.assertIsNotNone(validate_stub(source))


if __name__ == "__main__":
    unittest.main()
