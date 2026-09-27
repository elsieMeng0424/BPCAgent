"""Agent base class and the Basic / ReAct execution strategies."""

from abc import ABC, abstractmethod
import re
from typing import Iterator, List, Optional, Tuple

from .config import Config
from .messages import Message
from .models import MyLLM
from .tools import ToolRegistry

class Agent(ABC):
    """
    抽象的Agent基类
    """
    def __init__(
        self, 
        name: str, 
        llm: MyLLM, 
        sys_prompt: Optional[str] = None, 
        config: Optional[Config] = None    
    ):
        self.name = name
        self.llm = llm
        self.sys_prompt = sys_prompt
        self.config = config or Config()
        self._history: list[Message] = []

    @abstractmethod
    def run(self, input: str, **kwargs) -> str:
        """运行Agent"""
        pass

    def add_message(self, message: Message):
        """添加消息到历史记录"""
        self._history.append(message)

    def clear_history(self):
        """清空历史记录"""
        self._history.clear()

    def get_history(self) -> list[Message]:
        """获取历史记录"""
        return self._history.copy()
    
    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={self.llm.model})"


class BasicAgent(Agent):
    """
    支持简单对话的Agent
    """
    def __init__(
        self, 
        name: str,
        llm: MyLLM,
        sys_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional['ToolRegistry'] = None,
        enable_tool_calling: bool = True
    ):
        """
        初始化BasicAgent
        """
        super().__init__(name, llm, sys_prompt, config)
        self.tool_registry = tool_registry
        self.enable_tool_calling = enable_tool_calling and tool_registry is not None
        print(f"✅ Agent {name} 初始化完成，工具调用: {'启用' if self.enable_tool_calling else '禁用'}")

    def run(self, input: str, max_tool_iterations: int = 3, **kwargs) -> str:
        """
        实现简单的对话逻辑，支持工具调用
        """
        print(f"🤖 {self.name} 正在解决问题: {input}")

        # 构建消息列表并添加系统消息
        messages = []
        sys_prompt = self._get_system_prompt()
        messages.append({"role": "system", "content": sys_prompt})

        # 添加历史消息
        for history_message in self._history:
            messages.append({"role": history_message.role, "content": history_message.content})

        # 添加当前用户消息
        messages.append({"role": "user", "content": input})

        # 没有启动工具调用
        if not self.enable_tool_calling:
            response = self.llm.invoke(messages, **kwargs)
            self.add_message(Message(input, "user"))
            self.add_message(Message(response, "assistant"))
            print(f"✅ Agent {self.name} 响应完成")
            return response
        
        # 多轮工具调用
        return self._run_with_tools(messages, input, max_tool_iterations, **kwargs)

    def _get_system_prompt(self) -> str:
        """
        获取系统消息
        """
        # 构造基础消息
        base_prompt = self.sys_prompt or "You are a helpful AI assistant."

        # 如果没有工具调用
        if not self.enable_tool_calling or not self.tool_registry:
            return base_prompt

        # 获取工具描述
        tools_description = self.tool_registry.get_descriptions()
        if not tools_description or tools_description == "暂无可用工具":
            return base_prompt
        
        # 构造工具提示词
        tools_prompt = "\n\n## 可用工具\n"
        tools_prompt += "你可以使用以下工具来帮助回答问题:\n"
        tools_prompt += tools_description + "\n"

        tools_prompt += "\n## 工具调用格式\n"
        tools_prompt += "当需要使用工具时，请使用以下格式:\n"
        tools_prompt += "`[TOOL_CALL:{tool_name}:{parameters}]`\n"
        tools_prompt += "例如:`[TOOL_CALL:search:Python编程]` 或 `[TOOL_CALL:memory:recall=用户信息]`\n\n"
        tools_prompt += "工具调用结果会自动插入到对话中，然后你可以基于结果继续回答。\n"

        # 合并为系统提示词
        sys_prompt = base_prompt + tools_prompt
        return sys_prompt
    
    def run_using_stream(self, input: str, **kwargs) -> Iterator[str]:
        """
        流式运行
        """
        print(f"🌊 {self.name} 开始流式处理: {input}")

        # 获取消息
        messages = []
        if self.sys_prompt:
            messages.append({"role": "system", "content": self.sys_prompt})
        for message in self._history:
            messages.append({"role": message.role, "content":message.content})
        messages.append({"role": "user", "content": input})

        # 生成流式调用
        full_response = ""
        for chunk in self.llm.think(messages, **kwargs):
            full_response += chunk
            yield chunk

        # 保存到历史记录
        self.add_message(Message(input, "user"))
        self.add_message(Message(full_response, "assistant"))

    # --- 工具调用的核心逻辑 ---

    def _run_with_tools(self, messages: list, input: str, max_tool_iterations: int, **kwargs) -> str:
        """
        支持工具调用的运行逻辑
        """
        current_iteration = 0
        final_response = ""

        while current_iteration < max_tool_iterations:
            # 调用大模型生成回答
            response = self.llm.invoke(messages, **kwargs)

            # 检查输出文本中是否涉及工具调用
            tool_calls = self._parse_tool_calls(response)

            # 如涉及工具调用
            if tool_calls:
                print(f"🔧 检测到 {len(tool_calls)} 个工具调用")
                tool_call_results = []
                clean_response = response

                # 逐个执行工具调用
                for tool_call in tool_calls:
                    print(tool_call)
                    result = self._execute_tool_call(tool_call['tool_name'], tool_call['parameters'])
                    tool_call_results.append(result)
                    clean_response = clean_response.replace(tool_call['original'], "")

                # 向消息中添加工具调用结果
                messages.append({"role": "assistant", "content": clean_response})
                tool_results_text = "\n\n".join(tool_call_results)
                messages.append({"role": "user", "content": f"工具执行结果:\n{tool_results_text}\n\n请基于这些结果给出完整的回答。"})

                current_iteration += 1
                continue

            # 如果没有涉及到工具调用
            final_response = response
            break

        # 超过最大迭代次数
        if current_iteration >= max_tool_iterations and not final_response:
            final_response = self.llm.invoke(messages, **kwargs)

        # 保存历史记录
        self.add_message(Message(input, "user"))
        self.add_message(Message(final_response, "assistant"))
        print(f"✅ Agent {self.name} 响应完成")
         
        return final_response
    
    def _parse_tool_calls(self, text: str) -> list:
        """从文本中解析工具调用"""
        pattern = r'\[TOOL_CALL:([^:]+):([^\]]+)\]'
        matches = re.findall(pattern, text)

        tool_calls = []
        for name, param in matches:
            tool_calls.append({
                'tool_name': name.strip(),
                'parameters': param.strip(),
                'original': f'[TOOL_CALL:{name}:{param}]'
            })
        return tool_calls
    
    def _execute_tool_call(self, name: str, parameters: str) -> str:
        """执行工具调用"""
        if not self.tool_registry:
            return f"❌ 错误：Agent {self.name} 未配置工具注册表"
        
        try:
            # 获取Tool
            tool = self.tool_registry.get_tool(name)
            if not tool:
                return f"❌ 错误：Agent {self.name} 未配置工具 {name}"
            
            # 解析工具参数
            params = self._parse_tool_params(name, parameters)

            # 调用工具
            result = tool.run(params)
            return f"🔧 工具 {name} 执行结果: \n{result}"
        
        except Exception as e:
            return f"❌ Agent {self.name} 工具调用失败：{str(e)}"   
    
    def _parse_tool_params(self, tool_name: str, parameters: str) -> dict:
        """解析工具参数"""
        import json
        param_dict = {}

        # 尝试解析JSON格式
        if parameters.strip().startswith('{'):
            try:
                param_dict = json.loads(parameters)
                # JSON解析成功，进行类型转换
                param_dict = self._convert_parameter_types(tool_name, param_dict)
                return param_dict
            except json.JSONDecodeError:
                # JSON解析失败，继续使用其他方式
                pass

        if '=' in parameters:
            # 格式: key=value 或 action=search,query=Python
            if ',' in parameters:
                # 多个参数：action=search,query=Python,limit=3
                pairs = parameters.split(',')
                for pair in pairs:
                    if '=' in pair:
                        key, value = pair.split('=', 1)
                        param_dict[key.strip()] = value.strip()
            else:
                # 单个参数：key=value
                key, value = parameters.split('=', 1)
                param_dict[key.strip()] = value.strip()

            # 类型转换
            param_dict = self._convert_parameter_types(tool_name, param_dict)

            # 智能推断action（如果没有指定）
            if 'action' not in param_dict:
                param_dict = self._infer_action(tool_name, param_dict)
        else:
            # 直接传入参数，根据工具类型智能推断
            param_dict = self._infer_simple_parameters(tool_name, parameters)

        return param_dict

    def _convert_parameter_types(self, tool_name: str, param_dict: dict) -> dict:
        """
        根据工具的参数定义转换参数类型

        Args:
            tool_name: 工具名称
            param_dict: 参数字典

        Returns:
            类型转换后的参数字典
        """
        if not self.tool_registry:
            return param_dict

        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            return param_dict

        # 获取工具的参数定义
        try:
            tool_params = tool.get_parameters()
        except:
            return param_dict

        # 创建参数类型映射
        param_types = {}
        for param in tool_params:
            param_types[param.name] = param.type

        # 转换参数类型
        converted_dict = {}
        for key, value in param_dict.items():
            if key in param_types:
                param_type = param_types[key]
                try:
                    if param_type == 'number' or param_type == 'integer':
                        # 转换为数字
                        if isinstance(value, str):
                            converted_dict[key] = float(value) if param_type == 'number' else int(value)
                        else:
                            converted_dict[key] = value
                    elif param_type == 'boolean':
                        # 转换为布尔值
                        if isinstance(value, str):
                            converted_dict[key] = value.lower() in ('true', '1', 'yes')
                        else:
                            converted_dict[key] = bool(value)
                    else:
                        converted_dict[key] = value
                except (ValueError, TypeError):
                    # 转换失败，保持原值
                    converted_dict[key] = value
            else:
                converted_dict[key] = value

        return converted_dict

    def _infer_action(self, tool_name: str, param_dict: dict) -> dict:
        """根据工具类型和参数推断action"""
        if tool_name == 'memory':
            if 'recall' in param_dict:
                param_dict['action'] = 'search'
                param_dict['query'] = param_dict.pop('recall')
            elif 'store' in param_dict:
                param_dict['action'] = 'add'
                param_dict['content'] = param_dict.pop('store')
            elif 'query' in param_dict:
                param_dict['action'] = 'search'
            elif 'content' in param_dict:
                param_dict['action'] = 'add'
        elif tool_name == 'rag':
            if 'search' in param_dict:
                param_dict['action'] = 'search'
                param_dict['query'] = param_dict.pop('search')
            elif 'query' in param_dict:
                param_dict['action'] = 'search'
            elif 'text' in param_dict:
                param_dict['action'] = 'add_text'

        return param_dict

    def _infer_simple_parameters(self, tool_name: str, parameters: str) -> dict:
        """为简单参数推断完整的参数字典"""
        if tool_name == 'rag':
            return {'action': 'search', 'query': parameters}
        elif tool_name == 'memory':
            return {'action': 'search', 'query': parameters}
        else:
            return {'input': parameters}
        
    # --- 添加一些简便的工具处理方式 ---
    def add_tool(self, tool) -> None:
        """向工具注册表添加一个工具"""
        if not self.tool_registry:
            self.tool_registry = ToolRegistry()
            self.enable_tool_calling = True

        self.tool_registry.register_tool(tool)
        print(f"🔧 工具 '{tool.name}' 已添加至 Agent {self.name} 中")

    def has_tools(self) -> bool:
        """检查是否有可用工具"""
        return self.enable_tool_calling and self.tool_registry is not None
    
    def remove_tool(self, name: str) -> None:
        """移除工具"""
        if self.tool_registry:
            self.tool_registry.unregister_tool(name)
            print(f"🔧 工具 '{name}' 已从 Agent {self.name} 中移除")

    def list_tools(self) -> list:
        """列出所有可用工具"""
        if self.tool_registry:
            return self.tool_registry.list_tools()
        return []


# ReAct智能体的提示词模版(英文)
REACT_PROMPT_TEMPLATE = """
You are an AI assistant with both reasoning and action capabilities.
You are able to analyze problems step by step, determine what information is required, and retrieve that information by invoking appropriate tools in order to produce accurate and well-supported answers.

## Available Tools
{tools}

## Workflow
You MUST strictly follow the response format below.
Only ONE step may be executed per response.

Thought: Analyze the problem, identify the required information, and formulate a research or reasoning strategy.
Action: Select and invoke the appropriate tool to obtain information using one of the following formats:
- `{{tool_name}}[{{tool_input}}]` — invoke a tool to retrieve information.
- `Finish[final conclusion]` — use this only when you are confident that sufficient information has been obtained to answer the question.

## Important Rules
1. Every response MUST include both a Thought and an Action section.
2. Tool invocation MUST strictly follow the format: tool_name[parameters].
3. Use Finish ONLY when you are confident that the available information is sufficient to answer the question.
4. If the returned information is insufficient, continue using the same tool with different parameters or invoke other relevant tools.

## Current Task
**Question:** {question}

## Execution History
{history}

Begin your reasoning and actions now.
"""

class ReActAgent(Agent):
    """
    推理-行动智能体的实现
    """
    def __init__(
        self, 
        name: str,
        llm: MyLLM,
        sys_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional[ToolRegistry] = None,
        max_steps: int = 5,
        prompt_template: Optional[str] = None
    ):
        """
        初始化ReAct智能体
        """
        super().__init__(name, llm, sys_prompt, config)

        # 如果没有提供工具注册表就自己注册一个
        self.tool_registry = tool_registry if tool_registry else ToolRegistry()

        # 如果没有提示词模版就使用默认模版
        self.prompt_template = prompt_template if prompt_template else REACT_PROMPT_TEMPLATE

        self.max_steps = max_steps
        self.current_history: List[str] = []
        print(f"✅ ReAct Agent {name} 初始化完成，最大步数: {max_steps}")

    def run(self, input: str, **kwargs) -> str:
        """
        运行智能体
        """
        # 运行前清空历史，步数置零
        self.current_history = []
        current_step = 0

        print(f"\n🤖 ReAct Agent {self.name} 开始处理问题: {input}")

        # 推理-行动循环
        while current_step < self.max_steps:
            current_step += 1
            print(f"\n--- 第 {current_step} 步 ---")

            # 构建提示词
            tools = self.tool_registry.get_descriptions()
            history = "\n".join(self.current_history)
            prompt = self.prompt_template.format(
                tools = tools,
                question = input,
                history = history
            )

            # 调用大模型生成回答
            messages = [{"role": "user", "content": prompt}]
            response = self.llm.invoke(messages, **kwargs)
            if not response:
                print("❌ 错误：LLM未能返回有效响应。")
                break
            
            # 解析response
            thought, action = self._parse_response(response)

            # 检查解析结果
            if thought:
                print(f"🤔 思考: {thought}")
            if not action:
                print("⚠️ 警告：未能解析出有效的Action，流程终止。")
                break

            # 检查是否完成
            if action.startswith("Finish"):
                final_answer = self._parse_final_answer(action)
                print(f"🎉 最终答案: {final_answer}")

                self.add_message(Message(input, "user"))
                self.add_message(Message(final_answer, "assistant"))
                return final_answer
            
            # 执行工具调用
            tool_name, tool_input = self._parse_tools(action)
            if not tool_name or tool_input is None:
                self.current_history.append("Observation: Invalid Action format detected. Please check and correct the Action syntax.")
                continue
            print(f"🎬 行动: {tool_name}[{tool_input}]")

            observation = self.tool_registry.execute_tool(tool_name, tool_input)
            print(f"👀 获取观察: {observation}")

            # 更新历史
            self.current_history.append(f"Action: {action}")
            self.current_history.append(f"Observation: {observation}")

        print("⏰ 已达到最大步数，流程终止。")
        final_answer = "The task could not be completed within the predefined step limit."
        
        # 保存到历史记录
        self.add_message(Message(input, "user"))
        self.add_message(Message(final_answer, "assistant"))
        return final_answer

    def _parse_response(self, response: str) -> Tuple[Optional[str], Optional[str]]:
        """从response中解析出Thought和Action"""
        thought_match = re.search(r"Thought: (.*)", response)
        action_match = re.search(r"Action: (.*)", response)

        thought = thought_match.group(1).strip() if thought_match else None
        action = action_match.group(1).strip() if action_match else None

        return thought, action
    
    def _parse_final_answer(self, action: str) -> str:
        """解析Final"""
        match = re.match(r"\w+\[(.*)\]", action)
        return match.group(1) if match else ""
    
    def _parse_tools(self, action: str) -> Tuple[Optional[str], Optional[str]]:
        """解析action中的工具调用"""
        match = re.match(r"(\w+)\[(.*)\]", action)
        if match:
            return match.group(1), match.group(2)
        return None, None
