# BPCAgent

轻量级 Agent 开发包，用于创建智能体、注册工具和执行问答。项目目录名为 `BPCAgent`，Python 包名和安装名为 `bpcagent`。

## 目录结构

```text
BPCAgent/
├── pyproject.toml           # 包元数据、依赖与构建配置
├── requirements.txt        # 运行、示例和打包所需依赖
├── README.md
├── CONFIG_REFACTOR.log      # 统一配置入口的改动与验证记录
├── .env.example            # 模型配置模板，不含密钥
├── examples/
│   ├── single_qa.py        # 离线 / 真实模型单次问答
│   └── tool_execution.py   # 对象工具、函数工具和 Schema 导出
├── tests/
│   ├── test_tools.py       # 注册、校验、执行和结构化工具结果
│   ├── test_agents.py      # 问答、历史与实际工具执行
│   ├── test_config.py      # 配置优先级、校验、快照与脱敏
│   └── test_models.py      # 模型适配、流式与异常传播
└── src/bpcagent/
    ├── __init__.py         # 对外导出常用类
    ├── agents.py           # Agent、BasicAgent、ReActAgent
    ├── models.py           # MyLLM 模型接口
    ├── tools.py            # Tool、ToolArgs、FunctionTool、ToolResult、ToolRegistry
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

示例只显式读取 `BPCAgent/.env`，已设置的环境变量优先。`Config` 统一读取进程环境变量，包本身不自动加载文件。真实模型需支持 OpenAI 兼容的 Chat Completions 接口及当前传入的参数；不同服务的模型参数支持可能不同。

## 在代码中使用

配置好环境变量后，可以创建智能体并注册工具：

```python
from bpcagent import BasicAgent, CalculatorTool, Config, MyLLM, ToolRegistry

config = Config.resolve()
llm = MyLLM(config=config)

registry = ToolRegistry()
registry.register_tool(CalculatorTool())

agent = BasicAgent(
    name="Assistant",
    llm=llm,
    config=config,
    sys_prompt="你是一个简洁的助手，计算问题请调用工具。",
    tool_registry=registry,
)
answer = agent.run("计算 2+3*4。")
print(answer)
```

同一个 Agent 实例保存多次 `run()` 的输入和最终回答，可以使用 `get_history()` 查看、`clear_history()` 清空。普通流式问答使用 `run_using_stream()`。

## 统一配置入口

### 配置字段

| Config 字段 | 环境变量 | 默认值与含义 |
| --- | --- | --- |
| `model` | `LLM_MODEL_ID` | `None`，创建真实模型时必填 |
| `api_key` | `LLM_API_KEY` | `None`，创建真实模型时必填；使用 `SecretStr` 保存 |
| `base_url` | `LLM_BASE_URL` | `None`，创建真实模型时必填 |
| `temperature` | `TEMPERATURE` | `0.7`，非负有限数；`None` 时不发送此参数 |
| `max_tokens` | `MAX_TOKENS` | `None`，不发送此参数；设置时必须为正整数 |
| `timeout` | `LLM_TIMEOUT` | `120` 秒，必须为正有限数 |
| `max_tool_iterations` | `MAX_TOOL_ITERATIONS` | `3`，BasicAgent 工具交互轮次上限，正整数；每轮至多一次模型请求 |
| `max_steps` | `MAX_STEPS` | `5`，ReActAgent 步数上限，正整数 |
| `log_level` | `LOG_LEVEL` | `INFO`，保留字段，目前不控制日志输出 |
| `max_history_length` | `MAX_HISTORY_LENGTH` | `100`，正整数；保留字段，目前不裁剪历史 |
| `default_provider` | — | `openai`，保留字段，目前不切换模型提供方 |

`TEMPERATURE`、`MAX_TOKENS` 环境变量为空时解析为 `None`。例如模型不支持自定义温度时，可在 `.env` 中设置 `TEMPERATURE=`。`LLM_TIMEOUT` 等必需数值字段为空或非法时会报错。

### 优先级与快照

模型参数从高到低的优先级：

1. 单次 `invoke()` / `think()` 参数（`temperature`、`max_tokens`、`timeout`）。
2. `MyLLM(...)` 显式构造参数。
3. 传入 `Config` 中显式设置的字段。
4. 进程环境变量（应用可先用 `load_dotenv(..., override=False)` 加载 `.env`）。
5. `Config` 默认值。

```python
# 只指定 max_tokens，temperature 仍可来自环境变量。
partial = Config(max_tokens=500)
config = Config.resolve(partial)
llm = MyLLM(config=config, timeout=60)
answer = llm.invoke(
    [{"role": "user", "content": "你好"}],
    temperature=None,  # 本次不发送 temperature
    timeout=10,        # 本次使用 10 秒超时
)
```

`Config(...)` 仅创建并校验显式配置；`Config.resolve(...)` 是唯一的配置合并入口，按优先级合并各来源并返回完整快照。字段不可直接赋值，需要更改时仍使用 `resolve`：

```python
# 初次解析：默认值 + 进程环境 + 显式覆盖。
config = Config.resolve(overrides={"max_tokens": 500})

# 修改已有配置：不读取进程环境，返回新对象，原对象保持不变。
updated = Config.resolve(config, overrides={"max_steps": 8}, environ={})
```

`environ=None`（默认）读取进程环境；`environ={}` 禁用环境读取；也可以传入自定义环境字典用于测试。完整快照再次传入 `MyLLM` 或 Agent 时保留已解析的值；单次模型调用不会重新读取环境，也不会修改原配置。

测试或完全离线运行时使用 `Config.resolve(environ={})` 排除环境变量影响。仅使用模拟模型的 Agent 无需配置密钥；真实 `MyLLM` 在创建 SDK 客户端前检查模型名、密钥和服务地址。

### Agent 与模型如何共享配置

将同一个已解析的 `config` 分别传入 `MyLLM(config=config)` 和 `BasicAgent(..., config=config)`，如上方示例所示。Agent 使用自己的执行设置，已有模型实例继续使用创建它时的模型设置；仅向 Agent 传入 `Config(temperature=...)` 不会重新配置已有模型。

- `BasicAgent.run(..., max_tool_iterations=2)` 可覆盖本次工具迭代上限，不改变 Agent 默认值。
- `ReActAgent(..., max_steps=8)` 可覆盖构造时的步数设置。
- 未显式提供这些参数时，使用 Agent 配置中的值。
- BasicAgent 的 `max_tool_iterations` 限制工具模式下的模型交互轮次，最终回答也占一轮。一轮可以执行多个工具，因此它不是工具执行总次数上限。达到上限仍无最终回答时抛出 `AgentException`，不再追加模型请求，也不保存成功历史。
- ReActAgent 达到步数上限同样抛出 `AgentException`；使用传入的系统提示词，每次运行重新建立推理历史，尚不将上次会话自动拼入本次提示词。

### 接口与错误处理

`MyLLM` 的连接参数统一使用 `model`、`api_key`、`base_url`，与 `Config` 字段及 SDK 参数一致。位置参数顺序为模型名、密钥、服务地址。

模型配置统一通过 `llm.config` 读取，例如 `llm.config.temperature`、`llm.config.timeout` 和 `llm.config.base_url`。仅保留 Agent 使用的 `llm.model` 只读属性。显式传入 `temperature=None` 或 `max_tokens=None` 表示省略参数；`timeout=None`、`timeout=0` 都会报错。

```python
llm = MyLLM(
    model="your-model",
    api_key="your-api-key",
    base_url="https://your-provider.example/v1",
)
print(llm.config.timeout)
```

构造时的其他模型请求参数（如 `top_p`、`extra_body`）会传递给 SDK，同名单次参数覆盖构造参数，字典按整项替换。提供方特有参数由 SDK/服务端校验；`model`、连接配置和 Agent 参数不能在单次模型请求中覆盖，`stream` 由 `invoke/think` 决定。

`resolve` 及模型请求设置非法时抛出 `ConfigException`；直接 `Config(...)` 校验失败抛出 Pydantic `ValidationError`；SDK 调用失败抛出 `LLMException`。流式配置错误在开始迭代生成器时抛出。

`config.to_dict()` 和配置展示会掩码密钥，适合诊断，不能用于还原客户端凭据。`llm.config.api_key` 保持 `SecretStr` 类型，客户端初始化时才通过 `get_secret_value()` 取出明文传给 SDK；明文不应写入日志。

### 离线测试

在已激活的 Conda 环境内执行：

```bash
python -B -m unittest discover -s tests -v
```

测试不请求真实服务，覆盖配置合并、模型参数传递与现有问答和工具执行行为。

## 统一工具执行

工具执行流程为：`Agent → ToolRegistry → 参数校验 → Tool.run → ToolResult`。
对象工具和函数工具存储在同一注册表，所有框架调用都通过 `execute_tool` 执行一次，不自动重试。

### 定义对象工具

```python
from pydantic import Field
from bpcagent import Tool, ToolArgs, ToolRegistry

class UppercaseArgs(ToolArgs):
    input: str = Field(min_length=1, description="需要转成大写的文本")

class UppercaseTool(Tool[UppercaseArgs]):
    args_schema = UppercaseArgs

    def __init__(self):
        super().__init__(name="uppercase", description="将文本转为大写")

    def run(self, parameters: UppercaseArgs) -> str:
        return parameters.input.upper()

registry = ToolRegistry()
registry.register_tool(UppercaseTool())
result = registry.execute_tool("uppercase", {"input": "hello"})
if result.status == "success":
    print(result.data)
else:
    print(result.error.code, result.error.message)
```

参数模型及嵌套参数模型继承 `ToolArgs`：默认严格校验类型、拒绝未知字段、检查默认值；用 Pydantic `Field` 定义说明和约束。输入必须是 JSON 对象，JSON 布尔值可以使用，布尔字符串不会自动转换；工具确实需要转换时，在其参数模型中显式编写校验器。

`run()` 接收校验后的参数模型，成功返回原生 JSON 数据：字符串、数字、布尔值、列表、字符串键字典或 `None`。浮点数必须有限。工具执行失败时抛出异常，由注册表包装；普通文本中出现“错误”不会被自动识别为失败。任意对象、元组、非字符串字典键等返回值会产生 `serialization_error`。

### 注册函数工具

```python
def uppercase(input: str) -> str:
    return input.upper()

registry.register_function(
    "uppercase_function", "将文本转为大写", uppercase,
    args_schema=UppercaseArgs,
)
```

函数由 `FunctionTool` 包装后走同一注册路径。按参数模型的字段名传入关键字参数，嵌套模型保留实例类型；函数必须为普通同步函数，不接受仅位置参数、`*args` 或 `**kwargs`。签名不匹配时在注册阶段报错。

名称使用 1—64 个字母、数字、下划线或连字符；`Finish` 用于 ReAct 完成动作。名称重复默认抛出 `ToolException`，有意替换时指定 `replace=True`。`get_tool/list_tools/unregister_tool` 对所有工具行为一致。

### ToolResult 协议

每次执行返回 `call_id`、`tool_name`、`status`、`data`、`error`。

| 状态或错误码 | 行为 |
| --- | --- |
| `status="success"` | `error=None`；`data` 可以为 `None` |
| `tool_not_found` | 未找到工具，未执行 |
| `invalid_arguments` | 参数格式或校验失败，未执行 |
| `execution_error` | 工具执行时抛出异常 |
| `serialization_error` | 工具已经执行，但返回值不满足 JSON 数据协议 |

传入 `call_id` 时结果保留该 ID，否则生成新 ID；ID 仅用于关联，不提供自动去重。失败结果的 `data=None`，通过 `error.code` 判断类别。序列化失败不会自动重新执行工具。

### OpenAI 函数工具 Schema

```python
tools = registry.to_openai_tools()
```

导出 Chat Completions 的函数工具格式：

```json
{
  "type": "function",
  "function": {
    "name": "uppercase",
    "description": "将文本转为大写",
    "parameters": {
      "type": "object",
      "properties": {"input": {"type": "string", "minLength": 1}},
      "required": ["input"],
      "additionalProperties": false
    },
    "strict": false
  }
}
```

`parameters` 由 Pydantic 的 `model_json_schema()` 生成，完整输出还可能包含标题、描述和嵌套 `$defs`。Schema 描述输入结构；调用时提供实际参数值。无需另写一套 Schema。

当前明确导出 `strict=False`，保留默认值和可省略字段；本地严格类型校验始终执行。OpenAI 的服务端 `strict` 是另一层约束，本阶段未实现其 Schema 子集转换或服务验证。该外层格式对应 Chat Completions，Responses 使用不同封装，尚未增加适配。

参考入口：[OpenAI Function calling](https://platform.openai.com/docs/guides/function-calling)。本次网络访问受限，格式通过本地安装的 OpenAI SDK 类型定义核对，尚未进行真实 API 验证。

### 当前 Agent 的调用形式与后续范围

本阶段普通 `run()` 仍从模型文本提取请求，参数统一为 JSON 对象：

```text
[TOOL_CALL:calculate:{"input":"2+3*4"}]
Action: calculate[{"input":"2+3*4"}]
```

分别用于 BasicAgent 和 ReActAgent；JSON 解码器识别完整对象，支持嵌套数组、跨行 JSON，以及字符串中的逗号和括号。解析失败反馈结构化参数错误，不猜测字段。错误和成功均以 ToolResult JSON 反馈给模型，错误同样占用交互轮次。

**已完成：** 统一注册、参数模型、OpenAI 工具定义导出、统一执行结果、两种 Agent 接入和轮次耗尽停止。

**待实现：** 模型原生 `tool_calls` 的请求/返回处理及 `role="tool"` 消息配对、服务端严格 Schema 模式、独立工具总调用预算、详细运行记录、异步及流式工具循环。`run_using_stream()` 目前仍只处理普通文本；`MyLLM.invoke()` 仍返回文本，不能把 Schema 导出视为原生工具调用已接通。

### 工具离线演示

```bash
python -B examples/tool_execution.py
python -B examples/single_qa.py --with-tools
python -B -m unittest discover -s tests -v
```

第一个示例包含计算器对象工具与带数组、嵌套选项及默认值的统计函数工具，并演示参数错误。
