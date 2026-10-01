"""Offline checks for conversation history and real tool execution."""

from copy import deepcopy
import unittest
import json
import os
from unittest.mock import Mock, patch

from bpcagent import BasicAgent, CalculatorTool, Config, MyLLM, ReActAgent, ToolRegistry, ToolArgs, Tool, ToolCall, LLMResponse
from bpcagent.agents.exceptions import AgentException, ConfigException


def tool_response(name="calculate", arguments='{"input":"1+1"}', call_id="call_1"):
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
                       finish_reason="tool_calls")


class TextArgs(ToolArgs):
    input: str


class NestedArgs(ToolArgs):
    texts: list[str]
    options: TextArgs


class NestedTool(Tool[NestedArgs]):
    args_schema = NestedArgs

    def __init__(self):
        super().__init__("nested-echo", "返回嵌套参数")

    def run(self, parameters):
        return parameters.model_dump()



class AgentTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def make_model(self, *responses):
        llm = Mock(spec=MyLLM)
        llm.model = "test-model"
        remaining = iter(responses)
        calls = []

        def invoke(messages, **kwargs):
            calls.append(deepcopy(messages))
            response = next(remaining)
            return response if isinstance(response, LLMResponse) else LLMResponse(content=response, finish_reason="stop")

        llm.invoke.side_effect = invoke
        return llm, calls

    def test_single_question_and_history(self):
        llm, calls = self.make_model("你好！")
        agent = BasicAgent("test", llm, sys_prompt="简洁回答。", enable_tool_calling=False)

        self.assertEqual(agent.run("你好"), "你好！")
        self.assertEqual(calls[0], [
            {"role": "system", "content": "简洁回答。"},
            {"role": "user", "content": "你好"},
        ])
        self.assertEqual([(m.role, m.content) for m in agent.get_history()], [
            ("user", "你好"), ("assistant", "你好！"),
        ])

    def test_next_question_receives_history_and_clear_resets_it(self):
        llm, calls = self.make_model("已记住。", "小明", "不知道")
        agent = BasicAgent("test", llm, enable_tool_calling=False)
        agent.run("我叫小明")
        self.assertEqual(agent.run("我叫什么？"), "小明")
        self.assertEqual(calls[1][1:3], [
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "已记住。"},
        ])
        agent.clear_history()
        agent.run("我叫什么？")
        self.assertEqual(len(calls[2]), 2)

    def test_basic_agent_executes_calculator_and_returns_answer(self):
        for parameters in ('{"input": "2+3*4"}', '{\n"input": "2+3*4"\n}'):
            with self.subTest(parameters=parameters):
                llm, calls = self.make_model(tool_response(arguments=parameters), "结果是 14。")
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                agent = BasicAgent("test", llm, tool_registry=registry)

                self.assertEqual(agent.run("计算 2+3*4"), "结果是 14。")
                calculator.run.assert_called_once()
                self.assertEqual(calculator.run.call_args.args[0].input, "2+3*4")
                self.assertIn("14", calls[1][-1]["content"])
                self.assertEqual(len(agent.get_history()), 2)

    def test_react_agent_executes_tool_before_finish(self):
        llm, calls = self.make_model(
            tool_response(arguments='{"input":"2+3*4"}'),
            "14",
        )
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        agent = ReActAgent("test", llm, tool_registry=registry)

        self.assertEqual(agent.run("计算 2+3*4"), "14")
        self.assertIn('"data":14', calls[1][-1]["content"])

    def test_function_tool_description_and_execution(self):
        registry = ToolRegistry()
        registry.register_function("uppercase", "转成大写", lambda input: input.upper(), args_schema=TextArgs)
        self.assertIn("转成大写", registry.get_descriptions())
        self.assertEqual(registry.execute_tool("uppercase", {"input": "hello"}).data, "HELLO")

    def test_basic_iteration_config_and_single_run_override(self):
        for override, expected_iterations in ((None, 2), (1, 1)):
            with self.subTest(override=override):
                llm, _ = self.make_model(*([tool_response()] * expected_iterations), "2")
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                agent = BasicAgent("test", llm, config=Config(max_tool_iterations=2),
                                   tool_registry=registry)
                with self.assertRaisesRegex(AgentException, "step_limit_exceeded"):
                    agent.run("calculate", max_tool_iterations=override, temperature=0.1)
                self.assertEqual(calculator.run.call_count, expected_iterations)
                # 轮次耗尽后不再额外请求模型，也不写入成功历史。
                self.assertEqual(llm.invoke.call_count, expected_iterations)
                self.assertEqual(agent.get_history(), [])
                self.assertEqual(agent.config.max_tool_iterations, 2)
                for call in llm.invoke.call_args_list:
                    self.assertEqual(call.kwargs["temperature"], 0.1)
                    self.assertEqual(call.kwargs["tools"], registry.to_openai_tools())

    def test_react_step_config_and_constructor_override(self):
        for override, expected_steps in ((None, 2), (1, 1)):
            with self.subTest(override=override):
                llm, _ = self.make_model(*([tool_response()] * expected_steps))
                registry = ToolRegistry()
                registry.register_tool(CalculatorTool())
                config = Config(max_steps=2)
                agent = ReActAgent("test", llm, config=config, max_steps=override, tool_registry=registry)
                with self.assertRaisesRegex(AgentException, "step_limit_exceeded"):
                    agent.run("calculate")
                self.assertEqual(agent.get_history(), [])
                self.assertEqual(llm.invoke.call_count, expected_steps)
                self.assertEqual(agent.config.max_steps, expected_steps)
                self.assertEqual(config.max_steps, 2)

    def test_agent_environment_defaults_and_validation(self):
        llm, _ = self.make_model("unused")
        with patch.dict(os.environ, {"MAX_TOOL_ITERATIONS": "2", "MAX_STEPS": "4"}):
            basic = BasicAgent("test", llm)
            react = ReActAgent("test", llm)
        self.assertEqual(basic.config.max_tool_iterations, 2)
        self.assertEqual(react.max_steps, 4)
        for invalid in (0, -1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigException):
                basic.run("hello", max_tool_iterations=invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ConfigException):
                ReActAgent("test", llm, max_steps=invalid)
        llm.invoke.assert_not_called()

    def test_agent_config_does_not_modify_supplied_model(self):
        with patch("bpcagent.agents.models.OpenAI"):
            llm = MyLLM(model="test", api_key="test-key", base_url="https://example.invalid", temperature=0.2)
        original = llm.config
        agent = BasicAgent("test", llm, config=Config(temperature=0.9))
        self.assertEqual(agent.config.temperature, 0.9)
        self.assertIs(llm.config, original)
        self.assertEqual(llm.config.temperature, 0.2)

    def test_both_agents_execute_objects_and_functions_through_registry(self):
        arguments = {"texts": ['a,b] {嵌套文本}', '引号"与括号[ ]'], "options": {"input": "nested"}}
        encoded = json.dumps(arguments, ensure_ascii=False, indent=2)
        for agent_cls in (BasicAgent, ReActAgent):
            for function in (False, True):
                with self.subTest(agent=agent_cls.__name__, function=function):
                    registry = ToolRegistry()
                    if function:
                        registry.register_function("nested-echo", "返回嵌套参数",
                            lambda texts, options: {"texts": texts, "options": options.model_dump()},
                            args_schema=NestedArgs)
                    else:
                        registry.register_tool(NestedTool())
                    registry.execute_tool = Mock(wraps=registry.execute_tool)
                    first = tool_response("nested-echo", encoded)
                    finish = "完成"
                    llm, calls = self.make_model(first, finish)
                    agent = agent_cls("test", llm, tool_registry=registry, sys_prompt="系统要求")
                    self.assertEqual(agent.run("echo"), "完成")
                    registry.execute_tool.assert_called_once_with("nested-echo", arguments, call_id="call_1")
                    self.assertEqual(calls[0][0]["role"], "system")
                    self.assertIn("系统要求", calls[0][0]["content"])
                    result = json.loads(calls[1][-1]["content"])
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(result["data"], arguments)

    def test_multiple_calls_have_distinct_ids_and_keep_order(self):
        for cls in (BasicAgent, ReActAgent):
            with self.subTest(agent=cls.__name__):
                llm, calls = self.make_model(
                    LLMResponse(tool_calls=[
                        ToolCall(id="call_a", name="calculate", arguments='{"input":"1+1"}'),
                        ToolCall(id="call_b", name="calculate", arguments='{"input":"2+2"}'),
                    ], finish_reason="tool_calls"),
                    "2 和 4",
                )
                registry = ToolRegistry()
                registry.register_tool(CalculatorTool())
                agent = cls("test", llm, tool_registry=registry)
                self.assertEqual(agent.run("计算"), "2 和 4")
                results = [json.loads(message["content"]) for message in calls[1][-2:]]
                self.assertEqual([message["tool_call_id"] for message in calls[1][-2:]], ["call_a", "call_b"])
                self.assertEqual(calls[1][-3]["role"], "assistant")
                self.assertEqual([call["id"] for call in calls[1][-3]["tool_calls"]], ["call_a", "call_b"])
                self.assertEqual([r["data"] for r in results], [2, 4])
                self.assertNotEqual(results[0]["call_id"], results[1]["call_id"])

    def test_invalid_json_never_executes_and_model_can_correct_it(self):
        for agent_cls in (BasicAgent, ReActAgent):
            for bad in ('{"input":}', '{"input":"1", "input":"2"}', '[1,2]', 'not-json', '{"input":NaN}', '{} trailing'):
                with self.subTest(agent=agent_cls.__name__, bad=bad):
                    first = tool_response(arguments=bad)
                    good = tool_response()
                    last = "2"
                    llm, calls = self.make_model(first, good, last)
                    registry = ToolRegistry()
                    registry.register_tool(CalculatorTool())
                    registry.execute_tool = Mock(wraps=registry.execute_tool)
                    agent = agent_cls("test", llm, tool_registry=registry)
                    self.assertEqual(agent.run("计算"), "2")
                    registry.execute_tool.assert_called_once_with("calculate", {"input": "1+1"}, call_id="call_1")
                    self.assertIn("invalid_arguments", calls[1][-1]["content"])

    def test_two_tool_rounds_use_actual_previous_results(self):
        for cls in (BasicAgent, ReActAgent):
            with self.subTest(agent=cls.__name__):
                llm, calls = self.make_model(tool_response(),
                    tool_response(arguments='{"input":"2*3"}', call_id="call_2"), "6")
                registry = ToolRegistry()
                calculator = CalculatorTool()
                calculator.run = Mock(wraps=calculator.run)
                registry.register_tool(calculator)
                agent = cls("test", llm, tool_registry=registry)
                self.assertEqual(agent.run("先计算 1+1，再把结果乘以 3"), "6")
                self.assertEqual(calculator.run.call_count, 2)
                self.assertEqual(json.loads(calls[1][-1]["content"])["data"], 2)
                self.assertEqual(json.loads(calls[2][-1]["content"])["data"], 6)
                self.assertEqual(calls[2][-1]["tool_call_id"], "call_2")

    def test_unexpected_calls_without_enabled_tools_are_rejected(self):
        for cls in (BasicAgent, ReActAgent):
            llm, _ = self.make_model(tool_response())
            agent = cls("test", llm)
            with self.assertRaisesRegex(AgentException, "invalid_response"):
                agent.run("hello")
            self.assertEqual(agent.get_history(), [])

    def test_tool_failures_reach_both_agents_as_structured_errors(self):
        for agent_cls in (BasicAgent, ReActAgent):
            for name, args, code in (("missing", {}, "tool_not_found"),
                                     ("calculate", {}, "invalid_arguments"),
                                     ("calculate", {"input": "1/0"}, "execution_error")):
                with self.subTest(agent=agent_cls.__name__, code=code):
                    encoded = json.dumps(args)
                    first = tool_response(name, encoded)
                    finish = "无法计算"
                    llm, calls = self.make_model(first, finish)
                    registry = ToolRegistry()
                    registry.register_tool(CalculatorTool())
                    self.assertEqual(agent_cls("test", llm, tool_registry=registry).run("计算"), "无法计算")
                    self.assertIn(code, calls[1][-1]["content"])

    def test_empty_response_and_invalid_final_do_not_save_success_history(self):
        for agent_cls in (BasicAgent, ReActAgent):
            for response in (None, "", "   "):
                with self.subTest(agent=agent_cls.__name__, response=response):
                    llm, _ = self.make_model(response)
                    agent = agent_cls("test", llm)
                    with self.assertRaisesRegex(AgentException, "empty_response"):
                        agent.run("hello")
                    self.assertEqual(agent.get_history(), [])

    def test_repeated_invalid_calls_stop_at_budget(self):
        for agent_cls in (BasicAgent, ReActAgent):
            response = tool_response(arguments="bad")
            llm, _ = self.make_model(response, response)
            config = Config(max_tool_iterations=2, max_steps=2)
            registry = ToolRegistry()
            registry.register_tool(CalculatorTool())
            registry.execute_tool = Mock(wraps=registry.execute_tool)
            agent = agent_cls("test", llm, tool_registry=registry, config=config)
            with self.assertRaisesRegex(AgentException, "step_limit_exceeded"):
                agent.run("hello")
            self.assertEqual(llm.invoke.call_count, 2)
            registry.execute_tool.assert_not_called()
            self.assertEqual(agent.get_history(), [])


if __name__ == "__main__":
    unittest.main()
