# BPCAgent

从 `SciResearchSystem` 提取的轻量级 Agent 开发包，用于创建智能体、注册工具和执行问答。项目目录名为 `BPCAgent`，Python 包名和安装名为 `bpcagent`。

## 本次提取范围

- `Agent`：智能体基类与内存中的对话历史。
- `BasicAgent`：普通问答、流式问答及文本协议驱动的工具调用。
- `ReActAgent`：使用 Action / Observation 循环调用工具。
- `MyLLM`、`Config`、`Message` 和异常类：模型接口与基础数据结构。
- `Tool`、`ToolParameter`、`ToolRegistry`：工具定义、注册和执行。
- `CalculatorTool`：可直接运行的通用工具示例。

本阶段没有迁入业务智能体、业务工具、Workflow、Planner、Reflection、工具链或并行执行器。原始 `SciResearchSystem` 目录保留。

## 目录结构

```text
BPCAgent/
├── pyproject.toml           # 包元数据、依赖与构建配置
├── requirements.txt        # 运行、示例和打包所需依赖
├── README.md
├── .env.example            # 模型配置模板，不含密钥
├── examples/
│   └── single_qa.py        # 离线 / 真实模型单次问答
├── tests/
│   ├── test_agents.py      # 问答、历史与实际工具执行
│   └── test_models.py      # 模型适配、流式与异常传播
└── src/bpcagent/
    ├── __init__.py         # 对外导出常用类
    ├── agents.py           # Agent、BasicAgent、ReActAgent
    ├── models.py           # MyLLM 模型接口
    ├── tools.py            # Tool、ToolParameter、ToolRegistry
    ├── default_tools.py    # CalculatorTool 等内置工具
    ├── messages.py         # Message 与消息角色
    ├── config.py           # Config
    └── exceptions.py       # 框架异常
```

采用单层模块结构。安装后可从任意工作目录导入 `bpcagent`，无需手动修改 `sys.path`，也不依赖 `SciResearchSystem`。

### 结构参考与导入路径

参考 [Hugging Face smolagents](https://github.com/huggingface/smolagents/tree/main/src/smolagents) 的 `agents.py`、`models.py`、`tools.py`、`default_tools.py` 划分，以及 [Pydantic AI](https://github.com/pydantic/pydantic-ai/tree/main/pydantic_ai_slim/pydantic_ai) 的 `messages.py`、`exceptions.py` 命名。当前包按职责分为 7 个模块，工具接口与注册表放在一起，具体内置工具放在 `default_tools.py`。

顶层公开导入继续使用：

```python
from bpcagent import Agent, BasicAgent, ReActAgent, MyLLM
from bpcagent import Tool, ToolParameter, ToolRegistry, CalculatorTool
```

原先直接导入内部文件的代码，需要按下表调整。旧目录已移除：

| 原导入路径 | 新导入路径 |
| --- | --- |
| `bpcagent.core.agent`、`bpcagent.basic_agent`、`bpcagent.react_agent` | `bpcagent.agents` |
| `bpcagent.core.llm` | `bpcagent.models` |
| `bpcagent.core.message` | `bpcagent.messages` |
| `bpcagent.core.config` | `bpcagent.config` |
| `bpcagent.core.exception` | `bpcagent.exceptions` |
| `bpcagent.tools.core.base`、`bpcagent.tools.core.registry` | `bpcagent.tools` |
| `bpcagent.tools.calculator` | `bpcagent.default_tools` |

原先从 `bpcagent.core` 导入的 `Agent`、`Config`、`Message`、`MyLLM` 可直接从 `bpcagent` 导入；异常类从 `bpcagent.exceptions` 导入。`CalculatorTool` 可从 `bpcagent` 或 `bpcagent.default_tools` 导入。

新建的内置工具可以加入 `default_tools.py`；调用方自己的工具只需继承 `Tool` 并注册，无需修改包目录。

## 使用 Conda 安装

使用独立的 `bpcagent` Conda 环境，推荐 Python 3.11。在项目根目录执行：

```bash
cd BPCAgent
conda create -n bpcagent python=3.11 pip -y
conda activate bpcagent
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation -e .
```

已经创建过环境时，从 `conda activate bpcagent` 开始即可。Conda 管理 Python 环境，环境内的 `python -m pip` 安装本项目的 Python 依赖。最后一条命令将本项目以可编辑方式安装，修改 `src/` 后立即生效；构建依赖已由 `requirements.txt` 安装。

`requirements.txt` 包含以下直接依赖，它们的间接依赖由 pip 自动安装：

| 依赖 | 用途 |
| --- | --- |
| `openai` | OpenAI 兼容的模型接口 |
| `pydantic` | 消息、配置和工具参数的数据结构 |
| `python-dotenv` | 真实模型示例读取 `.env` |
| `build` | 生成 wheel 和源码分发包 |
| `setuptools` | Python 包构建后端 |
| `wheel` | wheel 构建工具 |

测试使用 Python 自带的 `unittest`，无需安装额外测试框架。`requirements.txt` 提供兼容版本范围，`pyproject.toml` 继续声明可安装包的元数据及依赖。

每次打开新终端，运行项目前先执行 `conda activate bpcagent`。可以使用 `conda env list` 查看环境，使用 `conda deactivate` 退出当前环境。

## 运行单次问答

### 离线演示

以下命令使用预设模型响应，不访问网络，也不需要 API 密钥：

```bash
python examples/single_qa.py
python examples/single_qa.py --with-tools
```

第一条验证问答入口与历史保存；第二条实际执行 `CalculatorTool`，将结果回传给模拟模型。预设问题为 `2 + 3 * 4`，预设最终回答为 `14`。这验证的是程序执行链路，不代表真实模型的回答能力。

### 真实模型问答

```bash
cp .env.example .env
```

填写 `LLM_API_KEY`、`LLM_MODEL_ID`、`LLM_BASE_URL`，然后执行：

```bash
python examples/single_qa.py --live "请用一句话介绍你自己。"
python examples/single_qa.py --live --with-tools "请使用计算器计算 2+3*4。"
```

示例只显式读取 `BPCAgent/.env`，已设置的环境变量优先。`MyLLM` 自身读取环境变量，不自动加载文件。真实模型需支持 OpenAI 兼容的 Chat Completions 接口及当前传入的参数；不同服务的模型参数支持可能不同。

## 在代码中使用

配置好环境变量后，可以创建智能体并注册工具：

```python
from bpcagent import BasicAgent, CalculatorTool, MyLLM, ToolRegistry

registry = ToolRegistry()
registry.register_tool(CalculatorTool())

agent = BasicAgent(
    name="Assistant",
    llm=MyLLM(),
    sys_prompt="你是一个简洁的助手，计算问题请调用工具。",
    tool_registry=registry,
)
answer = agent.run("计算 2+3*4。")
print(answer)
```

同一个 Agent 实例保存多次 `run()` 的输入和最终回答，可以使用 `get_history()` 查看、`clear_history()` 清空。普通流式问答使用 `run_using_stream()`。

## 添加自己的工具

继承 `Tool` 并实现 `run()` 和 `get_parameters()`，随后注册到 Agent 的 `ToolRegistry`：

```python
from bpcagent import Tool, ToolParameter

class UppercaseTool(Tool):
    def __init__(self):
        super().__init__(name="uppercase", description="将输入文本转换成大写。")

    def get_parameters(self):
        return [ToolParameter(name="input", type="string", description="输入文本")]

    def run(self, parameters):
        return parameters["input"].upper()

# registry.register_tool(UppercaseTool())
```

`BasicAgent` 的原有调用格式为 `[TOOL_CALL:uppercase:hello]`，也支持对象形式的 JSON 参数，例如 `[TOOL_CALL:uppercase:{"input":"hello"}]`。`ReActAgent` 使用 `Action: uppercase[hello]`。

建议给 `BasicAgent` 注册 `Tool` 对象。原有的 `register_function()` 入口保留，可通过 `ToolRegistry.execute_tool()` 或 `ReActAgent` 执行；`BasicAgent` 目前只按 `Tool` 对象查找。

## 测试与构建

测试使用 Python 标准库 `unittest`，模型与 SDK 请求均被模拟，计算器实际执行。不会读取 `.env` 或调用外部 API：

```bash
python -m unittest discover -s tests -v
python -m build
```

构建生成 `dist/` 下的 wheel 和源代码分发包。`examples/` 和 `tests/` 随源代码分发包提供。

## 相对原始代码的改动

主要执行逻辑沿用原始代码，提取和目录精简时做了以下调整：

1. 将 Agent、模型、工具、消息、配置和异常整理为单层模块，相关导入统一改为新路径。
2. 增加 `__init__.py`、公开导出、标准打包配置、文档、示例和测试。
3. 修复注册表读取函数工具描述时的 `escription` 拼写错误。
4. 模型调用失败时抛出已有的 `LLMException`，避免将异常对象作为正常回答返回。
5. `MyLLM.think()` 接收流式入口传来的参数，并转发温度和输出长度配置。

本阶段保留了原有文本工具协议、字符串返回值和 `print` 日志。工具调用结果暂时以用户消息回传给模型；对话历史只保存用户输入与最终回答；`Config.max_history_length` 尚未执行历史裁剪；流式入口不执行工具调用。后续可以在这个独立包中逐步改造这些接口。
