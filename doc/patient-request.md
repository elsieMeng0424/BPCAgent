# Natural-language patient requests

The runtime task can include both the patient and the date boundary:

```python
from bpcagent.workflow import AgentWorkflow

workflow = AgentWorkflow(
    data_dir="../bpc-patient-state/dataset",
    workspace_root="workspace",
    mode="demo",
)
result = workflow.run("看下BPC数据集病人051542前220天的病例，推荐他的下一步用药。")
print(result["patient_id"], result["cutoff_day"], result["status"])
```

The equivalent CLI is:

```bash
.venv/bin/bpc-workflow --mode demo \
  --request '看下BPC数据集病人051542前220天的病例，推荐他的下一步用药。'
```

Add `--patient-only` to run PatientStateAgent and its Review without the target
and drug stages. Replace `demo` with `live` to use the configured models and
credentials. The notebook's workflow section uses the same natural-language
input. Existing notebook outputs are historical: rerun the cells to refresh them.

## Scope resolution and tool decisions

1. `workflow/request.py` resolves explicit scope **before** creating an experiment
   or making a model request. A numeric patient suffix is matched against
   `patient_level_dataset.csv`; exactly one record must match. No institution
   prefix is guessed. Use a full ID when a suffix is ambiguous.
2. The supported phrase `前220天` means an **inclusive cutoff at relative day
   220**, measured from the patient's first BPC cancer diagnosis. It does not
   mean 220 days backwards from today. Earlier history, including negative days,
   is retained. `第50至220天` additionally sets a recent-observation start at day
   50 without discarding earlier diagnosis/treatment history.
3. The original task reaches every task Agent and Review unchanged. The resolved
   boundary is also available in `get_stage_context`. The host binds one patient
   and cutoff; the model cannot override them in an extraction tool call.
4. In live mode, PatientStateAgent chooses native tool calls according to its
   prompt and the task. Extraction is not eagerly executed by the runner.
   The prompt explains the intended order, while host validation enforces the
   required extraction, evidence query and completeness check before submission.

The scope resolver is a conservative parser, **not a general language model**.
Supported forms include `病人051542前220天`, `患者051542截至第220天`,
`GENIE-DFCI-051542第50至220天`, and
`patient GENIE-DFCI-051542 through day 220`.
Missing, conflicting or unsupported boundaries raise `RequestClarificationError`
(`ValueError`); the caller should ask for a clearer request and rerun. Calendar
dates are not converted into relative dates. Structured `patient_id`,
`cutoff_day`, and `start_day` arguments remain available; explicitly mentioned
natural-language boundaries must agree with them.

## PatientStateAgent tools

| Tool | Behavior |
| --- | --- |
| `bpc_state_extractor` | Calls the bundled BPCStateExtractor through the bounded shared-state adapter; saves snapshot JSON. Takes no model-supplied scope/path arguments. |
| `get_patient_state` | Alias of the same bounded extraction action. |
| `get_patient_evidence` | Resolves and saves only references from this snapshot at the same cutoff; available to all workflow roles after reading state. |
| `state_check` | Checks completeness, schema, time boundaries, missingness and units. Required before submission. |
| `run_shell` | Real Shell for optional inspection/computation after extraction; backend selected in `config/tools.jsonc`. |
| `get_shared_patient_state`, `get_stage_context` | Read shared facts, the task, stage instructions and review feedback. |
| `submit_output` | Validates the structured result and required preceding actions. |
| `read_context_record` | Retrieves exact archived tool output when context management is enabled. |

The extractor package and the Agent-facing extraction tool deliberately share
the name `bpc_state_extractor`. The package contains the extraction implementation;
the registered tool is its cutoff-bound adapter. It is not another Agent.

Extraction and shared-state reads return the compact `bpc-model-view-v1` format,
not the canonical on-disk schema. Evidence source bodies are fetched on demand;
observation records appear only once, with recent IDs as an index. Repeated
records use explicit column/row tables, preserving null, status, reason and
evidence references. `state_check` also uses lossless record tables. See
[model view format](patient-model-view.md). Full state files and Shell inputs
retain their original schema and evidence bodies.

Offline `demo` uses a scripted model: actual extraction/checks run, but no LLM is
consulted. Its default script does not call Shell. The regression suite includes
a separate scripted native-tool loop that executes real Shell against synthetic
bounded inputs, then submits output and runs Review.

## Shell isolation and artifacts

`config/workflow.jsonc` selects `"tools_file": "tools.jsonc"`. The tool file contains:

```jsonc
{
  "patient_shell": {
    "mode": "local", // local: Docker off | sandbox: Docker on | disabled: no Shell
    "allow_unsafe_local": true, // Local commands have the current user's host permissions
    "sandbox": {"image": "bpcagent-sandbox:0.1"}
  }
}
```

The checked-in file temporarily enables local Shell, so Docker is not required.
This is not isolation: commands can access host files and the network with the
current user's permissions. Local mode cannot enforce cutoff-only data access.
The library default, when no tools file is selected, remains `sandbox`.
CLI and notebook use the same file. Recreate the configuration and workflow after
editing it. `request.json` and per-turn Shell audit records preserve the active mode.
`command_timeout` and `max_output_bytes` under `sandbox` also apply in local mode;
Docker-specific limits do not apply there. An explicit `docker_path` is resolved
relative to the tools file. The former inline workflow `patient_shell` block is
no longer accepted; move it into the tools file.

To enable Docker, select `sandbox`, build the existing image and start Docker as
described in [sandbox setup](sandbox.md). See also [current host setup](docker-host-setup.md).
Container startup is lazy, so a run that does not request Shell needs no Docker.
Missing Docker/image or sandbox failure produces a tool error, **never an automatic
fallback to local execution**. The Agent must disclose failed/unavailable checks;
Review may request a revision or block the run.

The sandbox has no network and mounts only the current turn's exported input and
working directories. It does not mount raw datasets, future evaluation labels,
credentials, the whole repository, or another experiment. Input is read-only in
the container; scripts/output/tmp are writable.

```text
001_PatientStateAgent/
  shell_execution.json          # Execution attempts, isolation and cleanup audit
  shell/
    input/
      patient_state.json       # Authoritative cutoff-bound snapshot copy
      patient_evidence.json    # Evidence limited to that snapshot's references
      state_check.json
      normalized_markers.json
    scripts/
    output/                    # Generated diagnostic artifacts
    tmp/
```

The host exports input from authoritative memory before each command. Shell output
does **not** change the shared snapshot or original dataset. Unit corrections remain
proposals unless separately validated and integrated by host code. Check `exit_code`
and `timed_out` even when a tool invocation returns `status="success"`. Context
compaction retains these flags and permits exact output readback.

For explicitly trusted experiments only, `mode="local"` requires
`allow_unsafe_local=true`. Local mode uses the current host user's filesystem and
network permissions: its working directory is **not** a security boundary. A clean
child environment does not prevent reading credentials or future data from disk.
Do not use local mode for cutoff-isolation evaluation or untrusted commands.
`mode="disabled"` removes Shell from the Agent's registry.

## What the medication request currently returns

The default workflow still runs PatientState → Review → TargetSelection → Review
→ DrugSelection → Review. Scientific connectors and ranking are placeholders;
both demo and live output must therefore keep drug/target candidates empty and
explain the missing evidence. A completed workflow is **not** a validated medication
recommendation. Existing treatment is not automatically stopped or changed.
