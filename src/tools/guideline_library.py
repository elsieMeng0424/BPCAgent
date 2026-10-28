"""Versioned, page-aware document retrieval without clinical rule inference."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    publication_date: str
    kind: Literal["clinical_practice_guideline", "evidence_summary", "test_document"]
    format: Literal["pdf", "sections_json"]
    path: str
    source_url: str
    processing_allowed: bool = False
    external_model_allowed: bool = False
    permission_basis: str = ""
    ocr: Literal["off", "auto"] = "off"
    ocr_language: str = "eng"
    max_pages: int = Field(500, ge=1, le=2000)

    @model_validator(mode="after")
    def validate_permissions(self):
        if self.processing_allowed and not self.permission_basis.strip():
            raise ValueError("Allowed processing requires a documented permission basis")
        if self.external_model_allowed and not self.processing_allowed:
            raise ValueError("External model use requires processing permission")
        return self


class LibrarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    data_root: str
    library_dir: str = "derived/guidelines/library"
    sources: list[DocumentSource]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("Document IDs must be unique")
        return self


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def contained(root, value):
    """Reject absolute paths, traversal and symlinks escaping a host-owned root."""
    if Path(value).is_absolute():
        raise ValueError("Document paths must be relative to their configured root")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Document path escapes its configured root")
    return path


def load_library(config_file):
    # Lazy import avoids a workflow-config -> tools -> workflow-config cycle.
    from ..workflow.config import read_object
    config_file = Path(config_file).resolve()
    settings = LibrarySettings.model_validate(read_object(config_file))
    root = (config_file.parent / settings.data_root).resolve()
    directory = contained(root, settings.library_dir)
    return settings, root, directory


def fingerprint(source):
    return hashlib.sha256(source.model_dump_json().encode()).hexdigest()


def extract_pdf(path, source):
    """Keep full-page text and block coordinates; never infer arrows or rules."""
    import pymupdf
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("Encrypted PDF requires a separately authorized readable copy")
        if len(document) > source.max_pages:
            raise ValueError("PDF exceeds configured page limit")
        for number, page in enumerate(document, 1):
            method, warnings = "native_text", []
            text_page = None
            text = page.get_text("text", sort=True)
            if not re.search(r"\w", text):
                if source.ocr == "auto":
                    try:
                        text_page = page.get_textpage_ocr(language=source.ocr_language, dpi=150, full=True)
                        text = page.get_text("text", textpage=text_page, sort=True)
                        method = "ocr"
                        if not re.search(r"\w", text):
                            method = "ocr_no_text"
                            warnings.append("OCR produced no readable text; inspect the original page before drawing conclusions.")
                    except Exception:
                        method = "ocr_failed"
                        warnings.append("OCR failed; inspect the original page. No empty-page inference is valid.")
                else:
                    method = "needs_ocr"
                    warnings.append("No native text found; OCR is disabled. This is not a negative finding.")
            blocks = [{"bbox": list(b[:4]), "text": b[4], "block_number": b[5]}
                      for b in page.get_text("blocks", textpage=text_page, sort=True) if b[6] == 0]
            labels = list(dict.fromkeys(re.findall(r"\b(?:PROS-[A-Z0-9]+|ST-\d+|ABBR-\d+)\b", text)))
            links = [{"pdf_page": link["page"] + 1, "bbox": list(link["from"])}
                     for link in page.get_links() if link.get("page", -1) >= 0]
            kind = "updates" if re.search(r"(?mi)^\s*UPDATES\s*$", text) else "body"
            yield {"section_id": f"page-{number:04d}", "heading": f"PDF page {number}",
                   "pdf_page": number, "printed_page_label": page.get_label() or None,
                   "section_mentions": labels, "content_type": kind,
                   "text": text, "blocks": blocks, "page_links": links,
                   "extraction_method": method, "review_status": "needs_review",
                   "layout_status": "requires_visual_verification", "warnings": warnings,
                   "footnote_status": "preserved_in_page_text_not_semantically_resolved"}


def extract_sections(path):
    """Import already prepared, attributed text; keep references as source text."""
    sections = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(sections, list):
        raise ValueError("Section input must be a JSON array")
    for number, item in enumerate(sections, 1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("heading"), str):
            raise ValueError("Each section requires text and heading strings")
        yield {"section_id": f"section-{number:04d}", "heading": item["heading"],
               "anchor": item.get("anchor"), "pdf_page": None, "text": item["text"],
               "content_type": "body", "extraction_method": "prepared_text",
               "review_status": "needs_review", "layout_status": "text_only",
               "footnote_status": "references_preserved_as_source_text", "warnings": []}


def build_library(config_file):
    """Publish a complete new snapshot atomically; leave old snapshots intact."""
    settings, root, directory = load_library(config_file)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = "snapshot_" + uuid4().hex
    report = {"snapshot": snapshot, "created_at": datetime.now(timezone.utc).isoformat(),
              "knowledge_time_policy": "fixed_version_retrospective_research_not_historical_reconstruction",
              "documents": [], "blocked": []}
    with tempfile.TemporaryDirectory(prefix=".building_", dir=directory) as temporary:
        staging = Path(temporary)
        with closing(sqlite3.connect(staging / "index.sqlite")) as connection:
            connection.executescript("""
                CREATE TABLE documents (id TEXT PRIMARY KEY, fingerprint TEXT, sha256 TEXT, metadata TEXT);
                CREATE TABLE sections (document_id TEXT, section_id TEXT, payload TEXT,
                    PRIMARY KEY (document_id, section_id));
                CREATE VIRTUAL TABLE search USING fts5(document_id UNINDEXED, section_id UNINDEXED,
                    content_type UNINDEXED, heading, text, tokenize='unicode61');
            """)
            for source in settings.sources:
                if not source.processing_allowed:
                    report["blocked"].append({"document_id": source.id, "status": "permission_required"})
                    continue  # Do not even open a permission-blocked source.
                path = contained(root, source.path)
                if path.stat().st_size > 100_000_000:
                    raise ValueError("Source exceeds the 100 MB ingestion limit")
                sha = file_sha256(path)
                metadata = {**source.model_dump(exclude={"path"}), "sha256": sha}
                connection.execute("INSERT INTO documents VALUES (?,?,?,?)",
                                   (source.id, fingerprint(source), sha, json.dumps(metadata)))
                pages = extract_pdf(path, source) if source.format == "pdf" else extract_sections(path)
                export = staging / source.id
                export.mkdir()
                count, warnings = 0, []
                for section in pages:
                    count += 1
                    section.update(document_id=source.id, version=source.version, source_sha256=sha,
                                   source_url=source.source_url, document_kind=source.kind)
                    section["citation_id"] = f"{source.id}:{sha}:{section['section_id']}"
                    payload = json.dumps(section, ensure_ascii=False)
                    connection.execute("INSERT INTO sections VALUES (?,?,?)", (source.id, section["section_id"], payload))
                    connection.execute("INSERT INTO search VALUES (?,?,?,?,?)",
                                       (source.id, section["section_id"], section["content_type"], section["heading"], section["text"]))
                    (export / (section["section_id"] + ".json")).write_text(payload, encoding="utf-8")
                    (export / (section["section_id"] + ".md")).write_text(
                        f"# {section['heading']}\n\nSource: {source.title}; version: {source.version}\n\n"
                        f"Citation: {section['citation_id']}\n\nStatus: needs_review; not an executable clinical rule.\n\n"
                        + section["text"], encoding="utf-8")
                    if section["warnings"]:
                        warnings.append({"section_id": section["section_id"], "warnings": section["warnings"]})
                if file_sha256(path) != sha:
                    raise ValueError("Source changed during ingestion; snapshot was not published")
                report["documents"].append({"document_id": source.id, "sha256": sha, "sections": count, "warnings": warnings})
            connection.commit()
        write_json(staging / "report.json", report)
        os.replace(staging, directory / snapshot)
    # A unique temporary pointer also supports concurrent builders without torn JSON.
    pointer = directory / (".current_" + uuid4().hex + ".json")
    write_json(pointer, {"snapshot": snapshot})
    os.replace(pointer, directory / "current.json")
    return report


class GuidelineLibrary:
    """Read-only retrieval with permission, version and source-integrity checks."""

    def __init__(self, config_file, *, external=True):
        self.config_file = Path(config_file).resolve()
        self.external = external
        self._database = None

    def catalog(self):
        settings, _, _ = load_library(self.config_file)
        return [{"document_id": s.id, "title": s.title, "version": s.version, "kind": s.kind,
                 "processing_allowed": s.processing_allowed, "external_model_allowed": s.external_model_allowed,
                 "status": "eligible_for_indexing" if s.processing_allowed else "permission_required"}
                for s in settings.sources]

    def _source(self, document_id):
        settings, root, directory = load_library(self.config_file)
        source = next((s for s in settings.sources if s.id == document_id), None)
        if source is None:
            raise ValueError("Unknown document ID")
        if not source.processing_allowed or (self.external and not source.external_model_allowed):
            raise PermissionError("Document is not authorized for this processing destination")
        return source, contained(root, source.path), directory

    def _connection(self):
        if self._database is None:
            _, _, directory = load_library(self.config_file)
            pointer = json.loads((directory / "current.json").read_text())
            if not re.fullmatch(r"snapshot_[0-9a-f]{32}", pointer["snapshot"]):
                raise ValueError("Invalid document snapshot pointer")
            self._database = contained(directory, pointer["snapshot"] + "/index.sqlite")
        connection = sqlite3.connect(self._database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def snapshot_info(self):
        """Pin this reader to one snapshot while permission checks remain live."""
        with closing(self._connection()) as connection:
            documents = [{"document_id": row["id"], "sha256": row["sha256"],
                          "version": json.loads(row["metadata"])["version"]}
                         for row in connection.execute("SELECT * FROM documents ORDER BY id")]
        return {"snapshot": self._database.parent.name, "documents": documents,
                "knowledge_time_policy": "fixed_version_retrospective_research_not_historical_reconstruction"}

    def _verified(self, connection, document_id):
        source, path, _ = self._source(document_id)
        row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError("Document is not indexed; build the library first")
        if row["fingerprint"] != fingerprint(source) or row["sha256"] != file_sha256(path):
            raise ValueError("Document or manifest changed; rebuild the library before retrieval")
        return source, path, row["sha256"]

    def search(self, query, document_id=None, version=None, limit=5, include_updates=False):
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens or len(query) > 500 or not 1 <= limit <= 10:
            raise ValueError("Use a nonempty query up to 500 characters and limit 1..10")
        # Quote plain terms instead of exposing arbitrary FTS syntax to the model.
        expression = " AND ".join('"' + token + '"' for token in tokens)
        allowed, unavailable = [], []
        settings, _, _ = load_library(self.config_file)
        if document_id is not None:
            self._source(document_id)  # Explicit requests must not hide a permission failure.
        with closing(self._connection()) as connection:
            for source in settings.sources:
                if document_id is not None and source.id != document_id or version is not None and source.version != version:
                    continue
                try:
                    self._verified(connection, source.id)
                    allowed.append(source.id)
                except (PermissionError, ValueError, FileNotFoundError) as exc:
                    if document_id is not None:
                        raise
                    unavailable.append({"document_id": source.id, "reason": str(exc)})
            results = []
            if allowed:
                markers = ",".join("?" for _ in allowed)
                rows = connection.execute(
                    f"SELECT document_id, section_id, snippet(search,4,'[',']','…',45) AS excerpt "
                    f"FROM search WHERE search MATCH ? AND document_id IN ({markers}) "
                    "AND (? OR content_type != 'updates') ORDER BY rank LIMIT ?",
                    (expression, *allowed, include_updates, limit)).fetchall()
                for row in rows:
                    section = json.loads(connection.execute("SELECT payload FROM sections WHERE document_id=? AND section_id=?",
                                                            (row["document_id"], row["section_id"])).fetchone()[0])
                    results.append({key: section.get(key) for key in (
                        "document_id", "version", "document_kind", "section_id", "heading", "pdf_page", "source_url",
                        "citation_id", "review_status", "layout_status")} | {"excerpt": row["excerpt"], "excerpt_only": True})
        return {"results": results, "unavailable_sources": unavailable,
                "warning": "Search excerpts are not complete evidence. Read the section and linked footnotes before use. No hits do not establish absence."}

    def section(self, document_id, section_id, offset=0, max_chars=12000):
        if offset < 0 or not 1 <= max_chars <= 20000:
            raise ValueError("Invalid section pagination")
        self._source(document_id)
        with closing(self._connection()) as connection:
            self._verified(connection, document_id)
            row = connection.execute("SELECT payload FROM sections WHERE document_id=? AND section_id=?",
                                     (document_id, section_id)).fetchone()
            if row is None:
                raise ValueError("Unknown section ID")
            section = json.loads(row[0])
        text = section.pop("text")
        # Blocks are retained in the offline JSON, not duplicated in every model response.
        section.pop("blocks", None)
        if offset > len(text):
            raise ValueError("Offset exceeds section length")
        end = min(len(text), offset + max_chars)
        section.update(text=text[offset:end], offset=offset, total_chars=len(text),
                       next_offset=end if end < len(text) else None,
                       complete_section=offset == 0 and end == len(text),
                       warning="Read all fragments and cross-page footnotes; extraction does not validate clinical rules.")
        return section

    def page(self, document_id, pdf_page, output_directory):
        """Render one verified original page; returning a path alone is NOT model vision."""
        import pymupdf
        self._source(document_id)
        with closing(self._connection()) as connection:
            source, path, sha = self._verified(connection, document_id)
        if source.format != "pdf":
            raise ValueError("This source has no PDF pages")
        with pymupdf.open(path) as document:
            if type(pdf_page) is not int or not 1 <= pdf_page <= len(document):
                raise ValueError("PDF page is 1-based and must be within the document")
            page = document[pdf_page - 1]
            if page.rect.width <= 0 or page.rect.height <= 0:
                raise ValueError("Invalid page dimensions")
            scale = min(2.0, 1800 / max(page.rect.width, page.rect.height))
            image = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            output_directory = Path(output_directory)
            output_directory.mkdir(parents=True, exist_ok=True)
            destination = output_directory / f"{source.id}_{sha[:16]}_{pdf_page:04d}.png"
            image.save(destination)
        if file_sha256(path) != sha:
            raise ValueError("Source changed while rendering")
        return {"document_id": source.id, "version": source.version, "pdf_page": pdf_page,
                "citation_id": f"{source.id}:{sha}:page-{pdf_page:04d}", "source_sha256": sha,
                "image_path": str(destination.resolve()), "image_sha256": file_sha256(destination),
                "model_has_seen_image": False, "review_status": "needs_review"}
