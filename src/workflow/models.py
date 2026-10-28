"""Versioned contracts for the five-agent research workflow."""

from typing import Literal

from pydantic import Field, field_validator

from ..tools import ToolArgs


Stage = Literal["PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent"]
Role = Literal[
    "PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent", "MetaAgent"
]


class DocumentEvidenceOutput(ToolArgs):
    assessment_refs: list[str] = Field(default_factory=list, description=(
        "Successful Jev assessment IDs from this Agent turn, separate from patient/document evidence. "
        "These are unvalidated model judgments, not clinical approval."))
    document_refs: list[str] = Field(default_factory=list, description=(
        "Document citation IDs read completely or visually inspected in this Agent turn; separate from patient evidence_refs. "
        "A citation does not establish clinical validity."))


class PatientOutput(DocumentEvidenceOutput):
    summary: str = Field(min_length=1)
    limitations: list[str]


class PatientEvidenceArgs(ToolArgs):
    evidence_refs: list[str] = Field(min_length=1, description="References from the shared patient snapshot.")


class TargetCandidate(ToolArgs):
    debug_only: Literal[True] | None = None
    debug_score: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    document_refs: list[str] = Field(default_factory=list)
    target: str = Field(min_length=1)
    direction: Literal["inhibit", "activate", "unknown"]
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    status: Literal["unvalidated_hypothesis"] = "unvalidated_hypothesis"


class TargetOutput(DocumentEvidenceOutput):
    candidates: list[TargetCandidate]
    limitations: list[str]


class DrugCandidate(ToolArgs):
    debug_only: Literal[True] | None = None
    debug_score: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    document_refs: list[str] = Field(default_factory=list)
    drug: str = Field(min_length=1)
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    compatibility: Literal["not_assessed"] = "not_assessed"
    status: Literal["unvalidated_hypothesis"] = "unvalidated_hypothesis"


class DrugOutput(DocumentEvidenceOutput):
    candidates: list[DrugCandidate]
    limitations: list[str]


class ReviewOutput(DocumentEvidenceOutput):
    disposition: Literal["proceed", "revise", "block"]
    reason: str = Field(min_length=1, description="Explain approval, required correction, or terminal safety block.")
    issues: list[str]
    evidence_gaps: list[str]
    # Proceed only means the workflow can advance, not medical approval.
    clinical_validation: Literal["not_performed"] = "not_performed"

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("Review reason must not be blank")
        return value.strip()


class ImprovementProposal(ToolArgs):
    scope: Literal["prompt", "workflow", "data_quality", "ranking_rubric"]
    change: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    validation_plan: str = Field(min_length=1)
    applied: Literal[False] = False


class MetaOutput(DocumentEvidenceOutput):
    proposals: list[ImprovementProposal]


OUTPUT_MODELS = {
    "PatientStateAgent": PatientOutput,
    "TargetSelectionAgent": TargetOutput,
    "DrugSelectionAgent": DrugOutput,
    "ReviewAgent": ReviewOutput,
    "MetaAgent": MetaOutput,
}


class EmptyArgs(ToolArgs):
    pass


class PlaceholderArgs(ToolArgs):
    request: str = Field(min_length=1)
