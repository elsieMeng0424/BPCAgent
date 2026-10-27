from __future__ import annotations

import csv
from pathlib import Path

import pytest


def write_csv(root: Path, name: str, rows: list[dict[str, object]], delimiter: str = ",") -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with (root / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def build_fixture(root: Path, future_drug: str = "FutureDrug") -> Path:
    root.mkdir()
    patient = "P1"
    base = {"cohort": "Prostate", "record_id": patient}
    write_csv(root, "patient_level_dataset.csv", [{**base, "institution": "TEST", "birth_year": 1950,
        "naaccr_sex_code": "Male", "naaccr_race_code_primary": "Unknown", "naaccr_ethnicity_code": "Unknown"}])
    write_csv(root, "cancer_level_dataset_index.csv", [
        {**base, "ca_seq": 7, "first_index_ca_days": 0, "dob_ca_dx_days": 20000,
         "ca_type": "Prostate Cancer", "ca_d_site": "C61.9", "ca_histology": "Adenocarcinoma"},
        {**base, "ca_seq": 0, "first_index_ca_days": 20, "dob_ca_dx_days": 20020,
         "ca_type": "Second Prostate Cancer", "ca_d_site": "C61.9", "ca_histology": "Adenocarcinoma"},
    ])
    write_csv(root, "cancer_level_dataset_non_index.csv", [
        {**base, "ca_seq": 2, "first_index_ca_days": 150, "dob_ca_dx_days": 20150,
         "ca_type": "Future Other Cancer", "ca_d_site": "C67.9", "ca_histology": "Other"}
    ])
    write_csv(root, "regimen_cancer_level_dataset.csv", [
        {**base, "ca_seq": 7, "regimen_number": 1,
         "drugs_drug_1": "DrugA", "drugs_startdt_int_1": 20010, "dx_drug_start_int_1": 10, "dx_drug_end_int_1": 70,
         "drugs_drug_2": future_drug, "drugs_startdt_int_2": 20050, "dx_drug_start_int_2": 50, "dx_drug_end_int_2": 90,
         "drugs_drug_3": "Investigational Drug", "drugs_startdt_int_3": 20030, "dx_drug_start_int_3": 30, "dx_drug_end_int_3": 30},
        {**base, "ca_seq": "", "regimen_number": 2, "drugs_drug_1": "AmbiguousDrug",
         "drugs_startdt_int_1": 19995, "dx_drug_start_int_1": "", "dx_drug_end_int_1": ""},
        {**base, "ca_seq": 0, "regimen_number": 3, "drugs_drug_1": "OtherCancerDrug",
         "drugs_startdt_int_1": 20025, "dx_drug_start_int_1": 5, "dx_drug_end_int_1": ""},
    ])
    write_csv(root, "ca_radtx_dataset.csv", [{**base, "ca_seq": 7, "rt_number": 1, "rt_rt_int": 0,
        "dx_rt_start_days": 40, "rt_type": "External beam", "rt_total_dose": 800, "rt_fractions": 1}])
    write_csv(root, "pathology_report_level_dataset.csv", [
        {**base, "path_proc_number": 1, "path_rep_number": 1, "dx_path_proc_days": 10,
         "path_proc_type": "Biopsy", "path_ca_inv_any": "Yes", "path_pc_gleason": "Gleason 9",
         "mmr_yn": "No", "mmr_prepaint": "", "mmr_result": ""},
        {**base, "path_proc_number": 1, "path_rep_number": 2, "dx_path_proc_days": 10,
         "path_proc_type": "Addendum", "path_ca_inv_any": "Yes",
         "mmr_yn": "Yes", "mmr_prepaint": 20060, "mmr_result": "Proficient"},
    ])
    write_csv(root, "imaging_level_dataset.csv", [
        {**base, "scan_number": 1, "dx_scan_days": -5, "image_scan_type": "CT", "scan_sites": "Chest",
         "image_ca": "Yes", "image_overall": "Not stated/Indeterminate"},
        {**base, "scan_number": 2, "dx_scan_days": 40, "image_scan_type": "CT", "scan_sites": "Chest",
         "image_ca": "Yes", "image_overall": "Stable/No change"},
        {**base, "scan_number": 3, "dx_scan_days": 40, "image_scan_type": "MRI", "scan_sites": "Spine",
         "image_ca": "Yes", "image_overall": "Progressing/Worsening/Enlarging"},
    ])
    write_csv(root, "med_onc_note_level_dataset.csv", [{**base, "md_visit_number": 1, "dx_md_visit_days": 35,
        "md_type_ca_cur": "Prostate Cancer", "md_pca_status": "Not stated", "md_psa_status": "PSA Decreasing",
        "md_ca": "Yes", "md_ca_status": "Stable/No change"}])
    write_csv(root, "tm_level_dataset.csv", [
        {**base, "tm_number": 1, "dx_tm_days": 15, "tm_type": "PSA", "tm_num_result": 10,
         "tm_result_units": "ng/mL", "tm_normal_range_lower": 0, "tm_normal_range_upper": 4},
        {**base, "tm_number": 2, "dx_tm_days": 40, "tm_type": "PSA", "tm_num_result": 5,
         "tm_result_units": "ng/mL", "tm_normal_range_lower": 0, "tm_normal_range_upper": 4},
    ])
    write_csv(root, "cancer_panel_test_level_dataset.csv", [{**base, "cpt_number": 1, "ca_seq": 7,
        "cpt_genie_sample_id": "S1", "dob_cpt_report_days": 20080, "dx_cpt_rep_days": 80,
        "dx_path_proc_cpt_days": 10, "path_proc_number": 1, "path_rep_number": 1,
        "sample_type": "Primary tumor", "cpt_seq_assay_id": "TEST-PANEL"}])
    write_csv(root, "data_mutations_extended.txt", [{"Tumor_Sample_Barcode": "S1", "Hugo_Symbol": "TP53",
        "HGVSp_Short": "p.R248Q", "Variant_Classification": "Missense_Mutation", "Chromosome": 17,
        "Start_Position": 7577538, "Reference_Allele": "C", "Tumor_Seq_Allele2": "T"}], "\t")
    write_csv(root, "data_sv.txt", [{"Sample_Id": "S1", "Site1_Hugo_Symbol": "TMPRSS2",
        "Site2_Hugo_Symbol": "ERG", "Class": "DELETION", "Annotation": "fusion"}], "\t")
    write_csv(root, "tmb.tsv", [{"SAMPLE_ID": "S1", "tmb": "0.000004", "tmb_bin": "Mid (2-16)"}], "\t")
    write_csv(root, "data_gene_matrix.txt", [{"SAMPLE_ID": "S1", "mutations": "TEST-PANEL", "cna": "TEST-PANEL"}], "\t")
    (root / "data_CNA.txt").write_text("Hugo_Symbol\tS1\nPTEN\t-2\nTP53\t0\nUNKNOWN\tNA\nFRACTION\t-1.5\n", encoding="utf-8")
    (root / "cases_sv.txt").write_text("case_list_ids: S1\n", encoding="utf-8")
    (root / "data_gene_panel_TEST-PANEL.txt").write_text("stable_id: TEST-PANEL\ngene_list:\tTP53\tPTEN\n", encoding="utf-8")
    return root


@pytest.fixture
def fixture_data(tmp_path: Path) -> Path:
    return build_fixture(tmp_path / "dataset")
