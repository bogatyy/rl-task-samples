#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
requested=${1:-all}
exploit_override=${SMOKE_EXPLOIT_PATH:-}
# Independent smoke invocations must not share a fork. Use a process-scoped
# default range; callers that intentionally coordinate ports may still set
# SMOKE_PORT_BASE explicitly.
next_port=${SMOKE_PORT_BASE:-$((20000 + ($$ % 30000)))}
rpc_url="http://127.0.0.1:$((next_port + 1))"
private_key=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
anvil_pid=""
archive_relay_pid=""

tasks=()
for task_file in "$repo_root"/tasks/*/task.toml; do
  tasks+=("$(basename "$(dirname "$task_file")")")
done

source_env_file() {
  local env_file=$1
  if [[ $- == *x* ]]; then
    set +x
    source "$env_file"
  else
    source "$env_file"
  fi
}

if [[ -f "$repo_root/.env" ]]; then
  source_env_file "$repo_root/.env"
elif [[ -f "$repo_root/../env.sh" ]]; then
  source_env_file "$repo_root/../env.sh"
fi
if [[ "$requested" != "all" ]]; then
  valid=0
  for task_id in "${tasks[@]}"; do [[ "$requested" == "$task_id" ]] && valid=1; done
  (( valid == 1 )) || { echo "unknown smoke task: $requested" >&2; exit 2; }
fi

task_chain() {
  awk -F'"' '/^chain = / { print $2; exit }' "$repo_root/tasks/$1/task.toml"
}

rpc_variable_for_chain() {
  case "$1" in
    ethereum) echo ETH_RPC_URL ;;
    bsc) echo BSC_RPC_URL ;;
    arbitrum) echo ARBITRUM_RPC_URL ;;
    base) echo BASE_RPC_URL ;;
    polygon) echo POLYGON_RPC_URL ;;
    avalanche) echo AVALANCHE_RPC_URL ;;
    optimism) echo OPTIMISM_RPC_URL ;;
    linea) echo LINEA_RPC_URL ;;
    blast) echo BLAST_RPC_URL ;;
    gnosis) echo GNOSIS_RPC_URL ;;
    mantle) echo MANTLE_RPC_URL ;;
    sei) echo SEI_RPC_URL ;;
    *) echo "unsupported task chain: $1" >&2; return 2 ;;
  esac
}

alchemy_network_for_chain() {
  case "$1" in
    ethereum) echo eth-mainnet ;;
    bsc) echo bnb-mainnet ;;
    arbitrum) echo arb-mainnet ;;
    base) echo base-mainnet ;;
    polygon) echo polygon-mainnet ;;
    avalanche) echo avax-mainnet ;;
    optimism) echo opt-mainnet ;;
    linea) echo linea-mainnet ;;
    blast) echo blast-mainnet ;;
    gnosis) echo gnosis-mainnet ;;
    mantle) echo mantle-mainnet ;;
    sei) echo sei-mainnet ;;
    *) echo "unsupported task chain: $1" >&2; return 2 ;;
  esac
}

archive_rpc_for_task() {
  local task_id=$1 chain rpc_variable alchemy_network
  chain=$(task_chain "$task_id")
  rpc_variable=$(rpc_variable_for_chain "$chain")
  if [[ -n "${ALCHEMY_API_KEY:-}" ]]; then
    alchemy_network=$(alchemy_network_for_chain "$chain")
    printf 'https://%s.g.alchemy.com/v2/%s' "$alchemy_network" "$ALCHEMY_API_KEY"
    return
  fi
  [[ -n "${!rpc_variable:-}" ]] || {
    echo "set ALCHEMY_API_KEY or $rpc_variable for $chain archive access" >&2
    return 2
  }
  printf '%s' "${!rpc_variable}"
}

task_environment_value() {
  local task_id=$1 key=$2
  awk -v key="$key" '
    $1 == "ENV" && index($2, key "=") == 1 {
      sub("^" key "=", "", $2)
      print $2
    }
  ' "$repo_root/tasks/$task_id/environment/Dockerfile" | tail -1
}

task_verifier_value() {
  local task_id=$1 key=$2
  awk -F'"' -v key="$key" '
    /^\[verifier\.env\]$/ { in_section=1; next }
    in_section && /^\[/ { exit }
    in_section && $1 ~ "^" key "[[:space:]]*=[[:space:]]*$" { print $2; exit }
  ' "$repo_root/tasks/$task_id/task.toml"
}

smoke_root=$(mktemp -d /tmp/rl-smoke.XXXXXX)
stop_anvil() {
  if [[ -n "$anvil_pid" ]]; then
    kill "$anvil_pid" 2>/dev/null || true
    wait "$anvil_pid" 2>/dev/null || true
    anvil_pid=""
  fi
}
stop_archive_relay() {
  if [[ -n "$archive_relay_pid" ]]; then
    kill "$archive_relay_pid" 2>/dev/null || true
    wait "$archive_relay_pid" 2>/dev/null || true
    archive_relay_pid=""
  fi
}
cleanup() {
  stop_anvil
  stop_archive_relay
  if [[ "${SMOKE_KEEP_TMP:-0}" == 1 ]]; then
    echo "[smoke] kept diagnostics at $smoke_root" >&2
  else
    case "$smoke_root" in /tmp/rl-smoke.*) rm -rf -- "$smoke_root" ;; esac
  fi
}
trap cleanup EXIT
terminate() {
  trap - EXIT HUP INT TERM
  cleanup
  exit 143
}
trap terminate HUP INT TERM

prepare_project() {
  local task_id=$1
  local task_dir="$repo_root/tasks/$task_id"
  local project_dir="$smoke_root/$task_id"
  local exploit_source="$task_dir/solution/Exploit.sol"
  if [[ -n "$exploit_override" ]]; then
    exploit_source=$exploit_override
  fi
  [[ -f "$exploit_source" ]]
  mkdir -p "$project_dir"
  cp -R "$repo_root/docker/core/project/." "$project_dir/"
  cp -R "$task_dir/environment/project/." "$project_dir/"
  cp "$exploit_source" "$project_dir/src/Exploit.sol"
  cp "$task_dir/tests/ExploitGrader.sol" "$project_dir/src/ExploitGrader.sol"
  forge clean --root "$project_dir"
  forge build --root "$project_dir" --offline >/dev/null
  printf '%s\n' "$project_dir"
}

start_fork() {
  local base_block=$1
  local expected_block=$2
  local target_timestamp=${3:-}
  local archive_url=${4:-${ETH_RPC_URL:-}}
  local chain_id=${5:-1}
  local hardfork=${6:-osaka}
  [[ -n "$archive_url" ]]
  stop_anvil
  stop_archive_relay
  next_port=$((next_port + 1))
  rpc_url="http://127.0.0.1:$next_port"

  local relay_port_file="$smoke_root/archive-relay.port"
  local relay_port
  rm -f "$relay_port_file"
  printf '%s\n' "$archive_url" | ARCHIVE_RELAY_PORT=0 \
    ARCHIVE_RELAY_PORT_FILE="$relay_port_file" \
    python3 "$repo_root/docker/core/archive_relay.py" \
    >"$smoke_root/archive-relay.log" 2>&1 &
  archive_relay_pid=$!
  for _ in $(seq 1 120); do
    [[ -s "$relay_port_file" ]] && break
    if ! kill -0 "$archive_relay_pid" 2>/dev/null; then
      echo "[smoke] archive relay failed to start" >&2
      return 1
    fi
    sleep 0.1
  done
  relay_port=$(<"$relay_port_file")
  [[ "$relay_port" =~ ^[0-9]+$ ]]
  archive_url=""

  anvil --fork-url "http://127.0.0.1:$relay_port" --fork-block-number "$base_block" \
    --chain-id "$chain_id" --host 127.0.0.1 --port "$next_port" --hardfork "$hardfork" \
    --base-fee 0 --gas-price 0 --gas-limit 100000000 --silent \
    >"$smoke_root/anvil.log" 2>&1 &
  anvil_pid=$!

  for _ in $(seq 1 120); do
    cast block-number --rpc-url "$rpc_url" >/dev/null 2>&1 && break
    if ! kill -0 "$anvil_pid" 2>/dev/null; then
      echo "[smoke] Anvil failed to start; backend details were withheld to protect the archive credential" >&2
      return 1
    fi
    sleep 0.25
  done

  current=$(cast block-number --rpc-url "$rpc_url")
  [[ "$current" == "$base_block" ]] || {
    echo "expected base block $base_block, got $current" >&2
    return 1
  }

  if [[ -n "$target_timestamp" ]]; then
    cast rpc --rpc-url "$rpc_url" evm_setNextBlockTimestamp "$target_timestamp" >/dev/null
    cast rpc --rpc-url "$rpc_url" evm_mine >/dev/null
  fi

  current=$(cast block-number --rpc-url "$rpc_url")
  [[ "$current" == "$expected_block" ]] || {
    echo "expected fork block $expected_block, got $current" >&2
    return 1
  }
}

run_grade_script() {
  local task_id=$1 project_dir=$2 starting_value deployment grader passed_value grade_timeout
  starting_value=$(task_verifier_value "$task_id" GRADER_STARTING_VALUE)
  starting_value=${starting_value:-1ether}
  grade_timeout=${SMOKE_GRADE_TIMEOUT:-300}
  deployment=$(cd "$project_dir" && forge create src/ExploitGrader.sol:ExploitGrader \
    --offline --broadcast --rpc-url "$rpc_url" --private-key "$private_key" \
    --value "$starting_value" --gas-limit 100000000 \
    --legacy --gas-price 0 -vv)
  printf '%s\n' "$deployment"
  grader=$(printf '%s\n' "$deployment" | awk '/Deployed to:/ {print $3}' | tail -1)
  [[ "$grader" =~ ^0x[0-9a-fA-F]{40}$ ]]
  if ! cast send "$grader" 'grade()' --rpc-url "$rpc_url" --private-key "$private_key" \
    --gas-limit 100000000 --legacy --gas-price 0 --timeout "$grade_timeout" >/dev/null; then
    passed_value=$(cast call "$grader" 'passed()(bool)' --rpc-url "$rpc_url" 2>/dev/null | awk '{print $1}')
    if [[ "$passed_value" == true ]]; then
      return 0
    fi
    echo "[smoke] grade transaction failed" >&2
    cast call "$grader" 'grade()' --from 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 \
      --rpc-url "$rpc_url" --gas-limit 100000000 >&2 2>&1 || true
    if [[ "${SMOKE_TRACE_ON_FAILURE:-1}" == 1 ]]; then
      echo "[smoke] local trace follows" >&2
      cast call "$grader" 'grade()' --from 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 \
        --rpc-url "$rpc_url" --gas-limit 100000000 --trace >&2 2>&1 || true
    fi
    return 1
  fi
  passed_value=$(cast call "$grader" 'passed()(bool)' --rpc-url "$rpc_url" | awk '{print $1}')
  if [[ "$passed_value" != true ]]; then
    echo "[smoke] grader did not pass" >&2
    cast call "$grader" 'grade()' --from 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 \
      --rpc-url "$rpc_url" --gas-limit 100000000 >&2 2>&1 || true
    if [[ "${SMOKE_TRACE_ON_FAILURE:-1}" == 1 ]]; then
      echo "[smoke] local trace follows" >&2
      cast call "$grader" 'grade()' --from 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266 \
        --rpc-url "$rpc_url" --gas-limit 100000000 --trace >&2 2>&1 || true
    fi
    return 1
  fi
}

start_task_fork() {
  local task_id=$1 base expected timestamp archive chain_id hardfork
  base=$(task_environment_value "$task_id" FORK_BLOCK_NUMBER)
  [[ -n "$base" ]] || { echo "$task_id has no fork block" >&2; return 2; }
  expected=$(task_environment_value "$task_id" FORK_EXPECTED_BLOCK_NUMBER)
  expected=${expected:-$base}
  timestamp=$(task_environment_value "$task_id" FORK_TARGET_TIMESTAMP)
  chain_id=$(task_environment_value "$task_id" CHAIN_ID)
  chain_id=${chain_id:-1}
  hardfork=$(task_environment_value "$task_id" ANVIL_HARDFORK)
  hardfork=${hardfork:-osaka}
  archive=$(archive_rpc_for_task "$task_id")
  start_fork "$base" "$expected" "$timestamp" "$archive" "$chain_id" "$hardfork"
}

run_generic() {
  local task_id=$1 project_dir
  project_dir=$(prepare_project "$task_id")
  start_task_fork "$task_id"
  run_grade_script "$task_id" "$project_dir"
  echo "[smoke] $task_id passed"
}

for task_id in "${tasks[@]}"; do
  if [[ "$requested" == "all" || "$requested" == "$task_id" ]]; then
    run_generic "$task_id"
  fi
done

echo "[smoke] requested reference controls passed"
