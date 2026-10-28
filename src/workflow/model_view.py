"""Deterministic model-facing projections; never modify canonical patient data.

Tables remove repeated keys, not clinical values. Sparse records remain objects
so an absent key is never silently converted into an explicit null.
"""

from copy import deepcopy
import json


RECORDS_TABLE = "records-table-v1"
STATUS_TABLE = "field-status-table-v1"
VIEW_VERSION = "bpc-model-view-v1"
OBSERVATION_IDS = {
    "imaging": "imaging_id", "med_onc": "med_onc_id", "tumor_markers": "tumor_marker_id",
}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def compact_records(value):
    """Pack repeated uniform records only when their JSON representation shrinks.

    The encoding tags are reserved for this model view, not canonical PatientState.
    Column order is explicit; nested tables follow the same rule. Values, row order,
    empty collections, false, zero and null are preserved exactly.
    """
    if isinstance(value, dict):
        return {key: compact_records(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    records = [compact_records(item) for item in value]
    if len(records) < 2 or not all(isinstance(row, dict) for row in records):
        return records
    columns = list(records[0])
    if not columns or not all(set(row) == set(columns) for row in records):
        return records
    table = {"encoding": RECORDS_TABLE, "columns": columns,
             "rows": [[row[column] for column in columns] for row in records]}
    return table if _size(table) < _size(records) else records


def _compact_status(statuses):
    """Keep original JSON Pointers and every status/reason/evidence value."""
    table = compact_records([{"path": path, **status} for path, status in statuses.items()])
    if isinstance(table, dict):
        table["encoding"] = STATUS_TABLE
        if _size(table) < _size(statuses):
            return table
    return deepcopy(statuses)


def expand_tables(value):
    """Decode model-view tables for inspection and losslessness tests.

    This is not evidence retrieval: reference IDs remain IDs after decoding.
    Field-status paths refer to the decoded canonical layout, not table positions.
    """
    if isinstance(value, list):
        return [expand_tables(item) for item in value]
    if not isinstance(value, dict):
        return value
    if (set(value) == {"encoding", "columns", "rows"}
            and value["encoding"] in (RECORDS_TABLE, STATUS_TABLE)):
        columns = value["columns"]
        if not isinstance(columns, list) or not all(isinstance(k, str) for k in columns):
            raise ValueError("Invalid model-view table columns")
        if len(set(columns)) != len(columns) or not isinstance(value["rows"], list):
            raise ValueError("Invalid model-view table")
        rows = []
        for row in value["rows"]:
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError("Invalid model-view table row width")
            rows.append({key: expand_tables(item) for key, item in zip(columns, row)})
        if value["encoding"] == STATUS_TABLE:
            if "path" not in columns or any(not isinstance(row["path"], str) for row in rows):
                raise ValueError("Invalid field-status table paths")
            if len({row["path"] for row in rows}) != len(rows):
                raise ValueError("Duplicate field-status table paths")
            return {row["path"]: {key: item for key, item in row.items() if key != "path"} for row in rows}
        return rows
    return {key: expand_tables(item) for key, item in value.items()}


def patient_model_view(payload):
    """Project a full shared snapshot into an evidence-on-demand model view."""
    view = deepcopy(payload)
    state = view["patient_state"]
    # Keep the full reference vocabulary, but retrieve source bodies only on demand.
    state["evidence_refs"] = list(state["evidence_refs"])
    state["field_status"] = _compact_status(state["field_status"])
    recent = view.pop("recent_observations")
    view["recent_observation_ids"] = {
        kind: [row[OBSERVATION_IDS[kind]] for row in rows] for kind, rows in recent.items()
    }
    view = compact_records(view)
    view["model_view"] = {
        "version": VIEW_VERSION,
        "evidence": "Source bodies omitted; get_patient_evidence resolves the listed IDs at the fixed cutoff.",
        "tables": "Rows map positionally to columns; field-status-table-v1 maps path to its annotation.",
        "paths": "Field-status JSON Pointers address the original patient_state after table decoding.",
        "hash": "patient_state_sha256 identifies the complete canonical snapshot, not this projection.",
    }
    return view


def state_check_model_view(payload):
    """Use the same lossless table representation for the explicit check tool."""
    return compact_records({"completeness": payload["state_check"],
                            "normalized_markers": payload["normalized_markers"]})
