"""Offline budget, lossless compaction, readback and Agent integration checks."""

from copy import deepcopy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from bpcagent import BasicAgent, Config, LLMResponse, ReActAgent, ToolCall
from bpcagent.agents.context import ContextManager, ContextPolicy, encode
from bpcagent.agents.exceptions import AgentException
from bpcagent.tools import ToolArgs, ToolRegistry, ToolResult
from bpcagent.workflow import AgentWorkflow

if __package__:
    from .test_workflow import fixture_dataset
else:
    from test_workflow import fixture_dataset


class EmptyArgs(ToolArgs):
    pass


def tool_messages(name="fetch", data=None):
    return [
        {"role": "system", "content": "Keep patient P1 and cutoff 50 fixed; unknown is not negative."},
        {"role": "user", "content": "Check the facts."},
        {"role": "assistant", "content": None, "tool_calls": [
            ToolCall(id="call1", name=name, arguments="{}").to_dict()]},
        {"role": "tool", "tool_call_id": "call1", "content": ToolResult(
            tool_name=name, call_id="call1", status="success", data=data if data is not None else
            {"value": None, "field_status": "unknown", "number": 1.25, "padding": "患者" * 6000}
        ).model_dump_json()},
    ]


class ContextManagerTests(unittest.TestCase):
    def manager(self, **overrides):
        return ContextManager(ContextPolicy(**{"enabled": True, "max_input_tokens": 6000,
                                               "min_compact_chars": 1000, **overrides}))

    def test_budget_uses_model_route_cap_and_per_call_output_reservation(self):
        manager = self.manager(context_window=32000, gateway_context_window=24000,
                               max_input_tokens=30000, reserve_output_tokens=2000,
                               safety_margin_tokens=1000)
        self.assertEqual(manager.budget(4000), 19000)
        self.assertEqual(manager.budget(1000), 21000)
        with self.assertRaisesRegex(AgentException, "no input budget"):
            manager.budget(24000)
        # No model-name guessing: unknown capacities use only the local input cap.
        self.assertEqual(self.manager().budget(8192), 6000)

    def test_policy_validation(self):
        for options in ({"trigger_ratio": 0.5, "target_ratio": 0.6}, {"trigger_ratio": float("nan")},
                        {"context_window": 100}, {"max_input_tokens": True},
                        {"enabled": "true"}, {"keep_recent_messages": -1}, {"unknown": 1}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                ContextPolicy(**options)

    def test_archives_exact_tool_output_without_mutating_transcript(self):
        original = tool_messages()
        before = deepcopy(original)
        manager = self.manager()
        request = manager.prepare(original, {}, can_read=True)
        self.assertEqual(original, before)
        self.assertEqual(request[:3], original[:3])
        self.assertEqual(request[-1]["tool_call_id"], "call1")
        descriptor = json.loads(request[-1]["content"])
        identity = descriptor["record_id"]
        self.assertEqual(manager.records[identity], original[-1]["content"])
        for field, expected in (("value", None), ("field_status", "unknown"), ("number", 1.25)):
            page = manager.read(identity, "/data/" + field)
            self.assertTrue(page["complete"])
            self.assertEqual(json.loads(page["content"]), expected)
        self.assertLessEqual(manager.events[-1]["estimated_after"], 6000)
        self.assertEqual(manager.events[-1]["status"], "compacted")

    def test_patient_identity_and_cutoff_are_kept_in_index(self):
        data = {"patient_state": {"meta": {"patient_id": "P1", "target_cancer_id": "C7",
                                            "cutoff_day": 50, "schema_version": "1.2"},
                                  "observations": "x" * 15000}}
        request = self.manager().prepare(tool_messages("get_patient_state", data), {}, can_read=True)
        self.assertEqual(json.loads(request[-1]["content"])["patient_boundary"], data["patient_state"]["meta"])

    def test_shell_failure_flags_survive_compaction(self):
        data = {"stdout": "x" * 15000, "stderr": "failed", "exit_code": 2,
                "timed_out": False, "stdout_truncated": True, "stderr_truncated": False}
        request = self.manager().prepare(tool_messages("run_shell", data), {}, can_read=True)
        descriptor = json.loads(request[-1]["content"])
        self.assertEqual(descriptor["execution"]["exit_code"], 2)
        self.assertFalse(descriptor["execution"]["timed_out"])
        self.assertTrue(descriptor["execution"]["stdout_truncated"])

    def test_readback_is_paginated_exact_and_run_scoped(self):
        manager = self.manager()
        request = manager.prepare(tool_messages(), {}, can_read=True)
        identity = json.loads(request[-1]["content"])["record_id"]
        parts, offset = [], 0
        while True:
            page = manager.read(identity, offset=offset, limit=2000)
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(parts), manager.records[identity])
        with self.assertRaises(ValueError):
            self.manager().read(identity)
        manager.reset()
        with self.assertRaises(ValueError):
            manager.read(identity)
        with self.assertRaises(ValueError):
            manager.read("/etc/passwd")

    def test_pointer_escaping_and_page_validation(self):
        manager = self.manager()
        manager.records["r"] = encode({"a/b": {"~": [None, "unknown", 0]}})
        self.assertEqual(json.loads(manager.read("r", "/a~1b/~0/2")["content"]), 0)
        for kwargs in ({"pointer": "/a~2"}, {"pointer": "/a~1b/~0/-1"}, {"offset": -1},
                       {"offset": True}, {"limit": 2001}, {"offset": 10000}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, KeyError)):
                manager.read("r", **kwargs)

    def test_protected_content_fails_closed_instead_of_silent_truncation(self):
        for name in ("get_stage_context", "state_check", "submit_output", "read_context_record"):
            manager = self.manager()
            messages = tool_messages(name)
            before = deepcopy(messages)
            with self.subTest(tool=name), self.assertRaises(AgentException) as error:
                manager.prepare(messages, {}, can_read=True)
            self.assertEqual(error.exception.code, "context_budget_exceeded")
            self.assertEqual(messages, before)
        with self.assertRaises(AgentException):
            self.manager().prepare([{"role": "user", "content": "x" * 20000}], {}, can_read=True)

    def test_no_reader_does_not_archive_inaccessible_data(self):
        manager = self.manager()
        with self.assertRaises(AgentException):
            manager.prepare(tool_messages(), {}, can_read=False)
        self.assertEqual(manager.records, {})

    def test_tool_schemas_count_towards_budget(self):
        manager = self.manager()
        with self.assertRaises(AgentException):
            manager.prepare([], {"tools": [{"description": "x" * 10000}]}, can_read=True)

    def test_pending_or_orphan_tool_calls_are_rejected(self):
        messages = tool_messages()
        for bad in (messages[:-1], messages[:2] + messages[3:], messages + [messages[-1]]):
            with self.subTest(bad=bad), self.assertRaises(AgentException) as error:
                self.manager().prepare(bad, {}, can_read=True)
            self.assertEqual(error.exception.code, "invalid_context")

    def test_parallel_calls_and_provider_reasoning_remain_intact(self):
        messages = tool_messages()
        messages[2]["tool_calls"].append(ToolCall(id="call2", name="fetch", arguments="{}").to_dict())
        messages[2]["reasoning_content"] = "provider reasoning"
        messages[2]["response_items"] = [{"type": "reasoning", "encrypted_content": "opaque"}]
        messages.append({**messages[-1], "tool_call_id": "call2"})
        request = self.manager().prepare(messages, {}, can_read=True)
        self.assertEqual(request[2], messages[2])
        self.assertEqual([m["tool_call_id"] for m in request if m["role"] == "tool"], ["call1", "call2"])

    def test_old_assistant_output_compacts_but_user_constraints_remain(self):
        messages = [{"role": "user", "content": "Keep my original constraints."},
                    {"role": "assistant", "content": "old answer " * 2000},
                    {"role": "user", "content": "Continue."}]
        manager = self.manager(keep_recent_messages=1)
        request = manager.prepare(messages, {}, can_read=True)
        self.assertEqual(request[0], messages[0])
        self.assertEqual(request[2], messages[2])
        identity = json.loads(request[1]["content"])["record_id"]
        self.assertEqual(manager.records[identity], messages[1]["content"])

    def test_disabled_policy_is_a_noop_copy(self):
        original = tool_messages()
        manager = ContextManager(ContextPolicy())
        request = manager.prepare(original, {})
        self.assertEqual(request, original)
        self.assertIsNot(request, original)
        self.assertEqual(manager.events, [])


class ContextAgentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        stdout = redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def config(self):
        return Config(model="offline", max_tool_iterations=4, context={
            "enabled": True, "max_input_tokens": 7000, "min_compact_chars": 1000})

    def test_basic_and_react_read_archived_tool_data_and_save_full_audit(self):
        for cls in (BasicAgent, ReActAgent):
            with self.subTest(agent=cls.__name__):
                registry = ToolRegistry()
                registry.register_function("fetch", "Fetch fixture data", lambda: {"value": None, "padding": "x" * 15000}, args_schema=EmptyArgs)
                seen = []

                def invoke(messages, **options):
                    seen.append(deepcopy(messages))
                    if len(seen) == 1:
                        return LLMResponse(finish_reason="tool_calls", tool_calls=[ToolCall(id="c1", name="fetch", arguments="{}")])
                    if len(seen) == 2:
                        identity = json.loads(messages[-1]["content"])["record_id"]
                        return LLMResponse(finish_reason="tool_calls", tool_calls=[ToolCall(
                            id="c2", name="read_context_record", arguments=encode({"record_id": identity, "pointer": "/data/value"}))])
                    readback = json.loads(messages[-1]["content"])["data"]
                    self.assertEqual(json.loads(readback["content"]), None)
                    return LLMResponse(content="Verified null; not inferred.", finish_reason="stop")

                llm = Mock(invoke=invoke)
                agent = cls("test", llm=llm, config=self.config(), tool_registry=registry)
                agent.work_dir = self.root / cls.__name__
                result = agent.run("Read the value")
                self.assertTrue(result.finished, result.info)
                self.assertNotIn("read_context_record", registry.list_tools())
                raw_tool = next(m for m in result.transcript if m.get("tool_call_id") == "c1")
                self.assertEqual(len(json.loads(raw_tool["content"])["data"]["padding"]), 15000)
                log = json.loads(agent.log_path.read_text())
                self.assertEqual(log["transcript"], result.transcript)
                self.assertTrue(log["context"]["records"])
                self.assertTrue(any(e["status"] == "compacted" for e in log["context"]["events"]))

    def test_budget_failure_happens_before_model_call_and_is_logged(self):
        llm = Mock()
        agent = BasicAgent("test", llm=llm, config=self.config())
        agent.work_dir = self.root
        result = agent.run("x" * 20000)
        self.assertFalse(result.finished)
        self.assertEqual(result.info["stop_reason"], "context_budget_exceeded")
        llm.invoke.assert_not_called()
        self.assertEqual(json.loads(agent.log_path.read_text())["context"]["events"][-1]["status"], "budget_exceeded")

    def test_request_output_override_reduces_budget_and_default_is_explicit(self):
        agent = BasicAgent("test", llm=Mock(), config=self.config())
        request, options = agent._prepare_model_request([], {})
        self.assertEqual(options["max_tokens"], 8192)
        config = Config(context={"enabled": True, "context_window": 16000, "max_input_tokens": 12000,
                                 "reserve_output_tokens": 2000, "safety_margin_tokens": 1000})
        agent = BasicAgent("test", llm=Mock(), config=config)
        agent._prepare_model_request([], {"max_tokens": 10000})
        self.assertEqual(agent.context_manager.events[-1]["input_budget"], 5000)

    def test_streaming_budget_guard_preserves_full_history(self):
        llm = Mock(think=Mock(return_value=iter(["ok"])))
        agent = BasicAgent("test", llm=llm, config=self.config(), enable_tool_calling=False)
        agent.work_dir = self.root
        self.assertEqual(list(agent.run_using_stream("short")), ["ok"])
        with self.assertRaises(AgentException):
            list(agent.run_using_stream("x" * 20000))
        self.assertEqual(llm.think.call_count, 1)
        log = json.loads(agent.log_path.read_text())
        self.assertEqual(log["transcript"][-1]["content"], "x" * 20000)

    def test_workflow_audits_context_and_keeps_shared_snapshot_unchanged(self):
        data = fixture_dataset(self.root / "data")
        workflow = AgentWorkflow(data_dir=data, workspace_root=self.root / "runs")
        original = workflow.run(patient_id="P1", cutoff_day=50)
        workflow.configs = {role: Config.resolve(cfg, overrides={"context": {
            "enabled": True, "max_input_tokens": 1000000}}, environ={})
            for role, cfg in workflow.configs.items()}
        result = workflow.run(patient_id="P1", cutoff_day=50)
        self.assertTrue(result["finished"])
        self.assertEqual(original["patient_state_sha256"], result["patient_state_sha256"])
        audits = list(Path(result["task_dir"]).glob("*/context_usage.json"))
        self.assertEqual(len(audits), 6)
        for path in audits:
            audit = json.loads(path.read_text())
            self.assertTrue(audit["events"])
            self.assertEqual(audit["records"], {})
            transcript = json.loads((path.parent / "transcript.json").read_text())
            self.assertEqual(audit["last_request"], transcript[:-1])

    def test_workflow_review_receives_explicit_context_budget_failure(self):
        data = fixture_dataset(self.root / "data")
        workflow = AgentWorkflow(data_dir=data, workspace_root=self.root / "runs")
        workflow.configs["PatientStateAgent"] = Config.resolve(workflow.configs["PatientStateAgent"],
            overrides={"context": {"enabled": True, "max_input_tokens": 1000}}, environ={})
        result = workflow.run(patient_id="P1", cutoff_day=50)
        self.assertFalse(result["finished"])
        self.assertEqual(result["turns"][0]["error"], "context_budget_exceeded")
        self.assertEqual(result["turns"][1]["agent"], "ReviewAgent")
        self.assertEqual(result["accepted_outputs"], {})

    def test_reader_scope_is_reset_between_runs_and_is_not_shared_between_agents(self):
        registry = ToolRegistry()
        first = BasicAgent("first", llm=Mock(), config=self.config(), tool_registry=registry)
        second = BasicAgent("second", llm=Mock(), config=self.config(), tool_registry=registry)
        original = tool_messages()
        request = first.context_manager.prepare(original, {}, can_read=True)
        identity = json.loads(request[-1]["content"])["record_id"]
        other = second.tool_registry.execute_tool("read_context_record", {"record_id": identity})
        self.assertEqual(other.status, "error")
        first.llm.invoke.return_value = LLMResponse(content="new run", finish_reason="stop")
        first.work_dir = self.root
        self.assertTrue(first.run("new task").finished)
        result = first.tool_registry.execute_tool("read_context_record", {"record_id": identity})
        self.assertEqual(result.status, "error")
