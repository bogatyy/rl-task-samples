"""Run one model-issued shell command with bounded output capture.

The agent controller holds each command's output in memory. One command that
prints hundreds of megabytes (a recursive grep over large single-line dumps, a
decode loop over a misread length) could push the controller past the
container's memory limit, and the kernel then killed the controller itself
(exit 137), ending the trial. This runner:

* keeps at most HEAD_BYTES from the start and TAIL_BYTES from the end of the
  combined stdout/stderr, and says how many bytes it dropped in between;
* kills the command's whole process group on timeout;
* marks the command's processes as the kernel's preferred out-of-memory victims
  (oom_score_adj=1000), so a runaway tool is killed instead of the agent.

Standard library only, so it can be tested without mini-swe-agent installed.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass


HEAD_BYTES = 1 << 20
TAIL_BYTES = 1 << 20
# Raising one's own oom_score_adj needs no privileges. /bin/sh applies it to
# itself and the command it execs inherits it. Harmless where /proc is absent.
OOM_PREFERENCE_PREFIX = "{ echo 1000 > /proc/self/oom_score_adj; } 2>/dev/null; "


@dataclass
class ToolResult:
    output: str
    returncode: int | None
    timed_out: bool
    total_bytes: int
    omitted_bytes: int


class BoundedOutput:
    """Keep the first and last bytes of a stream; count everything."""

    def __init__(self, head_bytes: int = HEAD_BYTES, tail_bytes: int = TAIL_BYTES):
        self.head_limit = head_bytes
        self.tail_limit = tail_bytes
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            excess = len(self.tail) - self.tail_limit
            if excess > 0:
                del self.tail[:excess]

    @property
    def omitted(self) -> int:
        return max(0, self.total - len(self.head) - len(self.tail))

    def text(self) -> str:
        if self.omitted == 0:
            data = bytes(self.head + self.tail)
        else:
            marker = f"\n[... {self.omitted} bytes of output omitted by the harness ...]\n"
            data = bytes(self.head) + marker.encode() + bytes(self.tail)
        return data.decode("utf-8", errors="replace")


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_tool_command(
    command: str,
    cwd: str,
    env: dict[str, str] | None,
    timeout: float,
    *,
    head_bytes: int = HEAD_BYTES,
    tail_bytes: int = TAIL_BYTES,
    prefer_oom_kill: bool = True,
) -> ToolResult:
    """Run `command` through /bin/sh, like subprocess.run(shell=True) would."""
    shell_command = (OOM_PREFERENCE_PREFIX + command) if prefer_oom_kill else command
    process = subprocess.Popen(
        shell_command,
        shell=True,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    output = BoundedOutput(head_bytes, tail_bytes)
    deadline = time.monotonic() + timeout
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    timed_out = False
    try:
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for _key, _events in selector.select(timeout=min(remaining, 0.5)):
                try:
                    chunk = os.read(descriptor, 1 << 16)
                except BlockingIOError:
                    continue
                if not chunk:
                    eof = True
                    break
                output.add(chunk)
        if not timed_out:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if timed_out:
            _kill_group(process)
        selector.close()
        process.stdout.close()
    return ToolResult(
        output=output.text(),
        returncode=process.returncode,
        timed_out=timed_out,
        total_bytes=output.total,
        omitted_bytes=output.omitted,
    )
