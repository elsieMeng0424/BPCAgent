"""Configuration precedence, portable paths and secret isolation."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bpcagent.workflow.config import WorkflowConfiguration
from bpcagent.workflow.cli import main
from bpcagent.agents.exceptions import ConfigException


class WorkflowConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.workflow = self.config / "workflow.jsonc"
        self.write("workflow.jsonc", {"data_dir": "../../dataset", "top_m": 7})
        self.write("models.jsonc", {
            "profiles": {name: {"model": name, "provider": "openai"} for name in ("task", "review", "meta")},
        })
        self.write("providers.jsonc", {"providers": {"openai": {
            "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "api_mode": "responses"}}})
        self.write("agents.jsonc", {"agents": {
            role: {"llm": "review" if role == "ReviewAgent" else "meta" if role == "MetaAgent" else "task"}
            for role in ("PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent", "MetaAgent")}})
        self.write("secrets.local.json", {"OPENAI_API_KEY": "local-secret"})

    def write(self, name, data):
        (self.config / name).write_text(json.dumps(data))

    def test_file_paths_and_cli_overrides(self):
        cfg = WorkflowConfiguration(self.workflow)
        values = cfg.workflow_options({"top_m": 2, "top_k": None})
        self.assertEqual(values["data_dir"], str((self.config / "../../dataset").resolve()))
        self.assertEqual(values["workspace_root"], str(self.root / "workspace"))
        self.assertEqual(values["top_m"], 2)
        self.assertEqual(values["top_k"], 5)
        self.assertEqual(cfg.workflow_options({"data_dir": "my-data"})["data_dir"], "my-data")

    def test_tool_file_paths_and_immutable_execution_settings(self):
        directory = self.config / "tools"
        directory.mkdir()
        self.write("workflow.jsonc", {"data_dir": "data", "tools_file": "tools/execution.jsonc"})
        self.write("tools/execution.jsonc", {"patient_shell": {
            "mode": "local", "allow_unsafe_local": True,
            "sandbox": {"docker_path": "../bin/docker", "command_timeout": 17.0}}})
        configuration = WorkflowConfiguration(self.workflow)
        self.write("tools/execution.jsonc", {"patient_shell": {"mode": "disabled"}})
        first = configuration.workflow_options({})
        self.assertNotIn("tools_file", first)
        self.assertEqual(first["patient_shell"]["mode"], "local")
        self.assertEqual(first["patient_shell"]["sandbox"]["command_timeout"], 17.0)
        self.assertEqual(first["patient_shell"]["sandbox"]["docker_path"], str(self.config / "bin/docker"))
        first["patient_shell"]["mode"] = "disabled"
        self.assertEqual(configuration.workflow_options({})["patient_shell"]["mode"], "local")
        self.assertEqual(WorkflowConfiguration(self.workflow).workflow_options({})["patient_shell"]["mode"], "disabled")

    def test_invalid_or_missing_tool_file_does_not_select_a_fallback(self):
        self.write("workflow.jsonc", {"data_dir": "data", "tools_file": "tools.jsonc"})
        with self.assertRaises(FileNotFoundError):
            WorkflowConfiguration(self.workflow)
        for settings in ({"patient_shell": {"mode": "local"}},
                         {"patient_shell": {"mode": "unknown"}},
                         {"unknown_tool": {}}):
            self.write("tools.jsonc", settings)
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "tools"):
                WorkflowConfiguration(self.workflow)

    def test_no_tool_file_uses_library_sandbox_default(self):
        self.assertEqual(WorkflowConfiguration(self.workflow).workflow_options({})["patient_shell"]["mode"], "sandbox")

    def test_guideline_paths_and_separate_vision_profile(self):
        self.write("workflow.jsonc", {"data_dir": "data", "tools_file": "tools.jsonc"})
        self.write("tools.jsonc", {"guidelines": {"enabled": True, "library_config": "guidelines.jsonc",
                                                "vision_profile": "task"}})
        self.write("providers.jsonc", {"providers": {"openai": {
            "base_url": "https://example.invalid/v1", "api_key_env": "OPENAI_API_KEY",
            "api_mode": "chat_completions"}}})
        configuration = WorkflowConfiguration(self.workflow)
        self.assertEqual(configuration.workflow_options({})["guidelines"]["library_config"],
                         str(self.config / "guidelines.jsonc"))
        configs = configuration.model_configs(environ={})
        self.assertEqual(configs["guideline_vision_config"].model, "task")
        self.assertEqual(configs["guideline_vision_config"].api_key.get_secret_value(), "local-secret")
        self.assertNotIn("local-secret", repr(configs["guideline_vision_config"]))

    def test_unsupported_vision_protocol_fails_explicitly(self):
        self.write("workflow.jsonc", {"data_dir": "data", "tools_file": "tools.jsonc"})
        self.write("tools.jsonc", {"guidelines": {"enabled": True, "library_config": "guidelines.jsonc",
                                                "vision_profile": "task"}})
        with self.assertRaisesRegex(ValueError, "vision.*chat_completions"):
            WorkflowConfiguration(self.workflow).model_configs(environ={})

    def configure_jev(self, enabled):
        self.write("workflow.jsonc", {"data_dir": "data", "tools_file": "tools.jsonc"})
        self.write("tools.jsonc", {"jev_file": "nested/jev.jsonc"})
        (self.config / "nested").mkdir(exist_ok=True)
        self.write("nested/jev.jsonc", {"enabled": enabled, "rubrics_file": "rubrics.jsonc"})

    def test_disabled_jev_needs_no_key_or_rubric_and_does_not_change_existing_providers(self):
        self.configure_jev(False)
        configuration = WorkflowConfiguration(self.workflow)
        self.assertFalse(configuration.workflow_options({})["jev"]["enabled"])
        self.assertNotIn("jev_api_key", configuration.model_configs(environ={}))

    def test_jev_key_precedence_and_path_resolution(self):
        self.configure_jev(True)
        self.write("secrets.local.json", {"OPENAI_API_KEY": "local-secret", "TYPESAFE_API_KEY": "local-jev-secret"})
        configuration = WorkflowConfiguration(self.workflow)
        options = configuration.workflow_options({})
        self.assertEqual(options["jev"]["rubrics_file"], str(self.config / "nested/rubrics.jsonc"))
        self.assertNotIn("api_key", options["jev"])
        local = configuration.model_configs(environ={})["jev_api_key"]
        environment = configuration.model_configs(environ={"TYPESAFE_API_KEY": "env-jev-secret"})["jev_api_key"]
        self.assertEqual(local.get_secret_value(), "local-jev-secret")
        self.assertEqual(environment.get_secret_value(), "env-jev-secret")
        self.assertNotIn("env-jev-secret", repr(environment))
        with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
            configuration.model_configs(environ={"TYPESAFE_API_KEY": ""})

    def test_jev_toggle_applies_to_new_configuration_not_active_snapshot(self):
        self.configure_jev(False)
        first = WorkflowConfiguration(self.workflow)
        self.write("nested/jev.jsonc", {"enabled": True, "rubrics_file": "rubrics.jsonc"})
        self.assertFalse(first.workflow_options({})["jev"]["enabled"])
        second = WorkflowConfiguration(self.workflow)
        self.assertTrue(second.workflow_options({})["jev"]["enabled"])
        with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
            second.model_configs(environ={})

    def test_plan_file_snapshot_and_only_active_model_bindings(self):
        self.write("workflow.jsonc", {"data_dir": "data", "plan_file": "pipeline.jsonc"})
        self.write("pipeline.jsonc", {"version": "patient-only", "stages": [{"agent": "PatientStateAgent"}]})
        self.write("agents.jsonc", {"agents": {
            "PatientStateAgent": {"llm": "task"}, "ReviewAgent": {"llm": "review"},
            "MetaAgent": {"llm": "unconfigured-inactive-profile"}}})
        config = WorkflowConfiguration(self.workflow)
        self.write("pipeline.jsonc", {"version": "changed-on-disk", "stages": []})
        self.assertEqual(config.workflow_options({})["plan"]["version"], "patient-only")
        self.assertEqual(set(config.model_configs(environ={})["agent_configs"]), {"PatientStateAgent", "ReviewAgent"})

    def test_local_secrets_and_inactive_meta_binding(self):
        models = WorkflowConfiguration(self.workflow).model_configs(environ={})["agent_configs"]
        task = models["PatientStateAgent"]
        self.assertEqual(task.api_key.get_secret_value(), "local-secret")
        self.assertNotIn("MetaAgent", models)
        self.assertNotIn("local-secret", repr(task))
        self.assertNotIn("local-secret", task.model_dump_json())

    def test_patient_only_override_does_not_require_other_agent_profiles(self):
        self.write("agents.jsonc", {"agents": {
            "PatientStateAgent": {"llm": "task"}, "ReviewAgent": {"llm": "review"}}})
        configs = WorkflowConfiguration(self.workflow).model_configs(
            environ={}, plan={"stages": [{"agent": "PatientStateAgent"}]})
        self.assertEqual(set(configs["agent_configs"]), {"PatientStateAgent", "ReviewAgent"})

    def test_context_policy_from_profile_and_partial_agent_override(self):
        models = json.loads((self.config / "models.jsonc").read_text())
        models["profiles"]["task"]["context"] = {
            "enabled": True, "context_window": 20000, "reserve_output_tokens": 1000,
            "safety_margin_tokens": 100, "trigger_ratio": 0.9,
        }
        self.write("models.jsonc", models)
        agents = json.loads((self.config / "agents.jsonc").read_text())
        agents["agents"]["PatientStateAgent"]["context"] = {
            "max_input_tokens": 3000, "context_window": 4000, "target_ratio": 0.8,
        }
        self.write("agents.jsonc", agents)
        configs = WorkflowConfiguration(self.workflow).model_configs(environ={})["agent_configs"]
        patient = configs["PatientStateAgent"].context
        self.assertEqual(patient.context_window, 4000)
        self.assertEqual(patient.reserve_output_tokens, 1000)
        self.assertEqual(patient.target_ratio, 0.8)
        self.assertEqual(configs["TargetSelectionAgent"].context.context_window, 20000)
        self.assertFalse(configs["ReviewAgent"].context.enabled)

    def test_invalid_merged_context_policy_is_rejected(self):
        agents = json.loads((self.config / "agents.jsonc").read_text())
        agents["agents"]["PatientStateAgent"]["context"] = {"enabled": True, "context_window": 100}
        self.write("agents.jsonc", agents)
        with self.assertRaises(ConfigException):
            WorkflowConfiguration(self.workflow).model_configs(environ={})

    def test_environment_precedence_and_no_global_mutation(self):
        env = {"OPENAI_API_KEY": "env-secret", "TASK_LLM_MODEL_ID": "env-task",
               "META_LLM_API_KEY": "meta-secret", "LLM_MODEL_ID": "unrelated-model"}
        before = dict(os.environ)
        models = WorkflowConfiguration(self.workflow).model_configs(environ=env)["agent_configs"]
        self.assertEqual(models["PatientStateAgent"].model, "task")
        self.assertEqual(models["PatientStateAgent"].api_key.get_secret_value(), "env-secret")
        self.assertNotIn("MetaAgent", models)
        self.assertEqual(models["ReviewAgent"].model, "review")
        self.assertEqual(dict(os.environ), before)

    def test_legacy_model_schema_is_rejected(self):
        self.write("models.jsonc", {"task": {}, "review": {}, "meta": None})
        with self.assertRaisesRegex(ValueError, "profiles"):
            WorkflowConfiguration(self.workflow).model_configs(environ={})

    def test_missing_secrets_with_environment_is_supported(self):
        self.write("workflow.jsonc", {"data_dir": "data", "secrets_file": "absent.local.json"})
        cfg = WorkflowConfiguration(self.workflow).model_configs(environ={"OPENAI_API_KEY": "env-secret"})
        self.assertEqual(cfg["agent_configs"]["PatientStateAgent"].api_key.get_secret_value(), "env-secret")

    def test_invalid_config_does_not_echo_secret_values(self):
        self.write("models.jsonc", {"profiles": {"task": {"api_key": "DO-NOT-ECHO"}}})
        with self.assertRaises(ValueError) as error:
            WorkflowConfiguration(self.workflow).model_configs(environ={})
        self.assertNotIn("DO-NOT-ECHO", str(error.exception))
        self.assertIn("task.api_key", str(error.exception))
        self.write("workflow.jsonc", {"top_m": True})
        with self.assertRaises(ValueError):
            WorkflowConfiguration(self.workflow)

    def test_no_config_rejects_environment_only_live_mode(self):
        cfg = WorkflowConfiguration()
        with self.assertRaisesRegex(ValueError, "data_dir"):
            cfg.workflow_options({})
        with self.assertRaisesRegex(ValueError, "workflow.jsonc"):
            cfg.model_configs(environ={"TASK_LLM_MODEL_ID": "task", "TASK_LLM_API_KEY": "old-key"})

    def test_cli_loads_default_and_demo_never_reads_keys(self):
        # Invalid secret content must not affect offline execution.
        (self.config / "secrets.local.json").write_text("invalid-secret-json")
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch("bpcagent.workflow.cli.AgentWorkflow") as factory, redirect_stdout(io.StringIO()):
                factory.return_value.run.return_value = {"mode": "demo", "status": "completed", "finished": True,
                                                         "task_dir": str(self.root)}
                result = main(["--patient-id", "P1", "--cutoff-day", "50"])
        finally:
            os.chdir(previous)
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["top_m"], 7)
        self.assertNotIn("agent_configs", factory.call_args.kwargs)

    def test_cli_explicit_missing_file_fails_and_malformed_secret_is_redacted(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(["--config", str(self.root / "missing.json"), "--patient-id", "P1", "--cutoff-day", "50"])
        self.assertEqual(result, 2)
        (self.config / "secrets.local.json").write_text('{"TASK_KEY": "DO-NOT-ECHO" BROKEN}')
        with redirect_stderr(stderr):
            result = main(["--config", str(self.workflow), "--mode", "live", "--patient-id", "P1", "--cutoff-day", "50"])
        self.assertEqual(result, 2)
        self.assertNotIn("DO-NOT-ECHO", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
