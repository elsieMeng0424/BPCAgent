"""Opt-in text-match fixtures for orchestration, never scientific ranking."""

from copy import deepcopy


class GuidelineDebugTools:
    """Resolve a fixed research example against an authorized document snapshot.

    A score of 1 means a named topic occurs in a complete retrieved section. It
    says nothing about efficacy, patient eligibility, interactions or priority.
    The mapping is a hand-written fixture, not a learned pathway/docking result.
    """

    target = "androgen signaling (debug pathway label)"
    drug = "enzalutamide"

    def __init__(self, owner):
        self.owner = owner
        self.issued = {}

    def run(self, kind):
        owner = self.owner
        if not owner.read_shared or not owner.read_context or not owner.shared.ready:
            raise ValueError("Read the shared patient state and stage context first")
        state = owner.shared.read()["patient_state"]
        diagnosis = state["cancer_context"]["diagnosis"]
        result = {"status": "debug_only", "score_kind": "binary_document_text_match",
                  "clinical_validation": "not_performed", "patient_eligibility": "not_assessed",
                  "compatibility": "not_assessed", "candidates": [], "source_sections": [],
                  "limitations": ["NCI PDQ is an evidence summary, not a clinical practice guideline.",
                                  "Hand-written debug fixture; scores are not clinical ranking or confidence.",
                                  "No stopping, adding or switching treatment is recommended."]}
        if "prostate" not in str(diagnosis.get("value", "")).casefold():
            return result
        if kind == "drug":
            accepted = owner.context.get("accepted_outputs", {}).get("TargetSelectionAgent", {})
            if self.target not in {c["target"] for c in accepted.get("candidates", [])}:
                return result
        tools = owner.guideline_tools
        if tools is None:
            raise ValueError("Debug fixtures require enabled document tools")
        hits = tools.library.search(self.drug, document_id="nci-prostate-pdq", limit=10)["results"]
        for hit in hits:
            section = tools.library.section("nci-prostate-pdq", hit["section_id"], max_chars=12000)
            # Keep the full section, including qualifiers. Never trim it to fit.
            if (not section["complete_section"] or
                    "nonsteroidal antiandrogen" not in section["heading"].casefold() or
                    self.drug not in section["text"].casefold()):
                continue
            tools.record_evidence("get_guideline_section", section)
            candidate = {"target": self.target, "debug_only": True, "debug_score": 1.0,
                         "document_refs": [section["citation_id"]],
                         "evidence_refs": diagnosis["evidence_refs"],
                         "rationale": "Debug-only topic match in NCI PDQ; patient diagnosis anchors context only, not treatment eligibility.",
                         "status": "unvalidated_hypothesis"}
            candidate.update({"direction": "inhibit"} if kind == "target" else
                             {"drug": self.drug, "compatibility": "not_assessed"})
            result["candidates"] = [candidate]
            result["source_sections"] = [section]
            self.issued[kind] = deepcopy(candidate)
            break
        return result

    def validate(self, kind, candidates):
        issued = self.issued.get(kind)
        for candidate in candidates:
            if issued is None:
                raise ValueError("Call the debug scoring tool before submitting debug candidates")
            # Only rationale may be rephrased; provenance and fixture values are locked.
            if any(candidate.get(key) != value for key, value in issued.items() if key != "rationale"):
                raise ValueError("Candidate does not match the issued debug fixture")
