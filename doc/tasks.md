# Task 封装

`src/tasks/base.py` 同时定义抽象 `Task` 和 `TaskResult`。基类只规定 `run(...) -> TaskResult`，不规定配置、工作目录、Agent 类型或执行步骤；具体子类定义自己的查询参数。Task 不继承 Agent。

`PatientStateTask` 封装“提取 state/evidence → Docker sandbox 单位处理 → 收集产物”的现有流程。每次调用创建新的 `UnitPatientStateAgent`，该类直接继承 BasicAgent；内部提取阶段调用同一个 BPCStateExtractor 工具，不依赖已删除的独立 PatientStateAgent 类。普通患者提取请用主 workflow 的单阶段计划。

## 初始化一次，每次只传查询变量

以下示例在 `BPCAgent` 仓库目录运行。模型配置由应用加载；Task 不查找 `.env`，也不从包安装位置推测数据路径。

```python
from pathlib import Path
from dotenv import load_dotenv
from bpcagent import Config, PatientStateTask, PatientStateTaskConfig, SandboxConfig

load_dotenv(".env", override=False)
root = Path.cwd()
task = PatientStateTask(PatientStateTaskConfig(
    data_dir=root.parent / "bpc-patient-state" / "dataset",  # 本机原有数据；按实际位置修改
    # rules_path 省略：使用内置 BPCStateExtractor 的规则资源
    workspace_root=root / "workspace",
    agent_config=Config(max_tool_iterations=10),
    sandbox_config=SandboxConfig(image="bpcagent-sandbox:0.1"),
))

result = task.run(patient_id="GENIE-DFCI-051542", cutoff_day=220)
if result.finished:
    print(result.agent_result.output["text"])
    print(result.artifacts["normalized"])
else:
    print(result.info)
print(result.task_dir)
print(result.log_path)
```

Docker 和镜像需事先准备，详见 [sandbox.md](sandbox.md)。Task 始终传入有效的 `SandboxConfig`，不提供自动回退到宿主机执行的选项。

`PatientStateTaskConfig` 字段：

| 字段 | 约定 |
| --- | --- |
| `data_dir` | 必填，已存在的数据集目录 |
| `workspace_root` | 必填，任务总目录；可尚未创建 |
| `rules_path` | 可选；提供时必须存在，省略时沿用提取器默认规则 |
| `unit_corrections_path` | 可选，调用方信任的单位纠错 JSON；不配置也可直接应用有依据的模型推断修正 |
| `agent_config` | 复用 `Config`，默认每阶段最多 10 轮 |
| `sandbox_config` | 复用 `SandboxConfig`，默认镜像 `bpcagent-sandbox:0.1` |
| `timezone` | 目录时间的 IANA 时区，默认 `Asia/Shanghai` |

相对路径在配置对象创建时转换成绝对路径，切换当前目录不会改变数据或工作区位置。Task 初始化时通过 `Config.resolve()` 保存模型配置快照，后续运行不重新采用变化的环境配置。构造 Task 不请求模型、不创建任务目录。

`run()` 仅接受关键字参数 `patient_id`、`cutoff_day` 和可选的 `cancer_id`。`cutoff_day` 是现有接口的相对天数，允许负数；不接受布尔值、浮点数或日期字符串。患者 ID 原样用于查询，不自动清洗或替换；空值、路径分隔符、控制字符等不安全目录字符会被拒绝。

## 目录和结果

```text
workspace/YYYYMMDD_HHMMSS_patient-id_cutoff-day/
  input/
  scripts/
  output/
  tmp/
```

同秒、同患者、同截止日的重复运行会抛出 `FileExistsError`，不复用或覆盖已有目录。等待下一秒后可再提交同一查询。每次运行使用独立 Agent，因此患者绑定、对话和工具状态不会跨任务沿用。

`TaskResult` 字段：

| 字段 | 含义 |
| --- | --- |
| `finished` | 只有 Agent 明确成功且必要产物存在时才为 `True` |
| `task_dir` | 本次任务的绝对路径 |
| `artifacts` | 已存在的文件路径，键为 `state`、`evidence`、`unit_inputs`、`normalized`、`script` |
| `log_path` | 已存在的 Agent 日志绝对路径；不可用时为 `None` |
| `agent_result` | 原始 `AgentResult`，保留报告、轨迹和 Agent 完成状态 |
| `info` | Agent 信息的副本及 Task 层诊断 |

失败时仍收集已生成的文件，但文件存在不代表任务完成。提取文件有多个版本时按数值序号选择最新版本；只收集当前截止日的文件，跳过符号链接和工作区外路径。不从模型的文字报告中解析文件位置，也不重新执行生成的脚本。

如果 Agent 已成功而必要文件缺失，Task 返回 `finished=False`、`info.stop_reason=incomplete_artifacts`，保留原始 AgentResult 不变。配置、目录重名和程序异常继续抛出，不伪装成正常结果。打印和退出码由调用方处理。

任务不另存结果 JSON，不生成 `run_result.json`。Agent 日志保存在本次任务的 `task_dir/logs/UnitPatientStateAgent_*.json`，与 input/output/scripts 等产物同目录收纳；Task 返回绝对日志路径。

第一版不包含任务队列、批量调度、评分/奖励、断点恢复或通用 Agent 工厂。未来替换/合并 Agent 时，优先修改 `PatientStateTask._create_agent()`，保持查询接口和结果结构稳定。

## 单位核查与纠错（输出 schema 2.2）

`UnitPatientStateAgent` 内部先运行私有提取阶段，再使用 shell 读取文件、分析异常、编写并执行脚本；没有第二个 PatientStateAgent 类。固定规则及宿主验收使用的换算函数位于 `src/agents/unit_rules.py`。模型脚本不导入宿主包，所需规则从只读 `input/unit_inputs.json` 获取。

每次只处理 state 中当前患者、截止日内可见的 `observations.tumor_markers`；不向沙箱挂载完整 dataset。提取器已排除的日期未知、项目缺失等记录不在此次处理范围，结果不是完整原始数据 audit。state 顶层 `evidence_refs` 提供本次可见来源，单独保存的 evidence 文件可能只是其子集；两者源于相同数据，不构成独立佐证。时间边界遵循现有提取器的 `available_day`（肿瘤标志物使用采样日期代理），不声称获得真实报告发布时间。

处理政策：

- PSA 的 `ug/L`、`μg/L` 与 `ng/mL` 等值；`ng/dL` 数学上可乘 `0.01`，但本项目须先核查，以外部确认或显式模型推断作为处理依据。PSA `nmol/L` 不配置默认换算。
- 睾酮按原始项目名区分 total/free；仅写 `Testosterone` 时可以结合项目上下文、参考范围和可见历史推断类型，标记为 `inferred_correction` 后直接应用；仍有歧义时保持 `unknown`。换算后不合并总／游离轨迹。
- 睾酮 `ng/mL`、`ug/L` 乘 100，`pg/mL` 乘 0.1；只有单位核查通过且类型明确才应用。非标准单位和轨迹变化是核查线索，不自动认定错误。
- `nmol/L` 乘 `28.842` 沿用原项目的分子量 `288.42 g/mol` 约定，记录规则版本；不是对某个实验室方法的验证，也不混用 `1 / 0.0347`。
- 单位缺失或疑似误填可以根据当前患者证据直接修正，记录理由和实际使用的所有证据引用；不要求外部确认，但不能仅依据机构多数单位或自报置信度。证据不足时仍可保留候选。
- 只转换普通有限数字，零有效。比较符号、范围、百分比、NaN/Infinity 保持 unresolved；原始参考范围不修改。TMB、CNA、原始摘要及其他域保持原状，不输出伪造的标准化摘要。

`tumor_markers_standardized.json` 顶层严格包含 `schema_version`、`rules_version`、`source_state`（源文件名）、`meta`（原样）和 `tumor_markers`。旧 schema 结果不能作为新版任务输出验收。

每条记录保留所有原始字段、顺序和证据引用，额外添加：

| 对象 | 必需字段 |
| --- | --- |
| `unit_assessment` | `status`, `analyte_type`, `effective_unit`, `candidate_unit`, `correction_id`, `supporting_evidence_refs`, `conflicting_evidence_refs`, `evidence_observations`, `reason` |
| `normalization` | `status`, `value`, `unit`, `factor`, `reason` |

`unit_assessment.status`：

| 状态 | 意义与约束 |
| --- | --- |
| `accepted` | 按记录单位处理；不代表核对过原报告。可信资料确认相同单位／项目类型时填写对应 correction_id |
| `confirmed_correction` | 已授权资料确认更改或填补单位，必须匹配 correction_id |
| `inferred_correction` | 模型根据可见证据直接应用的单位修正或项目类型推断；effective_unit 和 analyte_type 为应用后的解释，correction_id、candidate_unit 为 null |
| `suspected_error` | 间接线索提示错误，可以填 candidate_unit，不应用 |
| `missing_unit` | 原单位缺失且未确认；可以保留候选 |
| `unresolved` | 项目类型未知、单位不支持或其他资料不足 |

`analyte_type` 只能为 `psa`、`total_testosterone`、`free_testosterone` 或 `unknown`。后面三种未解决状态的 effective_unit、correction_id 必须为 null。支持证据须包含自身引用以及理由中使用的其他记录（例如前后两条检验）的引用，所有引用必须在当前 state 中。模型应用推断时需解释如何权衡冲突；歧义仍无法解决时应保留未解决状态。理由为简短英文说明，描述依据、冲突、替代解释及缺少的资料，不依赖模型内部 reasoning 字段。

`normalization.status` 为 `normalized` 或 `unresolved`。已授权单位不代表数值一定能转换，例如 `<0.1` 仍是 unresolved。未解决时 value/unit/factor 为 null，reason 非空；成功时宿主按固定规则复算 value 和 factor。标准化输出由脚本生成，宿主不替模型生成结果。

## 可信纠错资料

在配置中设置 `unit_corrections_path=Path("unit_corrections.json")`。下面仅是合成资料的格式示例，不能作为真实患者的纠错依据：

```json
{
  "schema_version": "1.0",
  "version": "review-2026-10-15",
  "corrections": [
    {
      "id": "verified-fix-1",
      "patient_id": "SYNTHETIC",
      "cohort": "TEST",
      "target_cancer_id": "C1",
      "tumor_marker_id": "tm:3",
      "event_day": 100,
      "analyte_raw": "PSA",
      "value_raw": "362.03",
      "unit_raw": "nmol/L",
      "corrected_unit": "ug/L",
      "analyte_type": "psa",
      "available_day": 110,
      "source": "Verified source report or maintainer correction identifier",
      "source_version": "v1"
    }
  ]
}
```

所有示例字段均必需；value_raw、unit_raw 可以为 null，其余文本非空。原始项目名、数值、单位按原字符串精确匹配；id 必须唯一。corrected_unit 也可等于原单位，用于确认真实罕见单位或睾酮项目类型，此时输出 accepted。不能借此更改项目家族或将明确的 free 改成 total。

`event_day` 必须等于原记录日期；`available_day` 表示这条确认在患者时间轴上可用的日期（与 cutoff_day 相同起点），必须是整数、不早于测量日期且不晚于截止日。没有可信的可用时间，就不能把后来的更正回填进更早的截止日任务；当前版本不支持回溯修订模式。资料真实来源、可用日期和版本需由提供者核实，模型不能生成自己的授权。程序核验匹配关系与计算，不证明资料真实性。

Agent 构造时加载可信资料，模型运行期间不重新读取；每个 Task run 构造新 Agent，因此会读取当时配置。只将匹配患者、cohort、目标癌症、记录日期、原始字段及截止日的条目写入 unit_inputs 快照。未匹配条目不暴露给模型、不能应用；同记录匹配多个条目则报错，要求先消除资料歧义。快照记录资料版本及已筛选条目的 SHA-256；该 hash 用于复现，不是来源真实性认证。

## 执行、验收和统计

模型先检查输入，将脚本写到 `scripts/standardize_tm.py`，再单独调用：

```text
/usr/local/bin/python /workspace/scripts/standardize_tm.py
```

该执行调用不要夹带 `cd`、`&&`、heredoc 或其他命令；写脚本和读回输出分别调用 shell。宿主记录执行成功时的脚本摘要，脚本修改后须重新执行。所有脚本和临时文件留在任务工作区。输入文件、规则快照不可修改。

宿主验收结构、原值保留、证据引用范围、推断标记、已授权纠错、实际换算和脚本执行。模型推断本身不要求外部资料批准，验收不会把推断当成已证实事实；外部适用纠错资料存在时，不允许模型推断覆盖它。验收不能证明模型对临床上下文的全部语义判断正确；识别能力仍需人工核验样例和真实模型评估。合理的 unresolved 不会触发无意义的修复重试。

通过后，`TaskResult.info["unit_summary"]` 和现有 Agent 日志包含宿主计算的统计：

```json
{
  "total": 3,
  "normalized": 2,
  "unresolved": 1,
  "assessments": {
    "accepted": 1,
    "confirmed_correction": 1,
    "inferred_correction": 0,
    "suspected_error": 1,
    "missing_unit": 0,
    "unresolved": 0
  }
}
```

总数 = normalized + unresolved，也等于 assessments 各项之和；不能把两组计数相加。零条目标记录也可以完成。finished 表示执行和验收完成，不表示所有数据已解决；失败结果不携带成功统计。日志包含 `unit_rules_version`，Task 不创建额外的 run_result.json。


## 直接应用推断修正（schema 2.1）

从规则版本 `2026-10-15.2` 开始，默认允许模型直接应用有依据的推断，不需要再配置开关或提交外部纠错文件。例如将疑似误填为 nmol/L 的 PSA 解释为 ug/L：原始 unit_raw 不动，effective_unit 设为 ug/L，normalization 中实际输出 ng/mL 和对应数值，并标记 inferred_correction。不是只填写 candidate_unit。总／游离类型推断也使用该标记；不能改变原始数值、日期或把已明确的项目类型擅自换成另一个类型。

结果中的 `info.unit_changes` 由宿主在验收通过后生成，包含每条应用修正的记录 ID、日期、source_location、output_location、before、after、reason、correction_id 和支持／冲突证据。相同内容会追加到最终文字报告并保存在现有日志中，模型漏写报告条目也不会丢失审计记录。原始字段仍保留在输入与输出中；下游应读取 normalization 和 unit_assessment，不能继续把 unit_raw 当作修正后的单位。没有另外创建 run_result.json。

这是“可追溯的模型推断已应用”，不是医学事实已被独立验证；语义准确性仍需评估。若前后记录参与判断，必须同时列出它们的证据 ID。宿主能检查引用存在及范围，但不能自动证明自然语言理由中的每个因果判断。

## 检验记录与证据对应（schema 2.2）

规则版本 `2026-10-18.1` 在 `unit_inputs.json` 中增加由宿主生成的 `evidence_observations` 索引。每条包含 `tumor_marker_id`、`event_day`、`analyte_raw`、`value_raw`、`unit_raw`、`normal_range_lower_raw`、`normal_range_upper_raw` 和 `evidence_refs`；缺失原始字段以 null 表示。

模型按记录 ID 从索引选择原始快照，写入每条 `unit_assessment.evidence_observations`，必须包含本条记录及支持／冲突引用涉及的检验记录。宿主按原始 state 逐字段核对，并要求引用的检验记录属于同一项目家族；明确的总／游离睾酮不得互作同类历史。治疗等非检验上下文仍可用现有证据 ID 引用，不在此检验快照校验范围内。

例如，将 PSA 274.31 ug/L 配到睾酮记录的 ID 或证据 ID 会因字段不匹配失败；完整复制睾酮记录来支持 PSA 历史也会因项目不匹配失败。失败沿用现有验收反馈与 Shell 修复循环，不自动替模型选择新证据。

通过验收的快照同时进入 `info.unit_changes` 和宿主追加的文字报告，供直接回查。程序验证结构化检验事实的对应关系，不宣称能理解并证明任意自然语言理由或临床因果推断。旧 schema 2.1 文件保留不改写，新运行使用 2.2；外部调用参数不变。
