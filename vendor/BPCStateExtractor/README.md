# BPCStateExtractor

[BPCStateExtractor](https://github.com/Chenshengm/BPCStateExtractor) turns the structured GENIE BPC Prostate release into a deterministic, cutoff-safe patient snapshot. It extracts and minimally standardizes documented facts; it does **not** infer missing facts, recommend treatment, or generate training labels. The distribution name remains `bpc-patient-state`, the Python package is `bpc_state`, and the CLI is `bpc-state`; renaming the repository does not change these interfaces.

The central contract is:

```python
get_patient_state(patient_id, cutoff_day, cancer_id=None) -> dict
```

`cutoff_day` is inclusive and relative to the patient's first BPC project cancer diagnosis. To make a prediction for treatment day `d`, query day `d - 1`.

## What is enforced

- Analytic clinical CSV files are authoritative. `data_timeline_*.txt` files are not re-imported.
- The default target cancer is the earliest index cancer. `ca_seq=0` is never assumed.
- Facts are filtered individually by `available_day`, not by source row.
- NGS results enter only on the NGS report day. PD-L1/MSI/MMR results use their individual report dates.
- Regimens are expanded through `drugs_drug_1` … `drugs_drug_5`; a future component cannot leak through `regimen_drugs`.
- Missing drug end dates and last-administration dates do not imply ongoing treatment. Masked investigational-drug durations remain unknown.
- OS/PFS/TTNT, final follow-up, death, whole-course counts, and retrospective whole-course summaries are excluded from Agent state.
- Negative days and same-day conflicting observations are retained.
- Every fact carries stable content-addressed `evidence_refs`; evidence resolution returns only that fact's whitelisted source fields and raw values. References remain attached to the same fact across cutoffs.
- Radiotherapy completion totals and duration have no documented availability date and are omitted from historical state. Their exclusion is recorded in the audit sidecar.
- `BoundedPatientTool` fixes patient identity and maximum cutoff for both state and evidence calls.

The exact whitelist and time rules are in [config/field_rules.json](config/field_rules.json). Known data limitations and design decisions are in [docs/data-review.md](docs/data-review.md).

The broader research design and planned Agent/Workflow architecture are documented in [docs/Pathway-agent.md](docs/Pathway-agent.md). That document describes proposed work beyond the implemented patient-state extractor.

## Install

Python 3.10+ is required.

```bash
cd /home/u2021202188/bpc-patient-state
python3 -m pip install -e '.[test]'
```

The release data remain under `dataset/` and are excluded from Git.

## Python API

```python
from bpc_state import get_patient_state

state = get_patient_state(
    "GENIE-DFCI-051542",
    cutoff_day=220,
    data_dir="/home/u2021202188/bpc-patient-state/dataset",
)
```

For repeated queries, reuse one extractor so local indexes are cached:

```python
from bpc_state import BPCStateExtractor, BoundedPatientTool

extractor = BPCStateExtractor("/home/u2021202188/bpc-patient-state/dataset")
state = extractor.get_patient_state("GENIE-DFCI-051542", 220)

# The Agent cannot change patient or cross day 220.
tool = BoundedPatientTool(extractor, "GENIE-DFCI-051542", max_cutoff_day=220)
agent_state = tool.get_patient_state(219)
first_ref = next(iter(agent_state["evidence_refs"]))
evidence = tool.get_evidence(219, [first_ref])
```

The convenience function also reads `BPC_DATA_DIR` when `data_dir` is omitted.

## CLI

Extract state and an optional audit sidecar:

```bash
bpc-state --data-dir dataset state \
  --patient-id GENIE-DFCI-051542 \
  --cutoff-day 220 \
  --output state.json \
  --audit-out audit.json
```

Resolve visible evidence metadata:

```bash
bpc-state --data-dir dataset evidence \
  --patient-id GENIE-DFCI-051542 \
  --cutoff-day 220 \
  --ref YOUR_VISIBLE_EVIDENCE_REF
```

Export schemas:

```bash
bpc-state schema --output schemas/patient-state.schema.json
bpc-state tool-schema \
  --patient-id GENIE-DFCI-051542 \
  --max-cutoff-day 220 \
  --output schemas/example-bound-tools.json
```

Copy the `E-` followed by 24 hexadecimal characters reference from the state returned at an allowed cutoff. A future-only reference is rejected at an earlier cutoff.

The static response schema is [schemas/patient-state.schema.json](schemas/patient-state.schema.json), now version **1.2**. Bound tool schemas deliberately omit `patient_id`; the server-side `BoundedPatientTool` owns that authority.

## Output semantics

- `event_day`: when the clinical event occurred.
- `available_day`: earliest day the implementation permits the fact to enter state.
- `available_day_basis`: documented report time or explicitly labelled proxy.
- Empty arrays mean “no records were admitted by this cutoff”, not “not tested” or “negative”.
- `status_at_cutoff="unknown"` means the source does not establish ended/active status at that cutoff.
- Molecular `coverage` distinguishes profiled, not profiled, and unresolved. Missing files and missing CNA sample columns are unresolved, not negative. A zero-mutation profile requires the supplied mutation file plus sample profiling evidence.
- TMB is returned in its source representation with `unit="unconfirmed_source_unit"`; the tool does not silently convert it.
- CNA `value_raw` is preserved. `coverage.cna_missing_genes` and `coverage.cna_neutral_genes` distinguish `NA` from neutral `0`; fractional alterations are returned unchanged. Coverage has its own evidence references.
- Pathology `biomarker_testing` preserves a recorded `No` as "not reported on this pathology report"; it does not mean a negative assay. PD-L1 percentages, ranges, and CPS are retained in each dated result's `details_raw`.
- Required clinical input files fail explicitly when absent. Optional genomic files that are absent generate unresolved coverage and an issue flag.
- Nullable clinical fields are emitted as JSON `null`, with a JSON Pointer entry in `field_status` explaining the reason. Null is a representation, not a diagnosis of why data are absent. States distinguish `unknown`, `not_recorded`, `not_reported`, `not_tested`, `not_detected`, `not_applicable`, `not_collected`, `invalid`, `non_exact`, `masked`, `excluded_by_policy`, and `not_normalized`.
- An explicit source `Unknown` remains in the raw string; its numeric interpretation is null. Zero and a recorded `No` are never generic missing values. A bare `NA` is treated as ambiguous, not automatically as not applicable. Status metadata uses only admitted facts/evidence, never hidden future records.

For example, a marker with an empty source value has `value_numeric: null` plus `field_status["/observations/tumor_markers/0/value_numeric"].status == "not_recorded"`. If the source explicitly says `Unknown`, the same numeric null has status `unknown`. A future or absent stop date leaves `end_day: null` with status `unknown`; the tool does not reveal which of those was found in later data.

## Separate offline evaluation data

Agent input contains only the PatientState available by the inclusive cutoff. Actual subsequent observations are exported independently, with an explicit end day:

```bash
mkdir -p offline_evaluation
bpc-state --data-dir dataset evaluation \
  --patient-id GENIE-DFCI-051542 \
  --cutoff-day 220 --end-day 260 \
  --output offline_evaluation/followup.json
```

The `evaluation` command requires a separate output file. The corresponding Python interface is intentionally in its own module:

```python
from bpc_state.evaluation import OfflineEvaluationExporter

evaluation = OfflineEvaluationExporter("dataset").export(
    "GENIE-DFCI-051542", cutoff_day=220, end_day=260,
)
```

The output [schema](schemas/evaluation-data.schema.json) is separate from PatientState. It contains actual imaging, oncology assessments, and markers in `(cutoff_day, end_day]`, gated by availability through the end day; delayed results for earlier events are in `late_available_observations`. It excludes the entire future state, future treatments, molecular reports, survival labels, and predictions. Only evidence for the selected observations is retained. `none_recorded_in_window` does not imply no progression, no testing, complete follow-up, or absence of censoring.

The export records `purpose: offline_evaluation` and a canonical SHA-256 of its input state. It uses a separate extractor, is absent from `BoundedPatientTool` and Agent tool schemas, and does not alter the input state. The ignored `offline_evaluation/` directory must not be mounted or otherwise exposed to the Agent; tool API separation alone does not sandbox unrestricted shell/filesystem access.

## Real examples

The same public, de-identified patient is exported at three cutoffs:

- [Day 50](examples/GENIE-DFCI-051542-day-50.json): early systemic treatment only.
- [Day 220](examples/GENIE-DFCI-051542-day-220.json): radiographic progression and radiotherapy visible; NGS still hidden.
- [Day 260](examples/GENIE-DFCI-051542-day-260.json): NGS report and exact linked genomic profiles visible.

These files are generated from the local release, not hand-authored examples.

## Tests

```bash
pytest
```

Synthetic tests cover:

- future regimen components and future end dates;
- unknown end status and masked investigational-drug duration;
- delayed NGS reports and exact sample linkage;
- non-zero main `ca_seq` and ambiguous cancer association;
- pathology addendum biomarker report timing;
- negative days and same-day conflicting imaging;
- raw TMB/CNA semantics;
- patient/cutoff-bound evidence access and separate audit output.
- stable evidence references after future events are added;
- changes to later radiotherapy totals and masked drug duration;
- absent genomic files, absent CNA columns, and neutral versus missing gene calls;
- dated PD-L1 numeric results and explicit no-testing-report semantics;
- executable field allowlists and reporting delays.

[tests/test_snapshot_rechecks.py](tests/test_snapshot_rechecks.py) adds nine on-disk future-data mutation scenarios and reconstructs the extractor to compare the complete early JSON. It also checks all ten required clinical files individually, missing molecular files, all-missing CNA calls, and independent same-day PD-L1 tests.

[tests/test_real_release.py](tests/test_real_release.py) provides read-only integration checks against the local Prostate v1.0-public release: stable evidence across cutoffs, exact radiotherapy/NGS day boundaries, all 102 declared CNA samples without data columns, and dated quantitative PD-L1 results. These integration tests skip when the release is absent; set `BPC_DATA_DIR` to select its location.

Run the additional checks separately with:

```bash
pytest tests/test_snapshot_rechecks.py tests/test_real_release.py
```

The latest verification with the local release completed **94 tests passed, with no skips**. This checks the documented scenarios, not every possible input or historical availability assumption.

Additional tests in `test_missing_semantics.py` and `test_evaluation.py` check explicit null/status semantics, zero, missing/invalid numeric values, evaluation-window endpoints, late results, evidence pruning, input fingerprinting, and Agent-boundary isolation.

The same rules file is packaged as `bpc_state.resources/field_rules.json`, so normal wheel installations work outside this checkout. Custom rules can be supplied via `BPCStateExtractor(data_dir, rules_path=...)` or the CLI's global `--rules-path` option. Allowlists are applied before extraction and evidence creation; global forbidden fields override an allowlist entry. Time rules execute a restricted list of direct, DOB-relative, or cancer-relative field transforms and optional nonnegative `reporting_delay_days`. No arbitrary expression evaluation is used. Raw drug/unit/TMB/CNA preservation policies are fixed safety policies; unsupported alternatives fail explicitly.

Version 1.1 changes evidence IDs from sequential numbers to stable hashes and removes the ambiguous radiotherapy `dose_raw`, final fraction count, and final duration fields. Consumers should regenerate old examples and tool schemas.

Version 1.2 emits nullable fields explicitly and adds `field_status`; consumers must use null/status checks rather than interpreting omitted keys or inserting `"unknown"` into numeric fields. The field/time **rules file format remains version 1.1**. The offline evaluation export has its own schema and is never part of Agent state.

## Scope boundaries

The extractor intentionally does not normalize drug names beyond preserving a nullable normalized field, infer treatment lines, infer active therapy from a missing end date, interpret variant pathogenicity, convert TMB units, or combine imaging/PSA/oncologist assessments into a clinical response label.
