"""模拟 SDK 响应，验证模型接口、两类 Agent 和真实计算器的完整调用链。"""

from copy import deepcopy
from types import SimpleNamespace as NS
import json
import unittest
from unittest.mock import Mock, patch

from bpcagent import BasicAgent, ReActAgent, CalculatorTool, Config, MyLLM, ToolRegistry
from bpcagent.agents.exceptions import AgentException, ConfigException, LLMException


def sdk_response(content=None, calls=None, reason="tool_calls", refusal=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls, refusal=refusal),
                          finish_reason=reason)])


def sdk_call(identity="call_calc", name="calculate", arguments='{"input":"2+3*4"}', kind="function"):
    return NS(id=identity, type=kind, function=NS(name=name, arguments=arguments))


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("bpcagent.agents.models.OpenAI")
        self.client = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.config = Config.resolve(environ={}, overrides={
            "model": "test", "api_key": "test-key", "base_url": "https://example.invalid/v1"})
        self.llm = MyLLM(config=self.config)

    def test_both_agents_execute_real_calculator_and_pair_messages(self):
        for cls in (BasicAgent, ReActAgent):
            with self.subTest(agent=cls.__name__):
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                requests = []

                def create(**kwargs):
                    requests.append(deepcopy(kwargs))
                    if len(requests) == 1:
                        self.assertEqual(kwargs["tools"], registry.to_openai_tools())
                        return sdk_response("我会使用计算器。", [sdk_call()])
                    messages = kwargs["messages"]
                    self.assertEqual(messages[-2]["tool_calls"][0]["id"], "call_calc")
                    self.assertEqual(messages[-2]["content"], "我会使用计算器。")
                    self.assertEqual(messages[-1]["role"], "tool")
                    self.assertEqual(messages[-1]["tool_call_id"], "call_calc")
                    result = json.loads(messages[-1]["content"])
                    self.assertEqual(result["call_id"], "call_calc")
                    self.assertEqual(result["data"], 14)
                    return sdk_response("结果是 14。", reason="stop")

                self.client.chat.completions.create.side_effect = create
                agent = cls("calculator", self.llm, config=self.config, tool_registry=registry)
                self.assertEqual(agent.run("计算 2+3*4"), "结果是 14。")
                calculator.run.assert_called_once()
                self.assertEqual(calculator.run.call_args.args[0].input, "2+3*4")
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(agent.get_history()), 2)

    def test_malformed_call_batch_never_executes_tools(self):
        for calls in ([sdk_call(), sdk_call(identity="")], [sdk_call(), sdk_call()],
                      [sdk_call(kind="other")], [sdk_call(arguments={})]):
            for cls in (BasicAgent, ReActAgent):
                with self.subTest(calls=calls, agent=cls.__name__):
                    self.client.chat.completions.create.return_value = sdk_response(calls=calls)
                    registry = ToolRegistry()
                    calculator = CalculatorTool()
                    calculator.run = Mock(wraps=calculator.run)
                    registry.register_tool(calculator)
                    agent = cls("test", self.llm, config=self.config, tool_registry=registry)
                    with self.assertRaises(LLMException):
                        agent.run("计算")
                    calculator.run.assert_not_called()
                    self.assertEqual(agent.get_history(), [])

    def test_nonfinal_response_never_executes_or_saves_success(self):
        for reason in ("length", "content_filter", "unexpected"):
            self.client.chat.completions.create.return_value = sdk_response("partial", [sdk_call()], reason)
            registry = ToolRegistry()
            registry.register_tool(CalculatorTool())
            registry.execute_tool = Mock(wraps=registry.execute_tool)
            agent = BasicAgent("test", self.llm, config=self.config, tool_registry=registry)
            with self.assertRaises(AgentException):
                agent.run("计算")
            registry.execute_tool.assert_not_called()
            self.assertEqual(agent.get_history(), [])
        for response in (NS(choices=[]), sdk_response(refusal="refused")):
            self.client.chat.completions.create.return_value = response
            with self.assertRaises(LLMException):
                self.llm.invoke([])

    def test_tools_and_choice_are_request_scoped(self):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        definitions = registry.to_openai_tools()
        snapshot = deepcopy(definitions)
        self.client.chat.completions.create.return_value = sdk_response(calls=[sdk_call()])
        choice = {"type": "function", "function": {"name": "calculate"}}
        self.llm.invoke([], tools=definitions, tool_choice=choice)
        request = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["tool_choice"], choice)
        request["tools"][0]["function"]["name"] = "mutated"
        self.assertEqual(definitions, snapshot)
        self.client.chat.completions.create.return_value = sdk_response("done", reason="stop")
        self.llm.invoke([], tools=definitions, tool_choice="auto")
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["tool_choice"], "auto")
        self.llm.invoke([], tools=[])
        self.assertNotIn("tools", self.client.chat.completions.create.call_args.kwargs)
        with self.assertRaises(ConfigException):
            self.llm.invoke([], tool_choice="required")

    def test_agent_rejects_tool_list_override(self):
        agent = BasicAgent("test", self.llm, config=self.config)
        for kwargs in ({"tools": []}, {"extra_body": {"tools": []}}):
            with self.assertRaises(ConfigException):
                agent.run("hello", **kwargs)
        self.client.chat.completions.create.assert_not_called()

    def test_plain_stream_rejects_tools_and_empty_responses(self):
        agent = BasicAgent("test", self.llm, config=self.config)
        for parts in ([], [" ", "\n"]):
            self.client.chat.completions.create.return_value = iter([
                NS(choices=[NS(delta=NS(content=p), finish_reason=None)]) for p in parts])
            with self.assertRaisesRegex(AgentException, "empty_response"):
                list(agent.run_using_stream("hello"))
            self.assertEqual(agent.get_history(), [])
        for kwargs in ({"tools": []}, {"tool_choice": "auto"}):
            with self.assertRaises(ConfigException):
                list(self.llm.think([], **kwargs))
        for delta in (NS(content=None, tool_calls=[sdk_call()]), NS(content=None, refusal="refused")):
            self.client.chat.completions.create.return_value = iter([NS(choices=[NS(delta=delta)])])
            with self.assertRaises(LLMException):
                list(agent.run_using_stream("hello"))
            self.assertEqual(agent.get_history(), [])

    def test_react_resets_run_history_and_uses_custom_prompt(self):
        requests = []
        def create(**kwargs):
            requests.append(deepcopy(kwargs["messages"]))
            return sdk_response("回答", reason="stop")
        self.client.chat.completions.create.side_effect = create
        agent = ReActAgent("test", self.llm, config=self.config,
                           sys_prompt="系统要求", prompt_template="自定义策略")
        agent.run("第一问")
        agent.run("第二问")
        self.assertEqual(requests[1], [{"role": "system", "content": "系统要求\n\n自定义策略"},
                                       {"role": "user", "content": "第二问"}])
