# Research knowledge database

Local collection for the prostate-cancer workflow, checked on 2026-10-28. Raw
resources and SQLite files are ignored by Git. This database is **not yet wired
into TargetSelectionAgent or DrugSelectionAgent**; their scientific connectors
remain placeholders until retrieval, scoring and evidence validation are added.

The checked-in [collection status](collection-status.json) records the completed
2026-10-28 run, including exact counts, 97 successful representative structures,
three failed structure requests and guideline permission gaps. The local
`data/build-report.json` and download receipts contain runtime provenance.

## Contents and boundaries

| Resource | What is collected | What it does not establish |
| --- | --- | --- |
| KEGG `hsa05215` | Original KGML, PNG, pathway entry, gene list, human gene-to-UniProt mapping; derived typed graph and seed list | Not every edge is a physical interaction or actionable target |
| BioGRID `5.0.262` | All-species interaction TAB3, chemical associations, PTM records/relationships, identifier mapping; all raw columns indexed locally | No claim that all interactions are human, causal, prostate-specific or clinically validated |
| PINDER `2024-02` | Full index/metadata to filter; all rows matching either partner to an exact KEGG seed UniProt ID; a bounded representative PDB subset | Not the full structure library, not all matching structures, not protein-small-molecule docking results |
| NCCN | User-supplied version 1.2027 PDF locally indexed as 253 pages in the separate document library | External-model access disabled; no executable treatment rules inferred |
| AUA/ASCO | Source/version/access metadata and permission actions only | Guideline bodies not imported |
| NCI PDQ professional summary | Public source HTML plus text-only sections for retrieval, with attribution | An evidence summary, **not** a clinical practice guideline or substitute for NCCN/AUA/ASCO |

BioGRID "all" means all species and experimental systems in the pinned current
interaction release, plus its chemicals, PTMs and identifiers. Redundant export
formats, historical releases and the separate BioGRID ORCS screen database are
not mirrored. Source database labels, PubMed identifiers and experimental types
are preserved; physical and genetic interactions are not merged into one type.

PINDER matching keeps both intra-pathway pairs and one-hop partners. It uses exact
UniProt accessions from KEGG, without silently collapsing isoforms or names.
Unmapped genes, unmatched accessions and missing metadata are reported. The
other PINDER partner may be non-human or have an `UNDEFINED` accession; retaining
a matching human seed does not establish a human-human, prostate-specific PPI.
Keep those distinctions when constructing a downstream graph. The
structure sampler prioritizes seed coverage and at most one complex per protein
pair; it is a storage policy, not a scientific ranking. Invalid training splits
remain in the full matched index but are excluded from representative downloads.

## Files

```text
database/
  sources.json                     # Tracked provenance, rights and pending actions
  data/                            # Ignored by Git
    raw/
      kegg/hsa05215/                # Original image, KGML and mapping files
      biogrid/5.0.262/              # Complete ZIP exports, streamed during import
      pinder/2024-02/               # Full index/metadata; related_pdbs/ only
      guidelines/nci/              # Public professional evidence summary
    derived/
      kegg/                        # Typed graph and seed mapping
      pinder/2024-02/               # Matched Parquets, selection and structure reports
      guidelines/                  # Text-only NCI sections
    knowledge.sqlite               # Queryable local database
    build-report.json              # Actual imported row counts and scope
```

Every completed download has an adjacent `.receipt.json` recording its URL,
retrieval timestamp, SHA-256, size and whether fallback proxy was needed. A cache
hit rechecks the checksum. Invalid ZIP/Parquet/KGML/PNG/PDB content is not accepted
as a completed resource. Raw data are never uploaded to GitHub or model providers.

## Collect and build

Use the project environment. Optional dependencies can be installed with
`pip install -e '.[knowledge]'`; the collector also needs the `curl` executable.
Settings live in [config/database.jsonc](../config/database.jsonc), not LLM configs.
Paths are relative to that config file.

```bash
.venv/bin/python scripts/collect_database.py fetch
.venv/bin/python scripts/collect_database.py prepare
.venv/bin/python scripts/collect_database.py structures
.venv/bin/python scripts/collect_database.py build
```

`fetch` downloads the pinned BioGRID exports, PINDER index/metadata, one KEGG
pathway and NCI's public summary. It never downloads bulk PINDER archives or
NCCN/AUA/ASCO guideline bodies. Use `--sources kegg pinder` to select source groups.
`prepare` builds the KGML graph and all exact-accession matching index/metadata
rows. `structures` downloads bounded representative complexes and records failures.
`build` imports all BioGRID cells, the KEGG graph/mappings, selected PINDER rows,
source receipts and NCI text into SQLite. It refuses to overwrite an existing
database; use a new data root for a new release. Failed builds retain their
`.partial.sqlite` for inspection rather than being published as complete databases.
After downloading additional representatives, run `refresh-catalog` to verify
asset checksums and update source/structure availability without reimporting the
large BioGRID tables. The original index/metadata source versions are unchanged.

Downloads try direct HTTPS first. Only a failed direct request uses
`http://127.0.0.1:28374`; no 7890/17890 routing is used by this collector. TLS
verification remains enabled. KEGG requests are serialized below three per second.
The academic-use setting must reflect your actual entitlement; commercial use
needs a suitable KEGG license. Data remain local rather than being redistributed.

Raw PINDER Parquets retain their source dtypes. SQLite JSON converts numpy scalar
numbers to JSON numbers and explicitly represents non-finite source values as
`{"source_nonfinite":"NaN"}` (or signed Infinity), never as a fabricated zero or
unknown numeric value. BioGRID raw string cells, including `-` and empty cells,
are retained without inventing missingness interpretations.

## Read-only queries

```bash
.venv/bin/python scripts/query_database.py --gene AR --limit 5
.venv/bin/python scripts/query_database.py --gene PTEN --limit 5
.venv/bin/python scripts/query_database.py --text 'docetaxel' --limit 3
```

The gene query limits the BioGRID results to human-human interactions, maps human
Entrez IDs through KEGG to UniProt, and lists matching PINDER records with local
structure availability. The full BioGRID tables still include all species.
The text query searches only the permitted NCI summary, not guideline bodies.

Main SQLite tables: `sources`, `assets`, `imported_tables`, `kegg_genes`,
`kegg_uniprot`, `kegg_entries`, `kegg_relations`, `pinder_related`,
`pinder_metadata`, `pinder_structures`, `biogrid_interactions`,
`biogrid_chemicals`, `biogrid_ptms`, `biogrid_ptm_relationships`,
`biogrid_identifiers`, `documents`, `document_sections` (FTS5).

Convenience views `biogrid_prostate_seed_interactions` (either human partner is
a KEGG seed), `biogrid_prostate_internal_interactions` (both are seeds), and
`biogrid_prostate_chemicals` (human seed-gene chemical associations) retain all
original evidence columns. They are filters, not newly inferred interactions.

## Guideline access actions

The page-aware document library is separate from `knowledge.sqlite`. See
[document reading](../doc/guideline-reading.md) for ingestion, Markdown/JSON
exports, Agent tools and optional page vision. Its source manifest is
[`config/guidelines.jsonc`](../config/guidelines.jsonc). NCI text is eligible;
the user-provided NCCN version 1.2027 PDF is locally indexed at the user's request.
Its external-model access remains blocked; local processing does not establish
publisher authorization for AI/RAG or redistribution.

**Login is not the same as AI reuse permission.** Before extracting treatment
rules, creating embeddings or sending text to an LLM, obtain authorization for
the intended research processing. Do not send account passwords, cookies or
tokens to this project or commit them to Git.

- **NCCN:** sign in at the [professional prostate guideline page](https://www.nccn.org/guidelines/guidelines-detail?category=1&id=1459),
  verify the current PDF version, and consult [permissions](https://www.nccn.org/about/permissions/).
  The [NCCN rights notice](https://jnccn360.org/disclaimer/) directs reuse requests
  there. The `3.2026` journal excerpt is not assumed to be the current full guideline.
- **AUA/SUO and AUA/ASTRO:** the [AUA guideline portal](https://www.auanet.org/guidelines-and-quality/guidelines)
  flags restrictions on third-party AI use. Obtain institutional permission for
  the 2026 advanced and localized prostate guideline amendments before ingestion.
- **ASCO:** [terms](https://www.asco.org/about-asco/legal/terms-use) restrict AI
  processing and automated collection. Request the needed license through
  [ASCO permissions](https://www.asco.org/about-asco/legal/copyright-permission)
  (`permissions@asco.org` or `licensing@asco.org`). The catalog records the located
  mCRPC living-guideline version `2026.1.4` and the 2023 noncastrate update.
- **NCI:** [reuse policy](https://www.cancer.gov/policies/copyright-reuse) generally
  permits reuse of its text with exceptions for separately credited material.
  Text indexing excludes figures and tables. NCI explicitly says its
  [professional PDQ summaries are not clinical practice guidelines](https://www.cancer.gov/publications/pdq/information-summaries).

A permission inquiry can describe: "Internal academic research on a multi-agent
prostate-cancer decision-support prototype; local storage, structured extraction,
retrieval-augmented generation and evaluation; no clinical deployment or public
redistribution. Please specify whether use with externally hosted model APIs is
permitted, and any attribution, retention, user-count or licensing conditions."
If the actual project is commercial or publicly deployed, disclose that instead.

## Reproducibility and safety

These are current knowledge snapshots, not automatically historically valid
evidence at BPC patient cutoffs. Keep a separate knowledge-version policy for
retrospective evaluation. Do not mix protein interaction evidence, structure
availability and guideline recommendations into an unlabeled edge type. A future
guideline schema should retain disease state, prior therapy, biomarkers,
contraindications, recommendation strength, source section/page and version.

Run offline checks with:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_knowledge_database.py
```
