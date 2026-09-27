# BPCAgent

轻量级 Agent 开发包，用于创建智能体、注册工具和执行问答。项目目录名为 `BPCAgent`，Python 包名和安装名为 `bpcagent`。

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

采用单层模块结构。安装后可从任意工作目录导入 `bpcagent`，无需手动修改 `sys.path`。

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
