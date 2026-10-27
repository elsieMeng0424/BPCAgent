"""Provider credentials, reusable profiles, independent role configurations."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bpcagent.workflow import AgentWorkflow
from bpcagent.workflow.config import WorkflowConfiguration


ROLES = ["PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent"]


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.write("workflow.jsonc", {"data_dir": "data"})
        self.providers = {"providers": {"openai": {
            "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "api_mode": "responses"
        }}}
        self.profiles = {"profiles": {f"profile{i}": {
            "provider": "openai", "model": f"model{i}", "reasoning_effort": "medium"
        } for i in range(4)}}
        self.agents = {"agents": {role: {"llm": f"profile{i}"} for i, role in enumerate(ROLES)}}
        self.write("providers.jsonc", self.providers)
        self.write("models.jsonc", self.profiles)
        self.write("agents.jsonc", self.agents)
        self.write("secrets.local.json", {"OPENAI_API_KEY": "single-provider-key"})

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def load(self, env=None):
        return WorkflowConfiguration(self.root / "workflow.jsonc").model_configs(environ=env or {})["agent_configs"]

    def test_five_independent_configs_share_provider_key(self):
        configs = self.load()
        self.assertEqual(list(configs), ROLES)
        for i, role in enumerate(ROLES):
            self.assertEqual(configs[role].model, f"model{i}")
            self.assertEqual(configs[role].reasoning_effort, "medium")
            self.assertEqual(configs[role].api_key.get_secret_value(), "single-provider-key")
            self.assertEqual(configs[role].api_mode, "responses")
        flow = AgentWorkflow(data_dir=self.root, workspace_root=self.root, mode="live", agent_configs=configs)
        self.assertEqual(flow.configs, configs)

    def test_four_provider_routing_and_key_isolation(self):
        providers = ["openai", "claude", "deepseek", "gateway"]
        keys = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY", "gateway": "GATEWAY_API_KEY"}
        for name in keys:
            self.providers["providers"][name] = {
                "base_url": f"https://{name}.invalid/v1", "api_key_env": keys[name],
                "api_mode": "responses" if name == "openai" else "chat_completions"}
        for i, provider in enumerate(providers):
            self.profiles["profiles"][f"profile{i}"].update(provider=provider, reasoning_effort=None)
        self.write("providers.jsonc", self.providers)
        self.write("models.jsonc", self.profiles)
        self.write("secrets.local.json", {key: f"secret-{name}" for name, key in keys.items()})
        configs = self.load()
        for role, provider in zip(ROLES, providers):
            self.assertEqual(configs[role].default_provider, provider)
            self.assertEqual(configs[role].base_url, f"https://{provider}.invalid/v1")
            self.assertEqual(configs[role].api_key.get_secret_value(), f"secret-{provider}")

    def test_gateway_address_required_only_when_selected(self):
        self.providers["providers"]["gateway"] = {"base_url": None, "api_key_env": "GATEWAY_API_KEY"}
        self.write("providers.jsonc", self.providers)
        self.load()
        self.profiles["profiles"]["profile3"]["provider"] = "gateway"
        self.write("models.jsonc", self.profiles)
        with self.assertRaisesRegex(ValueError, "base_url.*gateway"):
            self.load()

    def test_claude_ignored_effort_and_unsupported_protocol_rejected(self):
        self.providers["providers"]["claude"] = {
            "base_url": "https://api.anthropic.com/v1/", "api_key_env": "ANTHROPIC_API_KEY"}
        self.profiles["profiles"]["profile3"]["provider"] = "claude"
        self.write("providers.jsonc", self.providers)
        self.write("models.jsonc", self.profiles)
        with self.assertRaisesRegex(ValueError, "ignores reasoning_effort"):
            self.load()
        self.profiles["profiles"]["profile3"]["reasoning_effort"] = None
        self.providers["providers"]["claude"]["api_mode"] = "responses"
        self.write("providers.jsonc", self.providers)
        self.write("models.jsonc", self.profiles)
        with self.assertRaisesRegex(ValueError, "requires chat_completions"):
            self.load()

    def test_no_legacy_keys_or_constructor_fallback(self):
        self.write("secrets.local.json", {"TASK_LLM_API_KEY": "old-task", "REVIEW_LLM_API_KEY": "old-review"})
        configs = self.load({"TASK_LLM_API_KEY": "old-env", "LLM_API_KEY": "generic"})
        self.assertIsNone(configs["PatientStateAgent"].api_key)
        with self.assertRaises(TypeError):
            AgentWorkflow(data_dir=self.root, workspace_root=self.root, mode="live", task_config={})
        with self.assertRaisesRegex(ValueError, "active"):
            AgentWorkflow(data_dir=self.root, workspace_root=self.root, mode="live")

    def test_environment_key_override_and_other_provider(self):
        self.providers["providers"]["gateway"] = {
            "base_url": "https://gateway.invalid/v1", "api_key_env": "GATEWAY_API_KEY", "api_mode": "chat_completions"
        }
        self.profiles["profiles"]["profile3"]["provider"] = "gateway"
        self.write("providers.jsonc", self.providers)
        self.write("models.jsonc", self.profiles)
        configs = self.load({"OPENAI_API_KEY": "env-openai", "GATEWAY_API_KEY": "env-gateway"})
        self.assertEqual(configs["PatientStateAgent"].api_key.get_secret_value(), "env-openai")
        self.assertEqual(configs["ReviewAgent"].api_key.get_secret_value(), "env-gateway")
        self.assertEqual(configs["ReviewAgent"].api_mode, "chat_completions")

    def test_missing_agent_unknown_profile_or_provider_rejected(self):
        del self.agents["agents"]["ReviewAgent"]
        self.write("agents.jsonc", self.agents)
        with self.assertRaisesRegex(ValueError, "active"):
            self.load()
        self.agents["agents"]["ReviewAgent"] = {"llm": "missing"}
        self.write("agents.jsonc", self.agents)
        with self.assertRaisesRegex(ValueError, "profile"):
            self.load()
        self.agents["agents"]["ReviewAgent"] = {"llm": "profile3"}
        self.profiles["profiles"]["profile3"]["provider"] = "missing"
        self.write("agents.jsonc", self.agents)
        self.write("models.jsonc", self.profiles)
        with self.assertRaisesRegex(ValueError, "provider"):
            self.load()

    def test_review_must_differ_from_every_task_model(self):
        self.profiles["profiles"]["profile3"]["model"] = "model1"
        self.profiles["profiles"]["profile3"]["reasoning_effort"] = "high"
        self.write("models.jsonc", self.profiles)
        with self.assertRaisesRegex(ValueError, "different model"):
            AgentWorkflow(data_dir=self.root, workspace_root=self.root, mode="live", agent_configs=self.load())

    def test_invalid_effort_and_inline_secret_are_rejected_without_echo(self):
        self.profiles["profiles"]["profile0"]["reasoning_effort"] = "typo"
        self.write("models.jsonc", self.profiles)
        with self.assertRaises(ValueError):
            self.load()
        self.profiles["profiles"]["profile0"]["reasoning_effort"] = "medium"
        self.profiles["profiles"]["profile0"]["api_key"] = "DO-NOT-ECHO"
        self.write("models.jsonc", self.profiles)
        with self.assertRaises(ValueError) as error:
            self.load()
        self.assertNotIn("DO-NOT-ECHO", str(error.exception))
