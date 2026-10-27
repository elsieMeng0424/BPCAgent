# GENIE BPC Prostate v1.0-public: extraction review

## Release inspected

The implementation was checked against the files in `dataset/`, including the 188-page Analytic Data Guide, Variable Synopsis workbook, release notes, manifest, and the actual headers and values in the analytic and genomic files.

The release contains 1,116 patients and ten analytic CSV datasets. The implementation uses these CSVs as the clinical source and the cBioPortal-formatted genomic profiles only for molecular data. Timeline files are useful for validation but are derived views and are not imported again.

## Grain and linkage

| Source | Grain | Link used |
|---|---|---|
| `patient_level_dataset.csv` | one row per patient | `cohort + record_id` |
| index/non-index cancer | one row per cancer diagnosis | `cohort + record_id + ca_seq` |
| regimen | one row per regimen–cancer association | `cohort + record_id + ca_seq`; expanded per drug slot |
| radiotherapy | one row per RT–cancer association | `cohort + record_id + ca_seq` |
| pathology | one row per pathology report | patient plus `path_proc_number + path_rep_number` |
| imaging | one row per scan report | patient; no reliable cancer key |
| medical oncology | one curated assessment per month | patient; no reliable cancer key |
| tumor marker | one result | patient; no reliable cancer key |
| cancer panel test | one NGS test–cancer association | patient/`ca_seq`, then `cpt_genie_sample_id` |
| mutation/CNA/SV/TMB | sample profile | exact sample ID |

The extractor does not perform a many-to-many merge. Each event is emitted independently, which avoids treatment × scan × laboratory row multiplication.

## Time rules implemented

| Fact | Event day | Available day |
|---|---|---|
| cancer diagnosis | `first_index_ca_days` | diagnosis-day proxy |
| drug component | DOB-relative drug start when present, otherwise cancer offset + `dx_drug_start_int_i` | start-day proxy |
| trusted drug end | cancer offset + `dx_drug_end_int_i` | end-day proxy; hidden before that day |
| radiotherapy | cancer offset + `dx_rt_start_days` | start-day proxy |
| pathology base facts | `dx_path_proc_days` | procedure-day proxy |
| PD-L1/MSI/MMR | biomarker `*_prepaint` minus first-index DOB interval | biomarker report day |
| imaging | `dx_scan_days` | scan-day proxy |
| oncology assessment | `dx_md_visit_days` | visit-day proxy |
| tumor marker | `dx_tm_days` | specimen-collection proxy |
| NGS report and all linked genomic results | collection from linked pathology when possible | `dob_cpt_report_days` minus origin DOB interval; associated cancer-relative report day is a fallback |

`rt_rt_int` was explicitly verified as radiation duration, not a date. `dx_drug_end_or_lastadm_int_i` is not used because it mixes a true end date with last known administration. Negative relative days are valid and retained.

Radiotherapy `rt_total_dose` and `rt_fractions` describe the total actually received (Guide p.128), not a documented plan at the start date. These, `rt_dose`, and `rt_rt_int` have no independently documented availability time and are excluded from every historical snapshot. In particular, the implementation does not infer their availability from `start + final_duration`, or substitute a per-fraction dose for a missing total.

## Facts intentionally excluded

- OS, PFS, TTNT and all event labels derived using later follow-up;
- final death/follow-up fields and whole-course record counts;
- `regimen_drugs`, because it can reveal components that start after the requested cutoff;
- final `drugs_dc_ynu` and last-administration fields for historical active-status inference;
- stage, grade, distant-metastasis summaries, and other diagnosis-level retrospective composites whose availability date is not established by this release;
- pathology biomarker results with no individual report date;
- NGS reports with no report date;
- raw rows through the evidence API.

Excluded record references and reasons can be written to a separate audit sidecar. The audit is not embedded in Agent state.

## Unresolved semantics retained rather than guessed

- Drug normalization is `null`; raw NCI-labelled drug text is retained.
- A missing drug end does not establish ongoing therapy.
- Investigational Drug `start=end` can represent privacy masking and is never accepted as a reliable stop.
- Cancer association remains `ambiguous` when `ca_seq` cannot resolve it; DOB-relative treatment start can still establish event time.
- Imaging, PSA numeric results, PSA trends in notes, and oncologist response assessments remain separate evidence streams.
- TMB source units are not stated clearly enough for automatic conversion. The raw value and source bin are returned.
- CNA raw calls are preserved, including fractional values; missing values, neutral zero, and alterations are handled separately.
- Mutation/CNA coverage uses the actual supplied profile together with `data_gene_matrix.txt` and sample membership. The release has 102 samples with a declared CNA panel but no corresponding CNA data column; these remain unresolved. Mutation files that are absent also remain unresolved even when a panel is declared.
- CNA missing and neutral gene lists are distinct; a panel-level coverage flag does not turn an `NA` cell into a negative gene result. SV row/case-list membership plus the supplied SV file establishes profiling; absent membership remains unresolved.
- PD-L1 numeric percentages, ranges, and CPS retain their original strings, including zero and `< 1`, and use the individual report date. A recorded `*_yn=No` is separately represented as "not reported on this pathology report" using the procedure-date proxy.

## Implementation review fixes (schema 1.1)

Sequential evidence IDs previously changed identity across cutoffs. Evidence IDs now hash the visible source fields and values; physical CSV row numbers do not enter the identifier when a complete natural key exists. A drug start and its later end use separate references, preserving the start reference after the end becomes visible.

Field rules are executed before reading clinical values into state or evidence. Time rules now execute their configured field transforms and reporting delays. Required clinical inputs fail explicitly when absent or malformed. Optional molecular input availability is reported in coverage issues. Rule resources are included in normal wheel installations.

## Explicit missingness and offline evaluation (schema 1.2)

Nullable clinical fields are serialized as `null`. A separate `field_status` map uses JSON Pointers to distinguish source blanks (`not_recorded`), explicit unknowns (`unknown`), not reported, not tested, not detected, not applicable, not collected, masked duration, policy exclusion, and unperformed normalization. Raw source text remains available; numeric fields never receive an `"unknown"` string. Zero and a bare `No` are not generic missingness. The status map is computed from admitted objects/evidence, not hidden dates or future rows.

`OfflineEvaluationExporter` independently exports actual imaging, oncology assessments, and tumor markers in a specified `(input_cutoff_day, evaluation_end_day]` window. Results for earlier events that become available later are kept in `late_available_observations`. This is observed follow-up, not a predicted state or a counterfactual label. An empty window means no admitted records and does not establish stability or complete follow-up. Only references for selected observations are retained, along with a hash of the exact input state.

The export is not a method on `BoundedPatientTool` and is absent from Agent tool schemas. Its files belong in the ignored `offline_evaluation/` directory, with filesystem access restricted by the application; an unrestricted shell could otherwise read any accessible file. The output schemas reject exchanging an evaluation document for a PatientState or vice versa.

## Dataset-level cautions

- Entry requires genomic sequencing and can create temporal selection bias; this tool does not correct it.
- UHN lacks some NAACCR registry fields.
- Investigational treatment names/durations may be masked by institution-specific rules.
- A documented inter-site imaging curation discrepancy exists for completely resolved metastatic loci.
- cBioPortal mutation views can differ from source processing filters; this implementation reads the supplied mutation file as-is.
