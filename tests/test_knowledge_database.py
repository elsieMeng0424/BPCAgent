"""Offline tests for source collection, exact seed matching and local indexing."""

import importlib.util
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/collect_database.py"
spec = importlib.util.spec_from_file_location("collect_database", SCRIPT)
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)


class KnowledgeDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "database.json"
        self.config.write_text(json.dumps({"root": "data", "fallback_proxy": "http://127.0.0.1:28374",
                                           "minimum_free_bytes": 0, "max_file_bytes": 10000}))

    def archive(self, name, text):
        path = self.root / "fixture.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, text)
        return path

    def test_direct_first_proxy_only_after_failure_and_cache_integrity(self):
        collector = collect.Collector(self.config)
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return SimpleNamespace(returncode=7, stderr="Connection failed", stdout="")
            Path(command[command.index("--output") + 1]).write_text("hsa:1\tup:P1\n")
            return SimpleNamespace(returncode=0, stderr="", stdout="https://example.org/genes")
        with patch.object(collect.subprocess, "run", side_effect=run):
            path = collector.fetch("test", "genes.tsv", "https://example.org/genes", "tsv")
            self.assertEqual(calls[0][calls[0].index("--noproxy") + 1], "*")
            self.assertNotIn("--proxy", calls[0])
            self.assertEqual(calls[1][calls[1].index("--proxy") + 1], "http://127.0.0.1:28374")
            receipt = json.loads(path.with_name("genes.tsv.receipt.json").read_text())
            self.assertTrue(receipt["used_fallback_proxy"])
            self.assertEqual(collector.fetch("test", "genes.tsv", "https://example.org/genes", "tsv"), path)
            self.assertEqual(len(calls), 2)
            path.write_text("changed")
            with self.assertRaisesRegex(ValueError, "differs"):
                collector.fetch("test", "genes.tsv", "https://example.org/genes", "tsv")

    def test_failed_content_validation_never_becomes_a_completed_asset(self):
        collector = collect.Collector(self.config)
        def run(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_text("<html>sign in</html>")
            return SimpleNamespace(returncode=0, stderr="", stdout="https://example.org/map")
        with patch.object(collect.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError):
                collector.fetch("test", "map.png", "https://example.org/map", "png")
        self.assertFalse((collector.root / "map.png").exists())
        self.assertFalse((collector.root / "map.png.receipt.json").exists())
        with self.assertRaises(ValueError):
            collector.fetch("test", "../outside", "https://example.org/map", "png")

    def test_biogrid_import_preserves_all_cells_and_rejects_malformed_rows(self):
        path = self.archive("BIOGRID-ALL-test.tab3.txt", "#BioGRID Interaction ID\tOfficial Symbol Interactor A\tScore\n1\tAR\t-\n2\tOTHER\t0\n3\t\t\n")
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE imported_tables (a,b,c,d,e,f)")
        counts = collect.import_tab_archive(connection, path, "interactions")
        self.assertEqual(counts["biogrid_interactions"], 3)
        self.assertEqual(connection.execute('SELECT "Score" FROM biogrid_interactions').fetchall(), [("-",), ("0",), ("",)])
        path = self.archive("BIOGRID-CHEMICALS-test.txt", "#Chemical ID\tName\n1\tDrug\textra\n")
        with self.assertRaisesRegex(ValueError, "Malformed row"):
            collect.import_tab_archive(connection, path, "chemicals")

    def test_identifier_license_preamble_is_not_imported_as_data(self):
        path = self.archive("BIOGRID-IDENTIFIERS-test.tab.txt", "License text\n\nA.) BIOGRID_ID\tDescription\nFile Version: test\nBIOGRID_ID\tIDENTIFIER_VALUE\tIDENTIFIER_TYPE\tORGANISM_OFFICIAL_NAME\n1\tAR\tOFFICIAL SYMBOL\tHomo sapiens\n")
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE imported_tables (a,b,c,d,e,f)")
        self.assertEqual(collect.import_tab_archive(connection, path, "identifiers")["biogrid_identifiers"], 1)
        self.assertEqual(connection.execute("SELECT IDENTIFIER_VALUE FROM biogrid_identifiers").fetchone()[0], "AR")

    def test_kgml_preserves_groups_typed_edges_and_exact_gene_mapping(self):
        folder = self.root / "raw/kegg/hsa05215"
        folder.mkdir(parents=True)
        (folder / "pathway.kgml").write_text('<pathway name="path:hsa05215"><entry id="1" name="hsa:1" type="gene"><graphics name="AR"/></entry><entry id="2" name="group" type="group"><component id="1"/></entry><relation entry1="1" entry2="2" type="PPrel"><subtype name="inhibition" value="--|"/></relation></pathway>')
        (folder / "genes.tsv").write_text("path:hsa05215\thsa:1\npath:hsa05215\thsa:2\n")
        (folder / "human_uniprot.tsv").write_text("hsa:1\tup:P1\nhsa:10\tup:P10\n")
        graph, seed = collect.kegg_graph(self.root, "hsa05215")
        self.assertEqual(seed["gene_uniprot"], [{"kegg_gene": "hsa:1", "uniprot": "P1"}])
        self.assertEqual(seed["unmapped_genes"], ["hsa:2"])
        self.assertEqual(graph["relations"][0]["subtypes"][0]["name"], "inhibition")
        self.assertEqual(graph["entries"][1]["components"], [{"id": "1"}])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "Install the knowledge extra")
    def test_pinder_matching_preserves_all_matches_not_prefix_matches(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        folder = self.root / "raw/pinder/test"
        folder.mkdir(parents=True)
        rows = [{"id": "a", "uniprot_R": "P1", "uniprot_L": "P2"},
                {"id": "b", "uniprot_R": "P10", "uniprot_L": "P20"},
                {"id": "c", "uniprot_R": "P2", "uniprot_L": "P1"}]
        pq.write_table(pa.Table.from_pylist(rows), folder / "index.parquet")
        pq.write_table(pa.Table.from_pylist([{"id": r["id"], "score": None} for r in rows]), folder / "metadata.parquet")
        selected, report = collect.related_pinder(self.root, "test", {"pathway": "hsa05215", "gene_uniprot": [{"uniprot": "P1"}]})
        self.assertEqual([r["id"] for r in selected], ["a", "c"])
        self.assertEqual(report["matched_index_rows"], 2)
        self.assertEqual(report["missing_metadata_ids"], [])
        self.assertEqual(pq.ParquetFile(self.root / "derived/pinder/test/pathway_metadata.parquet").metadata.num_rows, 2)

    def test_structure_selection_is_bounded_and_pair_unique(self):
        rows = [{"id": "a", "uniprot_R": "P1", "uniprot_L": "P2", "split": "test"},
                {"id": "b", "uniprot_R": "P2", "uniprot_L": "P1", "split": "train"},
                {"id": "c", "uniprot_R": "P3", "uniprot_L": "P4", "split": "invalid"},
                {"id": "d", "uniprot_R": "P3", "uniprot_L": "P5", "split": "train"}]
        chosen = collect.representative_complexes(rows, {"P1", "P2", "P3"}, 2)
        self.assertEqual([r["id"] for r in chosen], ["a", "d"])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "Install the knowledge extra")
    def test_filtered_dictionary_columns_do_not_retain_full_unrelated_dictionary(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        folder = self.root / "raw/pinder/test"
        folder.mkdir(parents=True)
        # A large global dictionary with one selected row reproduced the bloat
        # observed in the actual release without requiring real patient/data files.
        values = ["unrelated-" + str(i) * 20 for i in range(10000)] + ["selected"]
        ids = pa.DictionaryArray.from_arrays(pa.array([10000, 0], type=pa.int32()), pa.array(values))
        table = pa.Table.from_arrays([ids, pa.array(["P1", "P10"]), pa.array(["P2", "P20"])],
                                     names=["id", "uniprot_R", "uniprot_L"])
        pq.write_table(table, folder / "index.parquet")
        pq.write_table(pa.Table.from_pylist([{"id": "selected", "score": 1}]), folder / "metadata.parquet")
        collect.related_pinder(self.root, "test", {"pathway": "hsa05215", "gene_uniprot": [{"uniprot": "P1"}]})
        selected = self.root / "derived/pinder/test/pathway_index.parquet"
        self.assertLess(selected.stat().st_size, 10000)
        self.assertEqual(pq.read_table(selected).column("id").to_pylist(), ["selected"])

    def test_query_connection_cannot_modify_database(self):
        query_spec = importlib.util.spec_from_file_location("query_database", SCRIPT.with_name("query_database.py"))
        query = importlib.util.module_from_spec(query_spec)
        query_spec.loader.exec_module(query)
        path = self.root / "test.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.close()
        connection = query.connect_readonly(path)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO example VALUES ('changed')")

    def test_seed_views_filter_species_and_keep_evidence_types(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript('''
            CREATE TABLE kegg_genes (entrez_id TEXT);
            INSERT INTO kegg_genes VALUES ('1'), ('2');
            CREATE TABLE biogrid_interactions (
                "Entrez Gene Interactor A" TEXT, "Entrez Gene Interactor B" TEXT,
                "Organism ID Interactor A" TEXT, "Organism ID Interactor B" TEXT,
                "Experimental System Type" TEXT);
            INSERT INTO biogrid_interactions VALUES ('1','2','9606','9606','physical'),
                ('1','3','9606','9606','genetic'), ('1','2','10090','10090','physical');
            CREATE TABLE biogrid_chemicals ("Organism ID" TEXT, "Entrez Gene ID" TEXT);
            INSERT INTO biogrid_chemicals VALUES ('9606','1'), ('10090','1'), ('9606','9');
        ''')
        collect.create_seed_views(connection)
        self.assertEqual(connection.execute("SELECT count(*) FROM biogrid_prostate_seed_interactions").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT count(*) FROM biogrid_prostate_internal_interactions").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT count(*) FROM biogrid_prostate_chemicals").fetchone()[0], 1)
        self.assertEqual({r[0] for r in connection.execute('SELECT "Experimental System Type" FROM biogrid_prostate_seed_interactions')},
                         {"physical", "genetic"})

    def test_nonfinite_values_do_not_become_fabricated_zero_or_null(self):
        result = json.loads(collect.source_json({"score": float("nan"), "value": 0, "missing": None}))
        self.assertEqual(result["score"], {"source_nonfinite": "NaN"})
        self.assertEqual(result["value"], 0)
        self.assertIsNone(result["missing"])

    @unittest.skipUnless(importlib.util.find_spec("bs4"), "Install the knowledge extra")
    def test_nci_text_extract_omits_images_tables_and_duplicate_nested_lists(self):
        path = self.root / "nci.html"
        path.write_text('<article><h1>Prostate Cancer Treatment</h1><h2 id="tx">Treatment</h2><p>Evidence summary.</p><figure>IMAGE SECRET</figure><table><tr><td>TABLE SECRET</td></tr></table><ul><li><p>One option.</p></li></ul></article>')
        sections = collect.nci_sections(path)
        content = json.dumps(sections)
        self.assertNotIn("SECRET", content)
        self.assertEqual(content.count("One option."), 1)
        self.assertEqual(sections[0]["anchor"], "tx")


if __name__ == "__main__":
    unittest.main()
