# BPCAgent

一个用于注册工具、创建智能体并执行工具调用的 Python 框架。完整安装、配置与接口说明见 [doc/detail.md](doc/detail.md)。

## 可配置的迭代工作流

新增 `bpcagent.workflow`：PatientStateAgent → TargetSelectionAgent → DrugSelectionAgent
串行执行，每次 Agent 输出交给 ReviewAgent；被拒绝后携带 reason 重跑当前 Agent，
通过后才进入下一个阶段。MetaAgent 暂停接入，所有实际运行角色共享截止日绑定的患者状态。
支持离线 demo、独立 Review 模型配置、结构化输出、有限重试和逐阶段日志。
Python 入口统一为 `AgentWorkflow`；[test.ipynb](test.ipynb) 的“完整 AgentWorkflow”
一节可独立运行，目前配置为三个 DeepSeek Flash 任务 Agent + DeepSeek V4 Pro Review。
每次 workflow 实验统一保存在 `workspace/YYYY-MM-DD/run_<uuid>/`（北京时间）：
`logs/console.log` 保存控制台输出，各阶段目录保存输入、输出和调用轨迹。
Python、CLI、Notebook 共用此布局，不再向顶层 `log/` 分散写入日志。
一次 `workflow.run()` 只创建一个实验目录；Agent、Review 和重试都放在该目录内部。
连续调用多次才产生多个实验目录。回归测试的 Agent 日志写入系统临时目录，测试结束后清理。
2026-10-27 遗留的 455 个测试日志目录已归档到当日 `_test_runs/`，保留原文件，
当日 `RUN_INDEX.md` 列出 3 次 workflow 实验。历史目录整理工具默认只预览：

```bash
python scripts/organize_workspace.py workspace/2026-10-27
# 确认后添加 --apply；仅归档识别出的测试日志，不移动 workflow 实验。
```

科学工具暂为占位，demo 候选列表为空；真实患者提取和完整性检查实际执行。

```bash
python -m pip install -e .
python -m bpcagent.workflow.cli --data-dir ../bpc-patient-state/dataset \
  --patient-id GENIE-DFCI-051542 --cutoff-day 249 --start-day 159
```

默认无需 API key。完整架构、真实模型配置、离线评估和指南获取入口见
[迭代工作流使用说明](doc/workflow.md)。只提取患者时也使用下文的单阶段 workflow。

患者提取工具 [BPCStateExtractor](https://github.com/Chenshengm/BPCStateExtractor)
已直接集成在 `vendor/BPCStateExtractor/`，随本项目一并安装。
源码来源、同步状态和更新方法见 [内置工具说明](vendor/README.md)。
数据集不随源码打包；示例中的 `../bpc-patient-state/dataset` 是本机现有数据路径，可自行替换。

配置集中维护在 [config/](config/README.md)：`workflow.jsonc` 管理流程，`providers.jsonc`
管理 `openai` / `claude` / `deepseek` 官方平台和 `gateway` 路由平台，
`models.jsonc` 管理模型/推理档位，`agents.jsonc` 给启用的 Agent 分别绑定 LLM；
`pipeline.jsonc` 管理阶段、阶段提示和重试上限；
`secrets.local.json` 按服务商密钥名称存放 API key（Git 忽略）。
在仓库目录运行会自动加载配置，也可以传 `--config /path/to/workflow.jsonc`。

## 架构总览：统一 PatientStateAgent

目前是“通用 Agent 执行框架 + BPC 患者提取工具 + 可配置的任务／Review 编排”，
不是已经完成靶点和药物推荐的医学系统。建议先读多 Agent workflow，再按需深入底层。

| 层次 | 职责 | 首先看哪里 |
| --- | --- | --- |
| 调用入口 | 提供患者、截止日、demo/live 模式，读取配置 | [test.ipynb](test.ipynb)、[cli.py](src/workflow/cli.py) |
| 配置和流程定义 | 绑定各 Agent 模型，声明阶段顺序、指令和重试上限 | [config/](config/README.md)、[definition.py](src/workflow/definition.py) |
| Workflow 编排 | 调用任务 Agent，再调用 Review；决定继续、重做或停止，保存产物 | [runner.py](src/workflow/runner.py) |
| 角色与共享状态 | 角色提示词、可用工具、结构化提交、截止日绑定的患者快照 | [workflow/agents.py](src/workflow/agents.py)、[state.py](src/workflow/state.py) |
| 通用执行框架 | 模型请求 → 工具调用 → 工具结果回传 → 下一次模型请求 | [agents/agents.py](src/agents/agents.py)、[tools/tool.py](src/tools/tool.py) |
| 数据提取和执行工具 | 读取 BPC 数据、组装状态和证据；独立流程可调用 Shell/Docker | [内置 BPCStateExtractor](vendor/README.md)、[tools/](src/tools/) |

患者提取统一使用 workflow 版 `PatientStateAgent`；高级单位纠错任务作为单独能力保留：

| 路径 | 实际调用链 | 当前用途 |
| --- | --- | --- |
| 多 Agent workflow（本文主线） | `AgentWorkflow` → `workflow.agents.PatientStateAgent` → `SharedPatientState` → `bpc_state` | 提取并共享患者状态，然后进入靶点、药物、Review 流程 |
| 高级单位纠错任务 | `PatientStateTask` → `UnitPatientStateAgent` → `create_patient_tools()` → `bpc_state` | 单独执行提取、Shell 单位核查／纠错和宿主验收；Task 入口启用 Docker |

现在只有一个 `PatientStateAgent` 类，定义在 `src/workflow/agents.py`。
`from bpcagent import PatientStateAgent` 和 `from bpcagent.workflow import PatientStateAgent`
指向同一个类；旧的 `bpcagent.agents.patient_state` 模块及其患者参数构造接口已删除。
普通调用从 `AgentWorkflow.run(patient_id=..., cutoff_day=...)` 进入；只提取患者时使用单阶段计划，
仍保留 Review。原独立版的状态/证据落盘能力已合并到 workflow，每次重试独立记录。
`UnitPatientStateAgent` 直接继承 `BasicAgent`，其提取步骤为私有内部方法；Shell 纠错仍未自动接入主 workflow。

### 关键文件树

下面列出主要源码与阅读入口，省略缓存、环境文件及部分测试文件；`src/` 安装后的包名是 `bpcagent`，
内置工具安装后的包名是 `bpc_state`，映射由 `pyproject.toml` 定义。

```text
BPCAgent/
├── README.md                         # 架构、阅读路线和运行入口（本文）
├── pyproject.toml                    # 依赖、包映射、CLI 入口、pytest 配置
├── test.ipynb                        # 交互示例；workflow 配置、运行、查看结果三个单元
├── config/
│   ├── workflow.jsonc                # 模式、数据/输出路径、top_m/top_k、默认尝试次数
│   ├── pipeline.jsonc                # 阶段列表、逐阶段指令/尝试次数、Review 指令
│   ├── agents.jsonc                  # Agent 名称 → 模型档位名称
│   ├── models.jsonc                  # 模型档位 → provider/model/推理强度/请求参数
│   ├── providers.jsonc               # 服务商 → base_url/API 协议/密钥变量名
│   ├── secrets.example.json          # 密钥文件模板，没有真实密钥
│   ├── secrets.local.json            # 本机创建的密钥文件，Git 忽略
│   └── README.md                     # 配置选项、优先级和平台限制
├── src/
│   ├── __init__.py                   # 对外导出的通用 Agent、Tool、Task 等接口
│   ├── log_paths.py                  # 日期分区、唯一实验目录和日志路径
│   ├── workflow/                     # 多 Agent 主线
│   │   ├── __init__.py               # 导出 AgentWorkflow、WorkflowPlan、StageSpec
│   │   ├── cli.py                    # 命令行参数 → 配置 → workflow.run()
│   │   ├── config.py                 # 解析 JSONC、路径、模型绑定和本机密钥
│   │   ├── definition.py             # StageSpec / WorkflowPlan；校验依赖顺序
│   │   ├── runner.py                 # 调度、任务/Review 循环、反馈、终止和落盘
│   │   ├── agents.py                 # 五种角色、提示词、工具注册、submit_output 校验
│   │   ├── state.py                  # SharedPatientState、完整性检查、保守单位转换
│   │   ├── models.py                 # 各阶段输出与 Review/Meta 输出的数据结构
│   │   └── demo.py                   # 离线 DemoLLM；预设模型响应，不是流程调度器
│   ├── agents/                       # 可复用的单 Agent 执行框架
│   │   ├── agents.py                 # Agent / BasicAgent / ReActAgent；模型—工具循环
│   │   ├── config.py                 # 单个模型客户端的配置解析、校验和密钥脱敏
│   │   ├── models.py                 # LLM 客户端及 Chat Completions 请求/响应适配
│   │   ├── responses.py              # Responses API 的消息和工具调用适配
│   │   ├── messages.py               # Message / ToolCall / LLMResponse
│   │   ├── result.py                 # AgentResult：输出、轨迹、完成状态、附加信息
│   │   ├── exceptions.py             # 配置、模型、Agent 和工具异常
│   │   ├── unit_patient_state.py     # 私有提取阶段 + Shell 单位处理、证据核对和独立验收
│   │   └── unit_rules.py             # 单位/检验类型规则和转换计算
│   ├── tools/
│   │   ├── tool.py                   # ToolArgs / ToolResult / Tool / ToolRegistry
│   │   ├── bpc_patient.py            # 独立 Agent 的 BPC 工具适配及患者 JSON 保存器
│   │   ├── shell.py                  # 本机 Shell；工作目录不是安全隔离边界
│   │   ├── sandbox_shell.py          # Docker 内 Shell 的工具适配
│   │   └── calculator.py            # 计算器工具和通用工具调用示例
│   ├── tasks/
│   │   ├── base.py                   # Task 抽象接口、TaskResult
│   │   └── patient_state.py          # 独立患者任务：建目录、创建 Agent、收集产物
│   └── sandbox/
│       ├── config.py                # Docker 镜像、超时、资源等配置
│       ├── docker.py                # 容器创建、挂载、执行和清理
│       └── image/Dockerfile         # 患者处理沙箱镜像
├── vendor/BPCStateExtractor/          # 直接内置的工具项目，不是数据集
│   ├── src/bpc_state/
│   │   ├── repository.py            # 读取原始临床/分子数据文件
│   │   ├── extractor.py             # 组装状态与证据；BoundedPatientTool 绑定患者/日期上限
│   │   ├── models.py                # PatientState 等结构化数据模型
│   │   ├── semantics.py             # null/unknown 等缺失语义
│   │   ├── rules.py                 # 字段规则加载
│   │   ├── tool_schema.py           # 生成患者状态/证据工具的函数调用 Schema
│   │   ├── evaluation.py            # 单独导出 cutoff 之后的离线评估数据
│   │   └── cli.py                   # bpc-state 命令行
│   ├── config/field_rules.json      # 默认字段规则，作为包资源安装
│   ├── schemas/                     # 患者状态和离线评估 JSON Schema
│   └── tests/                       # 提取、缺失语义和时间泄漏回归测试
├── tests/                           # 框架、workflow、配置、日志和 notebook 测试
├── scripts/organize_workspace.py     # 保守归档历史测试日志，生成当日实验索引
├── examples/                        # 单次问答、直接工具调用、Shell 离线实验
├── doc/                             # workflow / tasks / sandbox / 底层接口详细说明
├── workspace/YYYY-MM-DD/run_<uuid>/ # 新 workflow 实验的全部产物和 logs/
└── log/                             # 旧版本遗留目录，新 workflow 不再写入
```

### 跟着一次运行读代码

**1. 从入口和配置开始。** 先看 [cli.py](src/workflow/cli.py) 的 `main()`，或 Notebook 的
`workflow-config` 单元。`WorkflowConfiguration.workflow_options()` 处理路径和流程参数；
live 模式额外调用 `model_configs()`，按 `agents → models → providers → secrets/环境变量`
解析每个启用角色的模型配置。同名环境变量优先于本机密钥文件。demo 不加载模型密钥。
配置文件内路径相对 `workflow.jsonc` 所在目录，CLI 显式路径相对当前目录。
直接实例化 `AgentWorkflow(...)` 不会自动读 `config/`，需像 Notebook 一样先调用配置加载器。

**2. 进入调度器。** 看 [runner.py](src/workflow/runner.py) 的 `__init__()`、`run()`、`_run()`。
构造时固定本次 `WorkflowPlan`，选择真实模型配置或 `DemoLLM`。
`run()` 创建 `workspace/日期/run_<uuid>/logs/console.log`，然后 `_run()` 创建共享患者状态、
保存 `request.json` 和 `workflow_plan.json`，按计划逐阶段执行。
`definition.py` 允许只执行合法的前缀阶段，但不允许 Drug 跳过 Target，也不能关闭 Review。

**3. 进入 PatientState 阶段。** 看 [workflow/agents.py](src/workflow/agents.py) 的
`WorkflowAgent.__init__()`：它将角色提示词和阶段指令组合，并为角色注册工具。
所有角色都有 `get_shared_patient_state`、`get_stage_context`、`submit_output`；
PatientState 额外拥有 `get_patient_state`、`get_patient_evidence`、`state_check`。
模型不能自由改患者 ID 或 cutoff，这些由宿主绑定。

**4. 看患者 JSON 从哪里来。** `get_patient_state` → `SharedPatientState.extract()` →
`BoundedPatientTool.get_patient_state()` → 内置 `BPCStateExtractor`。
第一次提取同时计算 `check_state()`、`normalize_markers()` 和近期观察视图，随后缓存成 JSON。
每次读取返回独立副本；重试不会重新生成一个不同的共享快照。
每次 Agent 调用 `get_patient_state` 都将本轮状态保存到阶段目录；当快照有证据引用时，
还必须成功调用 `get_patient_evidence` 保存至少一条同 cutoff 的证据，失败可修正后重试。
没有引用的快照显式保存 `status=no_evidence_refs` 的空证据文件，不伪造引用。
`state_check` 工具读取这个检查结果，并标记当前这次 PatientState Agent 已完成检查，
因此每次重试都必须调用它，才能通过 `submit_output`。

这里不要混淆三个数据：

- `patient_state`：cutoff 当天及以前可见的事实，保留更早的诊断和治疗历史。
- `recent_observations`：由 `start_day` 筛选的近期观察视图，不取代全部病史。
- cutoff 之后的状态：由独立 `evaluation.py` 导出用于离线评估，不加入共享状态或模型输入。

数字缺失保留 `null`，用 `field_status` 解释 unknown 等语义；不会把每个空字段都改成字符串。
Workflow 的单位处理只做已知单位的保守转换，保留原始值，不会自动修复所有可疑单位。

**5. 看一个 Agent 内部如何调用工具。** 跳到 [agents/agents.py](src/agents/agents.py) 的
`_run_with_tools()`、`_run_tool_loop()` 和 `_execute_tool_call()`：
调用 LLM → 收到 `ToolCall` → 解码参数 → `ToolRegistry.execute_tool()` 校验并执行 →
把 `ToolResult` 写回消息 → 再调用 LLM，直到最终响应或达到调用上限。
Workflow 的结构化结果不是自由文本，而是 `submit_output` 工具提交的对象，
Schema 在 [workflow/models.py](src/workflow/models.py)，额外校验在 `WorkflowAgent.submit()`。
只有 Agent 正常完成且结构化结果已提交，调度器才把该次执行记为成功。

**6. 回到调度器看 Review 和重试。** `_run()` 内部的 `invoke()` 每调用一次完整 Agent 就记录一个 turn。
任务输出后立即新建 ReviewAgent，它读取相同患者快照、阶段上下文和待审输出。

```text
PatientState → Review → TargetSelection → Review → DrugSelection → Review
                 │                           每个 Review 都使用同一套分支规则
                 ├─ proceed：接受当前输出，继续下一阶段
                 ├─ revise ：带 reason 和上一轮输出重跑当前阶段，再 Review
                 └─ block  ：停止；Review 执行失败也停止
```

无重试时为 6 个 turn，不是 6 次模型请求；一个 turn 内可以有多次模型与工具交互。
`max_stage_attempts` / `max_attempts` 限制整个阶段的尝试次数（含首次），
`max_tool_iterations` 限制一个 Agent 内部的模型—工具循环。
已通过 Review 的上游结果进入 `accepted_outputs`；被拒绝的结果留在轨迹和反馈中，不作为已接受结果传递。
超过尝试次数返回 `revision_limit`。Meta 的调用仍是注释，不会在每轮自动执行 RSI。

**7. 看落盘结果。** 每个 turn 保存输入上下文、完整调用轨迹和结构化输出；最后保存共享状态及总体结果。
日志查看顺序建议是：`workflow_result.json` → 出问题阶段的 `output.json` → `transcript.json` → `logs/console.log`。

### 先实际跑一个不消耗 API 的例子

在项目根目录执行（本机 `.venv` 已配置；数据路径可替换）：

```bash
.venv/bin/python -m bpcagent.workflow.cli \
  --config config/workflow.jsonc --mode demo \
  --patient-id GENIE-DFCI-051542 --cutoff-day 50 \
  --workspace-root workspace
```

demo 仍然读取真实 BPC 文件、提取患者 JSON、执行工具和保存结果；只有模型响应是预设的。
终端最后返回本次 `workflow_result.json` 的路径。用这个确切路径查看结果，不要随便选一个旧实验：

```python
import json
from pathlib import Path

# 换成刚才命令输出的 result 路径。
result_path = Path("workspace/YYYY-MM-DD/run_<uuid>/workflow_result.json")
result = json.loads(result_path.read_text(encoding="utf-8"))
print(result["status"], result["finished"])
for turn in result["turns"]:
    output = turn["output"] or {}
    print(turn["turn"], turn["agent"], turn["attempt"], turn["status"],
          output.get("disposition"), output.get("reason"))
print("日志:", result["log_path"])
```

正常无重试时，文件对应关系如下：

```text
workspace/日期/run_<uuid>/
├── request.json                     # 患者、日期边界、模式、候选数和默认尝试上限
├── workflow_plan.json               # 本次执行的阶段计划快照
├── logs/console.log                 # stdout/stderr 和运行开始、结束状态
├── 01_PatientStateAgent/
│   ├── context.json                 # 该 turn 的阶段指令、上游输出、已有反馈
│   ├── transcript.json              # 模型消息、工具调用、工具返回
│   └── output.json                  # 状态封装 + 校验后的结构化输出或失败原因
├── 02_ReviewAgent/                   # 同样三个文件；output 中查看 disposition/reason
├── 03_TargetSelectionAgent/
├── 04_ReviewAgent/
├── 05_DrugSelectionAgent/
├── 06_ReviewAgent/
├── patient_state.json               # 原始 cutoff 快照（成功提取后保存）
├── shared_patient_state.json        # 加上近期视图、单位转换、完整性报告和哈希
└── workflow_result.json             # 所有 turn、Review 反馈、接受的输出和最终状态
```

重试时编号继续递增，例如 `03_PatientStateAgent/`，不要仅凭序号推断角色。
PatientState 阶段还保存 `patient_state_day_<cutoff>_<序号>.json` 和
`patient_evidence_day_<cutoff>_<序号>.json`；证据可分多次查询，不覆盖先前批次。
每个 turn 的 `artifacts` 字段列出这些文件，路径相对本次实验的 `task_dir`。
输入检查或未预期异常可能导致没有最终结果文件，此时从 `workflow.last_run_dir` 和 `logs/console.log` 排查。
Notebook 的 workflow 示例目前默认 `MODE='live'`，想先走上述离线路径需改为 `'demo'`；
live 会调用配置的外部模型服务并发送截止日内患者信息，可能产生费用。

### 当前已实现和未实现的边界

- 已实现：真实患者提取、截止日限制、缺失语义、共享状态、基础检查、结构化输出、Review 有限重试和完整记录。
- 仍占位：workflow 中的 `search`、`bash`、`pathway_graph`、`graph_ranking`、`drug_catalog`、`dock`、`ept_meet`、`drug_metrics`。
  即使仓库有真实 `ShellTool`，也不代表 workflow 的 `bash` 已经接入它。
- 当前提交校验会拒绝非空的靶点／药物候选；demo 和 live 都不会绕过这一限制。
  接入真实科学工具时，需一起更新工具实现、候选校验和测试，而不是只修改提示词。
- MetaAgent 类和提案结构存在，但调度未启用；RSI 不会自动改代码、配置或数据集。
- `finished=true` 和 Review 的 `proceed` 表示流程执行成功／允许进入下一阶段，不代表医学正确性已验证。

继续读回归用例时，优先看 [test_workflow.py](tests/test_workflow.py)（完整运行和时间边界）、
[test_workflow_iteration.py](tests/test_workflow_iteration.py)（拒绝、重试、停止）、
[test_workflow_notebook.py](tests/test_workflow_notebook.py)（Notebook 示例）和
[test_log_paths.py](tests/test_log_paths.py)（日期、归档和防覆盖）。

## 独立 Agent 示例的配置

以下是独立 Agent/Tool/Task 用法，不是上述 workflow 配置入口。
示例在 `BPCAgent` 目录运行，要求已安装 `bpcagent` 和示例依赖 `python-dotenv`；
真实模型可由示例加载 `.env` 中的 `LLM_API_KEY`、`LLM_MODEL_ID`、`LLM_BASE_URL`。
不要将其与 workflow 使用的 `config/secrets.local.json` 和逐 Agent 模型绑定混淆。

## 已实现的智能体与工具

| 智能体 | 用途 | 调用入口 |
| --- | --- | --- |
| `BasicAgent` | 通用问答与工具调用，保留对话历史 | `agent.run("Task")` |
| `PatientStateAgent` | 提取并保存状态/证据、检查、共享快照和结构化提交，再交给 Review | `AgentWorkflow.run(...)`；可用单阶段计划 |
| `UnitPatientStateAgent` | 提取后通过 Shell 核查单位、根据证据直接应用推断修正并统一单位，报告改动位置与理由，独立验收后返回报告 | `agent.run()` |

| 工具类 | 注册名称 | 参数与行为 |
| --- | --- | --- |
| `CalculatorTool` | `calculate` | `input`：数学表达式，返回计算结果 |
| `ShellTool` | `run_shell` | `command`：在固定工作目录执行命令，读取／写入文件并返回退出码、输出和超时信息 |
| `PatientStateTool` | `get_patient_state` | `cutoff_day`：截止日期，返回患者状态并保存 JSON |
| `PatientEvidenceTool` | `get_patient_evidence` | `cutoff_day`、`evidence_refs`：返回对应证据并保存 JSON |

上表的工具类是直接调用工具/单位纠错任务的适配，可通过 `create_patient_tools()` 创建。
Workflow 复用同一个底层提取器及 JSON 保存器，并固定 cutoff：`get_patient_state` 无参数，
`get_patient_evidence` 只接受 `evidence_refs`，不让模型改变患者或日期。

## 通过 Task 运行完整患者流程

`PatientStateTask` 将配置、工作目录创建、Agent 调用和产物收集封装为任务入口。先用 `PatientStateTaskConfig` 配置数据路径、总工作区、模型和 sandbox，再调用 `task.run(patient_id=..., cutoff_day=..., cancer_id=...)`；其中 `cancer_id` 可省略。每次调用创建独立 Agent，返回 `TaskResult`，可直接读取 `finished`、`artifacts`、`task_dir`、`log_path` 和 `agent_result`。

抽象 `Task` 与 `TaskResult` 同在 `src/tasks/base.py`；现有 Agent 的执行逻辑保持不变。完整示例和参数说明见 [Task 使用文档](doc/tasks.md)。

## 注册工具、创建并使用智能体

```python
from dotenv import load_dotenv
from bpcagent import BasicAgent, CalculatorTool, Config, ToolRegistry

load_dotenv(".env", override=False)
config = Config(max_tool_iterations=6)
registry = ToolRegistry()
registry.register_tool(CalculatorTool())

agent = BasicAgent(
    name="CalculatorDemo",
    config=config,
    tool_registry=registry,
    sys_prompt="Use the calculate tool for arithmetic. Respond in English.",
)
result = agent.run("Calculate (12 + 8) * 3 using the tool.")
if result.finished:
    print(result.output["text"])
else:
    print("Run incomplete:", result.info)
```

也可以绕过模型直接调用已注册工具；`ToolResult` 的 `status` 为 `success` 或 `error`，结果在 `data`，错误在 `error`：

```python
result = registry.execute_tool("calculate", {"input": "2 + 3 * 4"})
print(result.data if result.status == "success" else result.error)
```

## 使用 Shell 工具

`ShellTool` 支持 macOS、Linux，在本机以当前用户权限执行命令。每次启动独立的非交互 shell：文件保留，`cd` 和 `export` 不跨调用保留。工作目录不是沙箱边界，工具不限制网络或目录外访问；实验请使用独立目录和合成数据。

```python
from pathlib import Path
from bpcagent import ShellTool, ShellToolConfig, ToolRegistry

work_dir = Path("/tmp/bpc-shell-demo")
work_dir.mkdir(parents=True, exist_ok=True)
registry = ToolRegistry()
registry.register_tool(ShellTool(ShellToolConfig(work_dir=work_dir)))
result = registry.execute_tool("run_shell", {
    "command": "printf 'hello\\n' > result.txt\ncat result.txt",
})
print(result.data if result.status == "success" else result.error)
```

应用通过 `ShellToolConfig` 配置 `work_dir`、`shell_path`（默认 `/bin/bash`）、`timeout_seconds`（30 秒）、`termination_grace_seconds`（2 秒）、`max_output_bytes`（每条输出流 32768 字节）和 `env_overrides`。子进程不自动继承模型密钥等主进程环境变量；如需虚拟环境的 Python，请配置 `PATH` 或在命令中使用解释器绝对路径。

结果包含 `exit_code`、`stdout`、`stderr`、`timed_out`、`duration_ms`、`stdout_truncated`、`stderr_truncated` 和 `cwd`。`ToolResult.status="success"` 表示成功取得执行结果，还需检查退出码和超时标记。非零退出码和超时不会触发自动重试，已写入的文件不会回滚。超时清理包括终止宽限期，因此实际耗时可以略长于配置时限。

只保留 stdout、stderr 的前 N 字节，超限部分继续读取但不保留；UTF-8 非法或被截断的字节使用替换字符。需要大量结果时直接写入文件，再按需读取。不要运行交互程序、后台服务或主动脱离进程组的任务。

独立实验复用 `BasicAgent`，不调用 PatientStateAgent：

```bash
# 模型响应预设，但 shell、脚本和文件读写实际执行；不访问模型服务
python examples/shell_experiment.py

# 使用 BPCAgent/.env 中的模型配置，真实验证多轮文件处理
python examples/shell_experiment.py --live
```

每次创建唯一实验目录（默认系统临时目录，可用 `--work-dir` 指定父目录），保留合成输入、输出和 `agent_result.json`。任务统计空字段，零值不算缺失；示例独立校验输出文件和原输入未变，模型仅声称完成不会通过。Agent 调用轨迹保存在本次实验目录的 `logs/` 中。

运行离线测试：`python -B -m unittest discover -s tests -v`。

## 使用统一患者阶段（不运行靶点／药物阶段）

只提取患者数据，也使用相同的 PatientStateAgent 和 ReviewAgent，不再构造独立提取版。
下面为离线模型示例，提取器仍真实读取数据：

```python
from pathlib import Path
from bpcagent.workflow import AgentWorkflow

root = Path.cwd()
workflow = AgentWorkflow(
    data_dir=root.parent / "bpc-patient-state" / "dataset",
    workspace_root=root / "workspace",
    mode="demo",
    plan={"version": "patient-only", "stages": [{"agent": "PatientStateAgent"}]},
)
result = workflow.run(patient_id="GENIE-DFCI-051542", cutoff_day=220)
print(result["status"], result["task_dir"])
for turn in result["turns"]:
    if turn["agent"] == "PatientStateAgent":
        artifacts = turn["artifacts"]  # 路径相对 result["task_dir"]
        print(artifacts)
```

正常运行包含 PatientState 和 Review 两个 turn；被拒绝时重跑患者阶段。
状态和证据保存到对应的 PatientState 阶段目录，查询/写入失败不计为完成。
快照有引用时，至少需要一次成功的同 cutoff 证据查询；这不代表已单独导出全部证据。
除这些提取要求外，还必须调用 state_check 并通过 submit_output 提交结构化结果。
真实模型使用同一个入口：通过 WorkflowConfiguration 加载 config/ 中的模型配置，
并将 pipeline 配置为只包含 PatientStateAgent；不能绕过 Review。

### 提取并统一单位

`UnitPatientStateAgent` 是保留的高级单位处理入口，直接继承 `BasicAgent`。
它需要在 WSL、Linux 或 macOS 下运行；单独读取 `.env` 模型配置，示例会调用真实模型：

```python
from pathlib import Path
from dotenv import load_dotenv
from bpcagent import Config, UnitPatientStateAgent
from bpcagent.log_paths import create_run_directory

load_dotenv(".env", override=False)
root = Path.cwd()

agent = UnitPatientStateAgent(
    patient_id="GENIE-DFCI-051542",
    cutoff_day=220,
    data_dir=root.parent / "bpc-patient-state" / "dataset",
    rules_path=None,  # 使用内置默认字段规则
    work_dir=create_run_directory(root / "workspace"),
    config=Config(max_tool_iterations=10),
)
result = agent.run()
print(result.output["text"] if result.finished else result.info)
print("Full log:", agent.log_path)
```

两个阶段各自使用 `max_tool_iterations` 轮次上限。标准化结果保存为 `tumor_markers_standardized.json`（schema `2.2`），保留原始字段，新增 `unit_assessment` 和 `normalization`。智能体可以根据当前患者的可见证据直接修正单位或补充总／游离睾酮类型，标记为 `inferred_correction` 并应用到标准化结果，无需外部确认。证据仍不足时才保留待核验。可选的 `unit_corrections_path` 用于提供优先采用的已确认资料；模型推断不会冒充 `confirmed_correction`。宿主将改动位置、前后值、理由和证据整理到 `info.unit_changes`，并追加到最终文字报告。每条评估还需提供 `evidence_observations` 原始检验快照，宿主核对记录 ID、项目、日期、数值、单位、参考范围与证据 ID 的对应关系，拒绝将睾酮记录当作 PSA 历史依据；通过校验的来源快照也会附在报告中。

输入目录新增 `unit_inputs.json`，保存固定规则和仅适用于当前患者、截止日、记录的纠错资料。模型必须保存 `standardize_tm.py`，在独立 shell 调用中执行提示词给出的完整命令；宿主程序检查执行成功、脚本内容未变及换算结果。验收失败会反馈模型修复。`result.info["unit_summary"]` 是宿主验收后的分类计数，存在 `unresolved` 不等于执行失败。日志保存在 `work_dir/logs/UnitPatientStateAgent_*.json`，包含规则版本、验收记录和最终状态。协议、纠错资料示例与限制见 [Task 使用文档](doc/tasks.md)。

如需隔离执行，额外传入 `sandbox_config=SandboxConfig(image="bpcagent-sandbox:0.1")`（从 `bpcagent` 导入 `SandboxConfig`）。需先启动本机 Docker 并构建镜像。此模式中 `work_dir` 参数是整个任务目录：提取文件位于 `input/`，脚本位于 `scripts/`，标准化结果位于 `output/`，临时文件位于 `tmp/`。输入对容器只读，每次 shell 调用使用独立的无网络容器。详见 [sandbox 配置、构建和测试说明](doc/sandbox.md)。未传入配置时保留本地执行方式；sandbox 失败不会退回本地执行。

### 单独调用患者工具

不需要智能体时，复用上例的 `root`、`create_run_directory`，用工厂创建工具并直接执行；此过程不调用模型：

```python
from bpcagent import ToolRegistry
from bpcagent.tools.bpc_patient import create_patient_tools

registry = ToolRegistry()
for tool in create_patient_tools(
    patient_id="GENIE-DFCI-051542",
    max_cutoff_day=220,
    data_dir=root.parent / "bpc-patient-state" / "dataset",
    rules_path=None,
    work_dir=create_run_directory(root / "workspace"),
):
    registry.register_tool(tool)

state = registry.execute_tool("get_patient_state", {"cutoff_day": 220})
if state.status != "success":
    raise RuntimeError(state.error)
refs = list(state.data["evidence_refs"])
if refs:
    evidence = registry.execute_tool("get_patient_evidence", {
        "cutoff_day": 220, "evidence_refs": refs[:1],
    })
    print("Evidence extraction:", evidence.status)
else:
    print("No evidence references available.")
```

也可把此注册表传给 `BasicAgent`。查询日期不能超过 `max_cutoff_day`；证据引用必须来自同一截止日期的状态结果。

## 创建新工具

在 `src/tools/uppercase.py` 定义参数模型和工具类。参数由框架校验，`run()` 返回可 JSON 序列化的数据，失败时抛出异常，由注册表包装为错误结果。

```python
from pydantic import Field
from .tool import Tool, ToolArgs


class UppercaseArgs(ToolArgs):
    text: str = Field(min_length=1, description="Text to convert to uppercase.")


class UppercaseTool(Tool[UppercaseArgs]):
    args_schema = UppercaseArgs

    def __init__(self):
        super().__init__(
            name="uppercase", description="Convert text to uppercase.",
        )

    def run(self, parameters: UppercaseArgs) -> dict:
        return {"text": parameters.text.upper()}
```

保存后即可导入注册：

```python
from bpcagent import ToolRegistry
from bpcagent.tools.uppercase import UppercaseTool

registry = ToolRegistry()
registry.register_tool(UppercaseTool())
result = registry.execute_tool("uppercase", {"text": "hello"})
print(result.data)
```

已有同步函数也可通过 `registry.register_function(name, description, func, args_schema=Args)` 注册，函数的命名参数应与参数模型一致。需要提取器、数据库连接或保存目录时，通过工具构造函数注入；可参考 [bpc_patient.py](src/tools/bpc_patient.py) 用工厂统一创建依赖。

## 创建新智能体

通用单 Agent 可在 `src/agents/` 新建文件，继承 `BasicAgent`，在构造函数中准备依赖、注册工具并固定提示词，通过 `run()` 接收任务。多 Agent 角色参考 [WorkflowAgent](src/workflow/agents.py)。下面的单 Agent 示例保存为 `src/agents/calculation.py`：

```python
from .agents import BasicAgent
from ..tools import CalculatorTool, ToolRegistry


class CalculationAgent(BasicAgent):
    def __init__(self, *, config=None, llm=None):
        registry = ToolRegistry()
        registry.register_tool(CalculatorTool())
        super().__init__(
            name="CalculationAgent",
            config=config, llm=llm, tool_registry=registry,
            sys_prompt="Use the calculate tool for arithmetic. Respond in English.",
        )

    def run(self, input="Calculate 2 + 3 using the tool.", **kwargs):
        return super().run(input, **kwargs)
```

在应用中加载环境配置后调用：

```python
from dotenv import load_dotenv
from bpcagent import Config
from bpcagent.agents.calculation import CalculationAgent

load_dotenv(".env", override=False)
agent = CalculationAgent(config=Config(max_tool_iterations=6))
result = agent.run("Calculate 15 * 8 using the tool.")
print(result.output["text"] if result.finished else result.info)
```

普通智能体可直接复用父类工具循环。Workflow 的 PatientStateAgent 通过 `submit_output` 的宿主校验要求提取、证据和检查先完成；若实现其他任务的特殊停止条件，也应在代码中校验，不能仅依靠提示词。

物理目录是 `src/agents`、`src/tools`，导入路径仍为 `bpcagent.agents`、`bpcagent.tools`。如需支持 `from bpcagent import NewAgent`，在对应子包的 `__init__.py` 和 `src/__init__.py` 中添加导出；工具同理。
