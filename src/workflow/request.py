"""Resolve explicit patient/day boundaries without interpreting medical intent.

The original natural-language task goes unchanged to the Agents. This parser
only binds safety-critical scope; it never guesses dates, patients or treatment.
"""

import csv
from pathlib import Path
import re


class RequestClarificationError(ValueError):
    """The task needs an explicit, unambiguous patient and relative day bound."""


def resolve_patient_id(value, data_dir):
    """Resolve a numeric ID suffix using the dataset, never an assumed institution."""
    if not isinstance(value, str) or not value.strip():
        raise RequestClarificationError("Specify a patient ID, e.g. GENIE-DFCI-051542 or patient 051542")
    value = value.strip()
    if not value.isdigit():
        return value
    from bpc_state_extractor.repository import CLINICAL_FILES

    with (Path(data_dir) / CLINICAL_FILES["patients"]).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"record_id", "cohort"} <= set(reader.fieldnames or []):
            raise ValueError("Invalid patient index header")
        matches = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed patient index row")
            identity = row["record_id"].strip()
            if identity == value or identity.rsplit("-", 1)[-1] == value:
                matches.append(identity)
    if len(matches) != 1:
        raise RequestClarificationError(
            "Patient suffix did not identify exactly one record; supply a unique full patient ID"
        )
    return matches[0]


def resolve_request(text, *, data_dir, patient_id=None, cutoff_day=None, start_day=None, cancer_id=None):
    """Accept natural language, structured scope, or matching combinations.

    '前220天' / 'first 220 days' means inclusive cutoff day 220, not a duration
    measured backwards from today. Calendar dates and ambiguous ranges require
    clarification. An optional recent-view start never deletes earlier history.
    """
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise RequestClarificationError("Task text must be a nonblank string")
    task = text or "Extract the bounded patient state and complete the configured workflow."
    scope_text = text or ""
    mentioned = set(re.findall(r"(?<![A-Za-z0-9_-])GENIE-[A-Za-z0-9]+-[A-Za-z0-9-]+", scope_text, re.I))
    mentioned.update(re.findall(
        r"(?:患者|病人|patient)(?:\s*(?:ID|编号))?\s*(?:是|为|:|：)?\s*([A-Za-z0-9][A-Za-z0-9_-]*)",
        scope_text, re.I))
    # Reject abbreviated patient lists instead of silently selecting their first member.
    if re.search(r"(?:患者|病人|patient).*?[A-Za-z0-9]\s*(?:和|及|、|,|\band\b)\s*\d{6}(?!\d)", scope_text, re.I):
        raise RequestClarificationError("Specify exactly one patient per workflow run")
    resolved = {resolve_patient_id(value.upper(), data_dir) for value in mentioned}
    if patient_id is not None:
        resolved.add(resolve_patient_id(patient_id, data_dir))
    if len(resolved) != 1:
        raise RequestClarificationError("Specify exactly one patient; the task and explicit patient_id must agree")

    scope_text = re.sub(r"(?:诊断|确诊)前\s*(\d+)\s*天", r"截至第-\1天", scope_text)

    ends, starts = set(), set()
    patterns = [
        r"(?:截至|截止(?:到|日)?|到第)\s*(?:诊断后|第)?\s*(-?\d+)\s*(?:天|日)",
        r"(?:前|最初)\s*(\d+)\s*天",
        r"(?:cutoff(?:_day)?|through\s+day|as\s+of\s+day)\s*[:=：]?\s*(-?\d+)(?![\d./-])\b",
        r"first\s+(\d+)\s+days\b",
    ]
    for pattern in patterns:
        ends.update(int(value) for value in re.findall(pattern, scope_text, re.I))
    for first, last in re.findall(r"第?\s*(-?\d+)\s*(?:天)?\s*(?:至|到|～|~)\s*第?\s*(-?\d+)\s*天", scope_text):
        starts.add(int(first))
        ends.add(int(last))
    for first, last in re.findall(r"days?\s+(-?\d+)\s+(?:to|through)\s+(?:day\s+)?(-?\d+)\b", scope_text, re.I):
        starts.add(int(first))
        ends.add(int(last))
    # A single explicit day can be used when no stronger cutoff/range cue exists.
    if not ends and cutoff_day is None:
        ends.update(int(value) for value in re.findall(r"(?:第\s*(-?\d+)\s*天)", scope_text))
        ends.update(int(value) for value in re.findall(r"\bday\s+(-?\d+)(?![\d./-])\b", scope_text, re.I))
    for value, values, name in ((cutoff_day, ends, "cutoff_day"), (start_day, starts, "start_day")):
        if value is not None:
            if type(value) is not int:
                raise RequestClarificationError(f"{name} must be an integer relative day")
            values.add(value)
    if len(ends) != 1 or len(starts) > 1:
        raise RequestClarificationError("Specify one inclusive cutoff day (e.g. 截至第220天); conflicting or missing dates cannot be guessed")
    cutoff = next(iter(ends))
    start = next(iter(starts)) if starts else None
    if start is not None and start > cutoff:
        raise RequestClarificationError("Recent-view start must not exceed the cutoff day")
    return {"input": task, "patient_id": next(iter(resolved)), "cutoff_day": cutoff,
            "start_day": start, "cancer_id": cancer_id}
