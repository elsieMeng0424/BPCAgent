from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Iterable


CLINICAL_FILES = {
    "patients": "patient_level_dataset.csv",
    "index_cancers": "cancer_level_dataset_index.csv",
    "non_index_cancers": "cancer_level_dataset_non_index.csv",
    "regimens": "regimen_cancer_level_dataset.csv",
    "radiotherapy": "ca_radtx_dataset.csv",
    "pathology": "pathology_report_level_dataset.csv",
    "imaging": "imaging_level_dataset.csv",
    "med_onc": "med_onc_note_level_dataset.csv",
    "tumor_markers": "tm_level_dataset.csv",
    "cpt": "cancer_panel_test_level_dataset.csv",
}


def is_present(value: object) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "na", "nan", "none", "null"}


def opaque_record_ref(
    source_file: str, row: dict[str, str], keys: Iterable[str]
) -> str:
    identity = "|".join(f"{key}={row.get(key, '')}" for key in keys)
    digest = hashlib.sha256(f"{source_file}|{identity}".encode()).hexdigest()[:16]
    return f"R-{digest}"


class DataRepository:
    """Read-only local index over a GENIE BPC release directory."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        if not self.data_dir.is_dir():
            raise FileNotFoundError(f"BPC data directory not found: {self.data_dir}")
        self._clinical: dict[str, dict[tuple[str, str], list[dict[str, str]]]] = {}
        self._mutations: dict[str, list[dict[str, str]]] | None = None
        self._sv: dict[str, list[dict[str, str]]] | None = None
        self._tmb: dict[str, dict[str, str]] | None = None
        self._gene_matrix: dict[str, dict[str, str]] | None = None
        self._case_lists: dict[str, set[str]] = {}
        self._panels: dict[str, set[str]] = {}
        self._cna_cache: dict[str, list[tuple[str, str]]] = {}
        self._cna_header: list[str] | None = None
        self._cna_rows: list[list[str]] = []

    def has_file(self, filename: str) -> bool:
        return (self.data_dir / filename).is_file()

    def _read_dicts(self, filename: str, delimiter: str = ",") -> list[dict[str, str]]:
        path = self.data_dir / filename
        if not path.exists():
            if filename in CLINICAL_FILES.values():
                raise FileNotFoundError(
                    f"Required clinical file is missing: {filename}"
                )
            return []
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            required = (
                {"cohort", "record_id"}
                if filename in CLINICAL_FILES.values()
                else {
                    "data_mutations_extended.txt": {
                        "Tumor_Sample_Barcode",
                        "Hugo_Symbol",
                    },
                    "data_sv.txt": {"Sample_Id"},
                    "data_gene_matrix.txt": {"SAMPLE_ID"},
                    "tmb.tsv": {"SAMPLE_ID", "tmb"},
                }.get(filename, set())
            )
            if not required.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f"Invalid or incomplete header in {filename}: expected {sorted(required)}"
                )
            rows = []
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError(
                        f"Malformed row in {filename} at line {reader.line_num}"
                    )
                rows.append(dict(row))
            return rows

    def clinical_rows(
        self, name: str, cohort: str, patient_id: str
    ) -> list[dict[str, str]]:
        if name not in CLINICAL_FILES:
            raise KeyError(name)
        if name not in self._clinical:
            grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
            for row_number, row in enumerate(
                self._read_dicts(CLINICAL_FILES[name]), start=2
            ):
                row["__row_number"] = str(row_number)
                grouped[(row.get("cohort", ""), row.get("record_id", ""))].append(row)
            self._clinical[name] = dict(grouped)
        return list(self._clinical[name].get((cohort, patient_id), []))

    def find_patient(self, patient_id: str) -> tuple[str, dict[str, str]]:
        if "patients" not in self._clinical:
            # Build this index without knowing cohort.
            grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
            for row_number, row in enumerate(
                self._read_dicts(CLINICAL_FILES["patients"]), start=2
            ):
                row["__row_number"] = str(row_number)
                grouped[(row.get("cohort", ""), row.get("record_id", ""))].append(row)
            self._clinical["patients"] = dict(grouped)
        groups = [
            (cohort, rows)
            for (cohort, rid), rows in self._clinical["patients"].items()
            if rid == patient_id
        ]
        if any(len(rows) != 1 for _, rows in groups):
            raise ValueError(f"Duplicate patient key: {patient_id}")
        matches = [(cohort, rows[0]) for cohort, rows in groups]
        if not matches:
            raise KeyError(f"Unknown patient_id: {patient_id}")
        if len(matches) > 1:
            raise ValueError(f"patient_id occurs in multiple cohorts: {patient_id}")
        return matches[0]

    def mutation_rows(self, sample_id: str) -> list[dict[str, str]]:
        if self._mutations is None:
            grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
            for n, row in enumerate(
                self._read_dicts("data_mutations_extended.txt", "\t"), start=2
            ):
                row["__row_number"] = str(n)
                grouped[row.get("Tumor_Sample_Barcode", "")].append(row)
            self._mutations = dict(grouped)
        return list(self._mutations.get(sample_id, []))

    def structural_variant_rows(self, sample_id: str) -> list[dict[str, str]]:
        if self._sv is None:
            grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
            for n, row in enumerate(self._read_dicts("data_sv.txt", "\t"), start=2):
                row["__row_number"] = str(n)
                grouped[row.get("Sample_Id", "")].append(row)
            self._sv = dict(grouped)
        return list(self._sv.get(sample_id, []))

    def tmb_row(self, sample_id: str) -> dict[str, str] | None:
        if self._tmb is None:
            self._tmb = {}
            for n, row in enumerate(self._read_dicts("tmb.tsv", "\t"), start=2):
                row["__row_number"] = str(n)
                self._tmb[row.get("SAMPLE_ID", "")] = row
        row = self._tmb.get(sample_id)
        return dict(row) if row else None

    def gene_matrix_row(self, sample_id: str) -> dict[str, str] | None:
        if self._gene_matrix is None:
            self._gene_matrix = {
                row.get("SAMPLE_ID", ""): row
                for row in self._read_dicts("data_gene_matrix.txt", "\t")
            }
        row = self._gene_matrix.get(sample_id)
        return dict(row) if row else None

    def case_list(self, profile: str) -> set[str]:
        if profile not in self._case_lists:
            path = self.data_dir / f"cases_{profile}.txt"
            values: set[str] = set()
            if path.exists():
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    if line.startswith("case_list_ids:"):
                        values.update(
                            x for x in line.split(":", 1)[1].strip().split("\t") if x
                        )
            self._case_lists[profile] = values
        return self._case_lists[profile]

    def panel_genes(self, panel_id: str | None) -> set[str]:
        if not panel_id:
            return set()
        if panel_id not in self._panels:
            path = self.data_dir / f"data_gene_panel_{panel_id}.txt"
            genes: set[str] = set()
            if path.exists():
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    if line.startswith("gene_list:"):
                        genes.update(
                            x for x in line.split(":", 1)[1].strip().split("\t") if x
                        )
            self._panels[panel_id] = genes
        return self._panels[panel_id]

    def cna_values(self, sample_id: str) -> list[tuple[str, str]]:
        if sample_id in self._cna_cache:
            return list(self._cna_cache[sample_id])
        self._load_cna()
        if sample_id not in (self._cna_header or []):
            self._cna_cache[sample_id] = []
            return []
        sample_index = self._cna_header.index(sample_id)
        # Missing/NA calls are retained so they cannot be mistaken for neutral zero.
        values = [(row[0], row[sample_index].strip()) for row in self._cna_rows]
        self._cna_cache[sample_id] = values
        return list(values)

    def has_cna_sample(self, sample_id: str) -> bool:
        self._load_cna()
        return sample_id in (self._cna_header or [])

    def _load_cna(self) -> None:
        if self._cna_header is not None:
            return
        path = self.data_dir / "data_CNA.txt"
        if not path.exists():
            self._cna_header = []
            return
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            header = next(reader, [])
            if (
                not header
                or header[0] != "Hugo_Symbol"
                or len(header) != len(set(header))
            ):
                raise ValueError("Invalid CNA header")
            rows = []
            genes = set()
            for row in reader:
                if len(row) != len(header) or not row[0] or row[0] in genes:
                    raise ValueError(
                        f"Invalid or duplicate CNA gene row at line {reader.line_num}"
                    )
                genes.add(row[0])
                rows.append(row)
        self._cna_header = header
        self._cna_rows = rows
