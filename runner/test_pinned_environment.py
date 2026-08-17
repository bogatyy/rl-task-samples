from __future__ import annotations

import unittest

from runner.pinned_environment import (
    AGENT_NETWORK,
    ANVIL_SERVICE,
    BACKEND_NETWORK,
    POLICY_SERVICE,
    add_policy_services,
)


class ComposePolicyTests(unittest.TestCase):
    def test_archive_service_is_not_on_agent_network(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="https://archive.invalid/secret",
            etherscan_api_key="secret-key",
            fork_block="25541000",
        )
        services = compose["services"]
        self.assertIn(AGENT_NETWORK, services["main"]["networks"])
        self.assertNotIn(AGENT_NETWORK, services[ANVIL_SERVICE]["networks"])
        self.assertIn(BACKEND_NETWORK, services[ANVIL_SERVICE]["networks"])
        self.assertIn(AGENT_NETWORK, services[POLICY_SERVICE]["networks"])
        self.assertIn(BACKEND_NETWORK, services[POLICY_SERVICE]["networks"])
        self.assertEqual(services["main"]["pids_limit"], 512)
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
            etherscan_api_key="etherscan-secret",
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
            etherscan_api_key="etherscan-secret",
            fork_block="1",
        )
        main = compose["services"]["main"]
        self.assertEqual(main["pids_limit"], 512)
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
            etherscan_api_key="etherscan-secret",
            fork_block="1",
        )
        main = str(compose["services"]["main"])
        self.assertNotIn("archive-secret", main)
        self.assertNotIn("etherscan-secret", main)
        self.assertEqual(
            compose["services"][ANVIL_SERVICE]["environment"]["ARCHIVE_RPC_URL"],
            "archive-secret",
        )
        self.assertEqual(
            compose["services"][POLICY_SERVICE]["environment"]["ETHERSCAN_API_KEY"],
            "etherscan-secret",
        )

    def test_chain_configuration_reaches_both_sidecars(self):
        compose = add_policy_services(
            {},
            image="task-image",
            archive_rpc="base-archive",
            etherscan_api_key="source-key",
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


if __name__ == "__main__":
    unittest.main()
