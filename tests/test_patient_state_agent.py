"""The sole PatientStateAgent integrates extraction artifacts and workflow checks."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bpcagent import Config, LLMResponse, PatientStateAgent
from bpcagent.workflow import AgentWorkflow, PatientStateAgent as WorkflowPatientStateAgent
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.state import SharedPatientState

if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


class PatientStateAgentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = fixture_dataset(self.root / "data")
        self.shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        stdout = redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def agent(self, *, directory="turn", shared=None, llm=None):
        return PatientStateAgent(shared=shared or self.shared, context={"stage": "PatientStateAgent"},
                                 directory=self.root / directory, config=Config(max_tool_iterations=8),
                                 llm=llm or DemoLLM("PatientStateAgent"))

    def prepare(self, agent):
        payload = agent.extract()
        agent.get_context()
        agent.state_check()
        return list(payload["patient_state"]["evidence_refs"])

    def test_only_one_public_patient_agent(self):
        import bpcagent.agents as backbone
        self.assertIs(PatientStateAgent, WorkflowPatientStateAgent)
        self.assertFalse(hasattr(backbone, "PatientStateAgent"))
        self.assertFalse((Path(__file__).resolve().parents[1] / "src/agents/patient_state.py").exists())

    def test_demo_saves_bounded_state_and_evidence_before_submission(self):
        agent = self.agent()
        result = agent.run("Extract, check and submit")
        self.assertTrue(result.finished)
        self.assertIsNotNone(agent.submitted)
        state = json.loads(Path(agent.artifacts["state"]).read_text())
        evidence = json.loads(Path(agent.artifacts["evidence"][0]).read_text())
        self.assertEqual(state, self.shared.read()["patient_state"])
        self.assertEqual(evidence["cutoff_day"], 50)
        self.assertEqual(evidence["patient_id"], "P1")
        self.assertEqual(evidence["status"], "retrieved")
        self.assertEqual(evidence["data"], {ref: state["evidence_refs"][ref] for ref in evidence["evidence_refs"]})
        self.assertTrue((agent.directory / "transcript.json").exists())

    def test_text_without_tools_cannot_complete_extraction(self):
        llm = Mock(invoke=Mock(return_value=LLMResponse(content="Done", finish_reason="stop")))
        workflow = AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs",
                                 plan={"stages": [{"agent": "PatientStateAgent"}]},
                                 llm_factory=lambda role: llm if role == "PatientStateAgent" else DemoLLM(role))
        result = workflow.run(patient_id="P1", cutoff_day=50)
        self.assertFalse(result["finished"])
        self.assertEqual(result["turns"][0]["status"], "failed")
        self.assertEqual(result["turns"][0]["artifacts"], {})

    def test_cached_state_does_not_count_as_this_turn_extraction(self):
        self.shared.extract()
        agent = self.agent()
        agent.get_shared()
        agent.get_context()
        agent.state_check()
        with self.assertRaisesRegex(ValueError, "get_patient_state"):
            agent.submit({"summary": "done", "limitations": []})
        with self.assertRaisesRegex(ValueError, "get_patient_state"):
            agent.get_evidence([next(iter(self.shared.read()["patient_state"]["evidence_refs"]))])

    def test_failed_evidence_query_or_write_cannot_enable_submission(self):
        agent = self.agent()
        refs = self.prepare(agent)
        with patch.object(self.shared, "get_evidence", side_effect=OSError("read failed")):
            result = agent.tool_registry.execute_tool("get_patient_evidence", {"evidence_refs": refs[:1]})
            self.assertEqual(result.status, "error")
        with patch.object(agent.patient_store, "save", side_effect=OSError("write failed")):
            result = agent.tool_registry.execute_tool("get_patient_evidence", {"evidence_refs": refs[:1]})
            self.assertEqual(result.status, "error")
        with self.assertRaisesRegex(ValueError, "get_patient_evidence"):
            agent.submit({"summary": "done", "limitations": []})
        agent.get_evidence(refs[:1])
        agent.submit({"summary": "done", "limitations": []})

    def test_failed_state_write_does_not_count_as_extraction(self):
        agent = self.agent()
        with patch.object(agent.patient_store, "save", side_effect=OSError("write failed")):
            result = agent.tool_registry.execute_tool("get_patient_state", {})
        self.assertEqual(result.status, "error")
        self.assertFalse(agent.state_saved)
        self.assertEqual(agent.artifacts, {})

    def test_empty_evidence_snapshot_saves_explicit_empty_artifact(self):
        payload = {"patient_state": {"meta": {"patient_id": "P0", "target_cancer_id": None}, "evidence_refs": {}},
                   "state_check": {"complete": False}, "normalized_markers": []}
        shared = SimpleNamespace(cutoff_day=50, ready=True, extract=lambda: payload, read=lambda: payload)
        agent = self.agent(shared=shared)
        self.prepare(agent)
        agent.submit({"summary": "no evidence", "limitations": ["No evidence references in snapshot"]})
        evidence = json.loads(Path(agent.artifacts["evidence"][0]).read_text())
        self.assertEqual(evidence["status"], "no_evidence_refs")
        self.assertEqual(evidence["data"], {})

    def test_evidence_batches_and_retries_keep_separate_artifacts(self):
        first = self.agent(directory="first")
        refs = self.prepare(first)
        first.get_evidence(refs[:1])
        first.get_evidence(refs[-1:])
        paths = first.artifacts["evidence"]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(Path(path).exists() for path in paths))
        first.submit({"summary": "done", "limitations": []})
        second = self.agent(directory="retry")
        self.prepare(second)
        with self.assertRaisesRegex(ValueError, "get_patient_evidence"):
            second.submit({"summary": "done", "limitations": []})
        self.assertNotEqual(first.artifacts["state"], second.artifacts["state"])

    def test_patient_only_workflow_still_reviews_and_exposes_artifacts(self):
        workflow = AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs",
                                 plan={"stages": [{"agent": "PatientStateAgent"}]})
        result = workflow.run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual([turn["agent"] for turn in result["turns"]], ["PatientStateAgent", "ReviewAgent"])
        artifacts = result["turns"][0]["artifacts"]
        directory = Path(result["task_dir"])
        self.assertTrue((directory / artifacts["state"]).is_file())
        self.assertTrue((directory / artifacts["evidence"][0]).is_file())
