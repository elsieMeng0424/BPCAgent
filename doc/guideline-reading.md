# Page-aware document reading

The document tools combine local text retrieval with optional single-page visual
inspection. They do not generate validated clinical rules or enable clinical drug
recommendations. Scientific-placeholder guards remain enabled unless the separate
`guideline_debug_tools` option is explicitly selected; that option admits only
host-issued, clearly labeled NCI text-match fixtures, never clinical scores.

## Current state

- NCI PDQ: 120 prepared, attributed text sections are indexed. These are evidence
  summaries, not clinical practice guidelines. The source extraction excluded
  images and tables with potentially separate reuse conditions.
- NCCN: local `prostate.pdf` is registered as version `1.2027`, dated `2026-10-12`.
  Its 253 pages are locally indexed at the user's explicit request, with native
  text, Markdown/JSON exports, block coordinates and page references. This local
  processing request is not recorded as a publisher license grant. External-model
  access remains disabled pending confirmation of applicable AI/RAG authorization.
- Page vision: implemented as a separate optional model call; disabled by default
  (`vision_profile: null`). Tests verify actual image payloads using a model
  double; no paid provider call was made. A local PNG path alone never means that
  the language model has seen the image.

## Files and responsibilities

```text
config/guidelines.jsonc           Source versions, paths, permissions, OCR settings
config/tools.jsonc                Tool enablement, optional vision profile/budget
scripts/guidelines.py             Offline build/catalog/search/section/page CLI
src/tools/guideline_library.py    Ingestion, provenance, FTS, retrieval, rendering
src/tools/guidelines.py           Agent interfaces, visual reading, per-turn audit
database/data/                    Ignored by Git
  raw/guidelines/                 Unmodified originals
  derived/guidelines/library/
    current.json                 Atomic pointer to latest published snapshot
    snapshot_<id>/
      index.sqlite               Documents, sections and SQLite FTS5 index
      report.json                Hashes, counts, blocked documents, OCR warnings
      <document_id>/*.json        Full text, block coordinates and provenance
      <document_id>/*.md          Readable text exports, not reconstructed arrows
workspace/<date>/<run>/<turn>_<Agent>/guidelines/
  <sequence>_<tool>.json          Exact arguments/results or sanitized error type
  pages/*.png                    Original pages rendered only on request
```

This library is separate from BioGRID/PINDER `knowledge.sqlite`; building it does
not rebuild or overwrite that database. Every build publishes a new snapshot and
retains earlier snapshots. Failed builds leave the active pointer unchanged. Old
snapshots consume space and are not automatically deleted. Raw files are unchanged.

## Build and inspect without a model

Run from the repository root:

```bash
.venv/bin/python -m pip install -e '.[documents]'
.venv/bin/python scripts/guidelines.py build
.venv/bin/python scripts/guidelines.py catalog
.venv/bin/python scripts/guidelines.py search docetaxel --limit 3
.venv/bin/python scripts/guidelines.py section nci-prostate-pdq section-0107
.venv/bin/python scripts/guidelines.py search docetaxel --document-id nccn-prostate --limit 3
.venv/bin/python scripts/guidelines.py section nccn-prostate page-0001
```

Prefer obtaining section IDs from search instead of assuming they survive source
updates. Citations are `document_id:source_sha256:section_id`; responses also
include version, document kind, source URL, and PDF page or HTML anchor if available.

Long sections return `next_offset`. Pass it as `--offset` to continue; collect all
fragments before using the section. `complete_section` is true only when one
response contains the entire section. Reading the final fragment alone does not
constitute a full read. Transport pagination does not summarize stored evidence.

PDF commands for a registered, authorized source (the ID below is a placeholder):

```bash
.venv/bin/python scripts/guidelines.py section authorized-document page-0001
.venv/bin/python scripts/guidelines.py page authorized-document 1 --output workspace/document-inspection
```

The CLI makes no model calls and checks local processing permission. Agent tools
also check `external_model_allowed`, including before returning text to an Agent.

## Workflow tools

`config/tools.jsonc` enables these tools for every instantiated workflow role:

| Tool | Behavior |
| --- | --- |
| `list_guideline_sources` | List metadata and permission eligibility, not indexing success. |
| `search_guideline` | Bounded keyword search; optional document/version filters; return snippets. |
| `get_guideline_section` | Read full text or explicit fragments with provenance and page links. |
| `get_guideline_page` | Render an original page; `model_has_seen_image=false`. |
| `inspect_guideline_page` | Send one original page to the vision model; interpretation remains unverified. |

Search uses FTS5 with **AND between plain terms**, not embeddings or medical
synonym expansion. Use short English keywords matching the source; Chinese queries
are not automatically translated. No hits do not establish a negative finding.
Pages with a standalone `UPDATES` line are excluded by default. This heuristic is
not a universal document classifier; use `include_updates=true` deliberately.

Normal AgentWorkflow/notebook configuration loading requires no additional user
API calls. Agents decide when to use tools. DemoLLM remains scripted and does not
automatically query documents; the integration test exercises actual retrieval in
every task/review turn.

Structured outputs accept `document_refs`, separate from patient `evidence_refs`.
The host rejects citations unless the current Agent has retrieved the whole
section (including gap-free pagination) or successfully inspected the original
page using vision. Reviews independently retrieve sources they cite. This proves
source access, **not medical correctness or claim entailment**. Other-page
footnotes and all applicable qualifiers still require independent checking.

`workflow_result.json` records the document snapshot ID, versions and hashes. All
turns share one pinned reader, so a new build does not change an existing run's
knowledge snapshot. Permission checks remain live: revocation blocks subsequent
reads. Source-file or manifest changes cause retrieval to fail until rebuilt.

## Optional page vision

1. Add/select a `models.jsonc` profile whose provider/model/route supports Chat
   Completions `image_url` input. Verify this with the provider; a profile name is
   not proof of image support.
2. Set `guidelines.vision_profile` in `tools.jsonc` to that profile name. Credentials
   use the existing provider environment variable or `secrets.local.json`.
3. Confirm source permission covers the selected model destination; run in `live`
   mode. The vision model is independent of the task/review DeepSeek models.

Only Chat Completions image input is currently supported by this helper, not
native PDF upload or Responses image input. Unsupported protocols are rejected.
No profile returns `vision_unavailable`; provider failures, incomplete responses
and budget exhaustion are explicit, not fabricated success.

Each call sends one PNG (longest edge at most 1800 pixels), its citation and a
bounded reading question. No whole PDF or automatic patient-state attachment is
sent. `max_vision_calls` defaults to two per Agent turn, including failed attempts;
workflow retries get a new turn budget. Output requests are capped at 2048 tokens.
Provider vision-token accounting is model-dependent and outside the text Agent's
context estimator. Audits retain image hashes and interpretations, not base64 or
credentials. Small footnotes may be unreadable at this resolution: report that
instead of guessing. Interpretations remain `needs_review`.

## PDF extraction and OCR limits

Native text is extracted first. Stored JSON preserves complete page text,
text-block coordinates, internal PDF links, page labels and section-label mentions.
Mentions such as `PROS-17` may be cross-references, not the current page identity.
Bookmarks are not treated as authoritative section mapping. Text sorting is only
a reading-order approximation; Markdown does not reconstruct tables or arrows.

`ocr: "auto"` invokes PyMuPDF/Tesseract only on pages with no native word characters.
It requires Tesseract and language data. Disabled OCR, failed OCR and OCR with no
readable text are explicit states. Mixed text/image pages do not receive automatic
region OCR; inspect them visually. Default `ocr: "off"` avoids re-recognizing
selectable text. OCR output is never promoted to a verified medical rule.

There is no executable clinical decision graph, automatic footnote resolution, or
certified human-review workflow in this version. All sections remain `needs_review`.

## Permission and time boundaries

Permissions in the manifest are host-maintained declarations, not a legal grant.
The supplied NCCN PDF is enabled for user-requested offline organization only;
this does not establish permission for AI/RAG or external-model use. Record the
scope and basis separately before enabling additional destinations or uses. Do
not bypass blocked sources through Shell. Local Shell mode is **not a security
sandbox**; use filesystem permissions and Docker isolation for stronger controls.

Use a fixed knowledge version for retrospective research. Do not claim historical
decision reconstruction when patient calendar dates are unknown. Post-cutoff
patient records remain evaluation-only; historical drug agreement alone does not
establish clinical quality.

## Tests

```bash
.venv/bin/python -m pytest -q tests/test_guidelines.py tests/test_workflow_config.py
```

Tests cover permissions/revocation, stale sources, path containment, atomic builds,
read-only SQL, exact pagination, PDF text/footnotes, update filtering, OCR failure,
rendering, actual image payloads with a model double, budgets, citation guards and
a full task/review workflow. No restricted guideline or real patient is required.
