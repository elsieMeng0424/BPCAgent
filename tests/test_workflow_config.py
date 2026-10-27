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
