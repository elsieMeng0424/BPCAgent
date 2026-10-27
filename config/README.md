# 配置入口

公共配置使用 `.jsonc`，字段旁的 `//` 行注释列出可选值、单位和限制。
读取器仅忽略字符串外的行注释，URL 中的 `https://` 不受影响；不支持块注释、
尾随逗号或单引号。普通 `.json` 仍按严格 JSON 解析，密钥文件不改格式。
推理档位注释列的是配置层允许值，不代表每个模型都支持全部档位。

| 文件 | 内容 | 纳入 Git |
| --- | --- | --- |
| `workflow.jsonc` | 数据/输出路径、demo/live、Top-M/K、重试次数 | 是 |
| `pipeline.jsonc` | 流程版本、任务阶段、阶段指令、独立重试预算和 Review 指令 | 是 |
| `providers.jsonc` | 服务商地址、API 协议、密钥名称 | 是 |
| `models.jsonc` | 可复用模型档位：模型 ID、推理强度、超时等 | 是 |
| `agents.jsonc` | 启用的 Agent 各自选择哪个模型档位；Meta 暂为注释 | 是 |
| `secrets.example.json` | 空密钥模板 | 是 |
| `secrets.local.json` | 本机真实 API key | 否 |

当前工作区已有 `secrets.local.json`，其中旧密钥予以保留。其他机器首次使用时复制
`secrets.example.json` 为 `secrets.local.json`；不要将真实 key 写入可提交的配置。

配置关系为 `Agent → 模型档位 → 服务商 → API key`，同一服务商密钥可以供多个模型共享。
服务商固定分为四类，按密钥的签发平台选择，而不是按模型名称选择：

| provider | 服务 | 密钥名 | 默认协议 |
| --- | --- | --- | --- |
| `openai` | OpenAI 官方 | `OPENAI_API_KEY` | Responses |
| `claude` | Claude 官方 | `ANTHROPIC_API_KEY` | Chat Completions 兼容接口 |
| `deepseek` | DeepSeek 官方 | `DEEPSEEK_API_KEY` | Chat Completions |
| `gateway` | 第三方路由平台 | `GATEWAY_API_KEY` | Chat Completions，可改 Responses |

通过路由平台调用 GPT / Claude / DeepSeek，`provider` 都填 `gateway`，使用路由平台
签发的 key 和该平台的模型 ID。`gateway.base_url` 默认是 `null`；选用前必须填写
平台实际 API 地址。未使用的 provider 无须填写密钥或 Gateway 地址。

例如 `https://gateway.ddit.ai/anthropic` 看起来是 Anthropic Messages 协议入口，
并非天然兼容 `chat_completions`。当前适配器只实现 Chat Completions / Responses；
若平台仅提供原生 Anthropic 接口，需要先增加原生适配器。请向平台确认其 OpenAI 兼容
URL 及协议，不要靠改路径后缀猜测；目前未验证该平台，`gateway.base_url` 仍保留空值。

## 当前 DeepSeek 联调配置

三个任务 Agent 使用 `deepseek_flash_low`（`deepseek-flash`），Review 使用
`deepseek_pro_low`（`deepseek-v4-pro`），四个 Agent 共享 `DEEPSEEK_API_KEY`。
Meta 继续关闭。OpenAI 档位保留用于后续切换。默认运行模式仍为 demo，显式选择 live
才调用付费 API。参见 [DeepSeek 官方接口说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)
及 [思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。

本机 DeepSeek 直连有效；若终端继承的代理影响连接，可以仅对本次进程移除代理变量，
无需修改密钥文件或全局网络配置。`test.ipynb` 新增的完整流程示例包含这个可选步骤。

例如 `models.jsonc` 中：

```json
{
  "profiles": {
    "astra_medium": {
      "provider": "openai",
      "model": "gpt-6-astra",
      "reasoning_effort": "medium",
      "temperature": null,
      "max_tool_iterations": 10
    }
  }
}
```

在 `agents.jsonc` 给每个 Agent 独立绑定：

```json
{
  "agents": {
    "PatientStateAgent": {"llm": "astra_medium"},
    "TargetSelectionAgent": {"llm": "astra_medium"},
    "DrugSelectionAgent": {"llm": "astra_medium"},
    "ReviewAgent": {"llm": "sol_medium"}
  }
}
```

仓库的模型文件也定义了 `sol_medium`（`gpt-6-sol` + `medium`），作为不同 Review 模型的
示例配置，不是模型质量比较结论。每个 Agent 可选择不同档位；要改变某一个 Agent 的
推理强度，可新增 `astra_high` 档位并只修改该 Agent 的 `llm`。
同模型不同推理强度不满足“Review 使用不同模型”的要求。

`workflow.jsonc.plan_file` 指向 `pipeline.jsonc`；`null` 使用内置默认阶段。
每个阶段的 `max_attempts` 为正整数或 `null`：正整数优先，`null` 沿用工作流/CLI 的
`max_stage_attempts`。阶段 `instructions` 和 `review_instructions` 会传给对应 Agent。
目前支持患者→靶点→药物的依赖链或它的前缀，不支持跳过上游、关闭 Review、注册任意代码。
Meta 不运行，也不要求模型/密钥；即使配置了 Meta 的 llm 绑定也不会加载或调用它。
配置在创建工作流时固定，本次运行不接受热更新；未来 RSI 候选计划应经过测试后用于下一次运行。

`providers.jsonc` 中 `openai` 使用 `https://api.openai.com/v1` 和 `responses` 协议，
引用密钥名 `OPENAI_API_KEY`。在 `secrets.local.json` 填写这个键一次即可共享；
也可以用同名环境变量覆盖。原有 TASK/REVIEW 密钥不会自动猜测为 OpenAI 官方密钥。

真实运行前确认模型访问权限、服务商地址和对应密钥，然后运行：

```bash
cd /home/u2021202188/BPCAgent
.venv/bin/bpc-workflow --mode live --patient-id GENIE-DFCI-051542 --cutoff-day 249
```

省略 `--mode live` 时默认 demo，不读取密钥文件，也不连接模型服务。
患者 ID、cutoff、start-day 仍为每次运行的参数，避免把患者选择固化成环境配置。

配置规则：

- 未指定 `--config` 时，在当前工作目录查找 `config/workflow.jsonc`；找不到时使用
  默认参数并要求 `--data-dir`。不会在机器上的其他目录搜索密钥。
- `--config /path/to/workflow.jsonc` 显式选择配置，文件缺失/格式错误直接报错。
- 文件内所有相对路径相对于 `workflow.jsonc` 所在目录；CLI 覆盖的路径相对于当前目录。
- 工作流参数：CLI > workflow.jsonc > 内置默认值。
- 新格式的模型名称/地址以 models.jsonc/providers.jsonc 为准，启用的 Agent 显式绑定，
  不再用 TASK/META 环境变量隐式更换模型。
- 密钥：环境变量 > secrets.local.json；不会修改进程环境，也不会把密钥写入运行产物。
- 通用旧版 `LLM_*` 变量用于原有单 Agent 示例，不覆盖此工作流配置。
- 模型参数文件拒绝内联 `api_key` 字段、未知字段和无效数值。

## API 协议和推理参数

已按 [OpenAI 模型文档](https://developers.openai.com/api/docs/models/gpt-6-astra) 和
[GPT-6 使用说明](https://developers.openai.com/api/docs/guides/latest-model) 核对：
GPT-6 Astra 支持 `medium`，其工具调用使用 Responses。
配置的 `reasoning_effort` 在 Responses 中转换成 `reasoning: {"effort": "medium"}`，
在 Chat Completions 中传为 `reasoning_effort`。非 none 推理时不发送 temperature/top_p 等采样参数。

Responses 会将工具定义/结果转换为对应协议，并回传原始 reasoning/function_call 输出项，
保留多轮工具上下文；默认 `store=false`。`max_tokens` 在 Responses 中作为
`max_output_tokens`，在带推理参数的 OpenAI 兼容 Chat Completions 中作为 `max_completion_tokens`；
DeepSeek 官方接口保留 `max_tokens`。其工具循环同时回传服务返回的 `reasoning_content`。
模型对推理档位的具体支持范围以服务商文档为准。当前只支持同步 Responses，
Responses 流式接口会明确报错；现有 Chat Completions 的流式行为保持兼容。

Claude 使用[官方 OpenAI SDK 兼容接口](https://platform.claude.com/docs/en/api/openai-sdk)，
不是 Anthropic 原生 Messages 适配器。该接口忽略 `reasoning_effort`，所以 Claude
档位必须设为 `null`（或省略），否则加载时报错；并不等于禁用模型自身的思考。
原生 thinking 配置、需要额外 workspace header 的密钥暂不支持；当前使用已绑定 workspace 的密钥。
工具输出仍在本地进行 schema 校验。DeepSeek 使用[官方兼容地址](https://api-docs.deepseek.com/quick_start/agent_integrations/)。
这两类在当前适配器中只接受 `chat_completions`。Gateway 必须支持所选 OpenAI 协议和工具调用，
不能仅凭模型名称假定其支持所有推理参数。未进行真实服务的付费调用验证。

不再兼容旧的 task/review/meta 模型文件、`task_config` / `review_config` / `meta_config`
Python 参数或纯 TASK_LLM_* 环境变量启动方式。Live 必须配置文件或显式提供启用角色的
`agent_configs`；无配置文件仍可运行离线 demo。旧密钥值保留在本机文件中以防丢失，
但不会作为新 provider 的密钥回退来源；需确认来源后填写对应的新键名。

Python 使用相同加载器：

```python
from bpcagent.workflow.config import WorkflowConfiguration
from bpcagent.workflow import AgentWorkflow

config = WorkflowConfiguration("config/workflow.jsonc")
options = config.workflow_options({"mode": "live"})
workflow = AgentWorkflow(**options, **config.model_configs())
result = workflow.run(patient_id="GENIE-DFCI-051542", cutoff_day=249)
```

配置目录是部署侧文件，不嵌入 wheel；使用 wheel 时保留此目录并用 `--config` 指向它。
`config/*.local.json` 已加入 `.gitignore`，Git 忽略不等于加密，应限制本地文件读取权限。
