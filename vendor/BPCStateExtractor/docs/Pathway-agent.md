# Pathway\-agent

# 基于患者时序信息与 KEGG 通路的靶点和药物推荐研究

## 相关文档

[From Targets to Pathways \(pathway\-centric foundation model\)](https://pelggp20yu.feishu.cn/docx/Wc0vdsmOOoB0bJxjrTCcDgIMnCb)

### 论文

1. agent设计

2. agent编排

3. RSI

4. Graph ranking

# 1015

`PatientStateAgent`：调用确定性工具提取截至 `cutoff_day`（含当天）已可用的患者事实，检查来源、时间和数据完备性，输出独立的 `PatientState` JSON。未来实际记录到的观察由离线 `OfflineEvaluationExporter` 导出为另一份评估文件，不放进 PatientState，不提供给候选生成或 Review 的输入工具。

数据完备性涉及：条目/单位缺失、原始记录明确未知、未提及、未检测、未检出，以及单位不一致或可疑。`null` 是没有可提供值时的 JSON 表示；`unknown` 是知识状态，二者不是同一层的概念。缺失数值用 `null`，不把字符串 `"unknown"` 填进数值字段。用 `field_status` 按 JSON Pointer 标记状态与原因，原始文字和来源继续保留。

| 情况 | 值的表示 | 状态/解释 |
| --- | --- | --- |
| 源字段空白或未提供 | `null` | `not_recorded` |
| 源记录明确写 Unknown | 原始文字保留；需要数值时为 `null` | `unknown` |
| 源记录明确未提及/未报告 | 原始文字保留 | `not_reported` |
| 明确未检测 | 原始文字保留 | `not_tested`，不能当检测阴性 |
| 明确未检出 | 原始文字保留 | `not_detected`，不能和未检测混淆 |
| 明确不适用/未收集 | 原始文字保留 | `not_applicable` / `not_collected` |
| 截止日无法确定是否停药 | `end_day: null` | `unknown`；不读取未来停药值解释过去 |
| 实验药物时长遮蔽 | `end_day: null` | `masked` |
| 数值0 | 数值 `0` | 保留，不当缺失 |

白名单排除使用 `excluded_by_policy`，尚未做名称归一化使用 `not_normalized`；不能把它们误报成原始数据缺失。空数组只表示当前没有纳入的记录，不证明未检查或阴性。

离线评估使用明确的 `(cutoff_day, end_day]` 窗口，只导出该窗口内、截至 `end_day` 已可用的真实影像、医生评估和肿瘤标志物。事件发生在截止日前、结果在截止日后才可用的记录放入单独的 `late_available_observations`，不作为未来新发生的事件。无记录标为 `none_recorded_in_window`，不能自动当作稳定或未进展；不从缺少记录推断完整随访或删失结论。当前不生成观察预测、疗效真值或最佳治疗标签。

两份数据分别保存：Agent可读的 `patient_state.json` 和仅离线评估器可读的 `offline_evaluation/followup.json`。后者保存输入快照哈希及独立证据表；Agent工具不暴露评估导出能力，部署时也不向Agent开放该输出目录。`state_check` 应验证缺失状态与证据引用，并检查输入中没有混入评估字段。

单位统一：

> 建议这个项目统一为：**PSA用 ****`ng/mL`****，睾酮用 ****`ng/dL`**。这与数据中多数记录一致，也便于对照常用实验室报告。[PSA报告单位](https://www.mayoclinic.org/tests-procedures/psa-test/about/pac-20384731)、[睾酮换算表](https://www.mayocliniclabs.com/order-tests/si-unit-conversion.html)。
>
> 但要先判断原单位是否可信。**单位不同可以换算；单位标错，直接换算反而会放大错误。**
>
> 对于确认正确的单位，转换规则可以固定为：
>
>

>
>
> 睾酮的摩尔浓度换算依据为实验室公布的系数；项目应固定系数版本。[Mayo换算依据](https://www.mayocliniclabs.com/order-tests/si-unit-conversion.html)。总睾酮和游离睾酮也需要区分，不能仅因单位一致就混成一条轨迹。
>
> 实际数据里，我建议这样处理：
>
>

> 有一个很明显的待核验例子：患者 `GENIE-UHN-011991` 的PSA记录为：
>
> ```Plain Text
> Day 2270：274.31 ug/L
> Day 2294：362.03 nmol/L
> Day 2302：364.10 ug/L
> ```
>
> 数值连续、参考范围也相同，**提示中间记录的单位可能误填**。但这只是推断，不能直接把 `nmol/L` 改成 `ug/L`；应核对原始检验报告或数据维护方的更正说明。
>
> 落到工具里，建议保留两层字段：
>
> ```Plain Text
> value_raw、unit_raw                  原始记录，始终保留
> value_standardized、unit_standardized 标准化结果
> unit_status、conversion_rule         是否可信、用了什么规则
> ```
>
> 确认正确的记录正常转换；疑似错误或缺单位的记录保留原值，`value_standardized=null`，不参与统一量纲的趋势计算。只有核验确认后，才记录“更正后的单位”和依据。参考范围也应随可信单位一起转换。
>
> 另外，历史快照中的自动判断只能使用当时或更早的信息；后续数值可以帮助离线审计，但不能用于补齐过去快照，否则会引入未来信息泄漏。
>
>

tool：BPC Patient State（用于从dataset提取结构化json数据）,state\_check（用于检验json数据完备性），bash（用于检查需要推断、本身有误的数据）

目标是已核实且成功标准化的记录采用统一单位；无法确定的值保留类型正确的 `null` 和原因，不由Agent猜测填充。当前提取工具仍保留原单位，不声称所有记录已经完成单位标准化。可疑单位由Agent提出复核项，确定性换算或更正必须有已核实的规则和依据。

`TargetSelectionAgent`: 从`PatientStateAgent`获取数据，根据癌症从kegg/pinder等数据库构建pathway graph（构建好可以存储到db），将患者的用药信息、诊断信息也转换之后标注在graph中，结合用药指南，通过graph ranking确定靶点（top M）

database:用药指南（医生用的针对病症的用药顺序说明）,kegg,pinder,biogrid

tool：bash，serach\(用于搜索文献和数据库，补全kegg信息\),**graph ranking**（目前想的是用一个类似rubirc model，可以通过RSI优化选择打分标准）

`DrugSelectionAgent`：确定靶点之后，用fda药物和用药指南的药物做docking,ept\(meet\)打分，选择候选药物，整合输出

> 现在版本只输出建议靶点和药物，不预测observation，也不预测用药时长
>
> 患者目前没停药怎么办?是终止当前用药还是考量新候选药物和当前用药的适用性和冲突性
>
> 预测出来的药如何评判好坏?旧药/新药
>
>

database：用药指南，fda药物

tool：bash,dock,ept/meet,search（查阅文献，补充药物相关信息），以及一个药物整体指标的计算tool

`ReviewAgent`：在每一轮都审视安全性和是否有逻辑错误、证据缺失。和医学任务workflow应该使用不同的模型

`Meta-Agent`：负责RSI



> 怎么利用到其他信息
>
>

# 1009

> **当前方案。** 保留基于患者时序数据、KEGG 通路和文献证据的靶点—药物推荐研究。`PathwayAgent` 的职责并入 `TargetSelectionAgent`；原 `SafetyReviewAgent` 更名为 `ReviewAgent`，负责不限于安全问题的专业医学复核。**三个任务 Agent 构成主 Workflow，JEV、ReviewAgent 和 Meta\-Agent 位于旁侧的评估/进化链**，不是主流程的三个串行任务节点。首版使用冻结的闭源 LLM，不训练参数；JEV 和 ReviewAgent 保持固定，不参与自进化。本节只记录当前架构，不展开后续可能的 Meta\-level RSI。

## 核心结构

系统包含 **5 个 Agent \+ 1 个 JEV 模块**：`PatientStateAgent`、`TargetSelectionAgent`、`DrugSelectionAgent`、`ReviewAgent`、`Meta-Agent`，其中前三者直接完成患者任务；`JEV` 是可重复调用的固定快速评估模块，不算独立 Agent。

![Image](https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/authcode/?code=YmZhMTgyYTA3MTJlMDhkNGJlOWJiYmFmODExMzJlY2RfZDMyMDVkMTVlOGRjOTMwOTk2ZjRkNmQ4ODIzOWQ0NGNfSUQ6NzY5NjM5ODA4NTgxMTA2NDA2MF8xNzkxOTYwNDM0OjE3OTIwNDY4MzRfVjM)



```Plaintext
flowchart LR
    subgraph W["医学任务 Workflow"]
        direction TB
        I((患者数据 / NGS / KEGG))
        P[PatientStateAgent]
        T[TargetSelectionAgent]
        D[DrugSelectionAgent]
        O((Top-K 候选与证据))
        I --> P --> T --> D --> O
    end

    subgraph F["阶段评估与系统进化"]
        direction TB
        J((JEV 快速评估))
        R[ReviewAgent]
        M[Meta-Agent]
        J --> R --> M
    end

    W -.->|各阶段输出、轨迹送 JEV| F
    W -->|最终结果、全程轨迹送 Meta| F
```

**读图：** 左右两列分别展示纵向的医学任务 Workflow 与评估/进化链；跨列连线汇总表示每个阶段的独立反馈（而非仅在结束时评估）。只有 Agent 使用方框，JEV 和输入/输出使用圆形。三个任务 Agent 的**每次阶段输出、原始证据与执行轨迹**均进入旁侧评估链；同一个 JEV 在各阶段重复调用，将**通用快评 \+ 医学初评 \+ 分项置信度**交给 ReviewAgent。ReviewAgent 同时获得原始阶段输出，进行有重点的专业复核，再把结果交给 Meta\-Agent。**整条 Workflow 完成后，完整输出和跨阶段轨迹也直接汇总给 Meta\-Agent**，让它分析全局协作问题。图中为清晰起见省略了工具调用、ReviewAgent 退回补证和阻断路径。

## JEV → ReviewAgent：固定的双层评估

- **JEV：快速、通用的初步评估。** 每个阶段检查结果是否满足预期、Schema/时间信息是否完整、工具调用是否冗余、流程是否可以简化、证据是否存在明显缺口；对于靶点/药物声明进行轻量医学初评，并按声明给出 `high / medium / low / unknown` 置信度、风险等级和待核实事项。置信度不是客观疗效概率，也不是医学正确性的保证。

- **ReviewAgent：以 JEV 为线索的专业评估。** 同时读取**原始阶段输出、患者状态、已有文献/工具证据与 JEV 结果**；优先深入核查低置信度、未知或高风险内容，必要时检索文献、知识库并要求补证。对核心靶点机制、药物—靶点匹配、时间泄漏、重要反证及安全约束保留必检项，不允许高置信度自动通过。

- **阶段闭环。** 若发现关键错误，ReviewAgent 可以要求当前任务 Agent 补证、返工或转人工；这属于**当前病例的任务内纠错**，不等于修改 Agent 的 Harness。通用低效或重复出现的专业问题则形成可追溯的改进记录，交由 Meta\-Agent 在之后评估。

两者的 Prompt/审核规则与强制检查项在实验中固定，**不可由 Meta\-Agent 修改**，以避免评估标准随进化漂移。

## Meta\-Agent：基于阶段反馈与全局结果改进系统

Meta\-Agent 是一条与业务 Workflow 并列的**离线进化链**，不替代任何医学任务 Agent，也不在当前患者任务中未经验证地热修改系统。它接收：

1. **每阶段原始信息：** 输入/输出、证据引用、工具轨迹、当前 Agent 配置与成本；

2. **JEV 快评：** 通用执行问题、医学初评、逐项置信度、可能的效率或接口改进机会；

3. **ReviewAgent 专业结论：** 证据核查、争议点、错误与退回原因、补证记录；

4. **Workflow 结束后的整体信息：** 最终候选及审核结果、跨阶段返工/通信/冗余、总体任务质量与成本。

据此区分两类优化：

|改进层级|可修改对象|典型例子|
|---|---|---|
|**Agent\-level**|`PatientStateAgent` / `TargetSelectionAgent` / `DrugSelectionAgent` 的 Prompt、Skill、工具使用策略、输出接口和错误恢复|靶点功能方向证据经常缺失时，改善 TargetSelectionAgent 的文献核查 Skill|
|**Workflow\-level**|三个任务 Agent 间的数据传递、调用条件、补证回退、工具路由和流程顺序|下游重复索要相同证据时，将相关字段提前纳入上游输出|

执行方式为：**汇总问题 → 归因与选择改进对象 → 提出修改 → 隔离测试 → 独立医学与端到端回归验证 → 接受/回滚并归档**。不能把“患者本身缺乏证据”误判为流程缺陷；也不能通过修改 JEV、ReviewAgent 或独立评分规则来提高分数。

## 初步验证重点

- **评估效率：** JEV 引导后 ReviewAgent 的文献检索开销、专业错误检出率、漏检率，尤其是高置信度但实际有问题的声明。

- **任务质量：** PatientState 时间准确性、靶点机制及药物证据合理性、反证遗漏、专家认可的 Top\-K 候选比例。

- **进化收益：** Agent\-level 与 Workflow\-level 改进的成功率、整体成本、返工次数、跨患者泛化和性能回退；固定底层模型、评估标准和预算。

至少比较：固定 Workflow、只有 JEV \+ ReviewAgent（无进化）、仅 Agent\-level 改进，以及 **Agent\-level \+ Workflow\-level 联合改进**。这首先属于可控的 **Self\-Evolving MAS**；是否达到严格 RSI，需另行验证进化后的优化器本身是否更擅长产生下一轮改进，本次方案暂不将其作为已实现功能。

每一次改进内容记录，metaagent根据历史改进内容迭代\-\>RSI

# 0917

基础方案：v0\.8 · 日期：2026\-09\-17（总体流程第 2 节已按 10\.9 更新同步）
主数据：GENIE BPC Prostate · 主要参考：TxAgent、OriGene

## 1\. 研究目标

从患者画像、历史治疗、随访观察和 NGS 结果出发，在 KEGG 通路图上标注患者证据，筛选候选靶点，再检索和评估匹配药物，输出有依据的下一步治疗研究建议。

**范围：** 先做前列腺癌、单次决策。K 是备选方案数量，H=1 表示每个备选只考虑下一步。真实随访到达后更新输入，再次运行。

**核心问题：** 相比只阅读患者摘要和检索文献，加入通路关系与治疗时间信息，能否提高靶点选择的合理性、证据完整性和解释质量？

第一阶段输出可供专家审核的候选假设。BPC 的历史处方用于研究真实治疗过程，不等同于患者的最佳药物标签；后续进展也不能直接作为某个靶点无效的真值。

## 2\. 总体流程（按 10\.9 当前架构同步）

系统共五个 Agent：三个任务 Agent（`PatientStateAgent`、`TargetSelectionAgent`〔含原 PathwayAgent〕、`DrugSelectionAgent`），一个固定专业复核 Agent（`ReviewAgent`），一个离线进化 Agent（`Meta-Agent`）。固定的 `JEV` 在**每个任务阶段输出后**进行快速通用评估与医学初评；ReviewAgent 随后结合原始输出和 JEV 置信度进行专业复核。评估链与医学任务 Workflow **并列**；每阶段信息和最终完整执行结果都进入 Meta\-Agent，支持 Agent 与 Workflow 两级改进。所有任务阶段共享同一份 PatientState，数据解析、图计算、文献检索和对接评分由工具执行。

```Plaintext
flowchart LR
    subgraph W["医学任务 Workflow"]
        direction TB
        I((BPC / NGS / KEGG))
        P[PatientStateAgent]
        T[TargetSelectionAgent]
        D[DrugSelectionAgent]
        O((Top-K 候选与证据))
        I --> P --> T --> D --> O
    end

    subgraph F["阶段评估与系统进化"]
        direction TB
        J((JEV 快速评估))
        R[ReviewAgent]
        M[Meta-Agent]
        J --> R --> M
    end

    W -.->|阶段输出与证据送 JEV| F
    W -->|全流程结果与轨迹送 Meta| F
```

图为左右两列、列内自上而下的纵向流程；跨列连线表示阶段输出与全程结果分别进入评估/进化系统，具体按阶段重复调用。图中只用方框表示 Agent。`JEV` 是同一模块按阶段多次调用；**ReviewAgent 同时读取阶段原始输出和 JEV 结果**，低置信度/高风险项优先复核，关键医学与安全检查仍然必做。关键错误可退回任务 Agent 补证（图中省略）。Meta\-Agent 读取各阶段的原始轨迹、两级评估和最终 Workflow 结果，提出改进候选；经独立验证后才更新**下一次**任务使用的 Agent/Workflow 版本。JEV、ReviewAgent 和独立评价标准始终固定。

|模块名称|类型|功能说明|主要输出|
|---|---|---|---|
|`PatientStateAgent`|可进化任务 Agent，调用确定性 ETL 工具|多表解析、时间截断和质量检查；歧义保留待核查|PatientState|
|`TargetSelectionAgent`（含 Pathway）|可进化任务 Agent|KEGG 通路映射、患者图、调用 GraphRankingTool 初筛 Top\-M、靶点功能和证据核查|患者通路图、靶点候选及证据|
|`GraphRankingTool`|独立 Tool，可封装图模型|依据患者证据、机制关系与治疗历史筛选靶点及干预方向|Top\-M 和分项得分|
|`DrugSelectionAgent`|可进化任务 Agent|匹配药物并检查既往治疗，按需调用 Docking/EPT|最多 K 个靶点—药物候选|
|`JEV`|**固定的快速评估模块**|每阶段通用检查、医学初评、逐项置信度、证据缺口及流程低效线索|初评、置信度、改进线索|
|`ReviewAgent`|**固定专业复核 Agent**|结合原始输出和 JEV 判断，优先核查低置信度/高风险结论，查文献与知识库，保留安全与时序必检|通过、补证、拒绝或人工审核及文献证据|
|`Meta-Agent`|进化控制 Agent|汇总每阶段 Agent 输出/轨迹、JEV、Review 与最终完整 Workflow，优化任务 Agent 和 Workflow|修改候选、验证结果、版本记录|

首版不训练新的数据抽取模型；所有 Agent 可以使用冻结的闭源 LLM，但评估与进化权限分开。ReviewAgent 可在**任务内**要求修正候选，Meta\-Agent 的 Harness/Workflow 修改则只在**离线测试通过后**用于后续任务。JEV 的置信度用于复核优先级，不直接作为医学真实性或临床疗效概率。

### 2\.1 通路如何参与选择

初版冻结一组前列腺癌相关 KEGG 通路及其版本。随访主要更新节点证据，不必每次重新构建整张图。

|信息|在图上如何表达|
|---|---|
|NGS|对应基因的具体变异、CNA、检测覆盖、样本和报告时间|
|Treatment|药物对应的靶点或作用机制、暴露区间、是否已结束|
|Observation|影像、医生评估和实验室变化；作为患者整体或相关治疗事件的证据|
|外部知识|节点功能、关系方向、药物作用和支持文献|

NGS 用于个体化分子注释；临床进展不能自动改写为某个节点激活，也不能凭未报告异常就判断基因为野生型。功能注释工具核查具体变异后，模型才讨论异常节点自身、下游或依赖节点是否适合干预。

KEGG 提供主要机制关系；BioGRID 按实验类型补充互作；PINDER 仅作为可选结构证据资源，不把物理接触自动改成激活或抑制。跨疾病共有节点可以降权，但不直接删除。

### 2\.2 药物选择如何收敛

已有明确药物—靶点证据时，优先评估疾病适用性、治疗史和限制条件。缺乏直接证据时，在 FDA 已批准小分子池内探索再定位，对结构条件合格的候选执行 docking 和 EPT。

这里的“新”指新的靶点—药物用途假设，首版不做新分子生成。曾使用过的药物需结合反应和停药背景核查，不一律拉黑。Docking/EPT 提供结合层面的计算证据，不能单独证明作用方向、患者疗效或安全性。

## 3\. BPC 数据与样本构建

> 数据示例

> 下面选取公开数据中的去标识化患者 `GENIE-DFCI-051542`。这位患者的临床、治疗、影像、PSA、病理和基因组资料都比较完整，很适合理解这些表如何串联。

> 所有时间统一表示为“相对前列腺癌诊断日”，诊断日记为 Day 0。

## 病例概览

|项目|数据|
|---|---|
|患者 ID|`GENIE-DFCI-051542`|
|医疗机构|DFCI|
|出生年份|1952|
|诊断年龄|63岁|
|癌症数|1个|
|病理类型|前列腺腺癌|
|分级|Grade III|
|Gleason|9|
|初诊分期|Stage IV|
|初诊远处转移|有，骨转移|
|总生存状态|死亡|
|诊断至死亡|291天|
|随访终止年龄|64岁|
|死亡信息来源|NDI（美国国家死亡指数）|

> 这部分主要来自：

- patient\_level\_dataset\.csv

- cancer\_level\_dataset\_index\.csv

## 病程时间线

|时间|事件|数据含义|
|---|---|---|
|Day \-8|PSA 300\.90 ng/mL|诊断前已显著升高|
|Day \-7|全身骨扫描|有肿瘤证据，累及骨及下肢长骨|
|Day 0|前列腺活检|腺癌，Gleason 9；作为诊断基准日|
|Day 19|开始 Flutamide|第一项系统治疗|
|Day 26|开始 Leuprolide|雄激素剥夺治疗|
|Day 55|PSA降至1\.01|骨扫描评价为改善/响应|
|Day 62|短期记录 Enzalutamide|表中起止日期均为Day 62，可能代表很短的暴露或快速停药|
|Day 63|开始 Bicalutamide \+ Docetaxel|持续至约Day 209|
|Day 153|PSA降至0\.26|肿瘤科记录也判断PSA下降|
|Day 188|头部CT|提示颅骨、椎骨部位存在肿瘤|
|Day 209|PSA 0\.11，睾酮 7 ng/dL|已达到去势水平，但同日CT显示影像进展|
|Day 209–210|CT、MRI|骨、椎体病灶进展，MRI还记录脑膜病灶|
|Day 214|转移部位细胞学取样|有浸润性肿瘤；该标本随后用于NGS|
|Day 215|单次外照射放疗|800 cGy、1次分割；结合剂量和时点，可能为姑息放疗，但数据未明确写“姑息”|
|Day 223|开始 Cabazitaxel \+ Carboplatin|持续至约Day 252|
|Day 244|CT显示进一步进展|椎体、盆腔软组织及肝脏病灶|
|Day 249|NGS报告返回|对Day 214标本进行测序|
|Day 272|PSA降至0\.08|尽管PSA很低，影像此前已经显示进展|
|Day 286|PSA回升至0\.27|肿瘤科仍记录有癌症证据|
|Day 291|死亡|诊断后约9\.6个月|

> 这个病例一个很有意思的地方是：PSA从300\.9迅速降到接近0\.1，但影像仍在Day 209以后显示明显进展。因此，单看PSA可能会误判病情，必须联合影像和医生评估。

> 数据集给出的派生PFS事件时间也是Day 209，与影像首次明确进展的时间一致。

## 治疗记录

> 来自 regimen\_cancer\_level\_dataset\.csv：

|Regimen|起始日|结束日|药物|
|---|---|---|---|
|1|19|62|Flutamide|
|2|26|287|Leuprolide|
|3|62|62|Enzalutamide|
|4|63|209|Bicalutamide \+ Docetaxel|
|5|223|252|Cabazitaxel \+ Carboplatin|

> 放疗来自 ca\_radtx\_dataset\.csv：

- Day 215开始

- 外照射

- 3D适形/常规适形技术

- 总剂量800 cGy

- 单次分割

> 注意：药物表中的“regimen”是数据整理单位，不一定完全等同于临床指南中的标准治疗线。比如 Leuprolide 与其他药同时使用，但根据该项目规则，LHRH激动剂会被单独记录成一个regimen。

## PSA和睾酮轨迹

> 来自 tm\_level\_dataset\.csv：

|相对诊断日|PSA ng/mL|Testosterone ng/dL|
|---|---|---|
|\-8|300\.90|—|
|55|1\.01|—|
|153|0\.26|—|
|209|0\.11|7|
|223|0\.11|—|
|244|0\.14|7|
|272|0\.08|7|
|286|0\.27|11|

> 这里可以看到：

- 雄激素治疗后睾酮降至很低水平。

- PSA产生非常明显的生化响应。

- 但影像在PSA很低时继续进展。

- `med_onc_note_level_dataset.csv` 中没有明确将患者标为CRPC，`md_pca_status` 为 `Not stated`，所以不能仅凭低睾酮和影像进展就把数据字段直接改写为CRPC；只能说临床表现“符合需要进一步评估的去势水平下进展”。

## 测序样本如何连接

> 在 cancer\_panel\_test\_level\_dataset\.csv 中：

|字段|值|
|---|---|
|`record_id`|`GENIE-DFCI-051542`|
|`cpt_genie_sample_id`|`GENIE-DFCI-051542-250158`|
|`ca_seq`|0|
|标本类型|Metastasis site unspecified|
|OncoTree|PRAD|
|测序平台|DFCI\-ONCOPANEL\-3|
|测序年份|2017|
|取样时间|Day 214|
|报告时间|Day 249|

> 因此连接方式是：

```Plaintext
临床患者：
record_id = GENIE-DFCI-051542

        ↓ cancer_panel_test_level_dataset.csv

测序样本：
cpt_genie_sample_id = GENIE-DFCI-051542-250158

        ↓ 基因组文件

Tumor_Sample_Barcode / SAMPLE_ID / Sample_Id
```

## 基因组结果

### 体细胞突变

> 在 data\_mutations\_extended\.txt 中，这个样本共有10条PASS突变：

|基因|蛋白改变|类型|
|---|---|---|
|`TP53`|p\.R248Q|错义突变|
|`RB1`|p\.F226Vfs\*7|移码缺失|
|`CTNNB1`|p\.S37P|错义突变|
|`ATM`|p\.R1575H|错义突变|
|`ERCC3`|p\.Q586Rfs\*25|移码缺失|
|`BCOR`|p\.X1283\_splice|剪接区域突变|
|`MYBL1`|p\.G403R|错义突变|
|`IDH2`|p\.R13K|错义突变|
|`NOTCH1`|p\.N2248T|错义突变|
|`RET`|p\.S909T|错义突变|

> 这里列出的是检测结果，不能仅凭出现某个突变就判断它一定是致病驱动或可用药靶点，还需要结合致病性注释、等位基因频率和临床知识库。

### 拷贝数改变

> 在 data\_CNA\.txt 中，比较显著的包括：

- `PTEN = -2`：深度/纯合缺失

- `KLLN = -2`：深度缺失

- `CDKN2A = -1`、`CDKN2B = -1`：浅缺失

- `MYC = +1`：拷贝数增益

- `ERG = +1`：拷贝数增益

- `BRCA1 = +1`、`CDK12 = +1`：拷贝数增益

> CNA编码通常为：

```Plaintext
-2 = 深度缺失
-1 = 浅缺失
 0 = 二倍体/无明显改变
+1 = 增益
+2 = 高水平扩增
```

### 结构变异与TMB

> data\_sv\.txt 中记录：

- 一个 `PIK3R1` 基因内结构变异

- DNA和RNA证据均为 `Yes`

> `tmb.tsv` 中：

- 原始值约为 `4.62 × 10⁻⁶ / bp`

- 换算约为 `4.62 mutations/Mb`

- 数据集分类为 `Mid (2–16)`

> 病理检测还显示：

- MMR四种蛋白 `MLH1/MSH2/MSH6/PMS2` 表达完整

- 结论为MMR proficient

- 没有证据提示dMMR

- 未进行/未记录MSI和PD\-L1阳性检测结果

## 如何理解这个病例

> 将所有表拼起来后，可以概括为：

> 63岁男性，初诊即为Gleason 9、Stage IV骨转移性前列腺腺癌。雄激素治疗及含Docetaxel方案后PSA快速下降，但约诊断后209天出现PSA较低情况下的影像学进展，随后接受单次放疗及Cabazitaxel/Carboplatin。转移灶测序显示TP53、RB1、CTNNB1等突变和PTEN深度缺失。患者诊断后291天死亡。

> 这个case很好地说明：该数据集不是一张“大宽表”，而是要依靠 `record_id` 和 `cpt_genie_sample_id` 把诊断、治疗、实验室、影像和基因组数据组合起来。

### 3\.1 需要哪些表

BPC 是多张结构化临床表加分子检测文件，不是原始病历全文。以下字段和文件对应本次使用的数据包。

|数据来源|主要提取信息|进入哪个部分|
|---|---|---|
|`patient_level_dataset.csv`|患者编号、性别等背景|PatientProfile|
|`cancer_level_dataset_index.csv`|研究癌症、诊断年龄、诊断分期和病理特征|PatientProfile|
|`regimen_cancer_level_dataset.csv`|药物、方案、各药开始和结束时间|TreatmentHistory|
|`cancer_panel_test_level_dataset.csv`|NGS 样本、panel、取材与报告时间|MolecularReports|
|`data_mutations_extended.txt`、`data_CNA.txt`、`data_sv.txt`|具体突变、拷贝数改变和结构变异|MolecularReports|
|`imaging_level_dataset.csv`、`med_onc_note_level_dataset.csv`|影像和医生的疾病状态评估|Observations|
|`tm_level_dataset.csv`|PSA、睾酮的数值、单位和时间|Observations|
|`pathology_report_level_dataset.csv`|带时间的病理结果|PatientProfile / Observations|
|`ca_radtx_dataset.csv`、`cancer_level_dataset_non_index.csv`|放疗、其他原发癌背景|补充治疗与画像|

患者用 `cohort + record_id` 关联；癌症层加 `ca_seq`；NGS 用 `cpt_genie_sample_id` 关联分子文件。各表先转成事件列表，再合并为患者状态，避免多对多连接造成重复。

### 3\.2 时间规则

主实验选预先定义的合格 NGS 报告返回时点，构建当时可知的患者状态；扩展实验再考虑新治疗开始前的时点。

$S(t_0)=\{P_{\leq t_0},T_{\leq t_0},O_{\leq t_0},N_{\leq t_0}\}$

P 是患者画像，T 是治疗历史，O 是临床观察，N 是已返回的 NGS 结果。所有信息均按可用时间截断。

- 取材日不等于报告可用日；NGS 优先使用 `dx_cpt_rep_days`，同时保留取材时间。

- 各表时间先统一到同一癌症诊断原点。缺签发时间的观察使用检查／就诊日期代理，并保留标记；同日先后不明时采用保守规则。

- 未来停药日期、最终随访、后续治疗和结局不进入当前输入。

- 药物分别保存时间：同一方案中的药不一定同时开始，方案编号也不等于临床治疗线数。

- 遮蔽药物保留 `unknown`；不能推断其靶点。临床试验的同日起止记录保留时间质量标记，不按真实一天暴露解释。

训练和测试按患者划分，同一人的所有时点留在同一集合。主要实验使用冻结的当前知识库做回顾性研究，不声称复现患者当年的知识条件。

## 4\. 真实数据示例：GENIE\-DFCI\-051542

以下患者事实来自已读取的 BPC 表；JSON 是本项目设计的统一格式。真实记录、知识注释和模型预测分别保存。

### 4\.1 患者时间线

该患者为男性，诊断年龄63岁，前列腺腺癌，诊断分期 IV，临床 Gleason 9。日期均为距研究癌症诊断的天数。

|日期|真实记录|
|---|---|
|19–62|Flutamide|
|26–287|Leuprolide|
|62|Enzalutamide，临床试验同日起止记录，实际持续时间不确定|
|63–209；90–195|Bicalutamide；Docetaxel，二者在同一方案行中|
|209–210|PSA 0\.11 ng/mL；骨扫描改善，但胸腹盆 CT、随后脊柱 MRI 记录进展|
|214|NGS 对应样本取材|
|223|开始 Cabazitaxel；同日医生评估进展，日内先后不明|
|244|腹盆 CT、医生评估均进展；PSA 0\.14 ng/mL，睾酮7 ng/dL|
|249|NGS 报告返回，样本 `GENIE-DFCI-051542-250158`，panel 为 `DFCI-ONCOPANEL-3`|
|251–252|Carboplatin；Cabazitaxel 在第251天记录结束|

同一 BPC 方案行包含 Cabazitaxel 和 Carboplatin，但开始日期分别为223和251。因此第244天的进展不能标为这两种药的联合疗效结果。这个病例也说明：PSA 与影像可能不一致，应保留结构化影像结论，无需输入原始影像像素。

### 4\.2 主任务输入：第249天报告返回后

此时可以使用 NGS 和第244天观察，但不能使用第251天的卡铂记录或未来停药时间。以下为精简输入，分子部分仅展示四条实际变异，不是该患者的完整变异清单。

```JSON
{
  "patient_id": "GENIE-DFCI-051542",
  "decision_day": 249,
  "time_origin": "index_cancer_diagnosis",
  "patient_profile": {
    "sex": "Male",
    "age_at_diagnosis": 63,
    "cancer": "Prostate adenocarcinoma",
    "stage_at_diagnosis": "IV",
    "clinical_gleason_score": 9
  },
  "treatment_history": [
    {"drug": "Flutamide", "start_day": 19, "end_day": 62},
    {"drug": "Leuprolide", "start_day": 26, "end_day": null},
    {"drug": "Enzalutamide", "start_day": 62, "end_day": null,
     "time_quality": "trial_duration_uncertain"},
    {"drug": "Bicalutamide", "start_day": 63, "end_day": 209},
    {"drug": "Docetaxel", "start_day": 90, "end_day": 195},
    {"drug": "Cabazitaxel", "start_day": 223, "end_day": null}
  ],
  "observations": [
    {"day": 244, "type": "CT", "result": "progressing"},
    {"day": 244, "type": "oncologist_assessment", "result": "progressing"},
    {"day": 244, "type": "PSA", "value": 0.14, "unit": "ng/mL"},
    {"day": 244, "type": "Testosterone", "value": 7, "unit": "ng/dL"}
  ],
  "molecular_reports": [{
    "sample_id": "GENIE-DFCI-051542-250158",
    "sample_collection_day": 214,
    "report_available_day": 249,
    "assay": "DFCI-ONCOPANEL-3",
    "variants": [
      {"gene": "CTNNB1", "protein_change": "p.S37P"},
      {"gene": "RB1", "protein_change": "p.F226Vfs*7"},
      {"gene": "TP53", "protein_change": "p.R248Q"},
      {"gene": "ATM", "protein_change": "p.R1575H"}
    ]
  }]
}
```

正式数据额外保存每条事实的来源表、行键、原始值和时间代理。`end_day: null` 只表示此快照没有可用的确定结束时间，不能单凭空值判为正在用药。

TargetSelectionAgent 的通路分析步骤将上述变异挂到对应节点，标注“第214天取材、第249天可用”。功能方向由证据工具核查，不直接把变异写成节点激活／失活，也不把 ATM 变异直接当成某药适用标签。

### 4\.3 输出格式与真实轨迹示范

主输出是最多 K 个备选，每个包含以下内容：

|字段|含义|
|---|---|
|`target`、`direction`|候选靶点与干预方向；无法确定时说明缺口|
|`treatment`|候选药物／方案、动作、计划时间；未知日期留空|
|`predicted_observations`|经验证的结局工具给出的指定时间窗口预测；首版可为空|
|`evidence`、`review`|通路依据、支持与反证、历史治疗核查、审核状态|

目前未运行靶点评分、药物评估或结局模型，不能为第249天虚构 Top\-K 和疗效。用该患者第222天之前的信息作输入，则能构建下面这条**截至第244天的真实后续参考输出**，演示相同事件格式如何进入下一次输入：

```JSON
{
  "patient_id": "GENIE-DFCI-051542",
  "input_cutoff_day": 222,
  "output_type": "historical_reference",
  "treatment": {
    "event_id": "TX_CABAZITAXEL_223",
    "status": "observed",
    "drug": "Cabazitaxel",
    "target": null,
    "target_annotation_status": "requires_external_drug_mapping",
    "start_day": 223,
    "end_day": null,
    "observed_through_day": 244
  },
  "predicted_observations": [],
  "observed_observations": [
    {"day": 244, "type": "CT", "result": "progressing"},
    {"day": 244, "type": "oncologist_assessment", "result": "progressing"},
    {"day": 244, "type": "PSA", "value": 0.14, "unit": "ng/mL"}
  ],
  "interpretation": "progression_recorded_during_exposure",
  "optimal_treatment_label": false
}
```

真实部署的候选改用 `status: proposed`，真实观察数组为空。治疗与随访实际发生后，才将确认的事件加入治疗历史、将检查加入观察列表；仅模拟的结果进入 `scenario_history`，不混入真实历史。

**这个病例的用途：** 第222→244天适合说明治疗与观察的衔接，但输入没有 NGS；第249天适合说明 NGS 后的通路注释。第251天新增卡铂已归在既有方案中，不能把它自动当成“NGS 指导的新方案”金标准。

## 5\. 模型训练与评估

### 5\.1 先跑通，再训练

|组件|首版|后续训练条件|
|---|---|---|
|各 Agent|规则转换＋冻结 LLM＋工具调用|有经过审核的抽取或工具轨迹后再微调|
|图排序|可解释的分项评分，筛 Top\-M|有专家候选排序标注后训练小型排序模型或 GNN|
|结局预测|可暂不启用|BPC 有足够方案、观察和随访支持时，独立训练|
|EPT|调用经过验证的适配模型|用独立蛋白—配体亲和力数据训练，BPC 不提供此类标签|

图排序初版考虑患者分子证据、机制联系、可干预性、既往治疗和反证。各分项单独展示；权重仅在开发集确定，排名分数不解释为疗效概率。

有专家偏好标签后，可以用配对排序损失：

$\mathcal{L}_{\mathrm{rank}}= -\log\sigma\left(s(S,c^{+})-s(S,c^{-})\right)$

S 是患者状态，c⁺ 和 c⁻ 是专家在相同输入下较认可与较不认可的“靶点＋方向”候选，s 是模型评分。专家标注时不看患者未来结局。

若研究结局预测，单独定义任务，例如给定实际治疗和当前状态，预测固定窗口内的疾病评估或一种定义明确的 PFS。未记录／不确定不当成稳定，删失不当成有效；需要使用与终点匹配的分类或生存模型。该预测不等于未使用药物的反事实获益。

### 5\.2 最小 Benchmark

BPC 提供患者事实和随访；专家补充候选合理性与证据审核标签。第一阶段采用以下评估，不铺开大量独立 benchmark。

|评估对象|主要指标|
|---|---|
|患者状态|字段与时间准确率、未来信息泄漏数|
|靶点／药物候选|专家认可的 Top\-K 比例、干预方向错误率、关键反证遗漏率|
|解释与流程|引用是否支持主张、路径是否参与排序、工具成功率和成本|
|可选结局模型|分类或生存指标、校准、有效样本和缺失／删失情况|

主终点为固定 K 槽位中专家认可候选的比例，同时报告输出覆盖率和已输出候选精度，允许证据不足时弃答。测试固定患者划分、知识版本、候选池及工具预算；统计按患者计算，不把多次就诊当作独立样本。

**优先对照：** 相同模型和证据预算下，比较“无图”“只有节点”“有类型和方向的图”；再分别移除 NGS、治疗历史、靶点复核，检验各部分是否有实际贡献。结构模块单独比较 docking 与 docking＋EPT。

## 6\. 实施顺序与参考

1. **数据原型：** 按本例构建少量人工核查的患者快照；完成药物时间、NGS 可用时间、缺失和来源处理。

2. **核心流程：** 实现共享 PatientState、KEGG 注释、图初筛、靶点复核和已知药物检索，输出一页患者图与候选证据表。

3. **小规模评估：** 冻结患者划分，完成专家标注和关键对照；再决定是否训练图模型。

4. **扩展验证：** 选择适合的候选接入 docking/EPT；数据支持后再做结局预测与多步情景模拟。

主要参考沿用原计划，分别借鉴治疗证据推理与靶点研究，不复现其完整系统：

- [TxAgent](https://arxiv.org/abs/2503.10970)：患者条件下的治疗推理和药物工具调用。

- [OriGene](https://github.com/GENTEL-lab/OriGene)：靶点假设、证据检索和复核。

- [GENIE BPC Prostate](https://aacrprojectgenie.org/data/prostate-1-0-public/)：主数据说明；字段含义以本次数据包及配套 Guide 为准。

- [KEGG KGML](https://www.kegg.jp/kegg/xml/docs/)：通路结构与关系解析。

本版按用户手绘图精简原计划。病例部分为实际数据示范；候选选择、训练和评估均为拟开展工作，尚无模型有效性结果。

# 下一步

BPC数据集合适吗

|BPC 数据集|癌种|截图中的版本|样本数（samples）|
|---|---|---|---|
|**BLADDER**|膀胱癌|v1\.0\-public|748|
|**BrCa**|乳腺癌|v1\.0\-public|1,223|
|**CRC**|结直肠癌|v2\.0\-public|1,551|
|**NSCLC**|非小细胞肺癌|v2\.0\-public|2,004|
|**PANC**|胰腺癌|v1\.0\-public|1,130|
|**Prostate**|前列腺癌|v1\.0\-public|1,227|

训练\-SFT

训练设计

|训练任务|输入|Label 从哪里来|模型学到什么|来源|
|---|---|---|---|---|
|历史治疗预测|截至 t 的患者状态|后续真实治疗行动|医生历史上如何选择|BPC|
|治疗后观察预测|截至 t 的状态＋明确的治疗行动＋预测窗口|窗口内真实观察|给定治疗后，可能观察到什么|BPC|
|通路与候选审核|截至 t 的状态＋工具查询结果|经审核的工具调用、证据判断、候选报告|你的 Agent 如何完成任务|自主构造|

预测 treatment\+observeration, observation怎么预测

|辅助终点|输出形式|标签注意事项|
|---|---|---|
|**影像疾病状态**|改善／稳定／进展／混合反应的概率|保留原始评估类别；无法判断时不强行赋值|
|**PSA 变化方向**|上升／基本稳定／下降的概率|需要治疗前基线与后续数值，并预先规定变化阈值|

靶点排序怎么排
