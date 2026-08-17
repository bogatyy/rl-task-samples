#!/usr/bin/env bash
set -uo pipefail

# Run reference solutions serially. The local smoke harness shares ports and
# must not be invoked concurrently.
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
result_root=${REFERENCE_RESULT_DIR:-/tmp/rl-reference-validation}
mode=${1:-pending}
mkdir -p "$result_root"

task_fingerprint() {
  local task_id=$1
  while IFS= read -r file; do
    shasum -a 256 "$file"
  done < <(
    find "$repo_root/tasks/$task_id" "$repo_root/docker/core" -type f
    printf '%s\n' "$repo_root/scripts/smoke_test.sh"
  ) | LC_ALL=C sort \
    | shasum -a 256 | awk '{print $1}'
}

tasks=()
if [[ "$mode" == "pending" || "$mode" == "all" || "$mode" == "failed" ]]; then
  while IFS= read -r task_file; do
    tasks+=("$(basename "$(dirname "$task_file")")")
  done < <(find "$repo_root/tasks" -mindepth 2 -maxdepth 2 -name task.toml | LC_ALL=C sort -r)
else
  tasks=("$@")
  if (( ${#tasks[@]} == 0 )); then
    tasks=("$mode")
  fi
fi

passed=0
failed=0
skipped=0
for task_id in "${tasks[@]}"; do
  task_dir="$repo_root/tasks/$task_id"
  if [[ ! -f "$task_dir/task.toml" ]]; then
    echo "[references] unknown task: $task_id" >&2
    failed=$((failed + 1))
    continue
  fi

  fingerprint=$(task_fingerprint "$task_id")
  state_file="$result_root/$task_id.state"
  prior_fingerprint=""
  prior_result=""
  if [[ -f "$state_file" ]]; then
    read -r prior_fingerprint prior_result < "$state_file" || true
  fi
  if [[ "$mode" == "pending" && "$prior_fingerprint" == "$fingerprint" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$mode" == "failed" && ( "$prior_fingerprint" != "$fingerprint" || "$prior_result" != "fail" ) ]]; then
    skipped=$((skipped + 1))
    continue
  fi

  echo "[references] checking $task_id"
  log_file="$result_root/$task_id.log"
  if SMOKE_TRACE_ON_FAILURE=${SMOKE_TRACE_ON_FAILURE:-0} \
      "$repo_root/scripts/smoke_test.sh" "$task_id" >"$log_file" 2>&1; then
    result=pass
    passed=$((passed + 1))
    echo "[references] passed $task_id"
  else
    result=fail
    failed=$((failed + 1))
    echo "[references] FAILED $task_id (see $log_file)" >&2
  fi
  printf '%s %s\n' "$fingerprint" "$result" >"$state_file.tmp"
  mv "$state_file.tmp" "$state_file"
done

echo "[references] passed=$passed failed=$failed skipped=$skipped"
(( failed == 0 ))
