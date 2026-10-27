# 内置 BPCStateExtractor 工具

`BPCStateExtractor/` 是工具项目的源码快照，不是 Git submodule，也不是运行时下载依赖。
安装 BPCAgent 会同时安装 `bpc_state` 包、默认规则资源和 `bpc-state` CLI。
不需要再安装相邻的 `bpc-patient-state` 工具项目，也不修改 `sys.path`。

## 来源与同步状态

- 上游：https://github.com/Chenshengm/BPCStateExtractor
- 同步日期：2026-10-27；对应工具上游提交 `ebddef6c2af07d75a9650d9efd4ff3a2666b1b3a`。
- 初次导入时通过本机 17890 HTTP CONNECT 代理和 SSH 验证上游 HEAD：
  `103370dcbacda218cf3484163bfbe02a0a206408`。
- 导入来源：本机原 `pathway-agent` 工作树；当前本地目录为 `/home/u2021202188/bpc-patient-state`。
- 随快照保留的 0.3.0 / Schema 1.2 更新（缺失语义、独立离线评估及配套测试）
  已于 2026-10-26 推送至上游提交 `ae96917d2ace4f3010588bf385599110a4251c43`。
- 研究设计文档编辑与改名、BPCStateExtractor 项目表述及本机路径修正
  已同步到上述上游提交；本目录保留的文件与该提交逐文件一致。
- 保留了工具的源码、规则、schema、示例、文档、测试和独立 `pyproject.toml`。
  未复制 `.git`、数据集、缓存、虚拟环境或密钥。
- 子目录 README 中的绝对路径是原项目用法；BPCAgent 的安装和调用方式以本文件
  和根目录文档为准。

## 安装与调用

在 BPCAgent 根目录：

```bash
python -m pip install -e '.[test]'
bpc-state --help
python -m pytest
```

`bpc_state` 是内置工具的 Python 包名，不随 GitHub 仓库改名而更改。
上游项目名统一为 BPCStateExtractor；本次只同步命名/链接和内置目录，未改上游现有 distribution 名 `bpc-patient-state`。
不要同时安装独立 `bpc-patient-state` 和 BPCAgent：两者提供相同的包及 CLI。
如果已有旧的独立安装，先卸载 `bpc-patient-state`，再重新安装 BPCAgent。

数据目录仍由调用方提供；本机暂沿用 `/home/u2021202188/bpc-patient-state/dataset`，
仓库改名不会自动搬迁本地数据。其他机器修改 `config/workflow.jsonc` 的 `data_dir`。
默认规则随安装包携带，不再需要指向相邻项目的 `config/field_rules.json`。

调用链：

```text
PatientStateAgent
  ├─ get_patient_state    → bpc_state.BoundedPatientTool → BPCStateExtractor
  ├─ get_patient_evidence → 同患者、同 cutoff 的证据引用
  └─ state_check          → BPCAgent 的完整性/单位检查

bpc-state evaluation     → 独立离线评估，不注册为 Agent 工具
```

五 Agent 工作流的证据工具只接受当前共享快照里的引用；患者 ID、数据路径和 cutoff
由宿主绑定，不向模型开放。尚未实现的科学工具仍保持占位，不会因源码集成而变成真实服务。
PatientStateAgent 仅保留 workflow 实现，每轮保存状态和证据，并执行 state_check 后提交给 Review。
只提取患者也使用同一角色的单阶段 workflow；工具本身仍可直接调用，不依赖 LLM。

## 后续更新

先 fetch 新上游并比较上游提交与本快照的本地改动，再人工合并工具子目录；
不要直接覆盖本目录或把上游 `.git` / `dataset` 拷入 BPCAgent。
更新后记录新的来源提交、本地补丁状态，并运行根目录测试以及工具测试：

```bash
python -m unittest discover -s tests
python -m pytest vendor/BPCStateExtractor/tests
BPC_DATA_DIR=/path/to/dataset python -m pytest vendor/BPCStateExtractor/tests/test_real_release.py
```

工具保留自己的测试和独立打包入口，BPCAgent 则将工具代码/规则一起打进自身的 wheel；
部署只需 BPCAgent wheel，无需访问上游仓库。
