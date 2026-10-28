"""Offline TypeSafe transport, permission, provenance and workflow integration tests."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from bpcagent.tools.jev import (
    AssessEvidenceArgs, AssessEvidenceTool, JevEvaluator, JevHTTPClient, JevProviderError,
    JevRubrics, JevSettings, patient_pointer, validate_response,
)
from bpcagent.workflow.config import read_object

ROOT = Path(__file__).resolve().parents[1]
RUBRICS = ROOT / "config/jev-rubrics.jsonc"


def settings(**kwargs):
    return JevSettings(enabled=True, rubrics_file=str(RUBRICS), allow_patient_data=True, **kwargs)


def provider_response(payload, *, choice="supported", confidence=0.95, assessable=True):
    answers = {}
    for name, question in payload["questions"].items():
        if question["type"] == "choice":
            selected = ("assessable" if assessable else "insufficient_evidence") if name == "assessability" else choice
            keys = list(question["criteria"])
            if selected not in keys:
                selected = "consistent"
            answers[name] = {"type": "choice", "choice": selected, "confidence": confidence,
                             "probabilities": {key: 1.0 if key == selected else 0.0 for key in keys}}
        else:
            top = len(question["criteria"]) - 1
            answers[name] = {"type": "score", "score": float(top), "confidence": confidence,
                             "legend": {str(i): value for i, value in enumerate(question["criteria"])},
                             "probabilities": {str(i): 1.0 if i == top else 0.0 for i in range(top + 1)}}
    return {"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 200, "output_tokens": 25}}


class FakeJev:
    def __init__(self, **options):
        self.requests = []
        self.options = options

    def evaluate(self, payload):
        self.requests.append(deepcopy(payload))
        return provider_response(payload, **self.options)


def make_tool(tmp_path, client=None, config=None, resolver=None):
    evaluator = JevEvaluator(config or settings(), client=client or FakeJev())
    evidence = {"patient_fields": [{"path": "/value", "value": None,
                                   "field_status": {"/value": {"status": "unknown", "reason": "not_recorded"}}}]}
    tool = AssessEvidenceTool(evaluator, resolver or (lambda args: deepcopy(evidence)), tmp_path, {})
    args = AssessEvidenceArgs(assessment_type="support", statement="The value is known.", patient_fields=["/value"])
    return tool, args


def test_disabled_does_not_load_rubrics_or_initialize_network(tmp_path):
    with patch("bpcagent.tools.jev.JevHTTPClient", side_effect=AssertionError("Must not construct client")):
        engine = JevEvaluator(JevSettings(rubrics_file="missing"), allow_network=True)
        tool = AssessEvidenceTool(engine, lambda args: pytest.fail("Must not resolve data"), tmp_path, {})
        result = tool.run(AssessEvidenceArgs(assessment_type="support", statement="Example"))
    assert result["status"] == "disabled"
    assert result["judge_confidence"] is None
    assert engine.calls == 0


def test_native_http_contract_and_no_secret_in_errors():
    captured = []

    def handler(request):
        captured.append(request)
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(200, json=provider_response(json.loads(request.content)))

    engine = JevEvaluator(settings())
    payload = {"model": "jev-1.14.0", "state": {"value": None}, "questions": engine.questions("support")}
    client = JevHTTPClient(settings(), SecretStr("private-test-key"), transport=httpx.MockTransport(handler))
    response = client.evaluate(payload)
    validate_response(response, payload["questions"])
    assert str(captured[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert set(json.loads(captured[0].content)) == {"model", "state", "questions"}
    client = JevHTTPClient(settings(), "private-test-key", transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="private-test-key")))
    with pytest.raises(JevProviderError, match="^http_401$"):
        client.evaluate(payload)


@pytest.mark.parametrize("case", ["redirect", "timeout", "large", "invalid_json"])
def test_transport_fails_closed(case):
    def handler(request):
        if case == "timeout":
            raise httpx.ReadTimeout("private-test-key")
        if case == "redirect":
            return httpx.Response(307, headers={"Location": "https://another.invalid"})
        return httpx.Response(200, content=b"x" * (100000 if case == "large" else 10))
    client = JevHTTPClient(settings(), "private-test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(JevProviderError) as error:
        client.evaluate({})
    assert "private-test-key" not in str(error.value)


def test_high_confidence_in_missing_evidence_does_not_become_high_quality(tmp_path):
    client = FakeJev(choice="insufficient_evidence", confidence=0.98)
    tool, args = make_tool(tmp_path, client)
    result = tool.run(args)
    assert result["status"] == "assessed"
    assert result["assessment"] == "insufficient_evidence"
    assert result["judge_confidence"] == 0.98
    assert result["review_required"] and not result["automatic_weight_change"]
    assert result["normalized_relevance"] is None
    assert "insufficient_evidence" in result["review_flags"]
    field = client.requests[0]["state"]["evidence"]["patient_fields"][0]
    assert field["value"] is None
    assert field["field_status"]["/value"]["status"] == "unknown"
    audit = json.loads(Path(tool.artifacts["jev_audit"][0]).read_text())
    assert audit["request_sha256"] and audit["rubric_sha256"]
    assert audit["response"]["model"] == "jev-1.14.0"


@pytest.mark.parametrize("assessable,confidence,expected", [(True, 0.95, 1.0), (False, 0.95, None), (True, 0.2, None)])
def test_relevance_keeps_assessability_and_confidence_separate(tmp_path, assessable, confidence, expected):
    tool, args = make_tool(tmp_path, FakeJev(assessable=assessable, confidence=confidence))
    result = tool.run(args.model_copy(update={"assessment_type": "relevance"}))
    assert result["normalized_relevance"] == expected
    assert result["review_required"]
    if not assessable:
        assert result["assessment"] is None
        assert result["answers"]["assessment"]["score"] == 3.0  # Raw judgment remains available.


def test_budgets_count_failed_calls_and_are_shared_between_turns(tmp_path):
    class Failing:
        def evaluate(self, payload):
            raise RuntimeError("never-log-this")
    tool, args = make_tool(tmp_path / "one", Failing(), settings(max_calls_per_turn=1, max_calls_per_run=2))
    assert tool.run(args)["reason"] == "provider_error"
    assert tool.run(args)["reason"] == "budget_exhausted"
    another = AssessEvidenceTool(tool.evaluator, tool.resolve_evidence, tmp_path / "two", {})
    assert another.run(args)["reason"] == "provider_error"
    third = AssessEvidenceTool(tool.evaluator, tool.resolve_evidence, tmp_path / "three", {})
    assert third.run(args)["reason"] == "budget_exhausted"
    assert tool.evaluator.calls == 2
    assert "never-log-this" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_offline_and_patient_permissions_prevent_calls(tmp_path):
    engine = JevEvaluator(settings(), allow_network=False)
    tool = AssessEvidenceTool(engine, lambda args: pytest.fail("No read"), tmp_path / "offline", {})
    args = AssessEvidenceArgs(assessment_type="support", statement="Example", patient_fields=["/value"])
    assert tool.run(args)["reason"] == "offline_mode"
    engine = JevEvaluator(settings().model_copy(update={"allow_patient_data": False}), client=FakeJev())
    tool = AssessEvidenceTool(engine, lambda args: pytest.fail("No read"), tmp_path / "permission", {})
    assert tool.run(args)["reason"] == "patient_data_not_authorized"
    assert engine.calls == 0


def test_empty_evidence_oversized_input_and_resolution_errors_never_call_model(tmp_path):
    tool, args = make_tool(tmp_path)
    assert tool.run(args.model_copy(update={"patient_fields": []}))["reason"] == "no_evidence_selected"
    tool.resolve_evidence = lambda args: {"text": "x" * 100000}
    assert tool.run(args)["reason"] == "input_too_large_select_narrower_evidence"
    tool.resolve_evidence = lambda args: (_ for _ in ()).throw(ValueError("private-source-path"))
    assert tool.run(args)["reason"] == "invalid_evidence_or_configuration"
    assert tool.evaluator.calls == 0


@pytest.mark.parametrize("mutation", ["nan", "missing_option", "sum", "wrong_choice", "missing_answer", "bad_usage", "wrong_type"])
def test_invalid_responses_are_rejected(mutation):
    engine = JevEvaluator(settings())
    questions = engine.questions("support")
    response = provider_response({"questions": questions})
    answer = response["answers"]["assessment"]
    if mutation == "nan":
        answer["confidence"] = float("nan")
    elif mutation == "missing_option":
        answer["probabilities"].pop("not_applicable")
    elif mutation == "sum":
        answer["probabilities"]["supported"] = 0.2
    elif mutation == "wrong_choice":
        answer["choice"] = "contradicted"
    elif mutation == "missing_answer":
        response["answers"] = {}
    elif mutation == "bad_usage":
        response["usage"]["input_tokens"] = -1
    else:
        answer["type"] = "noul"
    with pytest.raises(ValueError):
        validate_response(response, questions)


def test_invalid_response_is_not_a_citable_assessment(tmp_path):
    tool, args = make_tool(tmp_path, SimpleNamespace(evaluate=lambda payload: {"secret": "never-log"}))
    result = tool.run(args)
    assert result["reason"] == "invalid_provider_response"
    assert not tool.assessment_ids and result["judge_confidence"] is None
    assert "never-log" not in Path(tool.artifacts["jev_audit"][0]).read_text()


def test_json_pointers_preserve_null_false_zero_and_reject_paths():
    state = {"a": [None, False, 0], "x/y": {"~z": 1}}
    assert patient_pointer(state, "/a/0") is None
    assert patient_pointer(state, "/a/1") is False
    assert patient_pointer(state, "/a/2") == 0
    assert patient_pointer(state, "/x~1y/~0z") == 1
    for pointer in ("", "/", "/a/-1", "/a/01", "/x~2y", "/evaluation", "/etc/passwd"):
        with pytest.raises((ValueError, KeyError, IndexError)):
            patient_pointer(state, pointer)


def test_rubric_cannot_remove_missing_evidence_option():
    data = read_object(RUBRICS)
    del data["support"]["criteria"]["insufficient_evidence"]
    with pytest.raises(ValueError):
        JevRubrics.model_validate(data)


def test_workflow_evaluations_reach_review_and_keep_cutoff(tmp_path):
    from test_workflow import fixture_dataset
    from bpcagent.agents import LLMResponse, ToolCall
    from bpcagent.workflow import AgentWorkflow
    from bpcagent.workflow.demo import DemoLLM
    client = FakeJev()
    seen_review_assessments = []

    class AssessingDemo(DemoLLM):
        def __init__(self, role):
            super().__init__(role)
            self.assessed = False

        def invoke(self, messages, **kwargs):
            if self.tool_step == 2 and not self.assessed:
                self.assessed = True
                return LLMResponse(finish_reason="tool_calls", tool_calls=[ToolCall(
                    id="jev_assessment", name="assess_evidence", arguments=json.dumps({
                        "assessment_type": "support", "statement": "Patient observations are present.",
                        "patient_fields": ["/observations"]}))])
            response = super().invoke(messages, **kwargs)
            if self.tool_step == 3:
                results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                assessment = next(r["data"] for r in results if r["tool_name"] == "assess_evidence")
                for call in response.tool_calls:
                    if call.name == "submit_output":
                        output = json.loads(call.arguments)
                        output["assessment_refs"] = [assessment["assessment_id"]]
                        call.arguments = json.dumps(output)
                if self.role == "ReviewAgent":
                    context = next(r["data"] for r in results if r["tool_name"] == "get_stage_context")
                    seen_review_assessments.extend(context["stage_result"]["evidence_assessments"])
            return response

    workflow = AgentWorkflow(data_dir=fixture_dataset(tmp_path / "data"), workspace_root=tmp_path / "runs",
                             llm_factory=AssessingDemo, patient_shell={"mode": "disabled"},
                             jev=settings(), jev_client_factory=lambda: client)
    result = workflow.run(patient_id="P1", cutoff_day=50)
    assert result["finished"]
    assert result["jev"]["requests_attempted"] == 6
    assert len(seen_review_assessments) == 3
    for request in client.requests:
        evidence = request["state"]["evidence"]
        assert evidence["patient_boundary"]["cutoff_day"] == 50
        assert "999" not in json.dumps(evidence["patient_fields"])  # Fixture's future marker value.
    for turn in result["turns"]:
        assert turn["output"]["assessment_refs"] == [turn["evidence_assessments"][0]["assessment_id"]]
        assert turn["artifacts"]["jev_audit"]


@pytest.fixture
def prepared_agent(tmp_path):
    from test_workflow import fixture_dataset
    from bpcagent.agents import Config
    from bpcagent.workflow.agents import TargetSelectionAgent
    from bpcagent.workflow.demo import DemoLLM
    from bpcagent.workflow.state import SharedPatientState
    from bpcagent.tools.guideline_library import build_library
    data = fixture_dataset(tmp_path / "patients")
    shared = SharedPatientState(patient_id="P1", cutoff_day=50, data_dir=data)
    shared.extract()
    library_root = tmp_path / "library_data"
    library_root.mkdir()
    (library_root / "text.json").write_text(json.dumps([
        {"heading": "Test evidence", "anchor": "example", "text": "Synthetic evidence, not medical guidance."}]))
    source = {"id": "test", "title": "Synthetic source", "version": "v1", "publication_date": "2026-01-01",
              "kind": "test_document", "format": "sections_json", "path": "text.json",
              "source_url": "https://example.invalid", "processing_allowed": True,
              "external_model_allowed": True, "permission_basis": "Synthetic test fixture"}
    library_config = tmp_path / "library.json"
    library_config.write_text(json.dumps({"data_root": "library_data", "sources": [source,
        {**source, "id": "blocked", "processing_allowed": False, "external_model_allowed": False}]}))
    build_library(library_config)
    client = FakeJev()
    agent = TargetSelectionAgent(shared=shared, context={"top_m": 5}, directory=tmp_path / "agent",
                                 config=Config(model="offline"), llm=DemoLLM("TargetSelectionAgent"),
                                 guidelines={"enabled": True, "library_config": str(library_config)},
                                 jev_evaluator=JevEvaluator(settings(), client=client))
    agent.get_shared()
    agent.get_context()
    return agent, client


def test_host_rejects_unknown_sources_and_future_paths_without_network(prepared_agent):
    agent, client = prepared_agent
    for arguments in ({"patient_evidence_refs": ["not-in-this-snapshot"]}, {"patient_fields": ["/evaluation"]},
                      {"document_refs": ["blocked:fake:section-0001"]}, {"document_refs": ["test:fake:section-0001"]}):
        result = agent.jev_tool.run(AssessEvidenceArgs(assessment_type="support", statement="Example", **arguments))
        assert result["status"] == "not_assessed"
    assert not client.requests


def test_host_reads_actual_authorized_document_and_does_not_mutate_patient(prepared_agent):
    agent, client = prepared_agent
    before = agent.shared.read()
    source = agent.guideline_tools.library.section("test", "section-0001")
    result = agent.jev_tool.run(AssessEvidenceArgs(assessment_type="relevance", statement="Synthetic evidence",
                                                 document_refs=[source["citation_id"]], patient_fields=["/observations"]))
    assert result["status"] == "assessed"
    assert client.requests[0]["state"]["evidence"]["documents"][0]["text"] == source["text"]
    assert agent.shared.read() == before
    # Jev reading a source does not falsely claim the task model read its full text.
    assert source["citation_id"] not in agent.guideline_tools.citable_refs


def test_fabricated_assessment_ids_are_rejected(prepared_agent):
    agent, _ = prepared_agent
    with pytest.raises(ValueError, match="Assessment references"):
        agent.submit({"candidates": [], "limitations": [], "assessment_refs": ["jev_invented"]})


def test_disabled_workflow_has_no_assessment_tool(tmp_path):
    from bpcagent.agents import Config
    from bpcagent.workflow.agents import TargetSelectionAgent
    from bpcagent.workflow.demo import DemoLLM
    agent = TargetSelectionAgent(shared=None, context={}, directory=tmp_path,
                                 config=Config(model="offline"), llm=DemoLLM("TargetSelectionAgent"),
                                 jev_evaluator=JevEvaluator(JevSettings()))
    assert agent.jev_tool is None
    assert "assess_evidence" not in agent.tool_registry.list_tools()
