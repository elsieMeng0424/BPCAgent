"""The appended notebook example is self-contained; tests never call an API."""

import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bpcagent.workflow import AgentWorkflow
from bpcagent.log_paths import create_run_directory


class WorkflowNotebookTests(unittest.TestCase):
    def test_new_example_compiles_and_runs_with_mock_workflow(self):
        repository = Path(__file__).resolve().parents[1]
        notebook = json.loads((repository / "test.ipynb").read_text())
        cells = {c["id"]: "".join(c["source"]) for c in notebook["cells"] if c["id"].startswith("workflow-")}
        namespace = {}
        with TemporaryDirectory() as temp, patch("bpcagent.workflow.config.WorkflowConfiguration") as configuration, \
                patch("bpcagent.workflow.AgentWorkflow") as workflow, redirect_stdout(io.StringIO()):
            configuration.return_value.workflow_options.return_value = {"mode": "live"}
            configuration.return_value.model_configs.return_value = {"agent_configs": {}}
            workflow.return_value.configs = {}
            def fake_run(**kwargs):
                directory = create_run_directory(Path(temp) / "workspace")
                log = directory / "logs/console.log"
                log.write_text("synthetic console")
                return {"status": "completed", "finished": True, "task_dir": str(directory),
                        "log_path": str(log), "turns": []}
            workflow.return_value.run.side_effect = fake_run
            for name in ("workflow-config", "workflow-run", "workflow-results"):
                source = cells[name]
                ast.parse(source)
                self.assertNotIn("get_secret_value", source)
                self.assertNotIn("FiveAgentWorkflow", source)
                if name == "workflow-run":
                    namespace["PROJECT_DIR"] = Path(temp)
                before = dict(os.environ)
                exec(compile(source, "test.ipynb:" + name, "exec"), namespace)
                self.assertEqual(dict(os.environ), before)
            workflow.return_value.run.assert_called_once_with(
                patient_id="GENIE-DFCI-051542", cutoff_day=50, start_day=None)
            first_log = namespace["log_path"]
            self.assertEqual(first_log.parent, Path(namespace["workflow_result"]["task_dir"]) / "logs")
            self.assertTrue(first_log.is_file())
            exec(compile(cells["workflow-run"], "test.ipynb:workflow-run", "exec"), namespace)
            self.assertNotEqual(first_log, namespace["log_path"])
            self.assertTrue(first_log.is_file())
            self.assertTrue(namespace["log_path"].is_file())
        self.assertEqual(namespace["turn_summary"], [])

    def test_only_new_workflow_name_is_exported(self):
        import bpcagent.workflow as workflow
        self.assertIs(workflow.AgentWorkflow, AgentWorkflow)
        self.assertFalse(hasattr(workflow, "Five" + "AgentWorkflow"))
