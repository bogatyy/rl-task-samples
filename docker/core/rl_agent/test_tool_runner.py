from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

from rl_agent.tool_runner import BoundedOutput, run_tool_command


def run(command: str, timeout: float = 10, **kwargs):
    return run_tool_command(command, os.getcwd(), dict(os.environ), timeout, **kwargs)


def alive(pid: int) -> bool:
    """True for a running process; a killed but unreaped zombie counts as dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as stream:
            return stream.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class BoundedOutputTests(unittest.TestCase):
    def test_small_output_is_kept_exactly(self):
        buffer = BoundedOutput(8, 8)
        buffer.add(b"hello ")
        buffer.add(b"world")
        self.assertEqual((buffer.text(), buffer.omitted), ("hello world", 0))

    def test_large_output_keeps_head_and_tail(self):
        buffer = BoundedOutput(4, 4)
        for chunk in (b"abcd", b"efgh", b"ijkl", b"mnop"):
            buffer.add(chunk)
        self.assertEqual(buffer.total, 16)
        self.assertEqual(buffer.omitted, 8)
        self.assertEqual(
            buffer.text(), "abcd\n[... 8 bytes of output omitted by the harness ...]\nmnop"
        )


class RunToolCommandTests(unittest.TestCase):
    def test_output_stderr_and_returncode(self):
        result = run("printf 'out\\n'; printf 'err\\n' >&2; exit 3")
        self.assertEqual(result.output, "out\nerr\n")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.timed_out)

    def test_huge_output_is_bounded(self):
        size = 20_000_000
        result = run(f"head -c {size} /dev/zero | tr '\\0' a", head_bytes=1 << 20, tail_bytes=1 << 20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.total_bytes, size)
        self.assertEqual(result.omitted_bytes, size - (2 << 20))
        self.assertLess(len(result.output), (2 << 20) + 200)
        self.assertIn("bytes of output omitted by the harness", result.output)
        self.assertTrue(result.output.startswith("a") and result.output.endswith("a"))

    def test_timeout_keeps_partial_output_and_kills_the_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = os.path.join(directory, "child.pid")
            started = time.monotonic()
            result = run(f"echo before; sleep 30 & echo $! > {pid_file}; sleep 30", timeout=1)
            self.assertLess(time.monotonic() - started, 8)
            self.assertTrue(result.timed_out)
            self.assertIn("before", result.output)
            with open(pid_file) as stream:
                child = int(stream.read())
            for _ in range(50):
                if not alive(child):
                    break
                time.sleep(0.1)
            else:
                self.fail("background child survived the timeout")

    def test_redirected_background_job_does_not_block(self):
        started = time.monotonic()
        result = run("(sleep 5 >/dev/null 2>&1 &); echo started", timeout=10)
        self.assertEqual(result.output, "started\n")
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(sys.platform.startswith("linux"), "oom_score_adj is Linux-only")
    def test_commands_are_preferred_oom_victims(self):
        self.assertEqual(run("cat /proc/self/oom_score_adj").output.strip(), "1000")
        self.assertNotEqual(
            run("cat /proc/self/oom_score_adj", prefer_oom_kill=False).output.strip(), "1000"
        )


if __name__ == "__main__":
    unittest.main()
