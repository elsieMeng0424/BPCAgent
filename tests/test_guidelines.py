"""Offline document ingestion, permissions, citations and real image payload tests."""

from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bpcagent.tools.guideline_library import GuidelineLibrary, build_library, load_library
from bpcagent.tools.guidelines import GuidelineTools, GuidelineToolConfig
from bpcagent.tools.tool import ToolRegistry


@pytest.fixture
def library_fixture(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "sections.json").write_text(json.dumps([
        {"heading": "Treatment evidence", "anchor": "treatment", "text": "DrugAlpha evidence. Footnote a: special condition applies. " * 8},
        {"heading": "Other evidence", "anchor": "other", "text": "DrugBeta research findings."},
    ]))
    source = {"id": "public", "title": "Test evidence summary", "version": "v1", "publication_date": "2026-01-01",
              "kind": "test_document", "format": "sections_json", "path": "sections.json",
              "source_url": "https://example.invalid/evidence", "processing_allowed": True,
              "external_model_allowed": True, "permission_basis": "Synthetic test data owned by the test author."}
    blocked = {**source, "id": "restricted", "path": "DO-NOT-OPEN.pdf", "format": "pdf",
               "processing_allowed": False, "external_model_allowed": False}
    config = tmp_path / "library.json"
    config.write_text(json.dumps({"data_root": "data", "sources": [source, blocked]}))
    return config, root, source


def change_source(config, **values):
    payload = json.loads(config.read_text())
    payload["sources"][0].update(values)
    config.write_text(json.dumps(payload))


def test_build_search_and_complete_citation(library_fixture):
    config, root, _ = library_fixture
    report = build_library(config)
    assert report["documents"][0]["sections"] == 2
    assert report["blocked"] == [{"document_id": "restricted", "status": "permission_required"}]
    library = GuidelineLibrary(config)
    result = library.search("DrugAlpha")
    hit = result["results"][0]
    section = library.section("public", hit["section_id"])
    assert section["complete_section"]
    assert "Footnote a: special condition applies." in section["text"]
    assert section["citation_id"] == hit["citation_id"]
    assert section["review_status"] == "needs_review"
    assert section["pdf_page"] is None
    assert library.search("DrugAlpha", version="v2")["results"] == []
    exports = list((root / "derived/guidelines/library").glob("snapshot_*/public/*.md"))
    assert len(exports) == 2
    assert any(section["citation_id"] in path.read_text() for path in exports)


def test_blocked_sources_not_opened_and_cannot_be_read(library_fixture):
    config, _, _ = library_fixture
    # The blocked PDF does not exist, so a successful build proves it was skipped.
    build_library(config)
    library = GuidelineLibrary(config)
    for read in (lambda: library.search("text", "restricted"),
                 lambda: library.section("restricted", "page-0001"),
                 lambda: library._source("restricted")):
        with pytest.raises(PermissionError):
            read()


def test_revoking_permissions_blocks_existing_index(library_fixture):
    config, _, _ = library_fixture
    build_library(config)
    library = GuidelineLibrary(config)
    assert library.search("DrugAlpha")["results"]
    change_source(config, processing_allowed=False, external_model_allowed=False)
    with pytest.raises(PermissionError):
        library.section("public", "section-0001")
    assert library.search("DrugAlpha")["results"] == []


def test_local_permission_is_not_external_model_permission(library_fixture):
    config, _, _ = library_fixture
    change_source(config, external_model_allowed=False)
    build_library(config)
    assert GuidelineLibrary(config, external=False).section("public", "section-0001")["text"]
    with pytest.raises(PermissionError):
        GuidelineLibrary(config).section("public", "section-0001")
    assert GuidelineLibrary(config, external=False).search("DrugAlpha", "public")["results"]
    assert GuidelineLibrary(config).search("DrugAlpha")["results"] == []
    with pytest.raises(PermissionError):
        GuidelineLibrary(config).search("DrugAlpha", "public")


@pytest.mark.parametrize("change", ["source", "version"])
def test_stale_index_fails_closed(library_fixture, change):
    config, root, _ = library_fixture
    build_library(config)
    if change == "source":
        (root / "sections.json").write_text("[]")
    else:
        change_source(config, version="v2")
    with pytest.raises(ValueError, match="rebuild"):
        GuidelineLibrary(config).section("public", "section-0001")


def test_paginated_sections_reconstruct_exact_source(library_fixture):
    config, root, _ = library_fixture
    build_library(config)
    library = GuidelineLibrary(config)
    text, offset = "", 0
    while True:
        page = library.section("public", "section-0001", offset, 31)
        assert not page["complete_section"]
        text += page["text"]
        offset = page["next_offset"]
        if offset is None:
            break
    assert text == json.loads((root / "sections.json").read_text())[0]["text"]
    with pytest.raises(ValueError):
        library.section("public", "section-0001", len(text) + 1)


def test_failed_rebuild_leaves_published_snapshot_unchanged(library_fixture):
    config, root, _ = library_fixture
    report = build_library(config)
    change_source(config, path="missing.json")
    with pytest.raises(FileNotFoundError):
        build_library(config)
    pointer = json.loads((root / "derived/guidelines/library/current.json").read_text())
    assert pointer["snapshot"] == report["snapshot"]
    assert not list((root / "derived/guidelines/library").glob(".building_*"))


def test_paths_and_duplicate_ids_rejected(library_fixture):
    config, _, source = library_fixture
    change_source(config, path="../../private.json")
    with pytest.raises(ValueError, match="escapes"):
        build_library(config)
    payload = json.loads(config.read_text())
    payload["sources"] = [source, source]
    config.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="unique"):
        load_library(config)


def test_fts_does_not_execute_query_syntax_and_database_is_readonly(library_fixture):
    config, _, _ = library_fixture
    build_library(config)
    library = GuidelineLibrary(config)
    assert library.search('DrugAlpha" OR DrugBeta --')["results"] == []
    with closing(library._connection()) as connection:
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM sections")


@pytest.fixture
def pdf_fixture(library_fixture):
    pymupdf = pytest.importorskip("pymupdf")
    config, root, _ = library_fixture
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((60, 60), "PROS-TEST\nDrugAlpha condition -> Option A\nFootnote a: requires verification.")
        page.draw_line((100, 120), (220, 120))
        document.new_page().insert_text((60, 60), "UPDATES\nDrugAlpha old recommendation replaced.")
        document.new_page()  # Empty or image-only page must not silently imply absence.
        document.save(root / "test.pdf")
    change_source(config, path="test.pdf", format="pdf")
    return config, root


def test_pdf_native_text_blocks_footnotes_and_update_filter(pdf_fixture):
    config, root = pdf_fixture
    report = build_library(config)
    assert report["documents"][0]["sections"] == 3
    assert report["documents"][0]["warnings"][0]["section_id"] == "page-0003"
    library = GuidelineLibrary(config)
    assert len(library.search("DrugAlpha")["results"]) == 1
    assert len(library.search("DrugAlpha", include_updates=True)["results"]) == 2
    section = library.section("public", "page-0001")
    assert section["pdf_page"] == 1
    assert section["extraction_method"] == "native_text"
    assert "Footnote a" in section["text"]
    assert "PROS-TEST" in section["section_mentions"]
    export = root / "derived/guidelines/library" / report["snapshot"] / "public/page-0001.json"
    assert json.loads(export.read_text())["blocks"][0]["bbox"]


def test_ocr_failure_explicit_and_native_pages_do_not_use_ocr(pdf_fixture):
    import pymupdf
    config, _ = pdf_fixture
    change_source(config, ocr="auto")
    with patch.object(pymupdf.Page, "get_textpage_ocr", side_effect=RuntimeError("No Tesseract")) as ocr:
        build_library(config)
    assert ocr.call_count == 1
    section = GuidelineLibrary(config).section("public", "page-0003")
    assert section["extraction_method"] == "ocr_failed"
    assert section["warnings"]


@pytest.mark.parametrize("recognized", [True, False])
def test_ocr_success_and_empty_result_are_distinct(pdf_fixture, recognized):
    import pymupdf
    config, _ = pdf_fixture
    change_source(config, ocr="auto")

    def fake_ocr(page, **kwargs):
        if recognized:
            page.insert_text((60, 60), "Synthetic OCR text. Footnote applies.")
        return page.get_textpage()

    with patch.object(pymupdf.Page, "get_textpage_ocr", fake_ocr):
        build_library(config)
    section = GuidelineLibrary(config).section("public", "page-0003")
    assert section["extraction_method"] == ("ocr" if recognized else "ocr_no_text")
    assert ("Synthetic OCR text" in section["text"]) == recognized
    if not recognized:
        assert section["warnings"]


def test_render_is_not_a_visual_model_call(pdf_fixture, tmp_path):
    config, _ = pdf_fixture
    build_library(config)
    page = GuidelineLibrary(config).page("public", 1, tmp_path / "images")
    assert Path(page["image_path"]).read_bytes().startswith(b"\x89PNG")
    assert not page["model_has_seen_image"]
    for invalid in (0, 4, -1):
        with pytest.raises(ValueError):
            GuidelineLibrary(config).page("public", invalid, tmp_path)


def make_tools(config, tmp_path, **kwargs):
    registry = ToolRegistry()
    artifacts = {}
    tools = GuidelineTools(GuidelineToolConfig(enabled=True, library_config=str(config), max_vision_calls=1),
                          tmp_path / "turn", artifacts, **kwargs)
    tools.register(registry)
    return registry, tools, artifacts


def test_tool_permissions_and_missing_vision_are_explicit(library_fixture, tmp_path):
    config, _, _ = library_fixture
    build_library(config)
    registry, _, artifacts = make_tools(config, tmp_path)
    result = registry.execute_tool("inspect_guideline_page", {"document_id": "public", "pdf_page": 1, "question": "Read arrows"})
    assert result.data["status"] == "vision_unavailable"
    assert not result.data["model_has_seen_image"]
    blocked = registry.execute_tool("get_guideline_section", {"document_id": "restricted", "section_id": "page-0001"})
    assert blocked.status == "error"
    assert len(artifacts["guideline_audit"]) == 2
    invalid = registry.execute_tool("get_guideline_page", {"document_id": "public", "pdf_page": 0})
    assert invalid.status == "error"


def test_vision_receives_actual_image_and_is_bounded_and_audited(pdf_fixture, tmp_path):
    from bpcagent.agents import LLMResponse
    config, _ = pdf_fixture
    build_library(config)
    messages_seen = []

    class Vision:
        model = "offline-image-test"

        def invoke(self, messages, **kwargs):
            messages_seen.append(messages)
            return LLMResponse(content="A visible arrow. Footnote needs checking.", finish_reason="stop")

    registry, _, artifacts = make_tools(config, tmp_path, vision_llm=Vision())
    arguments = {"document_id": "public", "pdf_page": 1, "question": "Read visible arrows and footnotes"}
    first = registry.execute_tool("inspect_guideline_page", arguments)
    assert first.data["status"] == "interpreted_unverified"
    assert first.data["model_has_seen_image"]
    assert first.data["review_status"] == "needs_review"
    assert messages_seen[0][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert registry.execute_tool("inspect_guideline_page", arguments).data["status"] == "vision_budget_exhausted"
    assert len(messages_seen) == 1
    assert len(artifacts["guideline_pages"]) == 1
    audit = Path(artifacts["guideline_audit"][0]).read_text()
    assert "interpreted_unverified" in audit
    assert "base64" not in audit


def test_incomplete_vision_response_is_not_accepted(pdf_fixture, tmp_path):
    from bpcagent.agents import LLMResponse
    config, _ = pdf_fixture
    build_library(config)
    reader = SimpleNamespace(invoke=lambda *a, **k: LLMResponse(content="Partial", finish_reason="length"))
    registry, _, _ = make_tools(config, tmp_path, vision_llm=reader)
    result = registry.execute_tool("inspect_guideline_page", {"document_id": "public", "pdf_page": 1, "question": "Read"})
    assert result.data["status"] == "vision_incomplete"
    assert "interpretation" not in result.data


def test_provider_error_is_not_a_successful_interpretation(pdf_fixture, tmp_path):
    config, _ = pdf_fixture
    build_library(config)

    def fail(*args, **kwargs):
        raise RuntimeError("do-not-log-provider-secret")

    registry, _, artifacts = make_tools(config, tmp_path, vision_llm=SimpleNamespace(invoke=fail))
    result = registry.execute_tool("inspect_guideline_page", {"document_id": "public", "pdf_page": 1, "question": "Read"})
    assert result.data["status"] == "vision_failed"
    assert not result.data["model_has_seen_image"]
    assert "do-not-log-provider-secret" not in Path(artifacts["guideline_audit"][0]).read_text()


def test_reader_pins_snapshot_but_checks_current_permissions(library_fixture):
    config, _, _ = library_fixture
    first = build_library(config)
    reader = GuidelineLibrary(config)
    assert reader.snapshot_info()["snapshot"] == first["snapshot"]
    second = build_library(config)
    assert second["snapshot"] != first["snapshot"]
    assert reader.snapshot_info()["snapshot"] == first["snapshot"]
    assert GuidelineLibrary(config).snapshot_info()["snapshot"] == second["snapshot"]
    change_source(config, external_model_allowed=False)
    with pytest.raises(PermissionError):
        reader.section("public", "section-0001")


def test_only_complete_section_reads_become_citable(library_fixture, tmp_path):
    config, _, _ = library_fixture
    build_library(config)
    registry, tools, _ = make_tools(config, tmp_path)
    hit = registry.execute_tool("search_guideline", {"query": "DrugAlpha"}).data["results"][0]
    assert not tools.citable_refs
    args = {"document_id": "public", "section_id": hit["section_id"], "max_chars": 40}
    part = registry.execute_tool("get_guideline_section", args).data
    assert not tools.citable_refs
    registry.execute_tool("get_guideline_section", {**args, "offset": part["total_chars"] - 1})
    assert not tools.citable_refs  # A gap cannot be hidden by reading the end.
    registry.execute_tool("get_guideline_section", {**args, "offset": 40, "max_chars": 20000})
    assert tools.citable_refs == {hit["citation_id"]}


def test_workflow_runs_real_document_tools_in_each_task_and_review(library_fixture, tmp_path):
    from test_workflow import fixture_dataset
    from bpcagent.agents import LLMResponse, ToolCall
    from bpcagent.workflow import AgentWorkflow
    from bpcagent.workflow.demo import DemoLLM
    config, _, _ = library_fixture
    build_library(config)
    citation = GuidelineLibrary(config).section("public", "section-0001")["citation_id"]

    class ReadingDemo(DemoLLM):
        def __init__(self, role):
            super().__init__(role)
            self.reading_step = 0

        def invoke(self, messages, **kwargs):
            self.reading_step += 1
            if self.reading_step <= 2:
                name, args = ("search_guideline", {"query": "DrugAlpha"}) if self.reading_step == 1 else (
                    "get_guideline_section", {"document_id": "public", "section_id": "section-0001"})
                return LLMResponse(finish_reason="tool_calls", tool_calls=[
                    ToolCall(id=f"reading_{self.reading_step}", name=name, arguments=json.dumps(args))])
            response = super().invoke(messages, **kwargs)
            for call in response.tool_calls:
                if call.name == "submit_output":
                    output = json.loads(call.arguments)
                    output["document_refs"] = [citation]
                    call.arguments = json.dumps(output)
            return response

    workflow = AgentWorkflow(data_dir=fixture_dataset(tmp_path / "patients"), workspace_root=tmp_path / "runs",
                             llm_factory=ReadingDemo, guidelines={"enabled": True, "library_config": str(config)},
                             patient_shell={"mode": "disabled"})
    result = workflow.run(patient_id="P1", cutoff_day=50)
    assert result["finished"]
    assert len(result["turns"]) == 6
    assert result["guideline_snapshot"]["documents"][0]["version"] == "v1"
    for turn in result["turns"]:
        assert turn["output"]["document_refs"] == [citation]
        assert len(turn["artifacts"]["guideline_audit"]) == 2


def test_agent_rejects_fabricated_document_citations(library_fixture, tmp_path):
    from bpcagent.agents import Config
    from bpcagent.workflow.agents import TargetSelectionAgent
    from bpcagent.workflow.demo import DemoLLM
    config, _, _ = library_fixture
    agent = TargetSelectionAgent(shared=None, context={}, directory=tmp_path / "agent",
                                 config=Config(model="offline"), llm=DemoLLM("TargetSelectionAgent"),
                                 guidelines={"enabled": True, "library_config": str(config)})
    with pytest.raises(ValueError, match="Document citations"):
        agent.submit({"document_refs": ["invented:sha:page-0001"]})
