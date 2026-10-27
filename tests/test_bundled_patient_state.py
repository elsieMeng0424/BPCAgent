"""Bundled extractor, resource packaging and cutoff-bounded Agent integration."""

import io
import json
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import bpc_state
from bpc_state.rules import RuleSet

from bpcagent import Config
from bpcagent.tools.bpc_patient import create_patient_tools
from bpcagent.workflow.agents import PatientStateAgent
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.state import SharedPatientState
if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


class BundledPatientStateTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = fixture_dataset(self.root / "data")

    def test_bundled_package_and_default_rules(self):
        # Editable installs must not silently resolve the old sibling checkout.
        repository = Path(__file__).resolve().parents[1]
        source = Path(bpc_state.__file__).resolve()
        if "site-packages" not in source.parts:
            self.assertTrue(source.is_relative_to(repository / "vendor" / "BPCStateExtractor"), source)
        self.assertEqual(bpc_state.__version__, "0.3.0")
        rules = json.loads(files("bpc_state.resources").joinpath("field_rules.json").read_text())
        self.assertEqual(RuleSet().config, rules)

    def test_factory_uses_bundled_extractor_without_external_rules(self):
        state_tool, evidence_tool = create_patient_tools(
            patient_id="P1", max_cutoff_day=50, data_dir=self.data, work_dir=self.root / "out")
        with redirect_stdout(io.StringIO()):
            state = state_tool.run(state_tool.args_schema(cutoff_day=50))
            ref = next(iter(state["evidence_refs"]))
            evidence = evidence_tool.run(evidence_tool.args_schema(cutoff_day=50, evidence_refs=[ref]))
        self.assertEqual(state["meta"]["schema_version"], "1.2")
        self.assertEqual(evidence[ref], state["evidence_refs"][ref])
        self.assertIn("field_status", state)

    def test_workflow_evidence_tool_cannot_change_patient_or_cutoff(self):
        shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        with redirect_stdout(io.StringIO()):
            agent = PatientStateAgent(shared=shared, context={"stage": "PatientStateAgent"},
                                      directory=self.root, config=Config(model="offline"),
                                      llm=DemoLLM("PatientStateAgent"))
        registry = agent.tool_registry
        state = agent.extract()["patient_state"]
        ref = next(iter(state["evidence_refs"]))
        with redirect_stdout(io.StringIO()):
            result = registry.execute_tool("get_patient_evidence", {"evidence_refs": [ref]})
            self.assertEqual(result.status, "success")
            self.assertEqual(result.data[ref], state["evidence_refs"][ref])
            for extra in ({"cutoff_day": 200}, {"patient_id": "P2"}, {"data_dir": "/tmp"}):
                rejected = registry.execute_tool("get_patient_evidence", {"evidence_refs": [ref], **extra})
                self.assertEqual(rejected.error.code, "invalid_arguments")
            later = bpc_state.BPCStateExtractor(self.data).get_patient_state("P1", 150)
            future_refs = set(later["evidence_refs"]) - set(state["evidence_refs"])
            self.assertTrue(future_refs)
            rejected = registry.execute_tool("get_patient_evidence", {"evidence_refs": [next(iter(future_refs))]})
            self.assertEqual(rejected.error.code, "execution_error")
        self.assertNotIn("evaluation", str(registry.to_openai_tools()))
        self.assertEqual(shared.read()["patient_state"], state)


if __name__ == "__main__":
    unittest.main()
