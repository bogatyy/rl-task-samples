#!/usr/bin/env bash
set -euo pipefail

rpc_url=${RPC_URL:-http://127.0.0.1:8545}
archive_url=${ARCHIVE_RPC_URL:-}
anvil_host=${ANVIL_HOST:-127.0.0.1}
chain_id=${CHAIN_ID:-1}
hardfork=${ANVIL_HARDFORK:-osaka}
fork_block=${FORK_BLOCK_NUMBER:?FORK_BLOCK_NUMBER is required}
expected_block=${FORK_EXPECTED_BLOCK_NUMBER:-$fork_block}
timestamp=${FORK_TARGET_TIMESTAMP:-}
pid_file=/tmp/rl-task-anvil.pid
log_file=/tmp/rl-task-anvil.log
relay_pid_file=/tmp/rl-task-archive-relay.pid
relay_log_file=/tmp/rl-task-archive-relay.log
relay_port_file=/tmp/rl-task-archive-relay.port
relay_pid=""
pid=""
keep_children=0

cleanup_failed_start() {
  if (( keep_children == 0 )); then
    [[ -z "$pid" ]] || kill "$pid" 2>/dev/null || true
    [[ -z "$relay_pid" ]] || kill "$relay_pid" 2>/dev/null || true
  fi
}
trap cleanup_failed_start EXIT

if [[ "${1:-}" == --restart ]]; then
  if [[ -f "$pid_file" ]]; then
    pid=$(<"$pid_file")
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  if [[ -f "$relay_pid_file" ]]; then
    relay_pid=$(<"$relay_pid_file")
    kill "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" 2>/dev/null || true
    relay_pid=""
  fi
  rm -f "$pid_file" "$relay_pid_file" "$relay_port_file"
fi

if current=$(cast block-number --rpc-url "$rpc_url" 2>/dev/null); then
  [[ "$current" == "$expected_block" ]] && exit 0
  echo "[task] RPC is at block $current, expected $expected_block" >&2
  exit 1
fi

: "${archive_url:?an archive RPC is required}"
rm -f "$relay_port_file"
printf '%s\n' "$archive_url" | ARCHIVE_RELAY_PORT=8546 \
  ARCHIVE_RELAY_PORT_FILE="$relay_port_file" \
  python3 /usr/local/lib/rl-task/archive_relay.py >"$relay_log_file" 2>&1 &
relay_pid=$!
echo "$relay_pid" > "$relay_pid_file"
for _ in $(seq 1 120); do
  [[ -s "$relay_port_file" ]] && break
  kill -0 "$relay_pid" 2>/dev/null || {
    echo "[task] archive relay failed to start" >&2
    exit 1
  }
  sleep 0.1
done
[[ "$(<"$relay_port_file")" == 8546 ]]

unset ARCHIVE_RPC_URL
archive_url=""
anvil --fork-url http://127.0.0.1:8546 --fork-block-number "$fork_block" \
  --chain-id "$chain_id" --host "$anvil_host" --port 8545 --hardfork "$hardfork" \
  --base-fee 0 --gas-price 0 --gas-limit 100000000 --silent \
  >"$log_file" 2>&1 &
pid=$!
echo "$pid" > "$pid_file"

for _ in $(seq 1 120); do
  current=$(cast block-number --rpc-url "$rpc_url" 2>/dev/null || true)
  [[ "$current" == "$fork_block" ]] && break
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[task] Anvil failed to start; backend details were withheld to protect the archive credential" >&2
    exit 1
  fi
  sleep 0.25
done
[[ "$current" == "$fork_block" ]] || exit 1

if [[ -n "$timestamp" ]]; then
  cast rpc --rpc-url "$rpc_url" evm_setNextBlockTimestamp "$timestamp" >/dev/null
  cast rpc --rpc-url "$rpc_url" evm_mine >/dev/null
fi

current=$(cast block-number --rpc-url "$rpc_url")
[[ "$current" == "$expected_block" ]] || {
  echo "[task] RPC is at block $current, expected $expected_block" >&2
  exit 1
}
echo "[task] Anvil ready at block $current"
keep_children=1
