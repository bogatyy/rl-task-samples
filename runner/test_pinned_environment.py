from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from runner.pinned_environment import (
    AGENT_NETWORK,
    ANVIL_SERVICE,
    BACKEND_NETWORK,
    POLICY_IMAGE,
    POLICY_SERVICE,
    SOURCE_NETWORK,
    SOURCE_UPSTREAM_URL,
    PinnedForkDockerEnvironment,
    add_policy_services,
)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_does_not_remove_shared_task_image(self):
        env = object.__new__(PinnedForkDockerEnvironment)
        env._keep_containers = False
        env.prepare_logs_for_host = AsyncMock()
        env._run_docker_compose_command = AsyncMock()
        env._cleanup_resources_compose_file = Mock()

        await env.stop(delete=True)

        env.prepare_logs_for_host.assert_awaited_once()
        env._run_docker_compose_command.assert_awaited_once_with(
            ["down", "--volumes", "--remove-orphans"]
        )
        env._cleanup_resources_compose_file.assert_called_once()


class ComposePolicyTests(unittest.TestCase):
    def test_archive_service_is_not_on_agent_network(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="https://archive.invalid/secret",
            fork_block="25541000",
        )
        services = compose["services"]
        self.assertIn(AGENT_NETWORK, services["main"]["networks"])
        self.assertNotIn(AGENT_NETWORK, services[ANVIL_SERVICE]["networks"])
        self.assertIn(BACKEND_NETWORK, services[ANVIL_SERVICE]["networks"])
        self.assertIn(AGENT_NETWORK, services[POLICY_SERVICE]["networks"])
        self.assertIn(BACKEND_NETWORK, services[POLICY_SERVICE]["networks"])
        self.assertEqual(
            services["main"]["deploy"]["resources"]["limits"]["pids"], 512
        )
        self.assertNotIn("pids_limit", services["main"])
        self.assertEqual(services["main"]["cap_drop"], ["ALL"])
        self.assertEqual(
            services["main"]["cap_add"],
            ["CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETPCAP", "SETUID"],
        )
        self.assertEqual(
            services["main"]["security_opt"], ["no-new-privileges:true"]
        )

    def test_secret_sidecars_are_unprivileged(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="archive-secret",
            fork_block="1",
        )
        for service_name in (ANVIL_SERVICE, POLICY_SERVICE):
            service = compose["services"][service_name]
            self.assertEqual(service["user"], "10001:10001")
            self.assertEqual(service["cpus"], 2.0)
            self.assertEqual(service["mem_limit"], "4096m")
            self.assertEqual(service["pids_limit"], 256)
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(
                service["security_opt"], ["no-new-privileges:true"]
            )

    def test_main_security_boundary_overrides_looser_defaults(self):
        compose = {
            "services": {
                "main": {
                    "pids_limit": 4096,
                    "cap_drop": [],
                    "cap_add": ["ALL"],
                    "security_opt": [],
                    "networks": ["default", BACKEND_NETWORK],
                }
            }
        }
        add_policy_services(
            compose,
            image="task-image",
            archive_rpc="archive-secret",
            fork_block="1",
        )
        main = compose["services"]["main"]
        self.assertEqual(main["deploy"]["resources"]["limits"]["pids"], 512)
        self.assertNotIn("pids_limit", main)
        self.assertEqual(main["cap_drop"], ["ALL"])
        self.assertEqual(
            main["cap_add"],
            ["CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETPCAP", "SETUID"],
        )
        self.assertEqual(main["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(main["networks"], [AGENT_NETWORK])

    def test_secrets_are_only_in_sidecars(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="archive-secret",
            fork_block="1",
        )
        main = str(compose["services"]["main"])
        self.assertNotIn("archive-secret", main)
        self.assertEqual(
            compose["services"][ANVIL_SERVICE]["environment"]["ARCHIVE_RPC_URL"],
            "archive-secret",
        )
        for service in compose["services"].values():
            self.assertNotIn("ETHERSCAN_API_KEY", service.get("environment", {}))

    def test_chain_configuration_reaches_both_sidecars(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="base-archive",
            fork_block="40229652",
            expected_block="40229653",
            target_timestamp="1767225600",
            chain_id="8453",
            hardfork="cancun",
        )
        services = compose["services"]
        self.assertEqual(
            services[ANVIL_SERVICE]["environment"]["CHAIN_ID"], "8453"
        )
        self.assertEqual(
            services[ANVIL_SERVICE]["environment"]["ANVIL_HARDFORK"], "cancun"
        )
        self.assertEqual(
            services[ANVIL_SERVICE]["environment"]["FORK_BLOCK_NUMBER"],
            "40229652",
        )
        self.assertEqual(
            services[ANVIL_SERVICE]["environment"]["FORK_EXPECTED_BLOCK_NUMBER"],
            "40229653",
        )
        self.assertEqual(
            services[ANVIL_SERVICE]["environment"]["FORK_TARGET_TIMESTAMP"],
            "1767225600",
        )
        self.assertEqual(
            services[POLICY_SERVICE]["environment"]["EXPLORER_CHAIN_ID"], "8453"
        )


class SharedSourceCacheTests(unittest.TestCase):
    def test_policy_sidecar_uses_gateway_image_and_shared_cache(self):
        compose = add_policy_services(
            {}, image="task-image", archive_rpc="archive", fork_block="1"
        )
        policy = compose["services"][POLICY_SERVICE]
        self.assertEqual(policy["image"], POLICY_IMAGE)
        self.assertEqual(compose["services"][ANVIL_SERVICE]["image"], "task-image")
        self.assertEqual(policy["environment"]["SOURCE_UPSTREAM_URL"], SOURCE_UPSTREAM_URL)
        self.assertNotIn("SOURCE_UPSTREAM_TOKEN", policy["environment"])
        self.assertIn(SOURCE_NETWORK, policy["networks"])
        self.assertEqual(
            compose["networks"][SOURCE_NETWORK], {"external": True, "name": SOURCE_NETWORK}
        )

    def test_only_the_policy_sidecar_joins_the_shared_network(self):
        compose = add_policy_services(
            {"services": {"main": {"networks": ["default", SOURCE_NETWORK]}}},
            image="task-image",
            archive_rpc="archive",
            fork_block="1",
        )
        self.assertEqual(compose["services"]["main"]["networks"], [AGENT_NETWORK])
        self.assertNotIn(SOURCE_NETWORK, compose["services"][ANVIL_SERVICE]["networks"])

    def test_remote_upstream_with_token_and_no_local_network(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="archive",
            fork_block="1",
            policy_image="gateway:test",
            source_upstream_url="https://sources.example",
            source_upstream_token="${RL_TASK_POLICY_SOURCE_TOKEN}",
            source_network="",
        )
        policy = compose["services"][POLICY_SERVICE]
        self.assertEqual(policy["image"], "gateway:test")
        self.assertEqual(policy["environment"]["SOURCE_UPSTREAM_URL"], "https://sources.example")
        self.assertEqual(
            policy["environment"]["SOURCE_UPSTREAM_TOKEN"], "${RL_TASK_POLICY_SOURCE_TOKEN}"
        )
        self.assertEqual(policy["networks"], [AGENT_NETWORK, BACKEND_NETWORK, "default"])
        self.assertNotIn(SOURCE_NETWORK, compose["networks"])


if __name__ == "__main__":
    unittest.main()
