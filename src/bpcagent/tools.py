"""Tool definitions and the shared registry."""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

class ToolParameter(BaseModel):
    """工具参数定义"""
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None

class Tool(ABC):
    """工具基类"""
    def __init__(
        self,
        name: str,
        description: str
    ):
        self.name = name
        self.description = description

    @abstractmethod
    def run(self, parameters: Dict[str, Any]) -> str:
        """工具执行"""
        pass

    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        """获取工具参数定义"""
        pass


class ToolRegistry:
    """
    工具注册表类
    """
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}

    def register_tool(self, tool: Tool):
        """注册工具"""
        if tool.name in self._tools:
            print(f"⚠️ 警告:工具 '{tool.name}' 已存在，将覆盖。")
        self._tools[tool.name] = tool
        print(f"✅ 工具 '{tool.name}' 已注册。")

    def register_function(self, name: str, description: str, func: Callable[[str], str]):
        """直接把函数注册为工具"""
        if name in self._functions:
            print(f"⚠️ 警告:函数工具 '{name}' 已存在，将覆盖。")

        self._functions[name] = {"description": description, "func": func}
        print(f"✅ 函数工具 '{name}' 已注册。")

    def get_descriptions(self) -> str:
        """获取所有工具的描述"""
        descriptions = []

        for tool in self._tools.values():
            descriptions.append(f"- {tool.name}: {tool.description}")

        for name, info in self._functions.items():
            descriptions.append(f"- {name}: {info['description']}")
        
        return "\n".join(descriptions) if descriptions else "暂无可用工具"
    
    def get_tool(self, name: str) -> Optional[Tool]:
        """获取Tool对象"""
        return self._tools.get(name)

    def unregister_tool(self, name: str):
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            print(f"🗑️ 工具 '{name}' 已注销。")
        elif name in self._functions:
            del self._functions[name]
            print(f"🗑️ 工具 '{name}' 已注销。")
        else:
            print(f"⚠️ 工具 '{name}' 不存在。")

    def list_tools(self) -> list[str]:
        """列出所有工具"""
        return list(self._tools.keys()) + list(self._functions.keys())
    
    def execute_tool(self, name: str, input: str) -> str:
        """执行工具"""
        # 查找Tool对象
        if name in self._tools:
            tool = self._tools[name]
            try:
                return tool.run({"input": input})
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"
        # 查找函数工具        
        elif name in self._functions:
            func = self._functions[name]["func"]
            try:
                return func(input)
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"
        else:
            return f"错误：未找到名为 '{name}' 的工具。"
