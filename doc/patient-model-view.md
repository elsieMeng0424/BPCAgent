# Compact patient input for workflow agents

`bpc-model-view-v1` is a deterministic presentation layer, not an LLM summary or
a replacement for the canonical `PatientState` schema. It applies automatically
to `bpc_state_extractor`, its `get_patient_state` alias, and
`get_shared_patient_state`. All roles receive the same shared projection.

## Storage and model input

| Location | Contents |
| --- | --- |
| `patient_state.json` | Complete canonical cutoff-bounded state, including evidence source bodies |
| `shared_patient_state.json` | Full state, recent observations, completeness report, normalized markers and canonical hash |
| `patient_model_view.json` | Compact extraction/shared-state tool result, saved at workflow completion |
| Per-turn `patient_state_day_*.json` | Full state saved by PatientStateAgent extraction |
| Per-turn `patient_evidence_day_*.json` | Selected evidence queried by any role; separate files for successive batches |
| Patient Shell `input/*.json` | Full bounded exports, unchanged by model-view compaction |

`patient_state_sha256` continues to identify the full canonical state, not the
projection. Existing offline evaluation hash joins remain valid. No future labels
or post-cutoff evidence are made available. Historical run files are not rewritten.

## Four changes

1. **Evidence on demand.** The root `patient_state.evidence_refs` becomes an ordered
   list of IDs instead of a map containing every source body. References attached
   to facts and missingness annotations are unchanged. Any workflow role can call
   `get_patient_evidence({"evidence_refs": ["E-..."]})` after reading state. The
   tool resolves only references admitted to the same host-bound snapshot and
   saves its result in that role's turn directory. The PatientState role must
   still extract and successfully save evidence in its own turn before submission.
2. **Observation deduplication.** `recent_observation_ids` replaces the model
   payload's `recent_observations`. It maps imaging, med_onc and tumor_markers to
   their selected record IDs. `recent_start_day` remains explicit; boundaries are
   inclusive. All earlier cutoff-visible observations remain under
   `patient_state.observations`; this is an index, not a history truncation.
3. **Repeated-key tables.** Lists of uniform records use `columns` and `rows` when
   that encoding is smaller. All fields, record order and nested values survive.
   Different-key records remain objects: an absent key is not filled with null.
   Tables may be nested, and short lists may remain ordinary JSON arrays.
   The explicit `state_check` tool uses the same lossless encoding for its full
   completeness report and normalized markers.
4. **Explicit missingness.** Numeric null stays null. `field_status` retains every
   original status, reason and evidence reference. A compact field-status table
   includes a `path` column pointing into the original canonical patient-state
   layout, not into the table representation. Unknown, not recorded, not
   applicable and not normalized remain distinct; zero, false and empty lists are
   not converted to unknown. Missing stop dates do not imply ongoing treatment.

## Table format

Illustrative partial record table (actual views preserve all record fields):

```json
{
  "encoding": "records-table-v1",
  "columns": ["imaging_id", "event_day", "available_day", "assessment"],
  "rows": [
    ["image:5", 209, 209, "responding"],
    ["image:6", 209, 209, "progressing"],
    ["image:7", 209, 209, "stable"]
  ]
}
```

`rows[i][j]` is the value of `columns[j]` for record `i`. The table does not merge
different scans or replace conflicting assessments with a global conclusion.
`field-status-table-v1` uses the same structure, with `path` identifying the
original annotation key. Both encoding tags are reserved for the model view.

Inspect a saved view in a notebook:

```python
import json
from pathlib import Path
from bpcagent.workflow.model_view import expand_tables

run_dir = Path(result["task_dir"])
view = json.loads((run_dir / "patient_model_view.json").read_text())
decoded = expand_tables(view)
print(decoded["patient_state"]["treatment_history"])
print(decoded["patient_state"]["field_status"])
```

Decoding restores record objects and the field-status mapping, not evidence
source bodies or duplicated recent observations. Retrieve selected evidence via
the tool, or inspect the full saved snapshot locally. Host validation and Shell
execution never depend on decoding a model-provided view.

## Verification and limits

The historical GENIE-DFCI-051542 snapshot at day 220 has a 51,644-byte shared
payload and a 22,870-byte model view (55.7% smaller), measured as minified UTF-8
JSON. This is not a tokenizer count or a guarantee for other patients. Tool
envelopes, prompts and later evidence queries add context. The earlier 17.1 KB
prototype estimate is not the size of this implementation; this version retains
complete check findings and annotations plus format metadata.

Tests cover exact table round trips, null/zero/false distinctions, sparse rows,
recent-window boundaries, canonical snapshot immutability, same views across
roles, evidence retrieval and saved artifacts, future-reference rejection and
post-cutoff source mutation invariance. They use synthetic data and scripted
models, with no model API calls.

This change does **not** alter `BasicAgent` context thresholds, archived readback
paging, or retention of previously read pages. Large payloads can still trigger
context-budget limits; repeated paging remains a separate issue.
