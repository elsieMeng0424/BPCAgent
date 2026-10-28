"""Collect traceable research resources without sending data to a model.

Direct HTTPS is always attempted first. Only a failed direct request may use the
configured proxy. PINDER bulk archives and restricted guideline bodies are never
downloaded by this script. Raw assets and derived databases stay outside Git.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import zipfile

from bpcagent.workflow.config import read_object


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_asset(path, kind):
    with path.open("rb") as handle:
        signature = handle.read(16)
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            if not archive.infolist():
                raise ValueError("Empty ZIP")
    elif kind == "parquet":
        import pyarrow.parquet as pq
        pq.ParquetFile(path)  # Verify the footer/schema, not just the extension.
    elif kind == "kgml":
        if ET.parse(path).getroot().tag != "pathway":
            raise ValueError("Not a KGML pathway")
    elif kind == "png" and not signature.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not a PNG")
    elif kind == "pdb":
        with path.open(errors="replace") as handle:
            if not any(line.startswith(("ATOM  ", "HETATM")) for line in handle):
                raise ValueError("PDB has no atom records")
    elif kind == "tsv":
        with path.open() as handle:
            first = handle.readline()
        if "\t" not in first or "<html" in first.lower():
            raise ValueError("Not tab-separated data")
    if path.stat().st_size == 0:
        raise ValueError("Empty asset")


class Collector:
    def __init__(self, config):
        self.config_path = Path(config).resolve()
        self.settings = read_object(self.config_path)
        self.root = (self.config_path.parent / self.settings["root"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def fetch(self, source, relative, url, kind, *, max_bytes=None):
        if urlparse(url).scheme != "https":
            raise ValueError("Only HTTPS downloads are allowed")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Asset path escapes database root")
        receipt_path = path.with_name(path.name + ".receipt.json")
        if path.exists():
            if not receipt_path.exists():
                raise ValueError(f"Existing asset has no receipt: {path}")
            receipt = json.loads(receipt_path.read_text())
            if receipt["url"] != url or receipt["sha256"] != sha256(path):
                raise ValueError(f"Existing asset differs from receipt: {path}")
            print(f"Verified cached {relative}", flush=True)
            return path
        if shutil.disk_usage(self.root).free < self.settings["minimum_free_bytes"]:
            raise ValueError("Insufficient free disk space for collection")
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        if partial.exists():
            raise ValueError(f"Unfinished download needs inspection before retry: {partial}")
        routes = [None]
        if self.settings.get("fallback_proxy"):
            routes.append(self.settings["fallback_proxy"])
        errors = []
        for proxy in routes:
            print(f"Downloading {relative} ({'fallback proxy' if proxy else 'direct'})", flush=True)
            command = ["curl", "--fail", "--location", "--silent", "--show-error",
                       "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "8",
                       "--max-time", "1200", "--speed-time", "45", "--speed-limit", "1024",
                       "--max-filesize", str(max_bytes or self.settings["max_file_bytes"]),
                       "--output", str(partial), "--write-out", "%{url_effective}"]
            command += ["--proxy", proxy, "--noproxy", ""] if proxy else ["--noproxy", "*"]
            response = subprocess.run(command + [url], capture_output=True, text=True)
            try:
                if response.returncode:
                    raise ValueError(response.stderr.strip()[-800:])
                if partial.stat().st_size > (max_bytes or self.settings["max_file_bytes"]):
                    raise ValueError("Download exceeded byte limit")
                validate_asset(partial, kind)
                receipt = {"source": source, "url": url, "effective_url": response.stdout.strip(),
                           "retrieved_at": now(), "bytes": partial.stat().st_size,
                           "sha256": sha256(partial), "kind": kind,
                           "used_fallback_proxy": proxy is not None, "prior_failures": errors}
                partial.replace(path)
                write_json(receipt_path, receipt)
                print(f"Saved {relative}: {receipt['bytes']} bytes", flush=True)
                return path
            except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError) as error:
                errors.append(str(error))
                # Delete only the failed, unverified transfer created by this invocation.
                partial.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download {url}: {'; '.join(errors)}")

    def collect(self, groups):
        assets = []
        if "biogrid" in groups:
            version = self.settings["biogrid_release"]
            for component, extension in (("ALL", "tab3"), ("CHEMICALS", "chemtab"),
                                          ("PTMS", "ptm"), ("IDENTIFIERS", "tab")):
                filename = f"BIOGRID-{component}-{version}.{extension}.zip"
                assets.append(("biogrid", f"raw/biogrid/{version}/{filename}",
                               f"https://downloads.thebiogrid.org/Download/BioGRID/Release-Archive/BIOGRID-{version}/{filename}", "zip"))
        if "pinder" in groups:
            version = self.settings["pinder_release"]
            for filename in ("index.parquet", "metadata.parquet"):
                assets.append(("pinder", f"raw/pinder/{version}/{filename}",
                               f"https://pinderdata.org/{version}/{filename}", "parquet"))
        if "nci" in groups:
            assets.append(("nci", "raw/guidelines/nci/prostate-treatment-pdq.html",
                           "https://www.cancer.gov/types/prostate/hp/prostate-treatment-pdq", "html"))
        # Two simultaneous streams avoid saturating shared storage and networking.
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.fetch, *asset) for asset in assets]
            failures = []
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    failures.append(str(error))
        if "kegg" in groups:
            if not self.settings["academic_use"]:
                raise ValueError("KEGG collection requires an academic-use entitlement")
            pathway = self.settings["pathway"]
            for filename, endpoint, kind in (
                ("pathway.kgml", f"get/{pathway}/kgml", "kgml"),
                ("pathway.png", f"get/{pathway}/image", "png"),
                ("pathway.txt", f"get/{pathway}", "text"),
                ("genes.tsv", f"link/hsa/{pathway}", "tsv"),
                ("human_uniprot.tsv", "conv/uniprot/hsa", "tsv"),
            ):
                try:
                    self.fetch("kegg", f"raw/kegg/{pathway}/{filename}", f"https://rest.kegg.jp/{endpoint}", kind)
                except Exception as error:
                    failures.append(str(error))
                time.sleep(0.4)  # KEGG permits at most three requests per second.
        if failures:
            raise RuntimeError("\n".join(failures))


def kegg_graph(root, pathway):
    """Preserve KGML entries/groups and typed relations; do not invent PPI edges."""
    folder = root / "raw/kegg" / pathway
    tree = ET.parse(folder / "pathway.kgml").getroot()
    entries = [{**node.attrib, "graphics": [g.attrib for g in node.findall("graphics")],
                "components": [g.attrib for g in node.findall("component")]}
               for node in tree.findall("entry")]
    relations = [{**node.attrib, "subtypes": [s.attrib for s in node.findall("subtype")]}
                 for node in tree.findall("relation")]
    reactions = [{**node.attrib, "substrates": [s.attrib for s in node.findall("substrate")],
                  "products": [p.attrib for p in node.findall("product")]}
                 for node in tree.findall("reaction")]
    entry_ids = {e["id"] for e in entries}
    if any(r["entry1"] not in entry_ids or r["entry2"] not in entry_ids for r in relations):
        raise ValueError("KGML has dangling relations")
    graph = {"pathway": tree.attrib, "entries": entries, "relations": relations, "reactions": reactions,
             "source_sha256": sha256(folder / "pathway.kgml"),
             "interpretation": "Relations preserve KEGG types; not all denote direct binding or drug action."}
    write_json(root / "derived/kegg" / f"{pathway}.graph.json", graph)
    genes = set()
    for line in (folder / "genes.tsv").read_text().splitlines():
        genes.update(part for part in line.split("\t") if part.startswith("hsa:"))
    mapping = []
    for line in (folder / "human_uniprot.tsv").read_text().splitlines():
        parts = line.split("\t")
        gene = next((p for p in parts if p.startswith("hsa:")), None)
        accession = next((p.removeprefix("up:") for p in parts if p.startswith("up:")), None)
        if gene in genes and accession:
            mapping.append({"kegg_gene": gene, "uniprot": accession})
    if not genes or not mapping:
        raise ValueError("No KEGG genes or UniProt mappings")
    seed = {"pathway": pathway, "genes": sorted(genes), "gene_uniprot": mapping,
            "unmapped_genes": sorted(genes - {row["kegg_gene"] for row in mapping}),
            "source_sha256": {name: sha256(folder / name) for name in ("genes.tsv", "human_uniprot.tsv")}}
    write_json(root / "derived/kegg" / f"{pathway}.seeds.json", seed)
    return graph, seed


def related_pinder(root, release, seed):
    """Keep all index/metadata rows with at least one exact seed accession."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = root / "raw/pinder" / release
    destination = root / "derived/pinder" / release
    destination.mkdir(parents=True, exist_ok=True)
    accessions = {row["uniprot"] for row in seed["gene_uniprot"]}
    source = pq.ParquetFile(folder / "index.parquet")
    if not {"id", "uniprot_R", "uniprot_L"} <= set(source.schema_arrow.names):
        raise ValueError("Unexpected PINDER index schema")
    selected = []
    identifiers = set()
    counts = {"total_index_rows": source.metadata.num_rows, "matched_index_rows": 0,
              "both_partners_in_pathway": 0, "one_partner_in_pathway": 0}
    index_path = destination / "pathway_index.parquet"
    index_temporary = index_path.with_suffix(".partial.parquet")
    with pq.ParquetWriter(index_temporary, source.schema_arrow, use_dictionary=False) as writer:
        for batch in source.iter_batches(batch_size=16384):
            right = batch.column(source.schema_arrow.get_field_index("uniprot_R")).to_pylist()
            left = batch.column(source.schema_arrow.get_field_index("uniprot_L")).to_pylist()
            matches = [(r in accessions, l in accessions) for r, l in zip(right, left)]
            filtered = batch.filter(pa.array([r or l for r, l in matches]))
            if filtered.num_rows:
                rows = filtered.to_pylist()
                # Filtering a DictionaryArray retains the full source dictionary.
                # Materialize only selected values so each output row group does
                # not carry millions of unrelated identifiers or strings.
                writer.write_batch(pa.RecordBatch.from_pylist(rows, schema=source.schema_arrow))
                selected.extend(rows)
                identifiers.update(row["id"] for row in rows)
            counts["both_partners_in_pathway"] += sum(r and l for r, l in matches)
            counts["one_partner_in_pathway"] += sum(r != l for r, l in matches)
    counts["matched_index_rows"] = len(selected)
    index_temporary.replace(index_path)
    metadata = pq.ParquetFile(folder / "metadata.parquet")
    found_metadata = set()
    metadata_path = destination / "pathway_metadata.parquet"
    metadata_temporary = metadata_path.with_suffix(".partial.parquet")
    with pq.ParquetWriter(metadata_temporary, metadata.schema_arrow, use_dictionary=False) as writer:
        for batch in metadata.iter_batches(batch_size=16384):
            ids = batch.column(metadata.schema_arrow.get_field_index("id")).to_pylist()
            filtered = batch.filter(pa.array([identity in identifiers for identity in ids]))
            if filtered.num_rows:
                writer.write_batch(pa.RecordBatch.from_pylist(filtered.to_pylist(), schema=metadata.schema_arrow))
                found_metadata.update(filtered.column(metadata.schema_arrow.get_field_index("id")).to_pylist())
    metadata_temporary.replace(metadata_path)
    matched_accessions = {row[key] for row in selected for key in ("uniprot_R", "uniprot_L")} & accessions
    report = {**counts, "release": release, "seed_pathway": seed["pathway"],
              "selection": "Either partner's UniProt accession exactly matches a KEGG seed; no inferred isoform mappings.",
              "seed_uniprot_count": len(accessions), "matched_seed_uniprot": sorted(matched_accessions),
              "unmatched_seed_uniprot": sorted(accessions - matched_accessions),
              "missing_metadata_ids": sorted(identifiers - found_metadata),
              "source_sha256": {name: sha256(folder / name) for name in ("index.parquet", "metadata.parquet")}}
    write_json(destination / "selection.json", report)
    return selected, report


def representative_complexes(rows, accessions, limit):
    """Deterministic coverage-first sampling, not a binding/clinical ranking."""
    candidates = sorted((row for row in rows if row.get("split") != "invalid"), key=lambda row: (
        -int(row["uniprot_R"] in accessions and row["uniprot_L"] in accessions),
        -int(bool(row.get("pinder_xl"))), row["id"]))
    chosen, used_pairs, covered = [], set(), set()
    # First give each seed an opportunity to obtain a representative complex.
    for coverage_only in (True, False):
        for row in candidates:
            pair = tuple(sorted((row["uniprot_R"], row["uniprot_L"])))
            seeds = set(pair) & accessions
            if not seeds or pair in used_pairs or (coverage_only and seeds <= covered):
                continue
            if len(chosen) >= limit:
                return chosen
            chosen.append(row)
            used_pairs.add(pair)
            covered.update(seeds)
    return chosen


def download_structures(collector):
    import pyarrow.parquet as pq

    root, settings = collector.root, collector.settings
    release, pathway = settings["pinder_release"], settings["pathway"]
    seed = json.loads((root / "derived/kegg" / f"{pathway}.seeds.json").read_text())
    rows = pq.read_table(root / "derived/pinder" / release / "pathway_index.parquet").to_pylist()
    chosen = representative_complexes(rows, {r["uniprot"] for r in seed["gene_uniprot"]}, settings["structure_limit"])
    report = {"selection": "Coverage-first representatives, at most one complex per UniProt pair; not docking scores.",
              "limit": settings["structure_limit"], "budget_bytes": settings["structure_budget_bytes"],
              "available_related_rows": len(rows), "files": []}
    total = 0
    for row in chosen:
        identity = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity):
            raise ValueError("Unexpected PINDER structure identifier")
        remaining = settings["structure_budget_bytes"] - total
        if remaining <= 0:
            break
        record = {"id": identity, "uniprot_R": row["uniprot_R"], "uniprot_L": row["uniprot_L"],
                  "split": row["split"], "status": "pending"}
        relative = f"raw/pinder/{release}/related_pdbs/{identity}.pdb"
        try:
            path = collector.fetch("pinder", relative, f"https://pinderdata.org/{release}/pdbs/{identity}.pdb",
                                   "pdb", max_bytes=min(settings["structure_max_file_bytes"], remaining))
            total += path.stat().st_size
            record.update(status="downloaded", path=relative, bytes=path.stat().st_size, sha256=sha256(path))
        except (ValueError, RuntimeError) as error:
            record.update(status="failed", error=str(error))
        report["files"].append(record)
        report["downloaded_bytes"] = total
        write_json(root / "derived/pinder" / release / "structures.json", report)
    return report


def quote_name(value):
    return '"' + value.replace('"', '""') + '"'


def source_json(value):
    """Encode Arrow/numpy scalars without losing explicit non-finite values."""
    def convert(item):
        if isinstance(item, dict):
            return {key: convert(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(child) for child in item]
        if hasattr(item, "item"):
            item = item.item()
        if isinstance(item, float) and not math.isfinite(item):
            return {"source_nonfinite": "NaN" if math.isnan(item) else "Infinity" if item > 0 else "-Infinity"}
        return item
    return json.dumps(convert(value), allow_nan=False)


def import_tab_archive(connection, path, component):
    """Stream raw TSV cells into SQLite; preserve '-', blanks and all species."""
    counts = {}
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.is_dir() or not item.filename.endswith(".txt"):
                continue
            table = "biogrid_" + ("ptm_relationships" if ".ptmrel." in item.filename else component)
            with archive.open(item) as binary:
                handle = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                reader = csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
                # Identifiers have license notes and column descriptions before
                # their actual header; do not mistake that preamble for data.
                expected = "BIOGRID_ID" if component == "identifiers" else None
                header = None
                for line_number, candidate in enumerate(reader, 1):
                    if candidate and len(candidate) > 1 and (
                        candidate[0] == expected if expected else candidate[0].startswith("#")
                    ):
                        header = candidate
                        break
                    if line_number >= 100:
                        break
                if header is None:
                    raise ValueError(f"No recognized BioGRID header: {item.filename}")
                header[0] = header[0].lstrip("#")
                if not header or len({name.casefold() for name in header}) != len(header):
                    raise ValueError(f"Invalid BioGRID columns: {item.filename}")
                connection.execute(f"CREATE TABLE {quote_name(table)} ({','.join(quote_name(h) + ' TEXT' for h in header)})")
                statement = f"INSERT INTO {quote_name(table)} VALUES ({','.join('?' for _ in header)})"
                count, batch = 0, []
                for row in reader:
                    if not row:
                        continue
                    if len(row) != len(header):
                        raise ValueError(f"Malformed row {count + len(batch) + 2} in {item.filename}")
                    batch.append(row)
                    if len(batch) == 10000:
                        connection.executemany(statement, batch)
                        count += len(batch)
                        batch.clear()
                        if count % 500000 == 0:
                            print(f"Imported {table}: {count} rows", flush=True)
                if batch:
                    connection.executemany(statement, batch)
                    count += len(batch)
                for column in header:
                    if (column.startswith(("Entrez Gene", "Official Symbol", "Organism ID", "Organism Interactor"))
                            or column in {"BIOGRID_ID", "IDENTIFIER_VALUE"}):
                        name = f"{table}_index_{header.index(column)}"
                        connection.execute(f"CREATE INDEX {quote_name(name)} ON {quote_name(table)} ({quote_name(column)})")
                connection.execute("INSERT INTO imported_tables VALUES (?, ?, ?, ?, ?, ?)",
                                   (table, str(path.name), item.filename, sha256(path), count, json.dumps(header)))
                counts[table] = count
                print(f"Finished {table}: {count} rows", flush=True)
    return counts


def nci_sections(path):
    """Extract text only; omit images and tables that may include third-party material."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(), "html.parser")
    main = soup.find("article") or soup.find("main")
    if main is None or "Prostate Cancer Treatment" not in main.get_text():
        raise ValueError("NCI response is not the requested treatment summary")
    for node in main.select("script, style, nav, figure, img, table, form, header, footer"):
        node.decompose()
    sections, heading, fragment, paragraphs = [], "Overview", "", []
    def flush():
        if paragraphs:
            sections.append({"heading": heading, "anchor": fragment, "text": "\n".join(paragraphs)})
    for node in main.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        # Avoid nested list text appearing twice.
        if node.name in {"p", "li"} and node.find_parent("li"):
            continue
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if node.name.startswith("h"):
            flush()
            heading, fragment, paragraphs = text, node.get("id", ""), []
        else:
            paragraphs.append(text)
    flush()
    if not sections:
        raise ValueError("No NCI sections extracted")
    return sections


def create_seed_views(connection):
    """Expose human seed neighborhoods without modifying BioGRID's raw evidence."""
    connection.executescript('''
        CREATE VIEW IF NOT EXISTS biogrid_prostate_seed_interactions AS
        SELECT b.* FROM biogrid_interactions b
        WHERE "Organism ID Interactor A"='9606' AND "Organism ID Interactor B"='9606'
          AND ("Entrez Gene Interactor A" IN (SELECT entrez_id FROM kegg_genes)
            OR "Entrez Gene Interactor B" IN (SELECT entrez_id FROM kegg_genes));
        CREATE VIEW IF NOT EXISTS biogrid_prostate_internal_interactions AS
        SELECT b.* FROM biogrid_prostate_seed_interactions b
        WHERE "Entrez Gene Interactor A" IN (SELECT entrez_id FROM kegg_genes)
          AND "Entrez Gene Interactor B" IN (SELECT entrez_id FROM kegg_genes);
        CREATE VIEW IF NOT EXISTS biogrid_prostate_chemicals AS
        SELECT c.* FROM biogrid_chemicals c
        WHERE "Organism ID"='9606' AND "Entrez Gene ID" IN (SELECT entrez_id FROM kegg_genes);
    ''')


def build_database(collector):
    """Create a new local catalog and indexed data tables; never overwrite a DB."""
    import pyarrow.parquet as pq

    root, settings = collector.root, collector.settings
    target = root / "knowledge.sqlite"
    partial = root / "knowledge.partial.sqlite"
    if target.exists() or partial.exists():
        raise ValueError("Database already exists; use a new data root to rebuild without overwriting it")
    catalog_path = collector.config_path.parent.parent / "database/sources.json"
    catalog = json.loads(catalog_path.read_text())
    seed = json.loads((root / "derived/kegg" / f"{settings['pathway']}.seeds.json").read_text())
    graph = json.loads((root / "derived/kegg" / f"{settings['pathway']}.graph.json").read_text())
    counts = {}
    connection = sqlite3.connect(partial)
    try:
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-65536")
        connection.executescript("""
            CREATE TABLE sources (id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL);
            CREATE TABLE assets (path TEXT PRIMARY KEY, source TEXT, sha256 TEXT, metadata_json TEXT NOT NULL);
            CREATE TABLE imported_tables (table_name TEXT PRIMARY KEY, archive TEXT, member TEXT, sha256 TEXT, row_count INTEGER, columns_json TEXT);
            CREATE TABLE kegg_genes (kegg_id TEXT PRIMARY KEY, entrez_id TEXT);
            CREATE TABLE kegg_uniprot (kegg_id TEXT, uniprot TEXT, PRIMARY KEY (kegg_id, uniprot));
            CREATE TABLE kegg_entries (id TEXT PRIMARY KEY, entry_type TEXT, names TEXT, data_json TEXT);
            CREATE TABLE kegg_relations (entry1 TEXT, entry2 TEXT, relation_type TEXT, data_json TEXT);
            CREATE TABLE pinder_related (id TEXT PRIMARY KEY, uniprot_R TEXT, uniprot_L TEXT, split TEXT, data_json TEXT);
            CREATE TABLE pinder_metadata (id TEXT PRIMARY KEY, data_json TEXT);
            CREATE TABLE pinder_structures (id TEXT PRIMARY KEY, status TEXT, path TEXT, data_json TEXT);
            CREATE TABLE documents (id TEXT PRIMARY KEY, source TEXT, kind TEXT, url TEXT, sha256 TEXT, notes TEXT);
            CREATE VIRTUAL TABLE document_sections USING fts5(document_id UNINDEXED, heading, anchor UNINDEXED, text);
        """)
        connection.executemany("INSERT INTO sources VALUES (?, ?)", [(s["id"], json.dumps(s)) for s in catalog["sources"]])
        for receipt in root.rglob("*.receipt.json"):
            value = json.loads(receipt.read_text())
            path = str(receipt.relative_to(root)).removesuffix(".receipt.json")
            connection.execute("INSERT INTO assets VALUES (?, ?, ?, ?)", (path, value["source"], value["sha256"], json.dumps(value)))
        connection.executemany("INSERT INTO kegg_genes VALUES (?, ?)", [(g, g.removeprefix("hsa:")) for g in seed["genes"]])
        connection.executemany("INSERT INTO kegg_uniprot VALUES (?, ?)", sorted({(r["kegg_gene"], r["uniprot"]) for r in seed["gene_uniprot"]}))
        connection.executemany("INSERT INTO kegg_entries VALUES (?, ?, ?, ?)", [(e["id"], e["type"], e["name"], json.dumps(e)) for e in graph["entries"]])
        connection.executemany("INSERT INTO kegg_relations VALUES (?, ?, ?, ?)", [(r["entry1"], r["entry2"], r["type"], json.dumps(r)) for r in graph["relations"]])
        for filename, table in (("pathway_index.parquet", "pinder_related"), ("pathway_metadata.parquet", "pinder_metadata")):
            source = pq.ParquetFile(root / "derived/pinder" / settings["pinder_release"] / filename)
            for batch in source.iter_batches(batch_size=8192):
                if table == "pinder_related":
                    values = [(r["id"], r["uniprot_R"], r["uniprot_L"], r["split"], source_json(r)) for r in batch.to_pylist()]
                    connection.executemany("INSERT INTO pinder_related VALUES (?, ?, ?, ?, ?)", values)
                else:
                    connection.executemany("INSERT INTO pinder_metadata VALUES (?, ?)", [(r["id"], source_json(r)) for r in batch.to_pylist()])
            counts[table] = source.metadata.num_rows
        connection.execute("CREATE INDEX pinder_right ON pinder_related(uniprot_R)")
        connection.execute("CREATE INDEX pinder_left ON pinder_related(uniprot_L)")
        version = settings["biogrid_release"]
        for component, extension, table in (("ALL", "tab3", "interactions"), ("CHEMICALS", "chemtab", "chemicals"),
                                            ("PTMS", "ptm", "ptms"), ("IDENTIFIERS", "tab", "identifiers")):
            path = root / "raw/biogrid" / version / f"BIOGRID-{component}-{version}.{extension}.zip"
            with connection:
                counts.update(import_tab_archive(connection, path, table))
        nci = root / "raw/guidelines/nci/prostate-treatment-pdq.html"
        if nci.exists():
            sections = nci_sections(nci)
            connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)", (
                "nci-prostate-pdq", "nci", "evidence_summary_not_guideline",
                "https://www.cancer.gov/types/prostate/hp/prostate-treatment-pdq", sha256(nci),
                "NCI text-only extract; tables and images omitted; not a clinical practice guideline."))
            connection.executemany("INSERT INTO document_sections VALUES (?, ?, ?, ?)",
                                   [("nci-prostate-pdq", s["heading"], s["anchor"], s["text"]) for s in sections])
            counts["nci_sections"] = len(sections)
            write_json(root / "derived/guidelines/nci-sections.json", sections)
        connection.commit()
        # Capture receipts again after imports in case structure collection ran
        # concurrently. The manifest describes exactly this build's local assets.
        with connection:
            for receipt in root.rglob("*.receipt.json"):
                value = json.loads(receipt.read_text())
                path = str(receipt.relative_to(root)).removesuffix(".receipt.json")
                connection.execute("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?)",
                                   (path, value["source"], value["sha256"], json.dumps(value)))
            structure_report = root / "derived/pinder" / settings["pinder_release"] / "structures.json"
            if structure_report.exists():
                structures = json.loads(structure_report.read_text())["files"]
                connection.executemany("INSERT INTO pinder_structures VALUES (?, ?, ?, ?)",
                                       [(r["id"], r["status"], r.get("path"), json.dumps(r)) for r in structures])
                counts["pinder_structures_downloaded"] = sum(r["status"] == "downloaded" for r in structures)
        create_seed_views(connection)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        counts.update(kegg_genes=len(seed["genes"]), kegg_entries=len(graph["entries"]), kegg_relations=len(graph["relations"]))
    finally:
        connection.close()
    partial.replace(target)
    report = {"built_at": now(), "database": str(target.relative_to(root)), "bytes": target.stat().st_size,
              "counts": counts, "guideline_bodies": "Not stored in knowledge.sqlite; consult derived/guidelines/library/current.json for the separate document index and config/guidelines.jsonc for access controls",
              "pinder_scope": "All pathway-related index/metadata rows; representative PDB subset only"}
    write_json(root / "build-report.json", report)
    return report


def refresh_catalog(collector):
    """Update local source/structure availability without rebuilding large tables."""
    root = collector.root
    database = root / "knowledge.sqlite"
    if not database.is_file():
        raise FileNotFoundError(database)
    catalog = json.loads((collector.config_path.parent.parent / "database/sources.json").read_text())
    structure_report = root / "derived/pinder" / collector.settings["pinder_release"] / "structures.json"
    structures = json.loads(structure_report.read_text())["files"] if structure_report.exists() else []
    connection = sqlite3.connect(database)
    try:
        create_seed_views(connection)
        with connection:
            connection.executemany("INSERT OR REPLACE INTO sources VALUES (?, ?)",
                                   [(s["id"], json.dumps(s)) for s in catalog["sources"]])
            for receipt in root.rglob("*.receipt.json"):
                value = json.loads(receipt.read_text())
                relative = str(receipt.relative_to(root)).removesuffix(".receipt.json")
                if sha256(root / relative) != value["sha256"]:
                    raise ValueError(f"Asset checksum mismatch: {relative}")
                connection.execute("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?)",
                                   (relative, value["source"], value["sha256"], json.dumps(value)))
            connection.executemany("INSERT OR REPLACE INTO pinder_structures VALUES (?, ?, ?, ?)",
                                   [(r["id"], r["status"], r.get("path"), json.dumps(r)) for r in structures])
        report = json.loads((root / "build-report.json").read_text())
        report["guideline_bodies"] = "Not stored in knowledge.sqlite; consult derived/guidelines/library/current.json for the separate document index and config/guidelines.jsonc for access controls"
        report["counts"]["pinder_structures_downloaded"] = connection.execute(
            "SELECT count(*) FROM pinder_structures WHERE status='downloaded'").fetchone()[0]
        report["catalog_refreshed_at"] = now()
        report["bytes"] = database.stat().st_size
        write_json(root / "build-report.json", report)
        return report
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "prepare", "structures", "build", "refresh-catalog"))
    parser.add_argument("--config", default="config/database.jsonc")
    parser.add_argument("--sources", nargs="+", choices=("kegg", "pinder", "biogrid", "nci"),
                        default=["kegg", "pinder", "biogrid", "nci"])
    args = parser.parse_args()
    collector = Collector(args.config)
    if args.command == "fetch":
        collector.collect(args.sources)
    elif args.command == "prepare":
        graph, seed = kegg_graph(collector.root, collector.settings["pathway"])
        rows, report = related_pinder(collector.root, collector.settings["pinder_release"], seed)
        print(json.dumps({"kegg_entries": len(graph["entries"]), "kegg_relations": len(graph["relations"]),
                          "kegg_genes": len(seed["genes"]), "pinder": report}, indent=2))
    elif args.command == "structures":
        report = download_structures(collector)
        print(json.dumps({"requested": len(report["files"]),
                          "downloaded": sum(f["status"] == "downloaded" for f in report["files"]),
                          "bytes": report.get("downloaded_bytes", 0)}))
    elif args.command == "build":
        print(json.dumps(build_database(collector), indent=2))
    else:
        print(json.dumps(refresh_catalog(collector), indent=2))


if __name__ == "__main__":
    main()
