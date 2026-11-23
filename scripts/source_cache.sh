#!/usr/bin/env bash
# Run the shared, persistent Etherscan source cache that every trial reads through.
#
#   scripts/source_cache.sh up       build images, create the network, (re)start the cache
#   scripts/source_cache.sh status   health and hit/miss counters
#   scripts/source_cache.sh logs     follow upstream fetches and errors
#   scripts/source_cache.sh down     stop the container (cached files stay on disk)
#
# `up` is idempotent and cheap when nothing changed, so launchers call it first.
# Cached source lives on the host in $RL_SOURCE_CACHE_DIR and survives container
# restarts, image rebuilds and reboots. The container uses --restart unless-stopped,
# so it comes back whenever Docker starts.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cache_dir=${RL_SOURCE_CACHE_DIR:-$HOME/Code/etherscan-cache}
network=rl-shared
container=rl-source-cache
cache_image=rl-source-cache:v1
gateway_image=rl-policy-gateway:v2
host_port=${RL_SOURCE_CACHE_PORT:-8090}

load_key() {
  if [[ -z "${ETHERSCAN_API_KEY:-}" ]]; then
    local env_file
    for env_file in "$repo_root/.env" "$repo_root/../env.sh"; do
      if [[ -f "$env_file" ]]; then
        if [[ $- == *x* ]]; then set +x; fi
        # shellcheck disable=SC1090
        source "$env_file" >/dev/null 2>&1
        break
      fi
    done
  fi
  : "${ETHERSCAN_API_KEY:?set ETHERSCAN_API_KEY (or add it to .env / ../env.sh)}"
  export ETHERSCAN_API_KEY
}

wait_healthy() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$host_port/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[source-cache] not healthy after 30s; see: docker logs $container" >&2
  return 1
}

# With Docker's containerd image store, every build gets a fresh image ID even
# when nothing changed, so IDs cannot tell whether to rebuild or restart. A hash
# of each image's inputs, stored as a label, can; `up` then leaves a running
# cache alone, so launchers can call it while other trials are reading source.
inputs_hash() { cat "$@" | shasum -a 256 | cut -c1-16; }

ensure_image() {
  local image=$1 dockerfile=$2 context=$3 hash=$4 have
  have=$(docker image inspect -f '{{index .Config.Labels "rl.inputs"}}' "$image" 2>/dev/null || true)
  [[ "$have" == "$hash" ]] && return 0
  docker build -q --label "rl.inputs=$hash" -t "$image" -f "$dockerfile" "$context" >/dev/null
}

up() {
  load_key
  mkdir -p "$cache_dir"
  local cache_hash gateway_hash
  cache_hash=$(inputs_hash "$repo_root/docker/source-cache/Dockerfile" \
    "$repo_root/docker/source-cache/source_cache.py")
  gateway_hash=$(inputs_hash "$repo_root/docker/core/policy-gateway.Dockerfile" \
    "$repo_root/docker/core/policy_gateway.py")
  ensure_image "$cache_image" "$repo_root/docker/source-cache/Dockerfile" \
    "$repo_root/docker/source-cache" "$cache_hash"
  ensure_image "$gateway_image" "$repo_root/docker/core/policy-gateway.Dockerfile" \
    "$repo_root/docker/core" "$gateway_hash"
  if ! docker network inspect "$network" >/dev/null 2>&1; then
    # Internal: sidecars reach the cache, but nothing gets internet through it.
    docker network create --internal "$network" >/dev/null
  fi

  # Restart only when the image inputs or the run settings changed.
  local rate=${RL_SOURCE_UPSTREAM_RATE:-2.5} ttl=${RL_SOURCE_NOT_VERIFIED_TTL_DAYS:-30}
  local want running
  want="$cache_hash rate=$rate ttl=$ttl port=$host_port dir=$cache_dir"
  running=$(docker inspect -f '{{if .State.Running}}{{index .Config.Labels "rl.config"}}{{end}}' \
    "$container" 2>/dev/null || true)
  if [[ "$running" != "$want" ]]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
    # The key is passed by name, so it never appears on a command line.
    docker run -d --name "$container" \
      --restart unless-stopped \
      --network bridge \
      -p "127.0.0.1:$host_port:8080" \
      -v "$cache_dir:/cache" \
      -e ETHERSCAN_API_KEY \
      -e "UPSTREAM_RATE_PER_SECOND=$rate" \
      -e "NOT_VERIFIED_TTL_DAYS=$ttl" \
      --label "rl.config=$want" \
      --read-only --cap-drop ALL --security-opt no-new-privileges:true \
      --memory 512m --pids-limit 256 \
      "$cache_image" >/dev/null
    docker network connect "$network" "$container"
    echo "[source-cache] started $container ($cache_image) with $cache_dir"
  fi
  wait_healthy
  echo "[source-cache] healthy at http://127.0.0.1:$host_port (trials use http://$container:8080 on $network)"
}

status() {
  docker ps --filter "name=^${container}$" --format '{{.Names}}  {{.Status}}  {{.Image}}'
  curl -fsS "http://127.0.0.1:$host_port/v1/stats"
  echo
  local entries
  entries=$( (find "$cache_dir/v1" -name response.json 2>/dev/null || true) | wc -l | tr -d ' ')
  echo "[source-cache] $entries cached contracts in $cache_dir"
}

case "${1:-up}" in
  up) up ;;
  status) status ;;
  logs) docker logs -f "$container" ;;
  down) docker rm -f "$container" >/dev/null && echo "[source-cache] stopped; files kept in $cache_dir" ;;
  *) echo "usage: $0 {up|status|logs|down}" >&2; exit 2 ;;
esac
