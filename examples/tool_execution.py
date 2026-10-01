"""离线演示统一工具执行与 OpenAI Chat Completions 工具定义导出。"""

import json

from pydantic import Field

from bpcagent import CalculatorTool, ToolArgs, ToolRegistry


class SummaryOptions(ToolArgs):
    """嵌套参数同样继承 ToolArgs，拒绝未知字段。"""

    precision: int = Field(2, ge=0, le=6, description="结果保留的小数位数")
    absolute: bool = Field(False, description="是否先取绝对值")


class SummaryArgs(ToolArgs):
    values: list[float] = Field(min_length=1, description="要统计的数值列表")
    options: SummaryOptions = Field(default_factory=SummaryOptions)


def summarize(values: list[float], options: SummaryOptions) -> dict:
    """函数接收校验后的值，嵌套参数仍为模型实例。"""
    data = [abs(value) for value in values] if options.absolute else values
    return {"count": len(data), "total": round(sum(data), options.precision)}


def main() -> None:
    registry = ToolRegistry()
    registry.register_tool(CalculatorTool())
    registry.register_function("summarize", "统计数值列表", summarize, args_schema=SummaryArgs)

    print("OpenAI Chat Completions 工具定义（仅导出，不请求服务）：")
    print(json.dumps(registry.to_openai_tools(), ensure_ascii=False, indent=2))
    for name, arguments in (
        ("calculate", {"input": "2+3*4"}),
        ("summarize", {"values": [1.25, -2.5], "options": {"absolute": True}}),
        ("summarize", {"values": [1.0, 2.0]}),
        ("summarize", {"values": "参数类型不正确"}),
    ):
        result = registry.execute_tool(name, arguments)
        print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
