from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSource(StrictModel):
    source_file: str
    record_ref: str
    fields: list[str]
    values: dict[str, Any] = Field(default_factory=dict)


class EvidenceRecord(StrictModel):
    sources: list[EvidenceSource]


class StateMeta(StrictModel):
    schema_version: str = "1.2"
    patient_id: str
    cohort: str
    target_cancer_id: str
    cutoff_day: int
    day_origin: Literal["first_bpc_cancer_diagnosis"] = "first_bpc_cancer_diagnosis"
    availability_policy: Literal["documented_or_proxy"] = "documented_or_proxy"


class Demographics(StrictModel):
    birth_year: int | None = None
    sex_raw: str | None = None
    race_raw: str | None = None
    ethnicity_raw: str | None = None
    institution: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class TimedValue(StrictModel):
    value: Any
    event_day: int
    available_day: int
    available_day_basis: str
    evidence_refs: list[str]


class CancerDescriptor(StrictModel):
    cancer_id: str
    ca_seq_raw: str | None = None
    index_status: Literal["index", "non_index"]
    diagnosis: TimedValue
    site_raw: str | None = None
    histology_raw: str | None = None
    evidence_refs: list[str]


class CancerContext(StrictModel):
    target_cancer_id: str
    diagnosis: TimedValue
    site_raw: str | None = None
    histology_raw: str | None = None
    known_cancers: list[CancerDescriptor] = Field(default_factory=list)


class LastTreatmentEvent(StrictModel):
    type: Literal["start", "end"]
    event_day: int
    available_day: int
    available_day_basis: str


class Treatment(StrictModel):
    treatment_id: str
    regimen_id: str
    cancer_id: str | None = None
    cancer_association: Literal["target", "other", "ambiguous"]
    drug_name_raw: str
    drug_name_normalized: str | None = None
    start_day: int
    end_day: int | None = None
    end_day_basis: str | None = None
    status_at_cutoff: Literal["ended", "unknown"]
    duration_masked: bool = False
    last_known_treatment_event: LastTreatmentEvent
    evidence_refs: list[str]


class Radiotherapy(StrictModel):
    radiotherapy_id: str
    cancer_id: str | None = None
    cancer_association: Literal["target", "other", "ambiguous"]
    event_day: int
    available_day: int
    available_day_basis: str
    type_raw: str | None = None
    site_values_raw: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str]


class BiomarkerTesting(StrictModel):
    biomarker: Literal["PD-L1", "MSI", "MMR"]
    test_number: int
    reported_raw: str
    meaning: Literal["not_reported_on_pathology_report"] = (
        "not_reported_on_pathology_report"
    )
    event_day: int
    available_day: int
    available_day_basis: str = "procedure_date_proxy"
    evidence_refs: list[str]


class BiomarkerResult(StrictModel):
    biomarker: Literal["PD-L1", "MSI", "MMR"]
    test_number: int
    event_day: int
    available_day: int
    available_day_basis: Literal["biomarker_report_date"] = "biomarker_report_date"
    test_reported_raw: str
    result_raw: str | None = None
    details_raw: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str]


class PathologyReport(StrictModel):
    pathology_id: str
    event_day: int
    available_day: int
    available_day_basis: Literal["procedure_date_proxy"] = "procedure_date_proxy"
    procedure_type_raw: str | None = None
    procedure_raw: str | None = None
    invasive_cancer_raw: str | None = None
    gleason_raw: str | None = None
    biomarkers: list[BiomarkerResult] = Field(default_factory=list)
    biomarker_testing: list[BiomarkerTesting] = Field(default_factory=list)
    evidence_refs: list[str]


class Mutation(StrictModel):
    gene: str | None = None
    protein_change: str | None = None
    variant_classification_raw: str | None = None
    chromosome_raw: str | None = None
    start_position_raw: str | None = None
    reference_allele_raw: str | None = None
    tumor_allele_raw: str | None = None
    evidence_refs: list[str]


class CopyNumberAlteration(StrictModel):
    gene: str
    value_raw: str
    value_numeric: float | None = None
    evidence_refs: list[str]


class StructuralVariant(StrictModel):
    gene_1_raw: str | None = None
    gene_2_raw: str | None = None
    class_raw: str | None = None
    annotation_raw: str | None = None
    evidence_refs: list[str]


class TMBResult(StrictModel):
    value_raw: str
    unit: Literal["unconfirmed_source_unit"] = "unconfirmed_source_unit"
    bin_raw: str | None = None
    evidence_refs: list[str]


class MolecularCoverage(StrictModel):
    mutation: Literal["profiled", "not_profiled", "unresolved"]
    cna: Literal["profiled", "not_profiled", "unresolved"]
    sv: Literal["profiled", "not_profiled", "unresolved"]
    panel_gene_count: int | None = None
    cna_missing_genes: list[str] = Field(default_factory=list)
    cna_neutral_genes: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class MolecularReport(StrictModel):
    molecular_report_id: str
    sample_id: str
    cancer_id: str | None = None
    cancer_association: Literal["target", "other", "ambiguous"]
    collection_day: int | None = None
    available_day: int
    available_day_basis: Literal["ngs_report_date"] = "ngs_report_date"
    panel_id: str | None = None
    sample_type_raw: str | None = None
    mutations: list[Mutation] = Field(default_factory=list)
    copy_number_alterations: list[CopyNumberAlteration] = Field(default_factory=list)
    structural_variants: list[StructuralVariant] = Field(default_factory=list)
    tmb: TMBResult | None = None
    coverage: MolecularCoverage
    evidence_refs: list[str]


class ImagingObservation(StrictModel):
    imaging_id: str
    event_day: int
    available_day: int
    available_day_basis: Literal["scan_date_proxy"] = "scan_date_proxy"
    modality_raw: str | None = None
    sites_raw: str | None = None
    cancer_evidence_raw: str | None = None
    assessment_raw: str | None = None
    assessment: (
        Literal["responding", "stable", "progressing", "indeterminate", "other"] | None
    ) = None
    evidence_refs: list[str]


class MedOncObservation(StrictModel):
    med_onc_id: str
    event_day: int
    available_day: int
    available_day_basis: Literal["visit_date_proxy"] = "visit_date_proxy"
    cancer_type_raw: str | None = None
    prostate_status_raw: str | None = None
    psa_trend_raw: str | None = None
    cancer_evidence_raw: str | None = None
    assessment_raw: str | None = None
    evidence_refs: list[str]


class TumorMarkerObservation(StrictModel):
    tumor_marker_id: str
    event_day: int
    available_day: int
    available_day_basis: Literal["collection_date_proxy"] = "collection_date_proxy"
    analyte_raw: str
    value_raw: str | None = None
    value_numeric: float | None = None
    unit_raw: str | None = None
    normal_range_lower_raw: str | None = None
    normal_range_upper_raw: str | None = None
    evidence_refs: list[str]


class Observations(StrictModel):
    imaging: list[ImagingObservation] = Field(default_factory=list)
    med_onc: list[MedOncObservation] = Field(default_factory=list)
    tumor_markers: list[TumorMarkerObservation] = Field(default_factory=list)


class LatestImaging(StrictModel):
    event_day: int
    assessment_raw: str | None = None
    assessment: str | None = None
    evidence_refs: list[str]


class LatestTumorMarker(StrictModel):
    analyte_raw: str
    event_day: int
    value_raw: str | None = None
    value_numeric: float | None = None
    unit_raw: str | None = None
    evidence_refs: list[str]


class CurrentSummary(StrictModel):
    latest_imaging: list[LatestImaging] = Field(default_factory=list)
    latest_tumor_markers: list[LatestTumorMarker] = Field(default_factory=list)


class MissingInformation(StrictModel):
    field: str
    reason: str
    record_ref: str | None = None


class FieldStatus(StrictModel):
    status: Literal[
        "unknown",
        "not_recorded",
        "not_reported",
        "not_tested",
        "not_detected",
        "not_applicable",
        "not_collected",
        "invalid",
        "non_exact",
        "excluded_by_policy",
        "not_normalized",
        "masked",
    ]
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class PatientState(StrictModel):
    meta: StateMeta
    demographics: Demographics
    cancer_context: CancerContext
    treatment_history: list[Treatment] = Field(default_factory=list)
    radiotherapy_history: list[Radiotherapy] = Field(default_factory=list)
    pathology: list[PathologyReport] = Field(default_factory=list)
    molecular_reports: list[MolecularReport] = Field(default_factory=list)
    observations: Observations
    current_summary: CurrentSummary
    missing_information: list[MissingInformation] = Field(default_factory=list)
    evidence_refs: dict[str, EvidenceRecord] = Field(default_factory=dict)
    field_status: dict[str, FieldStatus] = Field(default_factory=dict)


class EvaluationMeta(StrictModel):
    schema_version: str = "1.2"
    purpose: Literal["offline_evaluation"] = "offline_evaluation"
    patient_id: str
    cohort: str
    target_cancer_id: str
    input_cutoff_day: int
    evaluation_end_day: int
    input_state_sha256: str
    day_origin: Literal["first_bpc_cancer_diagnosis"] = "first_bpc_cancer_diagnosis"
    interval: Literal["(input_cutoff_day, evaluation_end_day]"] = (
        "(input_cutoff_day, evaluation_end_day]"
    )


class EvaluationData(StrictModel):
    meta: EvaluationMeta
    # Actual records, not predictions or counterfactual treatment labels.
    observations: Observations
    late_available_observations: Observations
    window_status: dict[str, Literal["recorded", "none_recorded_in_window"]]
    evidence_refs: dict[str, EvidenceRecord] = Field(default_factory=dict)
    field_status: dict[str, FieldStatus] = Field(default_factory=dict)


class AuditExclusion(StrictModel):
    source_file: str
    record_ref: str
    reason: str
    fields: list[str] = Field(default_factory=list)


class AuditReport(StrictModel):
    patient_id: str
    cutoff_day: int
    exclusions: list[AuditExclusion] = Field(default_factory=list)
