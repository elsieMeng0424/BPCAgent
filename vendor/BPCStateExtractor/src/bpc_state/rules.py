from __future__ import annotations

import fnmatch
import json
import math
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from .repository import is_present


SECTION_BY_TABLE = {
    "patients": "demographics",
    "index_cancers": "cancer",
    "non_index_cancers": "cancer",
    "regimens": "treatment",
    "radiotherapy": "radiotherapy",
    "pathology": "pathology",
    "imaging": "imaging",
    "med_onc": "med_onc",
    "tumor_markers": "tumor_marker",
    "cpt": "cpt",
}


def number(value: object) -> float | None:
    if not is_present(value):
        return None
    try:
        parsed = float(str(value).strip())
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def integer(value: object) -> int | None:
    parsed = number(value)
    # Day indices and identifiers cannot be silently rounded across a cutoff.
    return int(parsed) if parsed is not None and parsed.is_integer() else None


class RuleSet:
    """Executable field allowlists and bounded arithmetic time rules (no eval)."""

    def __init__(self, path: str | Path | None = None):
        resource = (
            Path(path)
            if path is not None
            else files("bpc_state.resources").joinpath("field_rules.json")
        )
        with resource.open(encoding="utf-8") as handle:
            self.config: dict[str, Any] = json.load(handle)
        if self.config.get("schema_version") != "1.1":
            raise ValueError("Rules must use schema_version 1.1")
        fixed_policies = {
            "drug_name": "raw_only_normalized_null",
            "tumor_marker_units": "raw_only_no_implicit_conversion",
            "tmb": "raw_value_and_bin_unit_unconfirmed",
            "cna": "preserve_raw_skip_missing_emit_nonzero_only_use_coverage_for_neutral_profile",
        }
        for name, supported in fixed_policies.items():
            if self.config["normalization_rules"].get(name) != supported:
                raise ValueError(f"Unsupported normalization policy: {name}")
        for name, spec in self.config["time_rules"].items():
            delay = spec.get("reporting_delay_days", 0)
            if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
                raise ValueError(f"Invalid nonnegative reporting delay for {name}")
            for source in spec["sources"]:
                if source["origin"] not in {"direct", "birth", "cancer"}:
                    raise ValueError(f"Unknown time origin in {name}")
        for pattern in self.config["normalization_rules"]["imaging_assessment"]:
            if pattern != "otherwise":
                re.compile(pattern)

    def allowed(self, section: str, field: str) -> bool:
        if any(
            fnmatch.fnmatchcase(field, p)
            for p in self.config["excluded_derived_or_future_fields"]
        ):
            return False
        return any(fnmatch.fnmatchcase(field, p) for p in self.config.get(section, []))

    def project(self, section: str, row: dict[str, str]) -> dict[str, str]:
        # These internal keys establish source identity; they do not themselves
        # authorize additional fields in the fact or its evidence payload.
        return {
            k: v
            for k, v in row.items()
            if k in {"__row_number", "cohort", "record_id"} or self.allowed(section, k)
        }

    def event(
        self,
        row: dict[str, str],
        name: str,
        *,
        birth: int | None = None,
        cancer: int | None = None,
        **format_values: object,
    ) -> int | None:
        for source in self.config["time_rules"][name]["sources"]:
            raw = integer(row.get(source["field"].format(**format_values)))
            if raw is None:
                continue
            origin = source["origin"]
            if origin == "direct":
                return raw
            if origin == "birth" and birth is not None:
                return raw - birth
            if origin == "cancer" and cancer is not None:
                return raw + cancer
        return None

    def available(self, event_day: int, name: str) -> int:
        delay = self.config["time_rules"][name].get("reporting_delay_days", 0)
        if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ValueError(f"Invalid nonnegative reporting delay for {name}")
        return event_day + delay

    def basis(self, name: str) -> str:
        return self.config["time_rules"][name]["basis"]

    def assessment(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        mapping = self.config["normalization_rules"]["imaging_assessment"]
        for pattern, value in mapping.items():
            if pattern != "otherwise" and re.search(pattern, raw, flags=re.IGNORECASE):
                return value
        return mapping["otherwise"]
