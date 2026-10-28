"""Natural-language scope, native extractor calls and real patient Shell integration."""

from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from bpcagent import Config, LLMResponse, ToolCall
from bpcagent.sandbox import SandboxError
from bpcagent.tools.patient_shell import PatientShellConfig
from bpcagent.workflow import AgentWorkflow
from bpcagent.workflow.agents import PatientStateAgent
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.cli import main
from bpcagent.workflow.config import WorkflowConfiguration
from bpcagent.workflow.request import RequestClarificationError, resolve_request
from bpcagent.workflow.state import SharedPatientState

if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


REQUEST = "看下BPC数据集病人051542前220天的病例，推荐他的下一步用药。"
PATIENT = "GENIE-DFCI-051542"


class PatientRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = fixture_dataset(self.root / "data")
        for path in self.data.glob("*.csv"):
            path.write_text(path.read_text().replace("P1", PATIENT))
        stream = redirect_stdout(io.StringIO())
        stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)

    def test_exact_user_example_and_explicit_range(self):
        scope = resolve_request(REQUEST, data_dir=self.data)
        self.assertEqual(scope["patient_id"], PATIENT)
        self.assertEqual(scope["cutoff_day"], 220)
        self.assertEqual(scope["input"], REQUEST)
        ranged = resolve_request(f"查看{PATIENT}第50至220天的记录", data_dir=self.data)
        self.assertEqual((ranged["start_day"], ranged["cutoff_day"]), (50, 220))

    def test_supported_english_negative_days_and_matching_explicit_scope(self):
        for text, expected in ((f"Inspect patient {PATIENT} through day 220", 220),
                               ("患者051542诊断前20天的病历", -20),
                               (f"Patient {PATIENT}, cutoff_day=-5", -5)):
            with self.subTest(text=text):
                self.assertEqual(resolve_request(text, data_dir=self.data)["cutoff_day"], expected)
        scope = resolve_request(REQUEST, data_dir=self.data, patient_id=PATIENT, cutoff_day=220)
        self.assertEqual(scope["cutoff_day"], 220)

    def test_missing_conflicting_or_calendar_scope_requires_clarification(self):
        for text, options in (("推荐下一步用药", {}), ("病人051542的病例", {}),
                              ("病人051542截至2026年10月27日", {}),
                              ("patient 051542 through day 2026-10-27", {}),
                              ("病人051542截至第50天和截至第220天", {}),
                              (REQUEST, {"cutoff_day": 221}),
                              (REQUEST, {"patient_id": "GENIE-MSK-OTHER"}),
                              ("病人051542和051543前220天", {}),
                              (f"查看{PATIENT}和病人051543前220天", {}),
                              ("病人051542第220至50天", {})):
            with self.subTest(text=text), self.assertRaises(RequestClarificationError):
                resolve_request(text, data_dir=self.data, **options)

    def test_ambiguous_suffix_is_not_assigned_an_institution(self):
        path = self.data / "patient_level_dataset.csv"
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows.append({**rows[0], "record_id": "GENIE-MSK-051542"})
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaises(RequestClarificationError):
            resolve_request(REQUEST, data_dir=self.data)

    def test_full_natural_request_reaches_all_agents_and_native_tool_calls(self):
        workflow = AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs")
        result = workflow.run(REQUEST)
        self.assertTrue(result["finished"])
        self.assertEqual(result["patient_id"], PATIENT)
        self.assertEqual(result["cutoff_day"], 220)
        self.assertEqual(len(result["turns"]), 6)
        directory = Path(result["task_dir"])
        saved = json.loads((directory / "request.json").read_text())
        self.assertEqual(saved["input"], REQUEST)
        for transcript_file in directory.glob("*/transcript.json"):
            transcript = json.loads(transcript_file.read_text())
            self.assertEqual(next(m["content"] for m in transcript if m["role"] == "user"), REQUEST)
        transcript = json.loads((directory / "001_PatientStateAgent/transcript.json").read_text())
        calls = [c["function"]["name"] for m in transcript for c in m.get("tool_calls", [])]
        self.assertIn("bpc_state_extractor", calls)
        self.assertNotIn("run_shell", calls)  # The demo does not pretend Shell was needed or executed.
        self.assertEqual(result["accepted_outputs"]["DrugSelectionAgent"]["candidates"], [])

    def test_native_model_loop_runs_real_shell_then_review(self):
        class ShellDemo(DemoLLM):
            shell_called = False

            def invoke(self, messages, **kwargs):
                if self.role == "PatientStateAgent" and self.tool_step == 2 and not self.shell_called:
                    self.shell_called = True
                    return LLMResponse(finish_reason="tool_calls", tool_calls=[ToolCall(
                        id="patient_shell_check", name="run_shell", arguments=json.dumps({"command":
                            "python3 -c 'import json; from pathlib import Path; "
                            "s=json.loads(Path(\"input/patient_state.json\").read_text()); "
                            "assert s[\"meta\"][\"cutoff_day\"] == 220; print(\"cutoff verified\")'"}))])
                return super().invoke(messages, **kwargs)

        config = self.root / "workflow.json"
        config.write_text(json.dumps({"data_dir": str(self.data), "workspace_root": str(self.root / "runs"),
                                      "tools_file": "tools.json"}))
        (self.root / "tools.json").write_text(json.dumps({
            "patient_shell": {"mode": "local", "allow_unsafe_local": True}}))
        options = WorkflowConfiguration(config).workflow_options({
            "plan": {"stages": [{"agent": "PatientStateAgent"}]}})
        workflow = AgentWorkflow(**options, llm_factory=ShellDemo)
        with patch("bpcagent.tools.patient_shell.DockerSandbox") as docker:
            docker.directories = ("input", "scripts", "output", "tmp")
            result = workflow.run(REQUEST)
            docker.assert_not_called()
        self.assertTrue(result["finished"])
        self.assertEqual([t["agent"] for t in result["turns"]], ["PatientStateAgent", "ReviewAgent"])
        root = Path(result["task_dir"])
        audit = json.loads((root / result["turns"][0]["artifacts"]["shell_audit"]).read_text())
        self.assertEqual(audit["mode"], "local")
        self.assertEqual(json.loads((root / "request.json").read_text())["patient_shell"]["mode"], "local")
        self.assertEqual(audit["executions"][0]["exit_code"], 0)
        transcript = json.loads((root / "001_PatientStateAgent/transcript.json").read_text())
        shell_result = next(json.loads(m["content"]) for m in transcript
                            if m.get("tool_call_id") == "patient_shell_check")
        self.assertIn("cutoff verified", shell_result["data"]["stdout"])

    def test_cli_natural_request_patient_only(self):
        config = self.root / "workflow.json"
        config.write_text(json.dumps({"data_dir": str(self.data), "workspace_root": str(self.root / "cli_runs")}))
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--config", str(config), "--request", REQUEST, "--mode", "demo", "--patient-only"])
        self.assertEqual(status, 0)
        result = json.loads(Path(json.loads(output.getvalue())["result"]).read_text())
        self.assertEqual(len(result["turns"]), 2)
        self.assertEqual(result["input"], REQUEST)

    def agent(self, *, shell=None, llm=None):
        shared = SharedPatientState(patient_id=PATIENT, cutoff_day=50, data_dir=self.data)
        return PatientStateAgent(shared=shared, context={"stage": "PatientStateAgent"},
                                 directory=self.root / "turn", config=Config(max_tool_iterations=8),
                                 llm=llm or DemoLLM("PatientStateAgent"), shell_config=shell)

    def test_model_decides_extraction_and_cannot_override_scope(self):
        agent = self.agent(llm=Mock(invoke=Mock(return_value=LLMResponse(content="Not extracted", finish_reason="stop"))))
        self.assertFalse(agent.shared.ready)
        agent.run("Inspect this patient")
        self.assertFalse(agent.shared.ready)
        self.assertIsNone(agent.submitted)
        for arguments in ({"cutoff_day": 500}, {"patient_id": "OTHER"}, {"data_dir": "/tmp"}):
            rejected = agent.tool_registry.execute_tool("bpc_state_extractor", arguments)
            self.assertEqual(rejected.error.code, "invalid_arguments")

    def test_local_shell_requires_explicit_acknowledgement(self):
        with self.assertRaises(ValidationError):
            PatientShellConfig(mode="local")

    def test_real_shell_checks_bounded_input_without_mutating_shared_state(self):
        agent = self.agent(shell={"mode": "local", "allow_unsafe_local": True})
        rejected = agent.tool_registry.execute_tool("run_shell", {"command": "pwd"})
        self.assertEqual(rejected.status, "error")
        extracted = agent.tool_registry.execute_tool("bpc_state_extractor", {})
        self.assertEqual(extracted.status, "success")
        original = deepcopy(agent.shared.read())
        result = agent.tool_registry.execute_tool("run_shell", {"command": (
            "python3 -c 'import json; from pathlib import Path; "
            "s=json.loads(Path(\"input/patient_state.json\").read_text()); "
            "print(s[\"meta\"][\"patient_id\"]); "
            "Path(\"output/check.json\").write_text(json.dumps({\"cutoff_day\":s[\"meta\"][\"cutoff_day\"]}))'"
        )})
        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["exit_code"], 0, result.data)
        self.assertIn(PATIENT, result.data["stdout"])
        self.assertEqual(agent.shared.read(), original)
        inputs = agent.shell_tool.directory / "input"
        self.assertEqual({p.name for p in inputs.iterdir()}, {"patient_state.json", "patient_evidence.json",
                                                             "state_check.json", "normalized_markers.json"})
        self.assertEqual(json.loads((inputs / "patient_state.json").read_text()), original["patient_state"])
        self.assertEqual(json.loads((agent.shell_tool.directory / "output/check.json").read_text())["cutoff_day"], 50)

    def test_sandbox_failure_does_not_fall_back_to_local_shell(self):
        agent = self.agent()
        agent.extract()
        with patch("bpcagent.tools.patient_shell.DockerSandbox.execute", side_effect=SandboxError("offline test: no Docker")), \
                patch("bpcagent.tools.patient_shell.ShellTool") as local:
            result = agent.tool_registry.execute_tool("run_shell", {"command": "pwd"})
            self.assertEqual(result.status, "error")
            local.assert_not_called()
        self.assertEqual(agent.shell_tool.audit()["executions"][-1]["status"], "failed")

    def test_shell_input_symlink_cannot_overwrite_an_external_file(self):
        agent = self.agent(shell={"mode": "local", "allow_unsafe_local": True})
        agent.extract()
        agent.shell_tool._prepare()
        path = agent.shell_tool.directory / "input/patient_state.json"
        path.unlink()
        target = self.root / "untouched.json"
        target.write_text("original")
        path.symlink_to(target)
        with patch("bpcagent.tools.patient_shell.ShellTool") as local:
            result = agent.tool_registry.execute_tool("run_shell", {"command": "pwd"})
            self.assertEqual(result.status, "error")
            local.assert_not_called()
        self.assertEqual(target.read_text(), "original")

    def test_disabled_shell_is_not_registered(self):
        agent = self.agent(shell={"mode": "disabled"})
        self.assertIsNone(agent.tool_registry.get_tool("run_shell"))
        self.assertIsNotNone(agent.tool_registry.get_tool("bpc_state_extractor"))
