"""Run one question with a simulated model or an OpenAI-compatible service."""

import argparse
from pathlib import Path
import sys
from unittest.mock import Mock

from bpcagent import BasicAgent, CalculatorTool, MyLLM, ToolRegistry
from bpcagent.exceptions import BasicException


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="请计算 2 + 3 * 4。")
    parser.add_argument("--live", action="store_true", help="调用真实模型，需要配置 .env")
    parser.add_argument("--with-tools", action="store_true", help="注册计算器工具")
    args = parser.parse_args()

    try:
        if args.live:
            try:
                from dotenv import load_dotenv
            except ImportError:
                parser.error('真实模型示例需要先执行：python -m pip install -r requirements.txt')
            load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
            llm = MyLLM()
        else:
            print("离线演示：模型响应为预设文本，工具调用模式会实际执行计算器。")
            llm = Mock(spec=MyLLM)
            llm.model = "offline-demo"
            responses = ["2 + 3 * 4 = 14。"]
            if args.with_tools:
                responses.insert(0, "[TOOL_CALL:calculate:2+3*4]")
            llm.invoke.side_effect = responses

        registry = None
        if args.with_tools:
            registry = ToolRegistry()
            registry.register_tool(CalculatorTool())

        agent = BasicAgent(
            name="SingleQA",
            llm=llm,
            sys_prompt="请回答问题。如果有可用的计算器，请使用它完成计算。",
            tool_registry=registry,
            enable_tool_calling=args.with_tools,
        )
        answer = agent.run(args.question)
        print(f"\n回答：{answer}")
        print(f"本次保存了 {len(agent.get_history())} 条消息。")
        return 0
    except BasicException as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
