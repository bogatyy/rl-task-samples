#!/usr/bin/env bash
set -euo pipefail

if getent hosts pier-egress-proxy >/dev/null 2>&1; then
  agent_bin=/root/.local/bin/mini-swe-agent
  real_agent_bin=/root/.local/bin/mini-swe-agent.real
  if [[ -e "$agent_bin" && ! -e "$real_agent_bin" ]]; then
    mv "$agent_bin" "$real_agent_bin"
    ln -s /usr/local/bin/mini-swe-agent-wrapper "$agent_bin"
  fi
elif [[ -n "${ARCHIVE_RPC_URL:-}" ]]; then
  start-anvil
else
  echo "[task] archive RPC is not configured" >&2
fi

if [[ "${KEEP_ARCHIVE_RPC:-0}" != 1 ]]; then
  unset ARCHIVE_RPC_URL
fi

exec "$@"
