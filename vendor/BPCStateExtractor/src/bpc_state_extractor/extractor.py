from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from typing import Any, Iterable

from .models import (
    AuditExclusion,
    AuditReport,
    BiomarkerResult,
    BiomarkerTesting,
    CancerContext,
    CancerDescriptor,
    CopyNumberAlteration,
    CurrentSummary,
    Demographics,
    EvidenceRecord,
    EvidenceSource,
    ImagingObservation,
    LastTreatmentEvent,
    LatestImaging,
    LatestTumorMarker,
    MedOncObservation,
    MissingInformation,
    MolecularCoverage,
    MolecularReport,
    Mutation,
    Observations,
    PathologyReport,
    PatientState,
    Radiotherapy,
    StateMeta,
    StructuralVariant,
    TMBResult,
    TimedValue,
    Treatment,
    TumorMarkerObservation,
)
from .repository import CLINICAL_FILES, DataRepository, is_present, opaque_record_ref
from .rules import RuleSet, SECTION_BY_TABLE, integer, number
from .semantics import field_statuses


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "dataset"


def _to_float(value: object) -> float | None:
    return number(value)


def _to_int(value: object) -> int | None:
    return integer(value)


def _text(value: object) -> str | None:
    return str(value).strip() if is_present(value) else None


def _cancer_id(index_status: str, ca_seq: str | None, row: dict[str, str]) -> str:
    seq = (
        ca_seq
        if ca_seq is not None
        else opaque_record_ref("cancer", row, ["__row_number"])
    )
    return f"{index_status}:{seq}"


class _EvidenceBuilder:
    def __init__(self) -> None:
        self.records: dict[str, EvidenceRecord] = {}
        self._by_signature: dict[str, str] = {}

    def add(self, sources: list[EvidenceSource]) -> str:
        signature = json.dumps(
            [s.model_dump(mode="json") for s in sources], sort_keys=True
        )
        if signature in self._by_signature:
            return self._by_signature[signature]
        evidence_id = "E-" + hashlib.sha256(signature.encode()).hexdigest()[:24]
        self.records[evidence_id] = EvidenceRecord(sources=sources)
        self._by_signature[signature] = evidence_id
        return evidence_id


class BPCStateExtractor:
    """Extract deterministic facts visible on or before a relative cutoff day."""

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        rules_path: str | Path | None = None,
    ):
        self.repository = DataRepository(data_dir)
        self.rule_set = RuleSet(rules_path)
        self.rules = self.rule_set.config
        self.last_audit: AuditReport | None = None

    def _rows(self, table: str, cohort: str, patient_id: str) -> list[dict[str, str]]:
        return [
            self.rule_set.project(SECTION_BY_TABLE[table], row)
            for row in self.repository.clinical_rows(table, cohort, patient_id)
        ]

    @staticmethod
    def _row_ref(filename: str, row: dict[str, str], keys: Iterable[str]) -> str:
        natural_keys = [key for key in keys if key != "__row_number"]
        # Physical line numbers are used only when no complete natural identity exists.
        identity = (
            natural_keys
            if natural_keys and all(is_present(row.get(k)) for k in natural_keys)
            else [*natural_keys, "__row_number"]
        )
        return opaque_record_ref(filename, row, identity)

    def _source(
        self,
        filename: str,
        row: dict[str, str],
        keys: Iterable[str],
        fields: Iterable[str],
    ) -> EvidenceSource:
        visible_fields = sorted(
            set(
                f
                for f in fields
                if f in row and row.get(f) is not None and str(row[f]).strip()
            )
        )
        return EvidenceSource(
            source_file=filename,
            record_ref=self._row_ref(filename, row, keys),
            fields=visible_fields,
            values={field: row[field] for field in visible_fields},
        )

    def get_patient_state(
        self, patient_id: str, cutoff_day: int, cancer_id: str | None = None
    ) -> dict[str, Any]:
        if isinstance(cutoff_day, bool) or not isinstance(cutoff_day, int):
            raise TypeError("cutoff_day must be an integer")
        cohort, patient = self.repository.find_patient(patient_id)
        patient = self.rule_set.project("demographics", patient)
        evidence = _EvidenceBuilder()
        audit = AuditReport(patient_id=patient_id, cutoff_day=cutoff_day)
        missing: list[MissingInformation] = []

        index_rows = self._rows("index_cancers", cohort, patient_id)
        if not index_rows:
            raise ValueError(f"Patient has no BPC index cancer: {patient_id}")

        dobs = [_to_int(r.get("dob_ca_dx_days")) for r in index_rows]
        origin_dob = min((x for x in dobs if x is not None), default=None)

        def diagnosis_day(row: dict[str, str]) -> int | None:
            return self.rule_set.event(row, "cancer_diagnosis", birth=origin_dob)

        indexed: list[tuple[str, dict[str, str], int | None]] = []
        for row in index_rows:
            cid = _cancer_id("index", _text(row.get("ca_seq")), row)
            indexed.append((cid, row, diagnosis_day(row)))
        indexed.sort(key=lambda item: (item[2] is None, item[2] or 0, item[0]))
        target_id = cancer_id or indexed[0][0]
        if target_id not in {item[0] for item in indexed}:
            # Allow a raw ca_seq as a convenience without assuming that it is zero.
            matches = [
                item[0]
                for item in indexed
                if _text(item[1].get("ca_seq")) == str(target_id)
            ]
            if len(matches) == 1:
                target_id = matches[0]
            else:
                raise KeyError(f"Unknown index cancer_id for patient: {cancer_id}")
        target_day = next(day for cid, _, day in indexed if cid == target_id)
        if (
            target_day is None
            or self.rule_set.available(target_day, "cancer_diagnosis") > cutoff_day
        ):
            raise ValueError("Target cancer diagnosis is not available at cutoff")

        patient_source = self._source(
            CLINICAL_FILES["patients"],
            patient,
            ["cohort", "record_id"],
            self.rules["demographics"],
        )
        patient_eid = evidence.add([patient_source])
        demographics = Demographics(
            birth_year=_to_int(patient.get("birth_year")),
            sex_raw=_text(patient.get("naaccr_sex_code")),
            race_raw=_text(patient.get("naaccr_race_code_primary")),
            ethnicity_raw=_text(patient.get("naaccr_ethnicity_code")),
            institution=_text(patient.get("institution")),
            evidence_refs=[patient_eid],
        )

        known_cancers: list[CancerDescriptor] = []
        cancer_by_seq: dict[str, tuple[str, int | None]] = {}
        all_cancer_rows = [("index", row) for row in index_rows]
        all_cancer_rows += [
            ("non_index", row)
            for row in self._rows("non_index_cancers", cohort, patient_id)
        ]
        for status, row in all_cancer_rows:
            day = diagnosis_day(row)
            filename = CLINICAL_FILES[f"{status}_cancers"]
            ref = self._row_ref(filename, row, ["cohort", "record_id", "ca_seq"])
            if day is None:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="unknown_diagnosis_day",
                    )
                )
                continue
            available = self.rule_set.available(day, "cancer_diagnosis")
            if available > cutoff_day:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename, record_ref=ref, reason="after_cutoff"
                    )
                )
                continue
            cid = _cancer_id(status, _text(row.get("ca_seq")), row)
            src = self._source(
                filename, row, ["cohort", "record_id", "ca_seq"], self.rules["cancer"]
            )
            eid = evidence.add([src])
            diagnosis = TimedValue(
                value=_text(row.get("ca_type")) or "Cancer diagnosis",
                event_day=day,
                available_day=available,
                available_day_basis=self.rule_set.basis("cancer_diagnosis"),
                evidence_refs=[eid],
            )
            descriptor = CancerDescriptor(
                cancer_id=cid,
                ca_seq_raw=_text(row.get("ca_seq")),
                index_status=status,
                diagnosis=diagnosis,
                site_raw=_text(row.get("ca_d_site")),
                histology_raw=_text(row.get("ca_histology")),
                evidence_refs=[eid],
            )
            known_cancers.append(descriptor)
            if is_present(row.get("ca_seq")):
                cancer_by_seq[str(row["ca_seq"]).strip()] = (cid, day)

        target_descriptor = next(c for c in known_cancers if c.cancer_id == target_id)
        cancer_context = CancerContext(
            target_cancer_id=target_id,
            diagnosis=target_descriptor.diagnosis,
            site_raw=target_descriptor.site_raw,
            histology_raw=target_descriptor.histology_raw,
            known_cancers=sorted(
                known_cancers, key=lambda x: (x.diagnosis.event_day, x.cancer_id)
            ),
        )

        def association(row: dict[str, str]) -> tuple[str | None, str, int | None]:
            seq = _text(row.get("ca_seq"))
            if seq is None or seq not in cancer_by_seq:
                return None, "ambiguous", None
            cid, offset = cancer_by_seq[seq]
            return cid, "target" if cid == target_id else "other", offset

        treatments: list[Treatment] = []
        regimen_file = CLINICAL_FILES["regimens"]
        for row in self._rows("regimens", cohort, patient_id):
            cid, assoc, cancer_offset = association(row)
            if assoc == "other":
                continue
            regimen_number = _text(row.get("regimen_number")) or self._row_ref(
                regimen_file, row, ["__row_number"]
            )
            regimen_id = f"regimen:{cid or 'ambiguous'}:{regimen_number}"
            for slot in range(1, 6):
                drug_field = f"drugs_drug_{slot}"
                drug = _text(row.get(drug_field))
                if drug is None:
                    continue
                start = self.rule_set.event(
                    row, "drug_start", birth=origin_dob, cancer=cancer_offset, slot=slot
                )
                record_ref = self._row_ref(
                    regimen_file, row, ["cohort", "record_id", "regimen_number"]
                )
                if start is None:
                    audit.exclusions.append(
                        AuditExclusion(
                            source_file=regimen_file,
                            record_ref=record_ref,
                            reason="unknown_drug_start_day",
                            fields=[drug_field],
                        )
                    )
                    continue
                start_available = self.rule_set.available(start, "drug_start")
                if start_available > cutoff_day:
                    audit.exclusions.append(
                        AuditExclusion(
                            source_file=regimen_file,
                            record_ref=record_ref,
                            reason="drug_start_after_cutoff",
                            fields=[drug_field, f"dx_drug_start_int_{slot}"],
                        )
                    )
                    continue
                masked = drug.casefold() == "investigational drug"
                end = self.rule_set.event(
                    row, "drug_end", cancer=cancer_offset, slot=slot
                )
                end_available = (
                    self.rule_set.available(end, "drug_end")
                    if end is not None
                    else None
                )
                end_visible = (
                    end is not None
                    and end_available <= cutoff_day
                    and not masked
                    and end >= start
                )
                fields = [
                    drug_field,
                    f"drugs_startdt_int_{slot}",
                    f"dx_drug_start_int_{slot}",
                ]
                src = self._source(
                    regimen_file,
                    row,
                    ["cohort", "record_id", "regimen_number"],
                    fields + ["ca_seq", "regimen_number"],
                )
                eid = evidence.add([src])
                refs = [eid]
                if end_visible:
                    refs.append(
                        evidence.add(
                            [
                                self._source(
                                    regimen_file,
                                    row,
                                    ["cohort", "record_id", "regimen_number"],
                                    [drug_field, f"dx_drug_end_int_{slot}"],
                                )
                            ]
                        )
                    )
                    last = LastTreatmentEvent(
                        type="end",
                        event_day=end,
                        available_day=end_available,
                        available_day_basis=self.rule_set.basis("drug_end"),
                    )
                    status = "ended"
                else:
                    last = LastTreatmentEvent(
                        type="start",
                        event_day=start,
                        available_day=start_available,
                        available_day_basis=self.rule_set.basis("drug_start"),
                    )
                    status = "unknown"
                    reason = (
                        "masked_investigational_duration"
                        if masked
                        else "insufficient_evidence_as_of_cutoff"
                    )
                    missing.append(
                        MissingInformation(
                            field=f"treatment_history.{regimen_id}.drug_{slot}.status_at_cutoff",
                            reason=reason,
                            record_ref=record_ref,
                        )
                    )
                treatments.append(
                    Treatment(
                        treatment_id=f"{regimen_id}:drug:{slot}",
                        regimen_id=regimen_id,
                        cancer_id=cid,
                        cancer_association=assoc,
                        drug_name_raw=drug,
                        start_day=start,
                        end_day=end if end_visible else None,
                        end_day_basis=(
                            self.rule_set.basis("drug_end") if end_visible else None
                        ),
                        status_at_cutoff=status,
                        duration_masked=masked,
                        last_known_treatment_event=last,
                        evidence_refs=refs,
                    )
                )

        radiotherapy: list[Radiotherapy] = []
        rt_file = CLINICAL_FILES["radiotherapy"]
        for row in self._rows("radiotherapy", cohort, patient_id):
            cid, assoc, cancer_offset = association(row)
            if assoc == "other":
                continue
            day = self.rule_set.event(row, "radiotherapy", cancer=cancer_offset)
            ref = self._row_ref(rt_file, row, ["cohort", "record_id", "rt_number"])
            available = (
                self.rule_set.available(day, "radiotherapy")
                if day is not None
                else None
            )
            if day is None or available > cutoff_day:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=rt_file,
                        record_ref=ref,
                        reason="unknown_day" if day is None else "after_cutoff",
                    )
                )
                continue
            site_fields = [
                k
                for k, value in row.items()
                if k.startswith("rt_site___")
                and is_present(value)
                and str(value).strip() not in {"0", "Unchecked"}
            ]
            site_values = {key: str(row[key]) for key in site_fields}
            fields = [
                "ca_seq",
                "rt_number",
                "dx_rt_start_days",
                "rt_type",
                *site_fields,
            ]
            eid = evidence.add(
                [
                    self._source(
                        rt_file, row, ["cohort", "record_id", "rt_number"], fields
                    )
                ]
            )
            audit.exclusions.append(
                AuditExclusion(
                    source_file=rt_file,
                    record_ref=ref,
                    reason="radiotherapy_summary_availability_unknown",
                    fields=self.rules["radiotherapy_summary_exclusions"],
                )
            )
            radiotherapy.append(
                Radiotherapy(
                    radiotherapy_id=f"rt:{_text(row.get('rt_number')) or ref}",
                    cancer_id=cid,
                    cancer_association=assoc,
                    event_day=day,
                    available_day=available,
                    available_day_basis=self.rule_set.basis("radiotherapy"),
                    type_raw=_text(row.get("rt_type")),
                    site_values_raw=site_values,
                    evidence_refs=[eid],
                )
            )

        pathology = self._pathology(
            cohort, patient_id, cutoff_day, origin_dob, evidence, audit
        )
        imaging = self._imaging(cohort, patient_id, cutoff_day, evidence, audit)
        med_onc = self._med_onc(cohort, patient_id, cutoff_day, evidence, audit)
        tumor_markers = self._tumor_markers(
            cohort, patient_id, cutoff_day, evidence, audit
        )
        molecular = self._molecular(
            cohort,
            patient_id,
            cutoff_day,
            origin_dob,
            cancer_by_seq,
            target_id,
            pathology,
            evidence,
            audit,
            missing,
        )

        max_image_day = max((x.event_day for x in imaging), default=None)
        latest_imaging = [
            LatestImaging(
                event_day=x.event_day,
                assessment_raw=x.assessment_raw,
                assessment=x.assessment,
                evidence_refs=x.evidence_refs,
            )
            for x in imaging
            if x.event_day == max_image_day
        ]
        latest_markers: list[LatestTumorMarker] = []
        for analyte in sorted({x.analyte_raw for x in tumor_markers}):
            subset = [x for x in tumor_markers if x.analyte_raw == analyte]
            latest_day = max(x.event_day for x in subset)
            latest_markers.extend(
                LatestTumorMarker(
                    analyte_raw=x.analyte_raw,
                    event_day=x.event_day,
                    value_raw=x.value_raw,
                    value_numeric=x.value_numeric,
                    unit_raw=x.unit_raw,
                    evidence_refs=x.evidence_refs,
                )
                for x in subset
                if x.event_day == latest_day
            )

        state = PatientState(
            meta=StateMeta(
                patient_id=patient_id,
                cohort=cohort,
                target_cancer_id=target_id,
                cutoff_day=cutoff_day,
            ),
            demographics=demographics,
            cancer_context=cancer_context,
            treatment_history=sorted(
                treatments, key=lambda x: (x.start_day, x.treatment_id)
            ),
            radiotherapy_history=sorted(
                radiotherapy, key=lambda x: (x.event_day, x.radiotherapy_id)
            ),
            pathology=sorted(pathology, key=lambda x: (x.event_day, x.pathology_id)),
            molecular_reports=sorted(
                molecular, key=lambda x: (x.available_day, x.sample_id)
            ),
            observations=Observations(
                imaging=sorted(imaging, key=lambda x: (x.event_day, x.imaging_id)),
                med_onc=sorted(med_onc, key=lambda x: (x.event_day, x.med_onc_id)),
                tumor_markers=sorted(
                    tumor_markers, key=lambda x: (x.event_day, x.tumor_marker_id)
                ),
            ),
            current_summary=CurrentSummary(
                latest_imaging=latest_imaging, latest_tumor_markers=latest_markers
            ),
            missing_information=missing,
            evidence_refs=evidence.records,
        )
        self.last_audit = audit
        state.field_status = field_statuses(state, self.rule_set)
        return state.model_dump(mode="json")

    def _pathology(
        self,
        cohort: str,
        patient_id: str,
        cutoff: int,
        origin_dob: int | None,
        evidence: _EvidenceBuilder,
        audit: AuditReport,
    ) -> list[PathologyReport]:
        output: list[PathologyReport] = []
        filename = CLINICAL_FILES["pathology"]
        for row in self._rows("pathology", cohort, patient_id):
            ref = self._row_ref(
                filename,
                row,
                ["cohort", "record_id", "path_proc_number", "path_rep_number"],
            )
            day = self.rule_set.event(row, "pathology", birth=origin_dob)
            available = (
                self.rule_set.available(day, "pathology") if day is not None else None
            )
            if day is None or available > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason=(
                            "unknown_procedure_day" if day is None else "after_cutoff"
                        ),
                    )
                )
                continue
            base_fields = [
                "path_proc_number",
                "path_rep_number",
                "dx_path_proc_days",
                "path_proc_int",
                "path_proc_type",
                "path_proc",
                "path_ca_inv_any",
                "path_pc_gleason",
            ]
            base_eid = evidence.add(
                [
                    self._source(
                        filename,
                        row,
                        ["cohort", "record_id", "path_proc_number", "path_rep_number"],
                        base_fields,
                    )
                ]
            )
            biomarkers: list[BiomarkerResult] = []
            testing: list[BiomarkerTesting] = []
            for biomarker, prefix, result_field, detail_prefixes in [
                (
                    "PD-L1",
                    "pdl1",
                    "sum",
                    [
                        "test",
                        "perc",
                        "tclrange",
                        "tcurange",
                        "icperc",
                        "iclrange",
                        "icurange",
                        "num",
                        "lcpsrange",
                        "ucpsrange",
                    ],
                ),
                ("MSI", "msi", "result", ["high", "low"]),
                ("MMR", "mmr", "result", ["mlh1", "msh2", "msh6", "pms2"]),
            ]:
                for number, suffix in enumerate(["", "_2", "_3"], start=1):
                    yn_field = f"{prefix}_yn{suffix}"
                    reported = (_text(row.get(yn_field)) or "").casefold()
                    if reported == "no":
                        eid = evidence.add(
                            [
                                self._source(
                                    filename,
                                    row,
                                    [
                                        "cohort",
                                        "record_id",
                                        "path_proc_number",
                                        "path_rep_number",
                                    ],
                                    [yn_field, "dx_path_proc_days", "path_proc_int"],
                                )
                            ]
                        )
                        testing.append(
                            BiomarkerTesting(
                                biomarker=biomarker,
                                test_number=number,
                                reported_raw=str(row[yn_field]),
                                event_day=day,
                                available_day=available,
                                available_day_basis=self.rule_set.basis("pathology"),
                                evidence_refs=[eid],
                            )
                        )
                        continue
                    if reported != "yes":
                        continue
                    report_field = f"{prefix}_prepaint{suffix}"
                    report_day = self.rule_set.event(
                        row,
                        "pathology_biomarker",
                        birth=origin_dob,
                        prefix=prefix,
                        suffix=suffix,
                    )
                    if report_day is None:
                        audit.exclusions.append(
                            AuditExclusion(
                                source_file=filename,
                                record_ref=ref,
                                reason=f"unknown_{prefix}_report_day",
                                fields=[yn_field],
                            )
                        )
                        continue
                    report_available = self.rule_set.available(
                        report_day, "pathology_biomarker"
                    )
                    if report_available > cutoff:
                        audit.exclusions.append(
                            AuditExclusion(
                                source_file=filename,
                                record_ref=ref,
                                reason=f"{prefix}_report_after_cutoff",
                                fields=[yn_field, report_field],
                            )
                        )
                        continue
                    result_name = f"{prefix}_{result_field}{suffix}"
                    details = {
                        name: _text(row.get(name))
                        for name in [f"{prefix}_{d}{suffix}" for d in detail_prefixes]
                        if _text(row.get(name)) is not None
                    }
                    if prefix == "pdl1":
                        type_prefix = f"pdl1_type{suffix}___"
                        details.update(
                            {
                                key: value
                                for key, value in row.items()
                                if key.startswith(type_prefix) and is_present(value)
                            }
                        )
                    fields = [yn_field, report_field, result_name, *details]
                    eid = evidence.add(
                        [
                            self._source(
                                filename,
                                row,
                                [
                                    "cohort",
                                    "record_id",
                                    "path_proc_number",
                                    "path_rep_number",
                                ],
                                fields,
                            )
                        ]
                    )
                    biomarkers.append(
                        BiomarkerResult(
                            biomarker=biomarker,
                            test_number=number,
                            event_day=report_day,
                            available_day=report_available,
                            available_day_basis=self.rule_set.basis(
                                "pathology_biomarker"
                            ),
                            test_reported_raw=str(row[yn_field]),
                            result_raw=_text(row.get(result_name)),
                            details_raw=details,
                            evidence_refs=[eid],
                        )
                    )
            output.append(
                PathologyReport(
                    pathology_id=f"path:{_text(row.get('path_proc_number')) or '?'}:{_text(row.get('path_rep_number')) or '?'}",
                    event_day=day,
                    available_day=available,
                    available_day_basis=self.rule_set.basis("pathology"),
                    procedure_type_raw=_text(row.get("path_proc_type")),
                    procedure_raw=_text(row.get("path_proc")),
                    invasive_cancer_raw=_text(row.get("path_ca_inv_any")),
                    gleason_raw=_text(row.get("path_pc_gleason")),
                    biomarkers=biomarkers,
                    biomarker_testing=testing,
                    evidence_refs=[base_eid],
                )
            )
        return output

    def _imaging(
        self,
        cohort: str,
        patient_id: str,
        cutoff: int,
        evidence: _EvidenceBuilder,
        audit: AuditReport,
    ) -> list[ImagingObservation]:
        output: list[ImagingObservation] = []
        filename = CLINICAL_FILES["imaging"]
        fields = [
            "scan_number",
            "dx_scan_days",
            "image_scan_type",
            "scan_sites",
            "image_ca",
            "image_overall",
        ]
        for row in self._rows("imaging", cohort, patient_id):
            ref = self._row_ref(filename, row, ["cohort", "record_id", "scan_number"])
            day = self.rule_set.event(row, "imaging")
            available = (
                self.rule_set.available(day, "imaging") if day is not None else None
            )
            if day is None or available > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="unknown_scan_day" if day is None else "after_cutoff",
                    )
                )
                continue
            eid = evidence.add(
                [
                    self._source(
                        filename, row, ["cohort", "record_id", "scan_number"], fields
                    )
                ]
            )
            raw = _text(row.get("image_overall"))
            output.append(
                ImagingObservation(
                    imaging_id=f"image:{_text(row.get('scan_number')) or ref}",
                    event_day=day,
                    available_day=available,
                    available_day_basis=self.rule_set.basis("imaging"),
                    modality_raw=_text(row.get("image_scan_type")),
                    sites_raw=_text(row.get("scan_sites")),
                    cancer_evidence_raw=_text(row.get("image_ca")),
                    assessment_raw=raw,
                    assessment=self.rule_set.assessment(raw),
                    evidence_refs=[eid],
                )
            )
        return output

    def _med_onc(
        self,
        cohort: str,
        patient_id: str,
        cutoff: int,
        evidence: _EvidenceBuilder,
        audit: AuditReport,
    ) -> list[MedOncObservation]:
        output: list[MedOncObservation] = []
        filename = CLINICAL_FILES["med_onc"]
        fields = [
            "md_visit_number",
            "dx_md_visit_days",
            "md_type_ca_cur",
            "md_pca_status",
            "md_psa_status",
            "md_ca",
            "md_ca_status",
        ]
        for row in self._rows("med_onc", cohort, patient_id):
            ref = self._row_ref(
                filename, row, ["cohort", "record_id", "md_visit_number"]
            )
            day = self.rule_set.event(row, "med_onc")
            available = (
                self.rule_set.available(day, "med_onc") if day is not None else None
            )
            if day is None or available > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="unknown_visit_day" if day is None else "after_cutoff",
                    )
                )
                continue
            eid = evidence.add(
                [
                    self._source(
                        filename,
                        row,
                        ["cohort", "record_id", "md_visit_number"],
                        fields,
                    )
                ]
            )
            output.append(
                MedOncObservation(
                    med_onc_id=f"medonc:{_text(row.get('md_visit_number')) or ref}",
                    event_day=day,
                    available_day=available,
                    available_day_basis=self.rule_set.basis("med_onc"),
                    cancer_type_raw=_text(row.get("md_type_ca_cur")),
                    prostate_status_raw=_text(row.get("md_pca_status")),
                    psa_trend_raw=_text(row.get("md_psa_status")),
                    cancer_evidence_raw=_text(row.get("md_ca")),
                    assessment_raw=_text(row.get("md_ca_status")),
                    evidence_refs=[eid],
                )
            )
        return output

    def _tumor_markers(
        self,
        cohort: str,
        patient_id: str,
        cutoff: int,
        evidence: _EvidenceBuilder,
        audit: AuditReport,
    ) -> list[TumorMarkerObservation]:
        output: list[TumorMarkerObservation] = []
        filename = CLINICAL_FILES["tumor_markers"]
        fields = [
            "tm_number",
            "dx_tm_days",
            "tm_type",
            "tm_num_result",
            "tm_result_units",
            "tm_normal_range_lower",
            "tm_normal_range_upper",
        ]
        for row in self._rows("tumor_markers", cohort, patient_id):
            ref = self._row_ref(filename, row, ["cohort", "record_id", "tm_number"])
            day = self.rule_set.event(row, "tumor_marker")
            available = (
                self.rule_set.available(day, "tumor_marker")
                if day is not None
                else None
            )
            if day is None or available > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason=(
                            "unknown_collection_day" if day is None else "after_cutoff"
                        ),
                    )
                )
                continue
            analyte = _text(row.get("tm_type"))
            if analyte is None:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename, record_ref=ref, reason="missing_analyte"
                    )
                )
                continue
            eid = evidence.add(
                [
                    self._source(
                        filename, row, ["cohort", "record_id", "tm_number"], fields
                    )
                ]
            )
            output.append(
                TumorMarkerObservation(
                    tumor_marker_id=f"tm:{_text(row.get('tm_number')) or ref}",
                    event_day=day,
                    available_day=available,
                    available_day_basis=self.rule_set.basis("tumor_marker"),
                    analyte_raw=analyte,
                    value_raw=_text(row.get("tm_num_result")),
                    value_numeric=_to_float(row.get("tm_num_result")),
                    unit_raw=_text(row.get("tm_result_units")),
                    normal_range_lower_raw=_text(row.get("tm_normal_range_lower")),
                    normal_range_upper_raw=_text(row.get("tm_normal_range_upper")),
                    evidence_refs=[eid],
                )
            )
        return output

    def _molecular(
        self,
        cohort: str,
        patient_id: str,
        cutoff: int,
        origin_dob: int | None,
        cancer_by_seq: dict[str, tuple[str, int | None]],
        target_id: str,
        pathology: list[PathologyReport],
        evidence: _EvidenceBuilder,
        audit: AuditReport,
        missing: list[MissingInformation],
    ) -> list[MolecularReport]:
        output: list[MolecularReport] = []
        filename = CLINICAL_FILES["cpt"]
        visible_path = {x.pathology_id: x for x in pathology}
        cpt_rows = self._rows("cpt", cohort, patient_id)
        for row in cpt_rows:
            ref = self._row_ref(
                filename,
                row,
                ["cohort", "record_id", "cpt_number", "cpt_genie_sample_id"],
            )
            seq = _text(row.get("ca_seq"))
            cid, offset = (
                cancer_by_seq.get(seq, (None, None))
                if seq is not None
                else (None, None)
            )
            report_day = self.rule_set.event(
                row, "ngs", birth=origin_dob, cancer=offset
            )
            assoc = (
                "ambiguous"
                if cid is None
                else ("target" if cid == target_id else "other")
            )
            if report_day is None:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="unknown_ngs_report_day",
                    )
                )
                continue
            report_available = self.rule_set.available(report_day, "ngs")
            if report_available > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="ngs_report_after_cutoff",
                    )
                )
                continue
            sibling_seqs = set()
            for sibling in cpt_rows:
                if (sibling.get("cpt_genie_sample_id"), sibling.get("cpt_number")) != (
                    row.get("cpt_genie_sample_id"),
                    row.get("cpt_number"),
                ):
                    continue
                _, sibling_offset = cancer_by_seq.get(
                    sibling.get("ca_seq"), (None, None)
                )
                sibling_day = self.rule_set.event(
                    sibling, "ngs", birth=origin_dob, cancer=sibling_offset
                )
                if (
                    sibling_day is not None
                    and self.rule_set.available(sibling_day, "ngs") <= cutoff
                ):
                    sibling_seqs.add(sibling.get("ca_seq"))
            if (_to_int(row.get("cpt_n_ca_seq")) or 0) > 1 or len(sibling_seqs) > 1:
                assoc = "ambiguous"
                cid = None
            if assoc == "other":
                continue
            sample_id = _text(row.get("cpt_genie_sample_id"))
            if sample_id is None:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename, record_ref=ref, reason="missing_sample_id"
                    )
                )
                continue
            collection_day = None
            path_id = f"path:{_text(row.get('path_proc_number')) or '?'}:{_text(row.get('path_rep_number')) or '?'}"
            if path_id in visible_path:
                collection_day = visible_path[path_id].event_day
            if collection_day is None:
                rel_collection = _to_int(row.get("dx_path_proc_cpt_days"))
                collection_day = (
                    offset + rel_collection
                    if offset is not None and rel_collection is not None
                    else None
                )
            if collection_day is not None and collection_day > cutoff:
                audit.exclusions.append(
                    AuditExclusion(
                        source_file=filename,
                        record_ref=ref,
                        reason="collection_after_cutoff",
                        fields=["dx_path_proc_cpt_days"],
                    )
                )
                collection_day = None
            fields = [
                "cpt_number",
                "ca_seq",
                "cpt_n_ca_seq",
                "cpt_genie_sample_id",
                "dob_cpt_report_days",
                "dx_cpt_rep_days",
                "path_proc_number",
                "path_rep_number",
                "sample_type",
                "cpt_seq_assay_id",
            ]
            if collection_day is not None:
                fields.append("dx_path_proc_cpt_days")
            report_eid = evidence.add(
                [
                    self._source(
                        filename,
                        row,
                        ["cohort", "record_id", "cpt_number", "cpt_genie_sample_id"],
                        fields,
                    )
                ]
            )

            mutations: list[Mutation] = []
            variant_rows = self.repository.mutation_rows(sample_id)
            for raw_variant in variant_rows:
                variant = self.rule_set.project("mutation", raw_variant)
                vfields = [key for key in variant if key != "__row_number"]
                if not any(
                    is_present(variant.get(k))
                    for k in vfields
                    if k != "Tumor_Sample_Barcode"
                ):
                    continue
                veid = evidence.add(
                    [
                        self._source(
                            "data_mutations_extended.txt",
                            variant,
                            [
                                "Tumor_Sample_Barcode",
                                "Hugo_Symbol",
                                "Start_Position",
                                "Reference_Allele",
                                "Tumor_Seq_Allele2",
                            ],
                            vfields,
                        )
                    ]
                )
                mutations.append(
                    Mutation(
                        gene=_text(variant.get("Hugo_Symbol")),
                        protein_change=_text(variant.get("HGVSp_Short")),
                        variant_classification_raw=_text(
                            variant.get("Variant_Classification")
                        ),
                        chromosome_raw=_text(variant.get("Chromosome")),
                        start_position_raw=_text(variant.get("Start_Position")),
                        reference_allele_raw=_text(variant.get("Reference_Allele")),
                        tumor_allele_raw=_text(variant.get("Tumor_Seq_Allele2")),
                        evidence_refs=[veid],
                    )
                )
            cnas: list[CopyNumberAlteration] = []
            neutral_genes: list[str] = []
            missing_genes: list[str] = []
            cna_calls: dict[str, str] = {}
            for gene, raw in self.repository.cna_values(sample_id):
                synthetic = self.rule_set.project(
                    "cna", {"SAMPLE_ID": sample_id, "Hugo_Symbol": gene, "value": raw}
                )
                if "value" not in synthetic or "Hugo_Symbol" not in synthetic:
                    continue
                cna_calls[gene] = raw
                if not is_present(raw):
                    missing_genes.append(gene)
                    continue
                numeric = _to_float(raw)
                if numeric == 0:
                    neutral_genes.append(gene)
                    continue
                ceid = evidence.add(
                    [
                        EvidenceSource(
                            source_file="data_CNA.txt",
                            record_ref=opaque_record_ref(
                                "data_CNA.txt", synthetic, ["SAMPLE_ID", "Hugo_Symbol"]
                            ),
                            fields=["Hugo_Symbol", sample_id],
                            values={"Hugo_Symbol": gene, sample_id: raw},
                        )
                    ]
                )
                cnas.append(
                    CopyNumberAlteration(
                        gene=gene,
                        value_raw=raw,
                        value_numeric=numeric,
                        evidence_refs=[ceid],
                    )
                )
            svs: list[StructuralVariant] = []
            sv_rows = self.repository.structural_variant_rows(sample_id)
            for raw_sv in sv_rows:
                sv = self.rule_set.project("structural_variant", raw_sv)
                if not any(
                    is_present(v)
                    for k, v in sv.items()
                    if k not in {"Sample_Id", "__row_number"}
                ):
                    continue
                seid = evidence.add(
                    [
                        self._source(
                            "data_sv.txt",
                            sv,
                            [
                                "Sample_Id",
                                "Site1_Hugo_Symbol",
                                "Site2_Hugo_Symbol",
                                "__row_number",
                            ],
                            [k for k in sv if k != "__row_number"],
                        )
                    ]
                )
                svs.append(
                    StructuralVariant(
                        gene_1_raw=_text(sv.get("Site1_Hugo_Symbol")),
                        gene_2_raw=_text(sv.get("Site2_Hugo_Symbol")),
                        class_raw=_text(sv.get("Class")),
                        annotation_raw=_text(sv.get("Annotation")),
                        evidence_refs=[seid],
                    )
                )
            tmb_result = None
            tmb = self.repository.tmb_row(sample_id)
            if tmb:
                tmb = self.rule_set.project("tmb", tmb)
            if tmb and is_present(tmb.get("tmb")):
                teid = evidence.add(
                    [
                        self._source(
                            "tmb.tsv",
                            tmb,
                            ["SAMPLE_ID"],
                            ["SAMPLE_ID", "tmb", "tmb_bin"],
                        )
                    ]
                )
                tmb_result = TMBResult(
                    value_raw=str(tmb["tmb"]),
                    bin_raw=_text(tmb.get("tmb_bin")),
                    evidence_refs=[teid],
                )

            matrix = self.repository.gene_matrix_row(sample_id)
            if matrix:
                matrix = self.rule_set.project("gene_matrix", matrix)
            coverage_refs: list[str] = []
            if matrix:
                coverage_refs.append(
                    evidence.add(
                        [
                            self._source(
                                "data_gene_matrix.txt",
                                matrix,
                                ["SAMPLE_ID"],
                                self.rules["gene_matrix"],
                            )
                        ]
                    )
                )
            issues: list[str] = []
            mutation_file = self.repository.has_file("data_mutations_extended.txt")
            sequenced = sample_id in self.repository.case_list("sequenced")
            mutation_coverage = "unresolved"
            if not mutation_file:
                issues.append("mutation_file_missing")
            elif variant_rows or (
                matrix and is_present(matrix.get("mutations")) and sequenced
            ):
                mutation_coverage = "profiled"
            elif matrix and str(matrix.get("mutations", "")).upper() == "NA":
                mutation_coverage = "not_profiled"
            else:
                issues.append("mutation_sample_coverage_unresolved")

            cna_coverage = "unresolved"
            if not self.repository.has_file("data_CNA.txt"):
                issues.append("cna_file_missing")
            elif self.repository.has_cna_sample(sample_id) and any(
                is_present(raw) for raw in cna_calls.values()
            ):
                cna_coverage = "profiled"
            elif (
                matrix
                and str(matrix.get("cna", "")).upper() == "NA"
                and not self.repository.has_cna_sample(sample_id)
            ):
                cna_coverage = "not_profiled"
            else:
                issues.append(
                    "cna_sample_column_missing"
                    if not self.repository.has_cna_sample(sample_id)
                    else "cna_calls_missing_or_excluded"
                )
            if cna_calls:
                # One whitelisted sample column, never another patient's values.
                cna_source = EvidenceSource(
                    source_file="data_CNA.txt",
                    record_ref=opaque_record_ref(
                        "data_CNA.txt", {"SAMPLE_ID": sample_id}, ["SAMPLE_ID"]
                    ),
                    fields=[sample_id],
                    values={sample_id: cna_calls},
                )
                coverage_refs.append(evidence.add([cna_source]))
            sv_cases = self.repository.case_list("sv")
            # This case list aligns with samples having SV rows, but does not prove that
            # absent samples were assayed and negative. Absence therefore stays unresolved.
            sv_coverage = (
                "profiled"
                if self.repository.has_file("data_sv.txt")
                and (sv_rows or sample_id in sv_cases)
                else "unresolved"
            )
            if not self.repository.has_file("data_sv.txt"):
                issues.append("sv_file_missing")
            elif sv_coverage == "unresolved":
                issues.append("sv_sample_coverage_unresolved")
            for profile, included in [
                ("sequenced", sequenced),
                ("sv", sample_id in sv_cases),
            ]:
                if included:
                    coverage_refs.append(
                        evidence.add(
                            [
                                EvidenceSource(
                                    source_file=f"cases_{profile}.txt",
                                    record_ref=opaque_record_ref(
                                        f"cases_{profile}.txt",
                                        {"SAMPLE_ID": sample_id},
                                        ["SAMPLE_ID"],
                                    ),
                                    fields=["case_list_ids"],
                                    values={"case_list_ids": sample_id},
                                )
                            ]
                        )
                    )
            panel_id = _text(row.get("cpt_seq_assay_id"))
            panel_genes = self.repository.panel_genes(panel_id)
            if panel_genes:
                coverage_refs.append(
                    evidence.add(
                        [
                            EvidenceSource(
                                source_file=f"data_gene_panel_{panel_id}.txt",
                                record_ref=opaque_record_ref(
                                    "gene_panel", {"panel_id": panel_id}, ["panel_id"]
                                ),
                                fields=["stable_id", "gene_list"],
                                values={
                                    "stable_id": panel_id,
                                    "gene_list": sorted(panel_genes),
                                },
                            )
                        ]
                    )
                )
            else:
                issues.append("panel_gene_list_unresolved")
            if collection_day is None:
                missing.append(
                    MissingInformation(
                        field=f"molecular_reports.{sample_id}.collection_day",
                        reason="unknown_collection_day",
                        record_ref=ref,
                    )
                )
            output.append(
                MolecularReport(
                    molecular_report_id=f"ngs:{_text(row.get('cpt_number')) or ref}",
                    sample_id=sample_id,
                    cancer_id=cid,
                    cancer_association=assoc,
                    collection_day=collection_day,
                    available_day=report_available,
                    available_day_basis=self.rule_set.basis("ngs"),
                    panel_id=panel_id,
                    sample_type_raw=_text(row.get("sample_type")),
                    mutations=mutations,
                    copy_number_alterations=cnas,
                    structural_variants=svs,
                    tmb=tmb_result,
                    coverage=MolecularCoverage(
                        mutation=mutation_coverage,
                        cna=cna_coverage,
                        sv=sv_coverage,
                        panel_gene_count=len(panel_genes) if panel_genes else None,
                        cna_missing_genes=missing_genes,
                        cna_neutral_genes=neutral_genes,
                        issues=issues,
                        evidence_refs=coverage_refs,
                    ),
                    evidence_refs=[report_eid],
                )
            )
        return output

    def get_audit_report(self) -> dict[str, Any]:
        if self.last_audit is None:
            raise RuntimeError(
                "Call get_patient_state before requesting an audit report"
            )
        return self.last_audit.model_dump(mode="json", exclude_none=True)

    def get_evidence(
        self,
        patient_id: str,
        cutoff_day: int,
        evidence_refs: list[str],
        cancer_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.get_patient_state(patient_id, cutoff_day, cancer_id)
        available = state["evidence_refs"]
        unknown = [ref for ref in evidence_refs if ref not in available]
        if unknown:
            raise KeyError(
                f"Evidence refs are not visible at this cutoff: {', '.join(unknown)}"
            )
        return {ref: available[ref] for ref in evidence_refs}


class BoundedPatientTool:
    """Agent-facing boundary that fixes both patient identity and maximum cutoff."""

    def __init__(
        self,
        extractor: BPCStateExtractor,
        patient_id: str,
        max_cutoff_day: int,
        cancer_id: str | None = None,
    ):
        self.extractor = extractor
        self.patient_id = patient_id
        if isinstance(max_cutoff_day, bool) or not isinstance(max_cutoff_day, int):
            raise TypeError("max_cutoff_day must be an integer")
        self.max_cutoff_day = max_cutoff_day
        self.cancer_id = cancer_id

    def _validate_cutoff(self, cutoff_day: int) -> None:
        if isinstance(cutoff_day, bool) or not isinstance(cutoff_day, int):
            raise TypeError("cutoff_day must be an integer")
        if cutoff_day > self.max_cutoff_day:
            raise PermissionError(
                f"cutoff_day {cutoff_day} exceeds bound {self.max_cutoff_day}"
            )

    def get_patient_state(self, cutoff_day: int) -> dict[str, Any]:
        self._validate_cutoff(cutoff_day)
        return self.extractor.get_patient_state(
            self.patient_id, cutoff_day, self.cancer_id
        )

    def get_evidence(self, cutoff_day: int, evidence_refs: list[str]) -> dict[str, Any]:
        self._validate_cutoff(cutoff_day)
        return self.extractor.get_evidence(
            self.patient_id, cutoff_day, evidence_refs, self.cancer_id
        )


def get_patient_state(
    patient_id: str,
    cutoff_day: int,
    cancer_id: str | None = None,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Convenience interface requested by the patient-state tool contract."""
    resolved = data_dir or os.environ.get("BPC_DATA_DIR") or DEFAULT_DATA_DIR
    return BPCStateExtractor(resolved).get_patient_state(
        patient_id, cutoff_day, cancer_id
    )
