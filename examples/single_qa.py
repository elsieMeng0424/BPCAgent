"""Run one question with a simulated model or an OpenAI-compatible service."""

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

from bpcagent import BasicAgent, CalculatorTool, Config, LLMResponse, Message, MyLLM, ToolCall, ToolRegistry
from bpcagent.agents.exceptions import AgentException, BasicException


def run_protocol_test(llm, registry, question: str) -> str:
    """首轮指定计算器，真实执行后恢复自动选择，验证服务的工具协议。"""
    tools = registry.to_openai_tools()
    messages = [Message(question, "user").to_dict()]
    response = llm.invoke(messages, tools=tools,
                          tool_choice={"type": "function", "function": {"name": "calculate"}})
    if response.finish_reason not in {"stop", "tool_calls"} or not response.tool_calls:
        raise AgentException("协议测试失败：模型没有返回完整工具调用")
    if any(call.name != "calculate" for call in response.tool_calls):
        raise AgentException("协议测试失败：模型未选择指定的计算器")
    messages.append(Message(response.content, "assistant", tool_calls=response.tool_calls).to_dict())
    for call in response.tool_calls:
        result = registry.execute_tool(call.name, json.loads(call.arguments), call_id=call.id)
        if result.status != "success":
            raise AgentException(f"协议测试失败：{result.error}")
        messages.append(Message(result.model_dump_json(), "tool", tool_call_id=call.id).to_dict())
    response = llm.invoke(messages, tools=tools, tool_choice="auto")
    if response.finish_reason != "stop" or response.tool_calls or not response.content or not response.content.strip():
        raise AgentException("协议测试失败：回传结果后未得到最终回答")
    return response.content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="请计算 2 + 3 * 4。")
    parser.add_argument("--live", action="store_true", help="调用真实模型，需要配置 .env")
    parser.add_argument("--with-tools", action="store_true", help="注册计算器工具")
    parser.add_argument("--protocol-test", action="store_true", help="真实服务协议检查，首轮指定计算器")
    args = parser.parse_args()
    if args.protocol_test and not (args.live and args.with_tools):
        parser.error("--protocol-test 需要同时使用 --live --with-tools")

    try:
        llm = None
        if args.live:
            try:
                from dotenv import load_dotenv
            except ImportError:
                parser.error('真实模型示例需要先执行：python -m pip install -r requirements.txt')
            load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
            config = Config()
        else:
            print("离线演示：模型响应为预设结构化响应，工具调用模式会实际执行计算器。")
            config = Config.resolve(environ={})
            llm = Mock(spec=MyLLM)
            llm.model = "offline-demo"
            responses = [LLMResponse(content="2 + 3 * 4 = 14。", finish_reason="stop")]
            if args.with_tools:
                responses.insert(0, LLMResponse(tool_calls=[ToolCall(
                    id="call_calculator", name="calculate", arguments='{"input":"2+3*4"}')],
                    finish_reason="tool_calls"))
            llm.invoke.side_effect = responses

        registry = None
        if args.with_tools:
            registry = ToolRegistry()
            calculator = CalculatorTool()
            registry.register_tool(calculator)

        agent = BasicAgent(
            name="SingleQA",
            llm=llm,
            config=config,
            sys_prompt="请回答问题。如果有可用的计算器，请使用它完成计算。",
            tool_registry=registry,
            enable_tool_calling=args.with_tools,
        )
        if args.with_tools:
            # 只在示例中检查真实执行；生产工具及 Agent 不增加测试追踪字段。
            with patch.object(calculator, "run", wraps=calculator.run) as execution:
                answer = (run_protocol_test(agent.llm, registry, args.question) if args.protocol_test
                          else agent.run(args.question))
                if execution.call_count == 0:
                    raise AgentException("验证失败：模型未实际调用计算器，直接回答不算通过")
                print(f"已验证：CalculatorTool.run 实际执行 {execution.call_count} 次。")
        else:
            answer = agent.run(args.question)
        print(f"\n回答：{answer}")
        print(f"本次保存了 {len(agent.get_history())} 条消息。")
        return 0
    except (BasicException, ValueError) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
