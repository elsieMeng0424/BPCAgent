"""验证 Agent 产物、三态完成标记、消息快照和失败边界。"""

from dataclasses import asdict
import json
import unittest
from unittest.mock import Mock

from bpcagent import AgentResult, BasicAgent, CalculatorTool, Config, LLMResponse, LLM, ReActAgent, ToolCall, ToolRegistry
from bpcagent.agents.exceptions import AgentException, ConfigException, LLMException

if __package__:
    from .logging_support import isolate_agent_logs
else:
    from logging_support import isolate_agent_logs


def reply(text):
    return LLMResponse(content=text, finish_reason="stop")


def call(arguments='{"input":"2+3*4"}'):
    return LLMResponse(tool_calls=[ToolCall(id="call_1", name="calculate", arguments=arguments)],
                       finish_reason="tool_calls")


class ResultTests(unittest.TestCase):
    def setUp(self):
        isolate_agent_logs(self)

    def make_agent(self, cls, *responses, tools=False, **settings):
        llm = Mock(spec=LLM)
        llm.invoke.side_effect = responses
        registry = ToolRegistry()
        if tools:
            registry.register_tool(CalculatorTool())
        return cls("test", llm=llm, tool_registry=registry, sys_prompt="简洁回答",
                   config=Config.resolve(environ={}, overrides=settings))

    def test_default_containers_and_tristate_are_independent(self):
        first, second = AgentResult(), AgentResult()
        first.output["data"] = [1]
        first.transcript.append({"role": "user", "content": "hello"})
        first.info["note"] = "说明"
        self.assertEqual(asdict(second), {"output": {}, "transcript": [], "info": {}, "finished": None})
        for finished in (True, False, None):
            self.assertIs(asdict(AgentResult(finished=finished))["finished"], finished)
        self.assertIsNone(first.finished)

    def test_success_and_history_context(self):
        agent = self.make_agent(BasicAgent, reply("已记住"), reply("小明"))
        first = agent.run("我叫小明")
        self.assertIsInstance(first, AgentResult)
        self.assertIs(first.finished, True)
        self.assertEqual(first.output, {"text": "已记住"})
        self.assertEqual(first.info, {})
        self.assertEqual([m["role"] for m in first.transcript], ["system", "user", "assistant"])
        first.output["text"] = "修改返回值"
        first.transcript[-1]["content"] = "修改记录"
        second = agent.run("我叫什么？")
        self.assertEqual(second.output, {"text": "小明"})
        self.assertEqual(second.transcript[1:3], [
            {"role": "user", "content": "我叫小明"}, {"role": "assistant", "content": "已记住"}])
        agent.clear_history()
        self.assertEqual(second.transcript[-1]["content"], "小明")

    def test_tool_transcript_and_react_snapshot(self):
        for cls in (BasicAgent, ReActAgent):
            with self.subTest(agent=cls.__name__):
                agent = self.make_agent(cls, call(), reply("14"), reply("下一次回答"), tools=True)
                result = agent.run("计算")
                self.assertIs(result.finished, True)
                self.assertEqual([m["role"] for m in result.transcript],
                                 ["system", "user", "assistant", "tool", "assistant"])
                self.assertEqual(result.transcript[2]["tool_calls"][0]["id"], "call_1")
                self.assertEqual(result.transcript[3]["tool_call_id"], "call_1")
                self.assertEqual(json.loads(result.transcript[3]["content"])["data"], 14)
                result.transcript[2]["tool_calls"][0]["function"]["name"] = "changed"
                if cls is ReActAgent:
                    self.assertEqual(agent.current_history[2]["tool_calls"][0]["function"]["name"], "calculate")
                later = agent.run("下一问")
                self.assertEqual(result.transcript[-1]["content"], "14")
                self.assertEqual(later.output, {"text": "下一次回答"})
                self.assertFalse(any(m.get("tool_calls") for m in later.transcript))

    def test_tool_error_does_not_determine_episode_completion(self):
        for cls in (BasicAgent, ReActAgent):
            agent = self.make_agent(cls, call("bad-json"), call(), reply("14"), tools=True)
            result = agent.run("计算")
            tools = [json.loads(m["content"]) for m in result.transcript if m["role"] == "tool"]
            self.assertEqual([t["status"] for t in tools], ["error", "success"])
            self.assertEqual(tools[0]["error"]["code"], "invalid_arguments")
            self.assertIs(result.finished, True)
            self.assertEqual(result.info, {})

    def test_limit_keeps_executed_tool_results(self):
        for cls in (BasicAgent, ReActAgent):
            agent = self.make_agent(cls, call(), tools=True, max_tool_iterations=1, max_steps=1)
            result = agent.run("计算")
            self.assertIs(result.finished, False)
            self.assertEqual(result.output, {})
            self.assertEqual(result.info["stop_reason"], "step_limit_exceeded")
            self.assertTrue(result.info["error"])
            self.assertEqual(json.loads(result.transcript[-1]["content"])["data"], 14)
            self.assertEqual(agent.llm.invoke.call_count, 1)
            self.assertEqual(agent.get_history(), [])

    def test_model_failure_preserves_prior_tool_messages(self):
        for cls in (BasicAgent, ReActAgent):
            agent = self.make_agent(cls, call(), LLMException("离线模拟连接失败"), tools=True)
            result = agent.run("计算")
            self.assertIs(result.finished, False)
            self.assertEqual(result.output, {})
            self.assertEqual(result.info, {"stop_reason": "model_error", "error": "离线模拟连接失败"})
            self.assertEqual(result.transcript[-1]["role"], "tool")
            self.assertEqual(json.loads(result.transcript[-1]["content"])["data"], 14)
            self.assertEqual(agent.get_history(), [])

    def test_invalid_and_empty_responses_preserve_received_text(self):
        for response, reason in ((reply(None), "empty_response"),
                                 (LLMResponse(content="未完成的回答", finish_reason="length"), "invalid_response")):
            agent = self.make_agent(BasicAgent, response)
            result = agent.run("问题")
            self.assertIs(result.finished, False)
            self.assertEqual(result.info["stop_reason"], reason)
            self.assertEqual(result.output, {})
            self.assertEqual(result.transcript[-1], {"role": "assistant", "content": response.content})
            self.assertEqual(agent.get_history(), [])

    def test_configuration_and_unexpected_errors_still_raise(self):
        for cls in (BasicAgent, ReActAgent):
            for error in (ConfigException("配置错误"), RuntimeError("程序错误"), AgentException("未标识错误")):
                with self.subTest(agent=cls.__name__, error=type(error).__name__):
                    agent = self.make_agent(cls, error)
                    with self.assertRaises(type(error)) as caught:
                        agent.run("问题")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(agent.get_history(), [])
