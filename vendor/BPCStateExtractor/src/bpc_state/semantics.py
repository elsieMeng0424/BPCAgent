"""Missing-value semantics derived only from the facts already admitted to a snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .models import FieldStatus
from .rules import RuleSet, integer, number


# Output field -> source column. This also lets policy exclusions remain distinct
# from blanks without inspecting any hidden rows or future facts.
SOURCE_FIELDS = {
    "Demographics": (
        "demographics",
        {
            "birth_year": "birth_year",
            "sex_raw": "naaccr_sex_code",
            "race_raw": "naaccr_race_code_primary",
            "ethnicity_raw": "naaccr_ethnicity_code",
            "institution": "institution",
        },
    ),
    "CancerDescriptor": (
        "cancer",
        {
            "ca_seq_raw": "ca_seq",
            "site_raw": "ca_d_site",
            "histology_raw": "ca_histology",
        },
    ),
    "CancerContext": (
        "cancer",
        {"site_raw": "ca_d_site", "histology_raw": "ca_histology"},
    ),
    "ImagingObservation": (
        "imaging",
        {
            "modality_raw": "image_scan_type",
            "sites_raw": "scan_sites",
            "cancer_evidence_raw": "image_ca",
            "assessment_raw": "image_overall",
            "assessment": "image_overall",
        },
    ),
    "LatestImaging": (
        "imaging",
        {"assessment_raw": "image_overall", "assessment": "image_overall"},
    ),
    "MedOncObservation": (
        "med_onc",
        {
            "cancer_type_raw": "md_type_ca_cur",
            "prostate_status_raw": "md_pca_status",
            "psa_trend_raw": "md_psa_status",
            "cancer_evidence_raw": "md_ca",
            "assessment_raw": "md_ca_status",
        },
    ),
    "TumorMarkerObservation": (
        "tumor_marker",
        {
            "value_raw": "tm_num_result",
            "value_numeric": "tm_num_result",
            "unit_raw": "tm_result_units",
            "normal_range_lower_raw": "tm_normal_range_lower",
            "normal_range_upper_raw": "tm_normal_range_upper",
        },
    ),
    "LatestTumorMarker": (
        "tumor_marker",
        {
            "value_raw": "tm_num_result",
            "value_numeric": "tm_num_result",
            "unit_raw": "tm_result_units",
        },
    ),
    "PathologyReport": (
        "pathology",
        {
            "procedure_type_raw": "path_proc_type",
            "procedure_raw": "path_proc",
            "invasive_cancer_raw": "path_ca_inv_any",
            "gleason_raw": "path_pc_gleason",
        },
    ),
    "MolecularReport": (
        "cpt",
        {"panel_id": "cpt_seq_assay_id", "sample_type_raw": "sample_type"},
    ),
    "Mutation": (
        "mutation",
        {
            "gene": "Hugo_Symbol",
            "protein_change": "HGVSp_Short",
            "variant_classification_raw": "Variant_Classification",
            "chromosome_raw": "Chromosome",
            "start_position_raw": "Start_Position",
            "reference_allele_raw": "Reference_Allele",
            "tumor_allele_raw": "Tumor_Seq_Allele2",
        },
    ),
    "StructuralVariant": (
        "structural_variant",
        {
            "gene_1_raw": "Site1_Hugo_Symbol",
            "gene_2_raw": "Site2_Hugo_Symbol",
            "class_raw": "Class",
            "annotation_raw": "Annotation",
        },
    ),
    "Radiotherapy": ("radiotherapy", {"type_raw": "rt_type"}),
    "TMBResult": ("tmb", {"bin_raw": "tmb_bin"}),
}


def source_status(raw: Any) -> tuple[str, str] | None:
    """Recognize explicit source semantics; a bare No/zero is never generic missingness."""
    if not isinstance(raw, str):
        return None
    token = " ".join(raw.strip().casefold().split())
    if not token:
        return "not_recorded", "blank_source_value"
    exact = {
        "not stated": ("not_reported", "source_explicitly_not_stated"),
        "not mentioned": ("not_reported", "source_explicitly_not_mentioned"),
        "not reported": ("not_reported", "source_explicitly_not_reported"),
        "not tested": ("not_tested", "source_explicitly_not_tested"),
        "not detected": ("not_detected", "source_explicitly_not_detected"),
        "not applicable": ("not_applicable", "source_explicitly_not_applicable"),
        "not collected": ("not_collected", "source_explicitly_not_collected"),
        "na": ("unknown", "ambiguous_source_missing_token"),
        "n/a": ("unknown", "ambiguous_source_missing_token"),
        "nan": ("unknown", "ambiguous_source_missing_token"),
        "null": ("unknown", "ambiguous_source_missing_token"),
        "none": ("unknown", "ambiguous_source_missing_token"),
    }
    if token in exact:
        return exact[token]
    if token.startswith("unknown"):
        return "unknown", "source_explicitly_unknown"
    if token.endswith("not mentioned"):
        return "not_reported", "source_explicitly_not_mentioned"
    if token in {"indeterminate", "not stated/indeterminate"}:
        return "unknown", "source_indeterminate_or_not_stated"
    return None


def field_statuses(model: BaseModel, rules: RuleSet) -> dict[str, FieldStatus]:
    """Return JSON Pointer annotations without reading the repository or follow-up data."""
    evidence = getattr(model, "evidence_refs", {})
    output: dict[str, FieldStatus] = {}

    def source_value(refs: list[str], field: str | None) -> Any:
        for ref in refs:
            record = evidence.get(ref)
            if record is None:
                continue
            for source in record.sources:
                if field in source.values:
                    return source.values[field]
        return None

    def describe(node: BaseModel, key: str, value: Any, refs: list[str]):
        if key == "status_at_cutoff" and value == "unknown":
            return "unknown", "insufficient_evidence_as_of_cutoff"
        if (
            type(node).__name__ == "MolecularCoverage"
            and key in {"mutation", "cna", "sv"}
            and value == "unresolved"
        ):
            return "unknown", "profile_coverage_unresolved"
        if (
            type(node).__name__ == "TMBResult"
            and key == "unit"
            and value == "unconfirmed_source_unit"
        ):
            return "unknown", "source_unit_unconfirmed"
        section, mapping = SOURCE_FIELDS.get(type(node).__name__, (None, {}))
        source_field = mapping.get(key)
        if value is None and source_field and not rules.allowed(section, source_field):
            return "excluded_by_policy", "source_field_not_allowlisted"
        raw = source_value(refs, source_field) if source_field else value
        semantic = source_status(raw)
        if semantic:
            return semantic
        if value is not None:
            return source_status(value)
        if key == "drug_name_normalized":
            return "not_normalized", "raw_drug_name_retained_by_policy"
        if key in {"end_day", "end_day_basis"} and hasattr(node, "status_at_cutoff"):
            if node.duration_masked:
                return "masked", "investigational_duration_masked"
            return "unknown", "insufficient_evidence_as_of_cutoff"
        if key == "cancer_id" and hasattr(node, "cancer_association"):
            return "unknown", "ambiguous_cancer_association"
        if key == "collection_day":
            return "unknown", "collection_day_unavailable_as_of_cutoff"
        if key == "panel_gene_count":
            return "unknown", "panel_gene_list_unresolved"
        if key == "tmb":
            return "not_recorded", "no_admitted_tmb_result"
        if key in {"value_numeric", "birth_year"} and raw is not None:
            parser = integer if key == "birth_year" else number
            if parser(raw) is None:
                if str(raw).lstrip().startswith(("<", ">", "≤", "≥")):
                    return "non_exact", "source_reports_bound_not_exact_value"
                return "invalid", "source_numeric_value_not_parseable"
        return "not_recorded", "no_value_in_admitted_source"

    def escape(part: str) -> str:
        return part.replace("~", "~0").replace("/", "~1")

    def visit(value: Any, pointer: str, inherited_refs: list[str]):
        if isinstance(value, BaseModel):
            own_refs = getattr(value, "evidence_refs", None)
            refs = own_refs if isinstance(own_refs, list) else inherited_refs
            if not refs:
                refs = inherited_refs
            if not refs and hasattr(value, "diagnosis"):
                refs = value.diagnosis.evidence_refs
            for name in type(value).model_fields:
                if name in {
                    "meta",
                    "evidence_refs",
                    "field_status",
                    "missing_information",
                }:
                    continue
                child = getattr(value, name)
                path = f"{pointer}/{escape(name)}"
                if child is None or isinstance(child, str):
                    description = describe(value, name, child, refs)
                    if description:
                        status, reason = description
                        output[path] = FieldStatus(
                            status=status, reason=reason, evidence_refs=refs
                        )
                visit(child, path, refs)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}", inherited_refs)
        elif isinstance(value, dict):
            for name, child in value.items():
                path = f"{pointer}/{escape(str(name))}"
                semantic = source_status(child)
                if semantic:
                    status, reason = semantic
                    output[path] = FieldStatus(
                        status=status, reason=reason, evidence_refs=inherited_refs
                    )
                visit(child, path, inherited_refs)

    visit(model, "", [])
    return output
