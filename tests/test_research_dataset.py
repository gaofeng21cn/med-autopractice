"""Tests for the research dataset: extracted columns, codes, and long tables."""

import json
from pathlib import Path
import unittest

from med_autopractice.records.clinical_record import build_record
from med_autopractice.records.research_dataset import (
    TABLES,
    age_at,
    build_dataset,
    compute_los_days,
    modality_code,
    normalize_frequency,
    normalize_gender,
    normalize_route,
    pseudonym,
    render_research_record,
    render_table_csv,
    split_dose,
    table_summary,
)


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-history-collection.json"


def load_record():
    return build_record(json.loads(FIXTURE.read_text(encoding="utf-8")),
                        record_id="demo-research")


class NormalizationTests(unittest.TestCase):
    def test_gender_and_route_map_to_stable_codes(self):
        self.assertEqual(normalize_gender("男"), "M")
        self.assertEqual(normalize_gender("女性"), "F")
        self.assertEqual(normalize_gender(""), None)
        self.assertEqual(normalize_route("口服"), "PO")
        self.assertEqual(normalize_route("静脉滴注"), "IV_DRIP")
        self.assertEqual(normalize_route("鼻腔给药"), "OTHER")

    def test_frequency_keeps_wording_and_adds_doses_per_day(self):
        self.assertEqual(normalize_frequency("每日三次"), ("TID", 3.0))
        self.assertEqual(normalize_frequency("每天一次"), ("QD", 1.0))
        self.assertEqual(normalize_frequency("每晚一次"), ("QN", 1.0))
        self.assertEqual(normalize_frequency("每8小时一次"), ("Q8H", 3.0))
        code, per_day = normalize_frequency("遵医嘱")
        self.assertEqual(code, "OTHER")
        self.assertIsNone(per_day)

    def test_dose_splits_into_value_and_unit_without_guessing(self):
        self.assertEqual(split_dose("20mg"), (20.0, "mg"))
        self.assertEqual(split_dose("0.5 g"), (0.5, "g"))
        self.assertEqual(split_dose("按既往用药"), (None, None))

    def test_modality_uses_only_explicit_tokens(self):
        self.assertEqual(modality_code("颅脑MR平扫"), "MR")
        self.assertEqual(modality_code("常规心电图"), "ECG")
        self.assertEqual(modality_code("不带关键字的项目"), "OTHER")

    def test_age_and_length_of_stay_use_calendar_dates(self):
        self.assertEqual(age_at("1955-03-12", "2026-07-27T10:00"), 71)
        self.assertEqual(age_at("1955-03-12", "2026-03-11T10:00"), 70)
        self.assertIsNone(age_at("", "2026-03-11"))
        self.assertEqual(compute_los_days("2026-07-27T10:00", "2026-08-14T09:00"), 18.0)
        self.assertIsNone(compute_los_days("2026-08-14", "2026-07-27"))

    def test_datetime_maps_are_unwrapped_before_comparison(self):
        # The compiled record stores stamps as {"datetime": ...}; a raw
        # str() of that mapping would silently produce an empty field.
        self.assertEqual(age_at("1960-03-12", {"datetime": "2031-04-09T09:41"}), 71)
        self.assertEqual(
            compute_los_days({"datetime": "2031-04-09T09:41"},
                             {"datetime": "2031-04-16T11:35"}), 7.0)

    def test_subject_id_is_stable_and_carries_no_direct_identifier(self):
        # Synthetic hospital number only; real ones never enter this repository.
        sample = "SYNTH-HOSP-0001"
        first = pseudonym("primary", sample)
        self.assertEqual(first, pseudonym("primary", sample))
        self.assertNotEqual(first, pseudonym("other", sample))
        self.assertNotIn(sample, first)
        self.assertTrue(first.startswith("S"))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.record = load_record()
        self.dataset = build_dataset(self.record)

    def test_every_declared_table_is_produced(self):
        self.assertEqual(set(self.dataset), set(TABLES))
        summary = table_summary(self.dataset)
        self.assertEqual(summary["subject"], 1)
        self.assertEqual(summary["encounter"], len(self.record["encounters"]))
        self.assertEqual(summary["quality_summary"], 1)

    def test_rows_carry_the_subject_and_visit_keys(self):
        subject_id = self.dataset["subject"][0]["subject_id"]
        for name in ("encounter", "condition", "medication", "measurement"):
            for row in self.dataset[name]:
                self.assertEqual(row["subject_id"], subject_id)
        for row in self.dataset["condition"]:
            self.assertIn(row["visit_seq"], (1, 2))

    def test_no_direct_identifiers_reach_any_table(self):
        text = json.dumps(self.dataset, ensure_ascii=False)
        patient = self.record["patient"]
        for key in ("name", "id_card", "phone", "inpatient_no", "address"):
            value = patient.get(key)
            if value:
                self.assertNotIn(str(value), text, f"{key} leaked into the tables")
        # A contact person carries its own direct identifiers.
        contact = patient.get("contact") or {}
        for key in ("name", "phone"):
            if contact.get(key):
                self.assertNotIn(str(contact[key]), text)

    def test_condition_rows_flag_new_and_leading_diagnoses(self):
        rows = [row for row in self.dataset["condition"]
                if row["visit_seq"] == 1 and row["phase"] == "discharge"]
        added = [row["term_raw"] for row in rows if row["added_during_stay"]]
        self.assertEqual(added, ["示例新发症己"])
        self.assertTrue(rows[0]["first_discharge_diagnosis"])
        self.assertFalse(rows[1]["first_discharge_diagnosis"])

    def test_medication_rows_carry_normalized_codes_next_to_wording(self):
        row = next(row for row in self.dataset["medication"]
                   if row["drug_name"] == "示例药甲片")
        self.assertEqual((row["dose_value"], row["dose_unit"]), (10.0, "mg"))
        self.assertEqual(row["route_code"], "PO")
        self.assertEqual(row["frequency_code"], "QD")
        self.assertEqual(row["doses_per_day"], 1.0)
        self.assertEqual(row["total_quantity"], 7.0)
        self.assertEqual(row["parse_status"], "parsed")

    def test_unparsed_medication_stays_flagged_and_empty(self):
        row = next(row for row in self.dataset["medication"]
                   if row["parse_status"] == "unparsed")
        self.assertIsNone(row["dose_value"])
        self.assertIsNone(row["frequency_code"])
        self.assertTrue(row["raw"])

    def test_measurement_rows_split_date_test_and_result(self):
        row = self.dataset["measurement"][0]
        self.assertEqual(row["performed_date"], "2030-06-05")
        self.assertEqual(row["test_name"], "示例影像")
        self.assertEqual(row["result_text"], "示例检查所见甲")
        self.assertEqual(row["raw"], "2030-06-05 示例影像 示例检查所见甲")

    def test_quality_rows_carry_conflicts_and_missing_fields(self):
        kinds = {row["kind"] for row in self.dataset["qc_issue"]}
        self.assertIn("conflict", kinds)
        self.assertIn("missing", kinds)

    def test_quality_summary_counts_match_the_tables(self):
        row = self.dataset["quality_summary"][0]
        self.assertEqual(row["visits"], len(self.dataset["encounter"]))
        self.assertEqual(row["conditions"], len(self.dataset["condition"]))
        self.assertEqual(row["medications"], len(self.dataset["medication"]))
        self.assertEqual(row["unparsed_medications"], 1)

    def test_derived_columns_are_filled_from_the_compiled_record(self):
        subject = self.dataset["subject"][0]
        self.assertIsInstance(subject["age_at_last_visit"], int)
        for row in self.dataset["encounter"]:
            self.assertIsNotNone(row["los_days_computed"])
            # A recorded/computed gap is a data-quality signal, not a failure.
            self.assertIsNotNone(row["los_days_recorded"])

    def test_csv_columns_are_stable_and_boolean_values_are_machine_readable(self):
        header = render_table_csv("condition", []).splitlines()[0]
        self.assertEqual(tuple(header.split(",")), TABLES["condition"])
        rendered = render_table_csv("condition", self.dataset["condition"])
        body = rendered.splitlines()[1:]
        self.assertEqual(len(body), len(self.dataset["condition"]))
        self.assertIn(",true,", rendered + ",")

    def test_csv_ignores_extra_keys_so_subjects_concatenate(self):
        rows = [dict(self.dataset["subject"][0], unexpected="x")]
        header = render_table_csv("subject", rows).splitlines()[0]
        self.assertNotIn("unexpected", header)
        self.assertEqual(tuple(header.split(",")), TABLES["subject"])

    def test_unknown_table_is_refused(self):
        with self.assertRaises(ValueError):
            render_table_csv("not_a_table", [])

    def test_readable_view_shows_every_table_and_keeps_source_wording(self):
        rendered = "\n".join(render_research_record(self.record))
        for title in ("受试者（subject）", "就诊（encounter）", "诊断（condition）",
                      "用药（medication）", "检查检验（measurement）",
                      "手术与操作（procedure）"):
            self.assertIn(title, rendered)
        # Normalized codes appear, but the wording they came from is still there.
        self.assertIn("示例药甲片", rendered)
        self.assertIn("PO", rendered)

    def test_readable_view_spells_out_booleans_but_csv_keeps_them_parseable(self):
        rendered = "\n".join(render_research_record(self.record))
        condition_rows = [line for line in rendered.splitlines()
                          if line.startswith("| 1 | 出院 |")]
        self.assertTrue(condition_rows)
        self.assertIn("是", condition_rows[0])
        self.assertNotIn("true", rendered)
        csv_text = render_table_csv("condition", self.dataset["condition"])
        self.assertIn("true", csv_text)
        self.assertNotIn("是", csv_text.split("\n")[1])

    def test_missing_values_stay_empty_rather_than_guessed(self):
        rendered = render_table_csv("medication", self.dataset["medication"])
        unparsed = next(line for line in rendered.splitlines()
                        if "unparsed" in line and "示例药乙" in line)
        cells = unparsed.split(",")
        # dose_value, dose_unit, route_code, frequency_code, doses_per_day empty
        for index in (4, 5, 7, 8, 9):
            self.assertEqual(cells[index], "", f"column {index} should be empty")


if __name__ == "__main__":
    unittest.main()


class ResearchDatasetDocTests(unittest.TestCase):
    """Keep the published contract in step with the code it documents."""

    def setUp(self):
        self.doc = (Path(__file__).resolve().parents[1]
                    / "docs" / "research-dataset.md").read_text(encoding="utf-8")

    def test_every_table_is_named_in_the_documentation(self):
        for table in TABLES:
            self.assertIn(f"`{table}`", self.doc, f"{table} is undocumented")

    def test_documentation_states_the_rules_the_code_enforces(self):
        # These are the guarantees a reader relies on; losing them from the
        # document would silently widen what the dataset claims to be.
        for phrase in ("归一化与原文并列", "缺失一律留空", "不做临床判断",
                       "不是匿名化承诺"):
            self.assertIn(phrase, self.doc)

    def test_documentation_lists_the_columns_of_the_wide_tables(self):
        # Every column of every table must be documented, so a new field cannot
        # ship without the contract a downstream analysis reads.
        for table, columns in TABLES.items():
            for column in columns:
                self.assertIn(column, self.doc,
                              f"{table}.{column} is undocumented")
