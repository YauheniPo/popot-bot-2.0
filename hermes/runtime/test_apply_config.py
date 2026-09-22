"""Tests for the shared Hermes VPS runtime configuration planner."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("apply-config.py")
SPEC = importlib.util.spec_from_file_location("apply_config", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
apply_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_config)


class ApplyConfigTests(unittest.TestCase):
    def test_one_managed_model_overrides_ui_routes_without_changing_other_settings(self):
        settings = {'vps_hermes': {'config': {
            'ui_owned_sections': ['model', 'cron'],
            'managed_overlay': {'model': {'provider': 'fixture-cloud', 'default': 'fixture-model'}},
        }}, 'vps_deploy': {'features': {'workspace_ui': True}}}
        current = {'model': {'provider': 'old', 'default': 'old', 'max_tokens': 123},
                   'delegation': {'model': 'old'}, 'cron': {'model': 'old'}}
        operations = apply_config.build_operations(settings, current, {}, set())
        actual = {op.key: op.value for op in operations}
        for key in ('model.default', 'delegation.model', 'cron.model'):
            self.assertEqual(actual[key], 'fixture-model')
        for key in ('model.provider', 'delegation.provider', 'cron.model_provider'):
            self.assertEqual(actual[key], 'fixture-cloud')
        self.assertNotIn('model.max_tokens', actual)
        converged = {'model': {'provider': 'fixture-cloud', 'default': 'fixture-model'},
                     'delegation': {'provider': 'fixture-cloud', 'model': 'fixture-model'},
                     'cron': {'model_provider': 'fixture-cloud', 'model': 'fixture-model'}}
        self.assertEqual(apply_config.build_operations(settings, converged, {}, set()), [])

    def test_existing_profiles_get_managed_model_without_losing_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / 'profiles' / 'builder'
            profile.mkdir(parents=True)
            config = profile / 'config.yaml'
            config.write_text('model:\n  default: old\n  max_tokens: 123\ncustom: keep\n')
            settings = {'vps_hermes': {'config': {'managed_overlay': {
                'model': {'provider': 'test-cloud', 'default': 'test-model'}}}}}
            self.assertEqual(apply_config.sync_profile_models(home, settings), 1)
            value = apply_config.load_config(config)
            self.assertEqual(value['model'], {'provider': 'test-cloud', 'default': 'test-model', 'max_tokens': 123})
            self.assertEqual(value['delegation']['model'], 'test-model')
            self.assertEqual(value['custom'], 'keep')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(apply_config.sync_profile_models(home, settings), 0)
            config.unlink()
            outside = home / 'outside.yaml'
            outside.write_text('model: {}\n')
            config.symlink_to(outside)
            with self.assertRaises(ValueError):
                apply_config.sync_profile_models(home, settings)
            self.assertEqual(outside.read_text(), 'model: {}\n')

    def test_workspace_endpoint_is_included_in_manual_bootstrap(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / 'config/vps-defaults.yml')
        port = settings['vps_workspace_ui']['port']
        endpoint = f'https {port} http://127.0.0.1:{port}'
        self.assertIn(endpoint, apply_config.web_and_serve_assets(settings)[1])
        settings['vps_tailscale']['serve']['services'].append({
            'protocol': 'http', 'port': port, 'target': f'http://127.0.0.1:{port}',
        })
        with self.assertRaisesRegex(ValueError, 'overlap'):
            apply_config.web_and_serve_assets(settings)
        settings['vps_tailscale']['serve']['services'].pop()
        settings['vps_deploy']['features']['workspace_ui'] = False
        self.assertNotIn(endpoint, apply_config.web_and_serve_assets(settings)[1])

    def test_workspace_preserves_existing_operator_settings_but_not_security(self) -> None:
        settings = {
            'vps_deploy': {'features': {'workspace_ui': True}},
            'vps_hermes': {'config': {'ui_owned_sections': ['model', 'compression']}},
            'vps_runtime': {'set': {
                'model.max_tokens': 999, 'compression.threshold': 0.7,
                'approvals.mode': 'manual',
            }},
        }
        current = {'model': {'max_tokens': 123}, 'approvals': {'mode': 'off'}}
        operations = apply_config.build_operations(settings, current, {}, set())
        self.assertNotIn('model.max_tokens', [op.key for op in operations])
        self.assertIn('compression.threshold', [op.key for op in operations])
        self.assertIn(apply_config.Operation('set', 'approvals.mode', 'manual'), operations)
        settings['vps_deploy']['features']['workspace_ui'] = False
        self.assertIn('model.max_tokens', [op.key for op in apply_config.build_operations(settings, current, {}, set())])

    def test_workspace_cannot_move_security_sections_to_ui_ownership(self) -> None:
        settings = {
            'vps_deploy': {'features': {'workspace_ui': True}},
            'vps_hermes': {'config': {'ui_owned_sections': ['approvals']}},
        }
        with self.assertRaisesRegex(ValueError, 'ui_owned_sections'):
            apply_config.build_operations(settings, {}, {}, set())

    def test_repository_settings_have_required_structure_and_valid_types(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)

        for section in (
            "vps_deploy",
            "vps_hermes",
            "vps_runtime",
            "vps_github",
            "vps_ops",
            "vps_services",
        ):
            self.assertIsInstance(settings[section], dict)

        identity = settings["vps_deploy"]["identity"]
        for key in (
            "user",
            "user_home",
            "hermes_home",
            "hermes_bin",
            "workspace",
            "backup_dir",
        ):
            self.assertIsInstance(identity[key], str)
            self.assertTrue(identity[key])

        source = settings["vps_deploy"]["hermes_source"]
        for key in (
            "raw_base_url",
            "branch",
            "version",
            "release",
            "commit",
            "installer_sha256",
        ):
            self.assertIsInstance(source[key], str)
            self.assertTrue(source[key])
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(source["installer_sha256"], r"^[0-9a-f]{64}$")

        features = settings["vps_deploy"]["features"]
        self.assertTrue(features)
        self.assertTrue(all(isinstance(value, bool) for value in features.values()))
        self.assertTrue(features["searxng"])
        self.assertTrue(features["lock_public_ssh"])
        self.assertIsInstance(settings["vps_deploy"]["bundle"]["dir"], str)
        searxng = settings["vps_searxng"]
        self.assertEqual(searxng["image"], "docker.io/searxng/searxng:latest")
        self.assertEqual(searxng["bind_address"], "127.0.0.1")
        self.assertEqual(searxng["host_port"], 8888)
        self.assertEqual(searxng["valkey_image"], "docker.io/valkey/valkey:8-alpine")
        self.assertEqual(searxng["valkey_host"], "valkey")
        self.assertEqual(searxng["valkey_port"], 6379)
        self.assertTrue(settings["vps_tailscale"]["serve"]["enabled"])
        self.assertEqual(len(settings["vps_tailscale"]["serve"]["services"]), 5)

        overlay = settings["vps_hermes"]["config"]["managed_overlay"]
        self.assertIsInstance(overlay["model"]["default"], str)
        self.assertTrue(overlay["model"]["default"])
        self.assertIsInstance(overlay["model"]["provider"], str)
        self.assertTrue(overlay["model"]["provider"])
        self.assert_fallback_contract(overlay)
        self.assertIs(type(overlay["user_char_limit"]), int)
        self.assertGreater(overlay["user_char_limit"], 0)
        self.assertIs(type(overlay["memory_char_limit"]), int)
        self.assertGreater(overlay["memory_char_limit"], 0)
        self.assertIn(type(overlay["compression"]["threshold"]), (int, float))
        self.assertGreater(overlay["compression"]["threshold"], 0)
        self.assertLessEqual(overlay["compression"]["threshold"], 1)
        self.assertIsInstance(overlay["auxiliary"]["compression"]["provider"], str)
        self.assertIsInstance(overlay["auxiliary"]["compression"]["model"], str)
        self.assertEqual(
            overlay["auxiliary"]["compression"],
            {
                "provider": "openrouter",
                "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "fallback_chain": [
                    {"provider": "nvidia", "model": "nvidia/nemotron-3-ultra-550b-a55b"},
                ],
            },
        )
        routes = apply_config.managed_model_values(settings)
        self.assertEqual(routes['cron.model_provider'], overlay['model']['provider'])
        self.assertEqual(routes['cron.model'], overlay['model']['default'])
        self.assertIsInstance(overlay["cron"]["model_drift_guard"], bool)
        self.assertEqual(settings["vps_runtime"]["set"]["model.max_tokens"], 32768)
        self.assertIsInstance(overlay["web"]["search_backend"], str)
        self.assertIsInstance(overlay["web"]["extract_backend"], str)

        values = apply_config.build_asset_values(
            settings,
            hermes_user=identity["user"],
            hermes_group=identity["user"],
            user_home=identity["user_home"],
            hermes_home=identity["hermes_home"],
            hermes_bin=identity["hermes_bin"],
            workspace=identity["workspace"],
            backup_dir=identity["backup_dir"],
        )
        self.assertTrue(values)
        self.assertTrue(all(isinstance(value, str) and (value or key == "SEARXNG_URL") for key, value in values.items()))
        self.assertEqual(values["API_RETRY_PROVIDER"], settings["vps_ops"]["api_retry"]["provider"])
        self.assertEqual(values["API_RETRY_MODEL"], settings["vps_ops"]["api_retry"]["model"])
        self.assertEqual(values["API_RETRY_FALLBACKS"], "openrouter:inclusionai/ling-3.0-flash-sante:free")
        self.assertEqual(values["SEARXNG_URL"], "")

    def assert_runtime_contract(self, runtime: dict) -> None:
        # These are supported modes in the pinned Hermes gateway/display_config.py.
        # Numeric defaults remain tunable; budgets must stay bounded and positive.
        self.assertIsInstance(runtime["display.tool_progress"], str)
        self.assertIn(runtime["display.tool_progress"], {"off", "new", "all", "verbose", "log"})
        self.assertIsInstance(runtime["browser.backend"], str)
        self.assertTrue(runtime["browser.backend"].strip())
        for key in (
            "agent.max_turns",
            "goals.max_turns",
            "model.max_tokens",
            "tool_loop_guardrails.hard_stop_after.exact_failure",
            "tool_loop_guardrails.hard_stop_after.same_tool_failure",
            "tool_loop_guardrails.hard_stop_after.idempotent_no_progress",
            "tool_loop_guardrails.loop_caps.max_web_searches",
            "tool_loop_guardrails.loop_caps.max_subagents",
        ):
            self.assertIs(type(runtime[key]), int, key)
            self.assertGreater(runtime[key], 0, key)
        for key in ("proactive_prune_tokens", "agent.agent_cache.idle_ttl_secs"):
            self.assertIs(type(runtime[key]), int, key)
            self.assertGreaterEqual(runtime[key], 0, key)
        for key in (
            "tool_loop_guardrails.hard_stop_enabled",
            "display.platforms.telegram.interim_assistant_messages",
        ):
            self.assertIs(type(runtime[key]), bool, key)

    def test_repository_runtime_settings_follow_contract(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        self.assert_runtime_contract(settings["vps_runtime"]["set"])

    def test_runtime_contract_accepts_tuning_and_rejects_invalid_values(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        runtime = settings["vps_runtime"]["set"]
        for mode in ("off", "new", "all", "verbose", "log"):
            with self.subTest(mode=mode):
                self.assert_runtime_contract({**runtime, "display.tool_progress": mode, "agent.max_turns": 75})
        for key, invalid in (
            ("display.tool_progress", "typo"),
            ("display.tool_progress", True),
            ("agent.max_turns", "200"),
            ("agent.max_turns", True),
            ("agent.max_turns", 0),
            ("goals.max_turns", -1),
            ("proactive_prune_tokens", -1),
            ("agent.agent_cache.idle_ttl_secs", 1.5),
            ("tool_loop_guardrails.hard_stop_after.exact_failure", False),
            ("tool_loop_guardrails.hard_stop_enabled", "true"),
        ):
            with self.subTest(key=key, invalid=invalid):
                with self.assertRaises(AssertionError):
                    self.assert_runtime_contract({**runtime, key: invalid})

    def assert_fallback_contract(self, overlay: dict) -> None:
        chain = overlay["fallback_providers"]
        self.assertIsInstance(chain, list)
        routes = {(overlay["model"]["provider"], overlay["model"]["default"])}
        for entry in chain:
            self.assertIsInstance(entry, dict)
            for key in ("provider", "model"):
                self.assertIsInstance(entry.get(key), str, key)
                self.assertTrue(entry[key].strip(), key)
            route = (entry["provider"], entry["model"])
            self.assertNotIn(route, routes, "fallback must use a distinct provider/model pair")
            routes.add(route)

    def test_repository_fallback_chain_is_complete_and_distinct(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        self.assert_fallback_contract(settings["vps_hermes"]["config"]["managed_overlay"])

    def test_fallback_contract_rejects_entries_hermes_would_ignore(self) -> None:
        for chain in ({}, [{}], [{"provider": "primary-provider", "model": " "}],
                      [{"provider": "primary-provider", "model": "primary-model"}]):
            with self.subTest(chain=chain):
                with self.assertRaises(AssertionError):
                    self.assert_fallback_contract({
                        "model": {"provider": "primary-provider", "default": "primary-model"},
                        "fallback_providers": chain,
                    })

    def test_api_retry_fallbacks_rejects_malformed_managed_overlay(self) -> None:
        base = {'vps_hermes': {'config': {'managed_overlay': {}}}}
        cases = [
            {'fallback_providers': 'not-a-list'},
            {'fallback_providers': ['not-a-mapping']},
            {'fallback_providers': [{'provider': 'Bad Provider', 'model': 'valid-model'}]},
        ]
        for overlay in cases:
            with self.subTest(overlay=overlay), self.assertRaises(ValueError):
                apply_config.api_retry_fallbacks({
                    'vps_hermes': {'config': {'managed_overlay': overlay}}
                })

    def test_disabled_skills_are_unique_and_never_essential(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)
        disabled = settings["vps_hermes"]["config"]["managed_overlay"]["skills"]["disabled"]

        self.assertIsInstance(disabled, list)
        self.assertTrue(all(isinstance(skill, str) and skill.strip() for skill in disabled))
        self.assertEqual(len(disabled), len(set(disabled)))
        # hermes-agent is an ESSENTIAL_SKILL upstream drops from the list;
        # the local skills this deployment relies on must stay enabled.
        for kept in (
            "hermes-agent",
            "github",
            "codebase-inspection",
            "hermes-vps-ops",
            "telegram-logs-parser",
            "architecture-diagram",
            "excalidraw",
        ):
            self.assertNotIn(kept, disabled)

    def test_catalog_churn_names_are_a_subset_of_disabled_and_never_essential(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")

        churn = apply_config.catalog_churn_names(settings)
        disabled = apply_config.disabled_skill_names(settings)

        # Churn is an exemption list: every entry must also be disabled, or the
        # exemption would hide a name that nothing turns off.
        self.assertTrue(churn)
        self.assertLessEqual(churn, set(disabled))
        self.assertEqual(len(disabled), len(set(disabled)))

    def test_catalog_churn_names_defaults_to_empty_and_fails_closed(self) -> None:
        # A missing catalog_churn is optional and yields an empty set, and an
        # entirely absent overlay is valid too (every level defaults to {}).
        # A malformed overlay must raise ValueError (caught by main), never a
        # KeyError/TypeError traceback from a direct dict access.
        for absent in (
            {},
            {"vps_hermes": {}},
            {"vps_hermes": {"config": {"managed_overlay": {"skills": {"disabled": ["a"]}}}}},
        ):
            with self.subTest(value=absent):
                self.assertEqual(apply_config.catalog_churn_names(absent), set())
        for broken in (
            {"vps_hermes": []},
            {"vps_hermes": {"config": []}},
            {"vps_hermes": {"config": {"managed_overlay": []}}},
            {"vps_hermes": {"config": {"managed_overlay": {"skills": []}}}},
        ):
            with self.subTest(value=broken):
                with self.assertRaisesRegex(ValueError, "must be a mapping"):
                    apply_config.catalog_churn_names(broken)

    def test_verify_disabled_skills_flags_a_name_that_exists_nowhere(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "skills" / "productivity" / "known"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: known\n---\n", encoding="utf-8")
            (root / "skills" / ".archive" / "old").mkdir(parents=True)
            (root / "skills" / ".archive" / "old" / "SKILL.md").write_text(
                "---\nname: archived-only\n---\n", encoding="utf-8"
            )

            # A real name resolves; a typo and an archived-only name are flagged;
            # a catalog-churn name is exempt.
            unknown = apply_config.verify_disabled_skills(settings, root)

        self.assertNotIn("known", unknown)
        self.assertNotIn("github-auth", unknown)
        self.assertIn("apple-notes", unknown)
        self.assertIn("imessage", unknown)
        self.assertNotIn("archived-only", unknown)

    def test_verify_disabled_skills_is_a_no_op_without_a_catalog(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        with tempfile.TemporaryDirectory() as directory:
            # A fresh install has no skills tree yet; that must not fail a deploy.
            self.assertEqual(apply_config.verify_disabled_skills(settings, Path(directory)), [])

    def test_verify_disabled_skills_rejects_a_malformed_disable_list(self) -> None:
        for broken in (None, "not-a-list", ["ok", ""], [1]):
            with self.subTest(value=broken):
                settings = {"vps_hermes": {"config": {"managed_overlay": {
                    "skills": {"disabled": broken, "catalog_churn": []}}}}}
                with self.assertRaisesRegex(ValueError, "skills.disabled"):
                    apply_config.verify_disabled_skills(settings, Path("/nonexistent"))

    def test_verify_disabled_skills_rejects_a_malformed_catalog_churn(self) -> None:
        settings = {"vps_hermes": {"config": {"managed_overlay": {
            "skills": {"disabled": ["known"], "catalog_churn": "not-a-list"}}}}}
        with self.assertRaisesRegex(ValueError, "catalog_churn"):
            apply_config.verify_disabled_skills(settings, Path("/nonexistent"))

    def test_matt_pocock_engineering_skills_are_pinned_and_enabled(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")

        source = settings["vps_external_skills"]["matt_pocock_engineering"]
        self.assertIs(source["enabled"], True)
        self.assertEqual(source["repository"], "https://github.com/mattpocock/skills.git")
        self.assertRegex(source["revision"], r"^[0-9a-f]{40}$")
        self.assertTrue(source["checkout_dir"].startswith("/opt/"))
        self.assertEqual(
            source["skills_dir"],
            f"{source['checkout_dir']}/skills/engineering",
        )

    def test_repository_security_policy_requires_approval_and_github_auth(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        # Security invariants are intentionally separate from tunable defaults.
        self.assertEqual(settings["vps_runtime"]["set"]["approvals.mode"], "manual")
        self.assertIs(settings["vps_github"]["require_auth"], True)
        self.assertIs(settings["vps_github"]["enabled"], True)

    def test_repository_runtime_and_github_settings_have_valid_structure(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        runtime_settings = settings["vps_runtime"]["set"]
        self.assertTrue(runtime_settings)
        self.assertTrue(
            all(isinstance(key, str) and key for key in runtime_settings)
        )
        self.assertNotIn("unset", settings["vps_runtime"])
        self.assertIsInstance(settings["vps_github"]["enabled"], bool)
        self.assertIsInstance(settings["vps_github"]["require_auth"], bool)
        self.assertIsInstance(settings["vps_github"]["expected_login"], str)
        self.assertTrue(settings["vps_github"]["expected_login"].strip())
        self.assertIsInstance(settings["vps_github"]["access_probe_repository"], str)
        self.assertRegex(settings["vps_github"]["access_probe_repository"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        self.assertIsInstance(settings["vps_github"]["write_owners"], list)
        self.assertIsInstance(settings["vps_github"]["write_repositories"], list)
        self.assertIsInstance(settings["vps_services"]["gateway"], list)
        self.assertTrue(
            all(
                isinstance(owner, str) and owner
                for owner in settings["vps_github"]["write_owners"]
            )
        )
        self.assertTrue(
            all(
                isinstance(service, str) and service
                for service in settings["vps_services"]["gateway"]
            )
        )

    def test_manual_deploy_reads_pin_from_settings(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        # The manual deploy script must not carry its own copy of the source
        # pin: resolve_source_pin reads it from vps-defaults.yml at runtime.
        self.assertIn("resolve_source_pin", deploy_script)
        self.assertNotIn("DEFAULT_HERMES_COMMIT", deploy_script)
        self.assertNotIn("DEFAULT_INSTALLER_SHA256", deploy_script)
        self.assertIn('data["vps_deploy"]["hermes_source"]', deploy_script)
        self.assertIn("8#$settings_perms & 8#022", deploy_script)
        self.assertNotIn("settings_owner", deploy_script)
        self.assertNotIn("must be owned by root", deploy_script)
        self.assertIn('HERMES_BRANCH="${HERMES_BRANCH:-$source_branch}"', deploy_script)
        self.assertIn('HERMES_COMMIT="${HERMES_COMMIT:-$source_commit}"', deploy_script)
        self.assertIn('INSTALLER_SHA256="${INSTALLER_SHA256:-$source_installer_sha256}"', deploy_script)
        self.assertIn(
            "verify_updated_kanban_state\n  apply_local_hermes_patches",
            deploy_script,
        )

    def test_manual_deploy_preserves_path_options_and_resolves_gateway_service(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        for option, variable in (
            ("--user-home", "REQUESTED_USER_HOME"),
            ("--hermes-home", "REQUESTED_HERMES_HOME"),
            ("--workspace", "REQUESTED_HERMES_WORKSPACE"),
            ("--backup-dir", "REQUESTED_HERMES_BACKUP_DIR"),
        ):
            self.assertIn(option, deploy_script)
            self.assertIn(f'{variable}="$2"', deploy_script)
        self.assertIn("resolve_user_paths\n  resolve_managed_runtime", deploy_script)
        self.assertIn('systemctl start "$HERMES_GATEWAY_SERVICE"', deploy_script)

    @staticmethod
    def _asset_values(settings: dict) -> dict:
        identity = settings["vps_deploy"]["identity"]
        return apply_config.build_asset_values(
            settings,
            hermes_user=identity["user"],
            hermes_group=identity["user"],
            user_home=identity["user_home"],
            hermes_home=identity["hermes_home"],
            hermes_bin=identity["hermes_bin"],
            workspace=identity["workspace"],
            backup_dir=identity["backup_dir"],
        )

    @staticmethod
    def _asset_values_with(settings: dict, **overrides: str) -> dict:
        # The same defaults as _asset_values, with one identity value varied.
        identity = settings["vps_deploy"]["identity"]
        arguments = {
            "hermes_user": identity["user"],
            "hermes_group": identity["user"],
            "user_home": identity["user_home"],
            "hermes_home": identity["hermes_home"],
            "hermes_bin": identity["hermes_bin"],
            "workspace": identity["workspace"],
            "backup_dir": identity["backup_dir"],
        }
        arguments.update(overrides)
        return apply_config.build_asset_values(settings, **arguments)

    def test_dashboard_can_write_only_state_and_configured_workspace(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        settings = apply_config.load_settings(hermes_dir / 'config/vps-defaults.yml')
        identity = settings['vps_deploy']['identity']
        identity['hermes_home'] = '/srv/agent/state'
        identity['workspace'] = '/srv/agent/projects'
        template = (hermes_dir / 'ops/systemd/hermes-dashboard.service').read_text()
        rendered = apply_config.render_asset(template, self._asset_values(settings))
        writable = [line.partition('=')[2] for line in rendered.splitlines()
                    if line.startswith('ReadWritePaths=')]
        self.assertEqual(writable, [f"{identity['hermes_home']} {identity['workspace']}"])
        for protection in ('ProtectSystem=strict', 'ProtectHome=read-only',
                           'NoNewPrivileges=true', 'PrivateTmp=true'):
            self.assertIn(protection, rendered.splitlines())

    def test_vps_web_searxng_url_validation_rejects_malformed_values(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )

        # The default carries an empty searxng_url; build_asset_values must keep
        # SEARXNG_URL as an allowed empty string.
        values = self._asset_values(settings)
        self.assertEqual(values["SEARXNG_URL"], "")

        # vps_web must be a mapping.
        for bad_web in (None, "http://127.0.0.1:8888", 8888, ["http://127.0.0.1:8888"]):
            with self.subTest(vps_web=bad_web):
                with self.assertRaisesRegex(ValueError, "vps_web must be a mapping"):
                    self._asset_values({**settings, "vps_web": bad_web})

        # searxng_url must be a single-line string.
        for bad_url in (123, ["http://x"], "http://a\nb", "http://a\rb"):
            with self.subTest(searxng_url=bad_url):
                with self.assertRaisesRegex(ValueError, "vps_web.searxng_url must be a single-line string"):
                    self._asset_values({**settings, "vps_web": {"searxng_url": bad_url}})

        # A valid endpoint flows through to the rendered value.
        endpoint = "http://127.0.0.1:8888"
        self.assertEqual(
            self._asset_values({**settings, "vps_web": {"searxng_url": endpoint}})["SEARXNG_URL"],
            endpoint,
        )

    def test_tailscale_serve_endpoints_render_and_reject_malformed_values(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )
        # This test covers the explicit endpoint list; the optional companion
        # endpoint is covered independently above.
        settings['vps_deploy']['features']['workspace_ui'] = False

        # The repository default renders the managed endpoint list for the script.
        values = self._asset_values(settings)
        self.assertEqual(values["TAILSCALE_SERVE_ENDPOINTS"], "https 443 http://127.0.0.1:9119,"
                         "https 3000 http://127.0.0.1:3000,"
                         "https 9090 http://127.0.0.1:9090,"
                         "https 3001 http://127.0.0.1:3001,"
                         "https 8888 http://127.0.0.1:8888")

        # serve must be a mapping.
        with self.assertRaisesRegex(ValueError, "vps_tailscale.serve must be a mapping"):
            self._asset_values({**settings, "vps_tailscale": {"serve": "nope"}})

        # services must be a non-empty list.
        for bad_services in ([], "nope", None):
            with self.subTest(services=bad_services):
                with self.assertRaisesRegex(
                    ValueError, "vps_tailscale.serve.services must be a non-empty list"
                ):
                    self._asset_values({**settings, "vps_tailscale": {"serve": {"services": bad_services}}})

        # each entry must be a mapping with a valid port and loopback target.
        with self.assertRaisesRegex(ValueError, "entries must be mappings"):
            self._asset_values({**settings, "vps_tailscale": {"serve": {"services": ["nope"]}}})
        for bad_port in (0, 70000, True, "443"):
            with self.subTest(port=bad_port):
                with self.assertRaisesRegex(ValueError, "port must be 1-65535"):
                    self._asset_values(
                        {**settings, "vps_tailscale": {"serve": {"services": [{"protocol": "https", "port": bad_port, "target": "http://127.0.0.1:9119"}]}}}
                    )
        for bad_target in ("http://0.0.0.0:9119", "http://evil.example:9119", "https://127.0.0.1:9119", "ftp://127.0.0.1:80", 9119):
            with self.subTest(target=bad_target):
                with self.assertRaisesRegex(ValueError, "target must be a loopback URL"):
                    self._asset_values(
                        {**settings, "vps_tailscale": {"serve": {"services": [{"protocol": "https", "port": 443, "target": bad_target}]}}}
                    )

        # Protocol, port, and target render as one endpoint for the bootstrap script.
        custom = {"serve": {"services": [{"protocol": "http", "port": 443, "target": "http://127.0.0.1:9119"}]}}
        self.assertEqual(
            self._asset_values({**settings, "vps_tailscale": custom})["TAILSCALE_SERVE_ENDPOINTS"],
            "http 443 http://127.0.0.1:9119",
        )

        invalid_protocol = {"serve": {"services": [{"protocol": "tcp", "port": 443, "target": "http://127.0.0.1:9119"}]}}
        with self.assertRaisesRegex(ValueError, "protocol must be http or https"):
            self._asset_values({**settings, "vps_tailscale": invalid_protocol})

    def test_tailscale_disabled_does_not_require_serve_settings(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )
        settings["vps_deploy"]["features"]["tailscale"] = False
        settings.pop("vps_tailscale")

        self.assertEqual(self._asset_values(settings)["TAILSCALE_SERVE_ENDPOINTS"], "")

    def test_build_operations_applies_defaults_capabilities_and_unsets_overrides(self) -> None:
        settings = {
            "vps_runtime": {
                "set": {"terminal.cwd": "${HERMES_WORKSPACE}", "feature.enabled": True},
                "set_if_missing": {"model.max_tokens": 4096, "preserved.value": "new"},
                "unset": ["agent.max_turns", "missing.value"],
                "capabilities": {
                    "search": {
                        "set": {"web.search_backend": "provider"},
                        "unset_when_missing": ["web.search_backend"],
                    },
                    "extract": {"set": {"web.extract_backend": "extractor"}},
                },
            }
        }
        current = {
            "terminal": {"cwd": "/old"},
            "feature": {"enabled": True},
            "model": {},
            "preserved": {"value": "keep"},
            "agent": {"max_turns": 3},
            "web": {"search_backend": "old"},
        }

        operations = apply_config.build_operations(
            settings,
            current,
            {"HERMES_WORKSPACE": "/home/hermes/workspace"},
            {"search"},
        )

        self.assertEqual(
            operations,
            [
                apply_config.Operation("set", "terminal.cwd", "/home/hermes/workspace"),
                apply_config.Operation("set", "model.max_tokens", 4096),
                apply_config.Operation("unset", "agent.max_turns"),
                apply_config.Operation("set", "web.search_backend", "provider"),
            ],
        )

    def test_missing_capability_removes_only_its_explicit_fallback(self) -> None:
        settings = {
            "vps_runtime": {
                "capabilities": {
                    "search": {
                        "set": {"web.search_backend": "provider"},
                        "unset_when_missing": ["web.search_backend"],
                    },
                    "extract": {"set": {"web.extract_backend": "extractor"}},
                }
            }
        }
        current = {"web": {"search_backend": "old", "extract_backend": "portal"}}

        operations = apply_config.build_operations(settings, current, {}, set())

        self.assertEqual(operations, [apply_config.Operation("unset", "web.search_backend")])

    def test_load_settings_rejects_a_path_not_named_vps_defaults_yml(self) -> None:
        with self.assertRaisesRegex(ValueError, "vps-defaults.yml"):
            apply_config.load_settings(Path("/tmp/not-vps-defaults.yml"))

    def test_build_operations_rejects_non_mapping_runtime(self) -> None:
        with self.assertRaisesRegex(ValueError, "vps_runtime must be a mapping"):
            apply_config.build_operations({"vps_runtime": []}, {}, {}, set())

    def test_build_operations_rejects_non_mapping_capabilities(self) -> None:
        settings = {"vps_runtime": {"capabilities": []}}
        with self.assertRaisesRegex(ValueError, "capabilities must be a mapping"):
            apply_config.build_operations(settings, {}, {}, set())

    def test_load_settings_accepts_a_literal_dotdot_path(self) -> None:
        # install/common.sh builds --settings as "${SCRIPT_DIR}/../config/
        # vps-defaults.yml" -- a literal ".." component, never normalized by
        # the shell. load_settings() must resolve it, not reject it.
        settings_path = MODULE_PATH.parent / ".." / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)

        user = settings["vps_deploy"]["identity"]["user"]
        self.assertIsInstance(user, str)
        self.assertTrue(user)

    def test_main_apply_rejects_an_unsafe_hermes_bin_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "vps-defaults.yml"
            settings_path.write_text("vps_runtime: {}\n", encoding="utf-8")
            argv = [
                "apply-config.py",
                "apply",
                "--settings", str(settings_path),
                "--hermes-home", str(Path(temporary_directory) / "home"),
                "--hermes-bin", "/opt/hermes-bootstrap/bin/hermes bin",
                "--workspace", "/home/hermes/workspace",
            ]
            with mock.patch("sys.argv", argv):
                exit_code = apply_config.main()

        self.assertEqual(exit_code, 1)

    def test_service_groups_are_deduplicated_in_order(self) -> None:
        settings = {"vps_services": {"gateway": ["gateway.service"], "ops": ["a.service", "gateway.service"]}}

        self.assertEqual(
            apply_config.service_names(settings, ["gateway", "ops"]),
            ["gateway.service", "a.service"],
        )

    def test_set_operations_reject_non_string_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "set keys must be non-empty"):
            apply_config._set_operations({"set": {1: "value"}}, {}, {})

    def test_set_if_missing_operations_reject_non_string_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "set_if_missing keys must be non-empty"):
            apply_config._set_if_missing_operations({"set_if_missing": {1: "value"}}, {}, {})

    def test_capability_operations_reject_non_string_capability(self) -> None:
        with self.assertRaisesRegex(ValueError, "each capability must have a name"):
            apply_config._capability_operations({1: {"a": "b"}}, set(), {}, {})

    def test_verify_disabled_skills_rejects_a_malformed_overlay_shape(self) -> None:
        # Every level of the overlay path fails closed on a non-mapping.
        for broken in (
            {"vps_hermes": []},
            {"vps_hermes": {"config": []}},
            {"vps_hermes": {"config": {"managed_overlay": []}}},
            {"vps_hermes": {"config": {"managed_overlay": {"skills": []}}}},
        ):
            with self.subTest(value=broken):
                with self.assertRaisesRegex(ValueError, "must be a mapping"):
                    apply_config.disabled_skill_names(broken)

    def test_discover_skill_names_skips_files_without_frontmatter_or_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "skills" / "a" / "SKILL.md"
            good.parent.mkdir(parents=True)
            good.write_text("---\nname: alpha\n---\n", encoding="utf-8")
            no_frontmatter = root / "skills" / "b" / "SKILL.md"
            no_frontmatter.parent.mkdir(parents=True)
            no_frontmatter.write_text("# no frontmatter\n", encoding="utf-8")
            no_name = root / "skills" / "c" / "SKILL.md"
            no_name.parent.mkdir(parents=True)
            no_name.write_text("---\ndescription: nameless\n---\n", encoding="utf-8")

            self.assertEqual(apply_config.discover_skill_names(root), {"alpha"})

    def test_discover_skill_names_skips_unreadable_or_unparsable_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "skills" / "ok" / "SKILL.md"
            good.parent.mkdir(parents=True)
            good.write_text("---\nname: alpha\n---\n", encoding="utf-8")
            bad_yaml = root / "skills" / "bad" / "SKILL.md"
            bad_yaml.parent.mkdir(parents=True)
            bad_yaml.write_text("---\nname: [unclosed\n---\n", encoding="utf-8")

            errors = io.StringIO()
            with mock.patch("sys.stderr", errors):
                # Unparsable frontmatter is skipped, reported, not fatal.
                self.assertEqual(apply_config.discover_skill_names(root), {"alpha"})
            self.assertIn("invalid frontmatter", errors.getvalue())

            original = Path.read_text
            def selective(path_self, *args, **kwargs):
                if path_self.name == "SKILL.md" and path_self.parent.name == "ok":
                    raise OSError("denied")
                return original(path_self, *args, **kwargs)

            errors = io.StringIO()
            with mock.patch.object(Path, "read_text", selective), mock.patch("sys.stderr", errors):
                # An unreadable file is skipped and reported; the call still
                # returns the names it could read, so the advisory audit
                # continues instead of aborting the deployment.
                self.assertEqual(apply_config.discover_skill_names(root), set())
            self.assertIn("cannot read skill file", errors.getvalue())

    def test_main_apply_warns_but_does_not_abort_on_an_absent_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings_path = root / "vps-defaults.yml"
            settings_path.write_text(
                "vps_runtime:\n"
                "  set:\n"
                "    terminal.cwd: /home/hermes/workspace\n"
                "vps_hermes:\n"
                "  config:\n"
                "    managed_overlay:\n"
                "      skills:\n"
                "        catalog_churn: [retired]\n"
                "        disabled: [present, typoed]\n",
                encoding="utf-8",
            )
            skill = root / "home" / "skills" / "present"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: present\n---\n", encoding="utf-8")
            argv = [
                "apply-config.py", "apply",
                "--settings", str(settings_path),
                "--hermes-home", str(root / "home"),
                "--hermes-bin", "/opt/hermes-bootstrap/bin/hermes",
                "--workspace", "/home/hermes/workspace",
            ]
            errors = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch.object(
                apply_config, "run_operation"
            ) as run_operation, mock.patch("sys.stderr", errors):
                exit_code = apply_config.main()

        # The audit is advisory: a legitimately absent name (unseeded catalog,
        # agent-created skill) must never strand a deployment that already
        # stopped its gateway. It warns and the apply proceeds.
        self.assertEqual(exit_code, 0)
        run_operation.assert_called_once()
        self.assertIn("warning:", errors.getvalue())
        self.assertIn("typoed", errors.getvalue())
        # The exempt churn name must not be reported.
        self.assertNotIn("retired", errors.getvalue())

    def test_main_apply_is_quiet_when_every_disabled_name_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings_path = root / "vps-defaults.yml"
            settings_path.write_text(
                "vps_runtime:\n"
                "  set:\n"
                "    terminal.cwd: /home/hermes/workspace\n"
                "vps_hermes:\n"
                "  config:\n"
                "    managed_overlay:\n"
                "      skills:\n"
                "        catalog_churn: [retired]\n"
                "        disabled: [present, retired]\n",
                encoding="utf-8",
            )
            skill = root / "home" / "skills" / "present"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: present\n---\n", encoding="utf-8")
            argv = [
                "apply-config.py", "apply",
                "--settings", str(settings_path),
                "--hermes-home", str(root / "home"),
                "--hermes-bin", "/opt/hermes-bootstrap/bin/hermes",
                "--workspace", "/home/hermes/workspace",
            ]
            errors = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch.object(
                apply_config, "run_operation"
            ) as run_operation, mock.patch("sys.stderr", errors):
                exit_code = apply_config.main()

        self.assertEqual(exit_code, 0)
        run_operation.assert_called_once()
        self.assertNotIn("warning:", errors.getvalue())

    def test_main_apply_skips_the_audit_without_a_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings_path = root / "vps-defaults.yml"
            settings_path.write_text(
                "vps_runtime:\n"
                "  set:\n"
                "    terminal.cwd: /home/hermes/workspace\n"
                "vps_hermes:\n"
                "  config:\n"
                "    managed_overlay:\n"
                "      skills:\n"
                "        disabled: [anything-at-all]\n",
                encoding="utf-8",
            )
            argv = [
                "apply-config.py", "apply",
                "--settings", str(settings_path),
                "--hermes-home", str(root / "home"),
                "--hermes-bin", "/opt/hermes-bootstrap/bin/hermes",
                "--workspace", "/home/hermes/workspace",
            ]
            errors = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch.object(
                apply_config, "run_operation"
            ) as run_operation, mock.patch("sys.stderr", errors):
                exit_code = apply_config.main()

        # No skills tree yet (fresh install): no warning at all, deploy proceeds.
        self.assertEqual(exit_code, 0)
        run_operation.assert_called_once()
        self.assertNotIn("warning:", errors.getvalue())

    def test_managed_model_values_reject_incomplete_routes(self) -> None:
        for model in (
            {"provider": "", "default": "fixture-model"},
            {"provider": "fixture-cloud", "default": "  "},
            {"provider": 1, "default": "fixture-model"},
            "fixture-cloud/fixture-model",
        ):
            with self.subTest(model=model), self.assertRaisesRegex(
                ValueError, "managed model requires non-empty provider and default"
            ):
                apply_config.managed_model_values(
                    {"vps_hermes": {"config": {"managed_overlay": {"model": model}}}}
                )

    def test_sync_profile_models_refuses_symlinked_profiles_and_directories(self) -> None:
        settings = {"vps_hermes": {"config": {"managed_overlay": {
            "model": {"provider": "fixture-cloud", "default": "fixture-model"}}}}}

        # A symlinked profiles directory would route writes outside HERMES_HOME.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            real = home / "real-profiles"
            real.mkdir()
            (home / "profiles").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "profiles directory must not be a symlink"):
                apply_config.sync_profile_models(home, settings)

        # A symlinked profile entry would do the same for one profile.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profiles = home / "profiles"
            profiles.mkdir()
            outside = home / "outside"
            outside.mkdir()
            (profiles / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "profile directories must not be symlinks"):
                apply_config.sync_profile_models(home, settings)

    def test_sync_profile_models_skips_non_directories_and_absent_configs(self) -> None:
        settings = {"vps_hermes": {"config": {"managed_overlay": {
            "model": {"provider": "fixture-cloud", "default": "fixture-model"}}}}}

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profiles = home / "profiles"
            profiles.mkdir()
            (profiles / "notes.txt").write_text("not a profile\n", encoding="utf-8")
            (profiles / "empty-profile").mkdir()

            # A plain file and a directory without config.yaml are left alone.
            self.assertEqual(apply_config.sync_profile_models(home, settings), 0)
            self.assertEqual((profiles / "notes.txt").read_text(encoding="utf-8"), "not a profile\n")
            self.assertFalse((profiles / "empty-profile" / "config.yaml").exists())

    def test_sync_profile_models_rejects_a_non_mapping_section_without_writing(self) -> None:
        settings = {"vps_hermes": {"config": {"managed_overlay": {
            "model": {"provider": "fixture-cloud", "default": "fixture-model"}}}}}

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            profile = home / "profiles" / "broken"
            profile.mkdir(parents=True)
            config = profile / "config.yaml"
            config.write_text("model: not-a-mapping\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "model, delegation and cron must be mappings"):
                apply_config.sync_profile_models(home, settings)
            # Validation precedes writing: the broken profile is untouched.
            self.assertEqual(config.read_text(encoding="utf-8"), "model: not-a-mapping\n")

    def test_load_settings_reports_unreadable_or_non_mapping_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "vps-defaults.yml"
            invalid.write_text("vps_runtime: [unclosed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot read VPS settings from"):
                apply_config.load_settings(invalid)
            unreadable = root / "unreadable" / "vps-defaults.yml"
            unreadable.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "cannot read VPS settings from"):
                apply_config.load_settings(unreadable)

        with tempfile.TemporaryDirectory() as directory:
            non_mapping = Path(directory) / "vps-defaults.yml"
            non_mapping.write_text("- vps_runtime\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                apply_config.load_settings(non_mapping)

    def test_load_config_reports_unreadable_or_non_mapping_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "config.yaml"
            invalid.write_text("model: [unclosed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot read Hermes config from"):
                apply_config.load_config(invalid)
            as_directory = root / "config-as-directory"
            as_directory.mkdir()
            with self.assertRaisesRegex(ValueError, "cannot read Hermes config from"):
                apply_config.load_config(as_directory)
            non_mapping = root / "list.yaml"
            non_mapping.write_text("- model\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                apply_config.load_config(non_mapping)

        # An absent config is a fresh install, not an error.
        self.assertEqual(apply_config.load_config(Path("/nonexistent/config.yaml")), {})

    def test_render_value_substitutes_known_names_and_rejects_unknown_ones(self) -> None:
        self.assertEqual(apply_config.render_value("$KNOWN", {"KNOWN": "/home/hermes/workspace"}),
                         "/home/hermes/workspace")
        self.assertEqual(apply_config.render_value(123, {}), 123)
        for value in ("${MISSING}", "$MISSING", "prefix-${MISSING}-suffix"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "unresolved placeholder in VPS setting"
            ):
                apply_config.render_value(value, {"KNOWN": "/home/hermes/workspace"})

    def test_runtime_section_helpers_reject_bad_shapes(self) -> None:
        with self.assertRaisesRegex(ValueError, "vps_runtime.set must be a mapping"):
            apply_config._mapping({"set": "nope"}, "set")
        for value in ("nope", ["ok", ""], [1], {"key": "value"}):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "vps_runtime.unset must be a list of non-empty strings"
            ):
                apply_config._string_list({"unset": value}, "unset")

    def test_cli_value_renders_booleans_and_other_scalars(self) -> None:
        self.assertEqual(apply_config.cli_value(True), "true")
        self.assertEqual(apply_config.cli_value(False), "false")
        self.assertEqual(apply_config.cli_value(42), "42")
        self.assertEqual(apply_config.cli_value(0.5), "0.5")

    def test_run_operation_shells_out_to_the_hermes_cli(self) -> None:
        completed = mock.Mock(returncode=0, stdout="applied\n", stderr="")
        with mock.patch.object(apply_config.subprocess, "run", return_value=completed) as run:
            apply_config.run_operation(
                Path("/opt/hermes-bootstrap/bin/hermes"),
                apply_config.Operation("set", "terminal.cwd", "/home/hermes/workspace"),
            )
            run.assert_called_once_with(
                ["/opt/hermes-bootstrap/bin/hermes", "config", "set", "terminal.cwd",
                 "/home/hermes/workspace"],
                text=True, capture_output=True, check=False,
            )
        with mock.patch.object(apply_config.subprocess, "run", return_value=completed) as run:
            apply_config.run_operation(
                Path("/opt/hermes-bootstrap/bin/hermes"),
                apply_config.Operation("unset", "agent.max_turns"),
            )
            # An unset carries no value argument.
            run.assert_called_once_with(
                ["/opt/hermes-bootstrap/bin/hermes", "config", "unset", "agent.max_turns"],
                text=True, capture_output=True, check=False,
            )

    def test_run_operation_raises_with_the_cli_error_detail(self) -> None:
        operation = apply_config.Operation("set", "terminal.cwd", "/home/hermes/workspace")
        for completed, expected in (
            (mock.Mock(returncode=2, stdout="", stderr="unknown key\n"),
             "Hermes config set failed for terminal.cwd: unknown key"),
            (mock.Mock(returncode=2, stdout="stdout detail\n", stderr=""),
             "Hermes config set failed for terminal.cwd: stdout detail"),
            (mock.Mock(returncode=9, stdout="", stderr=""),
             "Hermes config set failed for terminal.cwd: exit 9"),
        ):
            with self.subTest(expected=expected):
                with mock.patch.object(apply_config.subprocess, "run", return_value=completed), \
                        self.assertRaisesRegex(RuntimeError, expected):
                    apply_config.run_operation(Path("/opt/hermes-bootstrap/bin/hermes"), operation)

    def test_service_names_rejects_bad_groups_and_unsafe_units(self) -> None:
        with self.assertRaisesRegex(ValueError, "vps_services must be a mapping"):
            apply_config.service_names({"vps_services": []}, ["gateway"])
        for values in ("gateway.service", [""], [1]):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValueError, "vps_services.gateway must be a list of non-empty strings"
            ):
                apply_config.service_names({"vps_services": {"gateway": values}}, ["gateway"])
        for unit in ("hermes-gateway", "../../etc/passwd.service", "gateway service.service",
                     "gateway.socket", "gateway.service\n"):
            with self.subTest(unit=unit), self.assertRaisesRegex(
                ValueError, "vps_services.gateway contains an unsafe unit name"
            ):
                apply_config.service_names({"vps_services": {"gateway": [unit]}}, ["gateway"])
        self.assertEqual(
            apply_config.service_names(
                {"vps_services": {"ops": ["hermes-ops@edge.service", "prune.timer"]}}, ["ops"]
            ),
            ["hermes-ops@edge.service", "prune.timer"],
        )

    def test_setting_value_and_scalar_validators_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "required VPS setting is missing: vps_ops.alert_target"):
            apply_config.setting_value({}, "vps_ops.alert_target")
        for value in (5, "has space", ""):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid VPS setting: vps_ops.alerts.target"
            ):
                apply_config._string_setting(
                    {"vps_ops": {"alerts": {"target": value}}}, "vps_ops.alerts.target", r"[A-Za-z0-9._:-]+"
                )
        self.assertEqual(
            apply_config._string_setting(
                {"vps_ops": {"alerts": {"target": "127.0.0.1:9119"}}},
                "vps_ops.alerts.target", r"[A-Za-z0-9._:-]+",
            ),
            "127.0.0.1:9119",
        )
        for value in (True, "5", 0, 99, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid VPS setting: vps_ops.limit"
            ):
                apply_config._integer_setting({"vps_ops": {"limit": value}}, "vps_ops.limit", 1, 10)
        self.assertEqual(
            apply_config._integer_setting({"vps_ops": {"limit": 10}}, "vps_ops.limit", 1, 10), 10
        )
        for value in ("true", "yes", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid VPS setting: vps_ops.required"
            ):
                apply_config._boolean_setting({"vps_ops": {"required": value}}, "vps_ops.required")
        self.assertIs(
            apply_config._boolean_setting({"vps_ops": {"required": False}}, "vps_ops.required"), False
        )

    def test_asset_values_require_one_gateway_and_unique_observability_ports(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        settings = apply_config.load_settings(settings_path)

        for gateway in ([], ["first.service", "second.service"]):
            with self.subTest(gateway=gateway), self.assertRaisesRegex(ValueError, "exactly one service"):
                self._asset_values(
                    {**settings, "vps_services": {**settings["vps_services"], "gateway": gateway}}
                )

        duplicated = apply_config.load_settings(settings_path)
        observability = duplicated["vps_observability"]
        observability["grafana"]["port"] = observability["dashboard"]["port"]
        with self.assertRaisesRegex(ValueError, "vps_observability ports must be unique"):
            self._asset_values(duplicated)

    def test_asset_values_reject_unsafe_rendered_settings(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")

        # An empty or multi-line identity value would corrupt the rendered unit.
        with self.assertRaisesRegex(ValueError, "unsafe rendered VPS setting: HERMES_USER"):
            self._asset_values_with(settings, hermes_user="")
        with self.assertRaisesRegex(ValueError, "unsafe rendered VPS setting: WORKSPACE"):
            self._asset_values_with(settings, workspace="/home/hermes\nworkspace")

    def test_render_asset_substitutes_known_and_rejects_unknown_placeholders(self) -> None:
        self.assertEqual(apply_config.render_asset("@NAME@", {"NAME": "hermes"}), "hermes")
        with self.assertRaisesRegex(ValueError, "unresolved template placeholders: @UNKNOWN@"):
            apply_config.render_asset("path=@UNKNOWN@", {"NAME": "hermes"})

    def test_main_services_prints_managed_units_once_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "vps-defaults.yml"
            settings_path.write_text(
                "vps_services:\n"
                "  gateway: [hermes-gateway.service]\n"
                "  ops: [hermes-ops@edge.service, prune.timer, hermes-gateway.service]\n",
                encoding="utf-8",
            )
            argv = ["apply-config.py", "services", "--settings", str(settings_path), "gateway", "ops"]
            output = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch("sys.stdout", output):
                exit_code = apply_config.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(),
                         "hermes-gateway.service\nhermes-ops@edge.service\nprune.timer\n")

    def test_main_value_prints_one_scalar_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "vps-defaults.yml"
            settings_path.write_text(
                "vps_ops:\n"
                "  api_retry:\n"
                "    max_attempts: 3\n"
                "  backup:\n"
                "    required: true\n",
                encoding="utf-8",
            )
            for key, expected in (("vps_ops.api_retry.max_attempts", "3"),
                                  ("vps_ops.backup.required", "true")):
                with self.subTest(key=key):
                    argv = ["apply-config.py", "value", "--settings", str(settings_path), key]
                    output = io.StringIO()
                    with mock.patch("sys.argv", argv), mock.patch("sys.stdout", output):
                        exit_code = apply_config.main()
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(output.getvalue(), expected + "\n")

            missing_errors = io.StringIO()
            argv = ["apply-config.py", "value", "--settings", str(settings_path), "vps_ops.missing"]
            with mock.patch("sys.argv", argv), mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", missing_errors):
                exit_code = apply_config.main()

            # A non-scalar setting cannot be printed as a CLI value.
            scalar_errors = io.StringIO()
            argv = ["apply-config.py", "value", "--settings", str(settings_path), "vps_ops.backup"]
            with mock.patch("sys.argv", argv), mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", scalar_errors):
                non_scalar_exit = apply_config.main()

        # A missing key fails loudly instead of printing an empty value.
        self.assertEqual(exit_code, 1)
        self.assertIn("required VPS setting is missing: vps_ops.missing", missing_errors.getvalue())
        self.assertEqual(non_scalar_exit, 1)
        self.assertIn("VPS setting is not scalar: vps_ops.backup", scalar_errors.getvalue())

    def test_main_render_substitutes_one_managed_asset(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        identity = apply_config.load_settings(settings_path)["vps_deploy"]["identity"]

        with tempfile.TemporaryDirectory() as temporary_directory:
            template = Path(temporary_directory) / "unit.service"
            template.write_text("User=@HERMES_USER@\nWorkspace=@WORKSPACE@\n", encoding="utf-8")
            argv = [
                "apply-config.py", "render",
                "--settings", str(settings_path),
                "--template", str(template),
                "--hermes-user", identity["user"],
                "--hermes-group", identity["user"],
                "--user-home", identity["user_home"],
                "--hermes-home", identity["hermes_home"],
                "--hermes-bin", identity["hermes_bin"],
                "--workspace", identity["workspace"],
                "--backup-dir", identity["backup_dir"],
            ]
            output = io.StringIO()
            with mock.patch("sys.argv", argv), mock.patch("sys.stdout", output):
                exit_code = apply_config.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            f"User={identity['user']}\nWorkspace={identity['workspace']}\n",
        )


if __name__ == '__main__':
    unittest.main()
