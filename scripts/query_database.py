"""Read-only examples over the local knowledge database; no model calls."""

import argparse
import json
from pathlib import Path
import sqlite3


def connect_readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def query_gene(connection, symbol, limit=10):
    if not symbol.strip() or not 1 <= limit <= 100:
        raise ValueError("Provide a nonblank gene and a limit between 1 and 100")
    interactions = connection.execute('''
        SELECT "BioGRID Interaction ID" AS interaction_id,
               "Official Symbol Interactor A" AS gene_a, "Official Symbol Interactor B" AS gene_b,
               "Experimental System" AS experiment, "Experimental System Type" AS evidence_type,
               "Publication Source" AS publication
        FROM biogrid_interactions
        WHERE ("Official Symbol Interactor A" = ? OR "Official Symbol Interactor B" = ?)
          AND "Organism ID Interactor A" = '9606' AND "Organism ID Interactor B" = '9606'
        LIMIT ?
    ''', (symbol, symbol, limit)).fetchall()
    # Symbol -> exact human Entrez identifiers from BioGRID -> KEGG -> UniProt.
    accessions = connection.execute('''
        SELECT DISTINCT u.uniprot FROM kegg_uniprot u JOIN kegg_genes k ON u.kegg_id = k.kegg_id
        WHERE k.entrez_id IN (
            SELECT "Entrez Gene Interactor A" FROM biogrid_interactions
            WHERE "Official Symbol Interactor A" = ? AND "Organism ID Interactor A" = '9606'
            UNION SELECT "Entrez Gene Interactor B" FROM biogrid_interactions
            WHERE "Official Symbol Interactor B" = ? AND "Organism ID Interactor B" = '9606'
        )
    ''', (symbol, symbol)).fetchall()
    proteins = [row[0] for row in accessions]
    structures = []
    if proteins:
        placeholders = ",".join("?" for _ in proteins)
        structures = connection.execute(f'''
            SELECT p.id, p.uniprot_R, p.uniprot_L, p.split,
                   coalesce(s.status, 'not_selected') AS local_structure_status, s.path AS local_structure_path
            FROM pinder_related p LEFT JOIN pinder_structures s ON s.id = p.id
            WHERE p.uniprot_R IN ({placeholders}) OR p.uniprot_L IN ({placeholders})
            ORDER BY (s.status = 'downloaded') DESC, p.id LIMIT ?
        ''', (*proteins, *proteins, limit)).fetchall()
    return {"gene": symbol, "scope": "human BioGRID interactions; KEGG-seeded PINDER subset",
            "biogrid": [dict(row) for row in interactions], "kegg_uniprot": proteins,
            "pinder": [dict(row) for row in structures],
            "warning": "Association/structural records are not therapeutic recommendations or drug docking scores."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="database/data/knowledge.sqlite")
    parser.add_argument("--gene", help="Exact official human gene symbol, for example AR or PTEN")
    parser.add_argument("--text", help="SQLite FTS query over NCI evidence-summary text, not restricted guidelines")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    connection = connect_readonly(args.database)
    try:
        if args.gene:
            result = query_gene(connection, args.gene, args.limit)
        elif args.text:
            result = [dict(row) for row in connection.execute(
                "SELECT document_id, heading, anchor, snippet(document_sections, 3, '[', ']', '…', 40) AS excerpt "
                "FROM document_sections WHERE document_sections MATCH ? ORDER BY rank LIMIT ?", (args.text, args.limit))]
        else:
            result = {"tables": [dict(row) for row in connection.execute("SELECT table_name, row_count FROM imported_tables")],
                      "sources": [json.loads(row[0]) for row in connection.execute("SELECT metadata_json FROM sources")]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
