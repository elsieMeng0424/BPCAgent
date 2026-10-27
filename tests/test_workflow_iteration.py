"""Agent turns, revision loops and independently versioned workflow plans."""

from collections import Counter
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from bpcagent import Config
from bpcagent.workflow import AgentWorkflow, WorkflowPlan
from bpcagent.workflow.agents import PatientStateAgent, ReviewAgent
from bpcagent.workflow.demo import DemoLLM
from bpcagent.workflow.models import ReviewOutput
from bpcagent.workflow.state import SharedPatientState

if __package__:
    from .test_workflow import AlteredDemo, fixture_dataset
else:
    from test_workflow import AlteredDemo, fixture_dataset


class WorkflowIterationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = fixture_dataset(self.root / "data")
        stdout = redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def workflow(self, **options):
        return AgentWorkflow(data_dir=self.data, workspace_root=self.root / "runs", **options)

    def test_retry_only_rejected_agent_and_keep_accepted_upstream(self):
        counts = Counter()

        def factory(role):
            counts[role] += 1
            return AlteredDemo(role, disposition="revise" if role == "ReviewAgent" and counts[role] == 2 else None)

        result = self.workflow(llm_factory=factory).run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual(counts, {"PatientStateAgent": 1, "TargetSelectionAgent": 2,
                                  "DrugSelectionAgent": 1, "ReviewAgent": 4})
        second = json.loads((Path(result["task_dir"]) / "05_TargetSelectionAgent/context.json").read_text())
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["accepted_outputs"], {"PatientStateAgent": result["accepted_outputs"]["PatientStateAgent"]})
        self.assertEqual(second["previous_feedback"]["stage_result"]["turn"], 3)
        self.assertTrue(second["previous_feedback"]["review_result"]["output"]["reason"])

    def test_failed_task_can_be_retried_by_review(self):
        counts = Counter()

        def factory(role):
            counts[role] += 1
            return AlteredDemo(role, early_stop=role == "PatientStateAgent" and counts[role] == 1,
                               disposition="revise" if role == "ReviewAgent" and counts[role] == 1 else None)

        result = self.workflow(llm_factory=factory).run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual(result["turns"][0]["status"], "failed")
        self.assertEqual(result["turns"][2]["status"], "completed")

    def test_failed_task_never_advanced_by_incorrect_approval(self):
        result = self.workflow(llm_factory=lambda role: AlteredDemo(
            role, early_stop=role == "PatientStateAgent", disposition="proceed" if role == "ReviewAgent" else None
        )).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["accepted_outputs"], {})
        self.assertEqual(len(result["turns"]), 2)

    def test_agent_exception_is_reviewed_and_does_not_leak_error_text(self):
        def factory(role):
            if role == "PatientStateAgent":
                raise RuntimeError("DO-NOT-ECHO-SECRET")
            return DemoLLM(role)
        result = self.workflow(llm_factory=factory).run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["turns"][1]["reviews_turn"], 1)
        self.assertNotIn("DO-NOT-ECHO-SECRET", json.dumps(result))

    def test_per_stage_retry_budget_plan_snapshot_and_instructions(self):
        plan = {"version": "candidate-2", "stages": [{
            "agent": "PatientStateAgent", "max_attempts": 3, "instructions": "Report unknown units explicitly."
        }], "review_instructions": "Reject unsupported unit inference."}
        workflow = self.workflow(plan=plan, max_stage_attempts=1, llm_factory=lambda role: AlteredDemo(
            role, disposition="revise" if role == "ReviewAgent" else None))
        plan["stages"].clear()
        workflow.plan.stages.clear()
        result = workflow.run(patient_id="P1", cutoff_day=50)
        self.assertEqual(result["status"], "revision_limit")
        self.assertEqual(len(result["turns"]), 6)
        self.assertEqual(result["accepted_outputs"], {})
        directory = Path(result["task_dir"])
        self.assertEqual(json.loads((directory / "workflow_plan.json").read_text()), result["workflow_plan"])
        review = json.loads((directory / "02_ReviewAgent/context.json").read_text())
        self.assertEqual(review["instructions"], "Reject unsupported unit inference.")
        self.assertEqual(review["stage_instructions"], "Report unknown units explicitly.")
        transcript = json.loads((directory / "01_PatientStateAgent/transcript.json").read_text())
        self.assertIn("Report unknown units explicitly.", transcript[0]["content"])

    def test_invalid_plans_rejected_before_execution(self):
        for plan in ([], {"stages": []}, {"stages": [{"agent": "MetaAgent"}]},
                     {"stages": [{"agent": "DrugSelectionAgent"}]},
                     {"stages": [{"agent": "PatientStateAgent"}] * 2},
                     {"stages": [{"agent": "PatientStateAgent", "max_attempts": True}]},
                     {"disable_review": True}):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                self.workflow(plan=plan)

    def test_state_check_is_inside_patient_turn_and_required_every_retry(self):
        shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=self.data)
        for attempt in (1, 2):
            agent = PatientStateAgent(shared=shared, context={"attempt": attempt}, directory=self.root / f"attempt_{attempt}",
                                      config=Config(), llm=DemoLLM("PatientStateAgent"))
            agent.extract()
            agent.get_context()
            with self.assertRaisesRegex(ValueError, "state_check"):
                agent.submit({"summary": "done", "limitations": []})
            agent.state_check()
            agent.get_evidence(list(shared.read()["patient_state"]["evidence_refs"])[:1])
            agent.submit({"summary": "done", "limitations": []})
        review = ReviewAgent(shared=shared, context={}, directory=self.root, config=Config(), llm=DemoLLM("ReviewAgent"))
        self.assertNotIn("state_check", review.tool_registry.list_tools())

    def test_review_reason_required_and_nonblank(self):
        for reason in (None, "", "  "):
            output = {"disposition": "revise", "issues": [], "evidence_gaps": []}
            if reason is not None:
                output["reason"] = reason
            with self.assertRaises(ValidationError):
                ReviewOutput.model_validate(output)

    def test_meta_needs_no_config_or_key(self):
        roles = WorkflowPlan().active_roles
        configs = {role: Config(model=role, api_key="test", base_url="https://example.invalid") for role in roles}
        configs["MetaAgent"] = Config()  # Inactive: not validated or constructed.
        workflow = self.workflow(mode="live", agent_configs=configs)
        self.assertEqual(set(workflow.configs), set(roles))
