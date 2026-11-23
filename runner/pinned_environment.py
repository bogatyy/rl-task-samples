"""Pier Docker environment with isolated source and pinned-fork services."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pier.environments.docker.docker import DockerEnvironment


POLICY_SERVICE = "pier-policy"
ANVIL_SERVICE = "pier-anvil"
AGENT_NETWORK = "pier-egress-internal"
BACKEND_NETWORK = "pier-chain-backend"
# Shared, host-wide services (docker/source-cache, started by
# scripts/source_cache.sh). Only the policy sidecar joins this network.
SOURCE_NETWORK = "rl-shared"
SOURCE_UPSTREAM_URL = "http://rl-source-cache:8080"
POLICY_IMAGE = "rl-policy-gateway:v2"


def add_policy_services(
    compose: dict[str, Any],
    *,
    image: str,
    archive_rpc: str,
    fork_block: str,
    policy_image: str = POLICY_IMAGE,
    source_upstream_url: str = SOURCE_UPSTREAM_URL,
    source_upstream_token: str = "",
    source_network: str = SOURCE_NETWORK,
    expected_block: str | None = None,
    target_timestamp: str | None = None,
    chain_id: str = "1",
    hardfork: str = "osaka",
) -> dict[str, Any]:
    """Add services while keeping archive Anvil off the agent-facing network."""
    services = compose.setdefault("services", {})
    networks = compose.setdefault("networks", {})
    networks.setdefault(AGENT_NETWORK, {"internal": True})
    networks[BACKEND_NETWORK] = {"internal": True}
    if source_network:
        networks[source_network] = {"external": True, "name": source_network}

    main = services.setdefault("main", {})
    # Pier also adds deploy.resource limits; Compose rejects a separate
    # pids_limit when those two settings disagree. Keep the process cap in
    # the same resource-limit block instead.
    main.pop("pids_limit", None)
    main.setdefault("deploy", {}).setdefault("resources", {}).setdefault("limits", {})[
        "pids"
    ] = 512
    main["cap_drop"] = ["ALL"]
    # The trusted root controller needs to read model-owned submissions and
    # atomically write Harbor bind mounts before dropping every capability for
    # model-issued commands.
    main["cap_add"] = [
        "CHOWN",
        "DAC_OVERRIDE",
        "KILL",
        "SETGID",
        "SETPCAP",
        "SETUID",
    ]
    main["security_opt"] = ["no-new-privileges:true"]
    # Replace, rather than extend, inherited membership. Any shared fallback
    # network would let the model bypass the policy gateway and reach Anvil.
    main["networks"] = [AGENT_NETWORK]
    main.setdefault("depends_on", {})[POLICY_SERVICE] = {
        "condition": "service_healthy"
    }

    anvil_environment = {
        "ANVIL_HOST": "0.0.0.0",
        "KEEP_ARCHIVE_RPC": "1",
        "RPC_URL": "http://127.0.0.1:8545",
        "ARCHIVE_RPC_URL": archive_rpc,
        "CHAIN_ID": chain_id,
        "ANVIL_HARDFORK": hardfork,
        "FORK_BLOCK_NUMBER": fork_block,
    }
    if expected_block is not None:
        anvil_environment["FORK_EXPECTED_BLOCK_NUMBER"] = expected_block
    if target_timestamp is not None:
        anvil_environment["FORK_TARGET_TIMESTAMP"] = target_timestamp

    services[ANVIL_SERVICE] = {
        "image": image,
        "user": "10001:10001",
        "cpus": 2.0,
        "mem_limit": "4096m",
        "pids_limit": 256,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "entrypoint": [
            "/bin/bash",
            "-lc",
            "start-anvil && exec tail -f /dev/null",
        ],
        "environment": anvil_environment,
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "cast block-number --rpc-url http://127.0.0.1:8545 >/dev/null",
            ],
            "interval": "1s",
            "timeout": "2s",
            "retries": 120,
        },
        "networks": [BACKEND_NETWORK, "default"],
    }

    policy_environment = {
        "EXPLORER_CHAIN_ID": chain_id,
        "FORK_BLOCK_NUMBER": fork_block,
        "RPC_BACKEND_URL": f"http://{ANVIL_SERVICE}:8545",
        "SOURCE_UPSTREAM_URL": source_upstream_url,
    }
    if source_upstream_token:
        policy_environment["SOURCE_UPSTREAM_TOKEN"] = source_upstream_token
    policy_networks = [AGENT_NETWORK, BACKEND_NETWORK, "default"]
    if source_network:
        policy_networks.append(source_network)

    services[POLICY_SERVICE] = {
        "image": policy_image,
        "user": "10001:10001",
        "cpus": 2.0,
        "mem_limit": "4096m",
        "pids_limit": 256,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "entrypoint": ["python3", "/usr/local/lib/rl-task/policy_gateway.py"],
        "environment": policy_environment,
        "depends_on": {ANVIL_SERVICE: {"condition": "service_healthy"}},
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "python3 -c \"import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8545/health').read(); "
                "urllib.request.urlopen('http://127.0.0.1:8081/health').read()\"",
            ],
            "interval": "1s",
            "timeout": "2s",
            "retries": 120,
        },
        "networks": policy_networks,
    }
    return compose


class PinnedForkDockerEnvironment(DockerEnvironment):
    """Use policy sidecars for agent environments; leave verifiers unchanged."""

    async def stop(self, delete: bool) -> None:
        if not delete or self._keep_containers:
            await super().stop(delete)
            return

        # Pier's --rmi all removes the shared task image used by other trials'
        # sidecars. Keep images until the whole multi-trial job has finished.
        await self.prepare_logs_for_host()
        try:
            await self._run_docker_compose_command(
                ["down", "--volumes", "--remove-orphans"]
            )
        except Exception as exc:
            self.logger.warning(f"Docker compose down failed: {exc}")
        self._cleanup_resources_compose_file()

    def __init__(self, *args: Any, **kwargs: Any):
        self._agent_environment = (
            kwargs.get("agent_install_spec") is not None
            or kwargs.get("network_allowlist") is not None
        )
        super().__init__(*args, **kwargs)

        if not self._agent_environment:
            return

        archive_rpc = os.environ.get(
            "RL_TASK_ARCHIVE_RPC", os.environ.get("ETH_RPC_URL", "")
        )
        if not archive_rpc:
            raise ValueError("an archive RPC is required by the isolated Anvil service")
        # Keep generated compose files secret-free. Docker Compose substitutes
        # these from Pier's host environment only when it starts the sidecars.
        os.environ["RL_TASK_POLICY_ARCHIVE_RPC"] = archive_rpc
        self._archive_rpc = "${RL_TASK_POLICY_ARCHIVE_RPC}"

        # Verified source comes from the shared source cache, which holds the
        # explorer key; see scripts/source_cache.sh. Override the upstream for
        # a remote cache (set RL_SOURCE_NETWORK= to skip the local network).
        self._source_upstream_url = os.environ.get(
            "RL_SOURCE_UPSTREAM_URL", SOURCE_UPSTREAM_URL
        )
        if not self._source_upstream_url.startswith(("http://", "https://")):
            raise ValueError("RL_SOURCE_UPSTREAM_URL must be an http(s) URL")
        self._source_network = os.environ.get("RL_SOURCE_NETWORK", SOURCE_NETWORK)
        self._policy_image = os.environ.get("RL_POLICY_GATEWAY_IMAGE", POLICY_IMAGE)
        self._source_upstream_token = ""
        if os.environ.get("RL_SOURCE_UPSTREAM_TOKEN"):
            os.environ["RL_TASK_POLICY_SOURCE_TOKEN"] = os.environ["RL_SOURCE_UPSTREAM_TOKEN"]
            self._source_upstream_token = "${RL_TASK_POLICY_SOURCE_TOKEN}"

        self._task_image = self.task_env_config.docker_image or ""
        if not self._task_image:
            raise ValueError("the isolated environment requires a prebuilt task image")
        self._fork_block = self._persistent_env.get("FORK_BLOCK_NUMBER", "")
        if not self._fork_block.isdigit():
            raise ValueError("FORK_BLOCK_NUMBER must be a decimal integer")
        self._expected_block = self._persistent_env.get(
            "FORK_EXPECTED_BLOCK_NUMBER", self._fork_block
        )
        if not self._expected_block.isdigit():
            raise ValueError("FORK_EXPECTED_BLOCK_NUMBER must be a decimal integer")
        self._target_timestamp = self._persistent_env.get("FORK_TARGET_TIMESTAMP")
        if self._target_timestamp is not None and not self._target_timestamp.isdigit():
            raise ValueError("FORK_TARGET_TIMESTAMP must be a decimal integer")
        self._chain_id = self._persistent_env.get("CHAIN_ID", "1")
        if not self._chain_id.isdigit():
            raise ValueError("CHAIN_ID must be a decimal integer")
        self._hardfork = self._persistent_env.get("ANVIL_HARDFORK", "osaka")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self._hardfork):
            raise ValueError("ANVIL_HARDFORK is invalid")

        # These are the only endpoints and explorer credential visible to the
        # agent. The real archive URL and Etherscan key stay in sidecars.
        for secret_name in tuple(self._persistent_env):
            upper = secret_name.upper()
            if (
                (upper.endswith("_RPC_URL") and upper != "RPC_URL")
                or upper.startswith(("ARCHIVE_RPC_", "RL_TASK_POLICY_"))
                or upper == "KEEP_ARCHIVE_RPC"
            ):
                self._persistent_env.pop(secret_name, None)
        self._persistent_env.update(
            {
                "RPC_URL": f"http://{POLICY_SERVICE}:8545",
                "EXPLORER_API_URL": f"http://{POLICY_SERVICE}:8081/api",
                "ETHERSCAN_API_KEY": "source-only",
            }
        )

    def _prepare_egress_proxy_compose(self) -> None:
        if not self._agent_environment:
            super()._prepare_egress_proxy_compose()
            return

        super()._prepare_egress_proxy_compose()
        path = self._egress_proxy_compose_path
        if path is not None and path.exists():
            compose = json.loads(path.read_text())
        else:
            path = self.trial_paths.trial_dir / "docker-compose-policy.json"
            self._egress_proxy_compose_path = path
            compose = {}

        add_policy_services(
            compose,
            image=self._task_image,
            archive_rpc=self._archive_rpc,
            fork_block=self._fork_block,
            policy_image=self._policy_image,
            source_upstream_url=self._source_upstream_url,
            source_upstream_token=self._source_upstream_token,
            source_network=self._source_network,
            expected_block=self._expected_block,
            target_timestamp=self._target_timestamp,
            chain_id=self._chain_id,
            hardfork=self._hardfork,
        )
        path.write_text(json.dumps(compose, indent=2))
        path.chmod(0o600)

    def agent_process_env(
        self, env: dict[str, str] | None
    ) -> dict[str, str] | None:
        merged = super().agent_process_env(env)
        if not self._agent_environment:
            return merged
        result = dict(merged or {})
        bypass = {"localhost", "127.0.0.1", POLICY_SERVICE}
        for key in ("NO_PROXY", "no_proxy"):
            bypass.update(part.strip() for part in result.get(key, "").split(","))
            result[key] = ",".join(sorted(part for part in bypass if part))
        return result
