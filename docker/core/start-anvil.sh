#!/usr/bin/env bash
set -euo pipefail

rpc_url=${RPC_URL:-http://127.0.0.1:8545}
archive_url=${ARCHIVE_RPC_URL:-}
anvil_host=${ANVIL_HOST:-127.0.0.1}
chain_id=${CHAIN_ID:-1}
hardfork=${ANVIL_HARDFORK:-osaka}
fork_block=${FORK_BLOCK_NUMBER:?FORK_BLOCK_NUMBER is required}
expected_block=${FORK_EXPECTED_BLOCK_NUMBER:-$fork_block}
replay=${FORK_REPLAY_TRANSACTION_HASHES:-}
timestamp=${FORK_TARGET_TIMESTAMP:-}
storage_patches=${FORK_STORAGE_PATCHES:-}
pid_file=/tmp/rl-task-anvil.pid
log_file=/tmp/rl-task-anvil.log

if [[ "${1:-}" == --restart ]]; then
  if [[ -f "$pid_file" ]]; then
    pid=$(<"$pid_file")
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
fi

if current=$(cast block-number --rpc-url "$rpc_url" 2>/dev/null); then
  [[ "$current" == "$expected_block" ]] && exit 0
  echo "[task] RPC is at block $current, expected $expected_block" >&2
  exit 1
fi

: "${archive_url:?an archive RPC is required}"
anvil --fork-url "$archive_url" --fork-block-number "$fork_block" \
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

if [[ -n "$storage_patches" ]]; then
  IFS=',' read -r -a patches <<< "$storage_patches"
  for patch in "${patches[@]}"; do
    IFS=':' read -r target slot value extra <<< "$patch"
    [[ -n "$target" && -n "$slot" && -n "$value" && -z "${extra:-}" ]] || exit 1
    cast rpc --rpc-url "$rpc_url" anvil_setStorageAt "$target" "$slot" "$value" >/dev/null
    [[ "$(cast storage "$target" "$slot" --rpc-url "$rpc_url")" == "$value" ]] || exit 1
  done
fi

if [[ -n "$replay" ]]; then
  cast rpc --rpc-url "$rpc_url" evm_setAutomine false >/dev/null
  IFS=',' read -r -a hashes <<< "$replay"
  for hash in "${hashes[@]}"; do
    raw=$(cast rpc --rpc-url "$archive_url" eth_getRawTransactionByHash "$hash")
    [[ -n "$raw" && "$raw" != null ]] || exit 1
    cast rpc --rpc-url "$rpc_url" eth_sendRawTransaction "$raw" >/dev/null
  done
fi

if [[ -n "$timestamp" ]]; then
  cast rpc --rpc-url "$rpc_url" evm_setNextBlockTimestamp "$timestamp" >/dev/null
fi

if [[ -n "$replay" || -n "$timestamp" ]]; then
  cast rpc --rpc-url "$rpc_url" evm_mine >/dev/null
fi
[[ -z "$replay" ]] || cast rpc --rpc-url "$rpc_url" evm_setAutomine true >/dev/null

current=$(cast block-number --rpc-url "$rpc_url")
[[ "$current" == "$expected_block" ]] || {
  echo "[task] RPC is at block $current, expected $expected_block" >&2
  exit 1
}
echo "[task] Anvil ready at block $current"
