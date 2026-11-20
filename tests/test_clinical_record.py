import json
import tempfile
from pathlib import Path
import unittest

from med_autopractice.records.clinical_record import (
    SCHEMA_VERSION,
    build_problem_list,
    build_record,
    date_part,
    dedupe_preserving_order,
    normalize_datetime,
    parse_medication,
    render_clinical_summary,
    render_markdown,
    split_numbered_list,
    validate_record,
)


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-history-collection.json"


def load_record():
    collection = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return build_record(collection, record_id="synthetic-record")


class NormalizationTests(unittest.TestCase):
    def test_split_numbered_list_keeps_identifiers_intact(self):
        items = split_numbered_list("1.示例诊断甲2.示例诊断乙3.示例慢性病丙")
        self.assertEqual(items, ["示例诊断甲", "示例诊断乙", "示例慢性病丙"])
        self.assertEqual(split_numbered_list("X12.345x010 示例肿物"),
                         ["X12.345x010 示例肿物"])

    def test_split_numbered_list_handles_separators_and_chinese_numbering(self):
        self.assertEqual(split_numbered_list("1、甲；2、乙；3、丙"), ["甲", "乙", "丙"])
        self.assertEqual(split_numbered_list("甲、乙、丙"), ["甲、乙、丙"])

    def test_dedupe_keeps_first_wording_and_order(self):
        self.assertEqual(dedupe_preserving_order(["甲类", "乙类", "甲类 ", "甲 类"]),
                         ["甲类", "乙类"])

    def test_normalize_datetime_returns_local_wall_clock(self):
        self.assertEqual(normalize_datetime("2031年4月16日 11:35"), "2031-04-16T11:35")
        self.assertEqual(normalize_datetime("2031-04-16 14:52:17"), "2031-04-16T14:52:17")
        self.assertEqual(normalize_datetime("2031-04-16"), "2031-04-16")
        self.assertIsNone(normalize_datetime(""))
        self.assertIsNone(normalize_datetime("2026-13-40"))
        self.assertEqual(date_part("2031-04-16T11:35"), "2031-04-16")

    def test_parse_medication_extracts_fields_and_flags_unknown_lines(self):
        parsed = parse_medication("※80片装(示例商品名)示例药甲片,每次量: 0.50 mg,口服,每日三次,总量: 21片")
        self.assertEqual(parsed["name"], "(示例商品名)示例药甲片")
        self.assertEqual(parsed["dose"], "0.50 mg")
        self.assertEqual(parsed["route"], "口服")
        self.assertEqual(parsed["frequency"], "每日三次")
        self.assertEqual(parsed["total"], "21片")
        self.assertTrue(parsed["parsed"])
        self.assertFalse(parsed["uncertain"])

        plain = parse_medication("示例药乙片 10 mg 口服 每天一次 总量 7片")
        self.assertEqual(plain["dose"], "10mg")
        self.assertEqual(plain["frequency"], "每天一次")
        self.assertEqual(plain["total"], "7片")

        unknown = parse_medication("（示例药丙按既往用药继续服用）")
        self.assertFalse(unknown["parsed"])
        self.assertTrue(unknown["uncertain"])
        self.assertEqual(unknown["raw"], "（示例药丙按既往用药继续服用）")
        self.assertTrue(unknown["notes"])

    def test_digit_inside_a_product_name_is_not_read_as_a_dose(self):
        # "碳酸钙D3片" is the product name; the dose is the later 600 mg.
        parsed = parse_medication("示例钙D3片 600 mg 口服 每晚一次 总量7片")
        self.assertEqual(parsed["name"], "示例钙D3片")
        self.assertEqual(parsed["dose"], "600mg")
        self.assertEqual(parsed["total"], "7片")
        self.assertTrue(parsed["parsed"])


class BuildRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = load_record()

    def test_built_record_satisfies_its_contract(self):
        self.assertEqual(validate_record(self.record), [])
        self.assertEqual(self.record["schema_version"], SCHEMA_VERSION)

    def test_diagnoses_are_cleaned_without_losing_order(self):
        first = self.record["encounters"][0]["sections"]["discharge_diagnoses"]["items"]
        self.assertEqual(first, ["示例诊断甲", "示例诊断乙", "示例新发症己", "示例慢性病丙"])
        admission = self.record["encounters"][0]["sections"]["admission_diagnoses"]["items"]
        self.assertEqual(admission,
                         ["示例诊断甲", "示例诊断乙", "示例合并症戊", "示例慢性病丙"])

    def test_discharge_orders_are_split(self):
        orders = self.record["encounters"][0]["sections"]["discharge_orders"]["items"]
        self.assertEqual(orders, ["注意休息，避免劳累。", "如有不适及时就诊。"])

    def test_second_encounter_uses_disease_certificate_and_reports_gaps(self):
        second = self.record["encounters"][1]
        self.assertEqual(second["provenance"]["document"], "疾病证明书")
        self.assertEqual(second["provenance"]["basis"], "only_read_document")
        self.assertEqual(second["admission"]["datetime"], "2030-09-01T09:10")
        missing = {item["field"] for item in second["missing"]}
        self.assertIn("discharge_status", missing)
        self.assertIn("discharge_medications", missing)

    def test_missing_fields_are_never_invented(self):
        second = self.record["encounters"][1]
        self.assertIsNone(second["sections"]["discharge_status"]["text"])
        self.assertEqual(second["sections"]["discharge_medications"]["items"], [])

    def test_same_day_time_difference_is_recorded_as_conflict_not_overwritten(self):
        first = self.record["encounters"][0]
        conflicts = {item["field"]: item for item in first["conflicts"]}
        self.assertIn("discharge.datetime", conflicts)
        conflict = conflicts["discharge.datetime"]
        self.assertTrue(conflict["same_calendar_date"])
        self.assertEqual(conflict["resolution"], "kept_both_review_required")
        self.assertEqual({item["source"] for item in conflict["values"]},
                         {"出院记录", "inpatient_list"})
        self.assertEqual(first["discharge"]["datetime"], "2030-06-10T13:20")
        self.assertEqual(len(first["discharge"]["observations"]), 2)

    def test_different_calendar_day_difference_is_marked(self):
        conflicts = {item["field"]: item for item in self.record["encounters"][1]["conflicts"]}
        self.assertFalse(conflicts["discharge.datetime"]["same_calendar_date"])

    def test_second_precision_is_not_reported_as_a_conflict(self):
        first = self.record["encounters"][0]
        fields = {item["field"] for item in first["conflicts"]}
        self.assertNotIn("admission.datetime", fields)
        self.assertEqual(first["admission"]["datetime"], "2030-06-04T08:24")

    def test_validation_rejects_emptied_required_fields(self):
        broken = json.loads(json.dumps(self.record))
        broken["encounters"][0]["sections"]["chief_complaint"]["text"] = ""
        broken["encounters"][0]["conflicts"][0]["values"] = [{"source": "x", "value": "1"}]
        broken["encounters"][0]["provenance"].pop("document")
        errors = validate_record(broken)
        self.assertTrue(any("chief_complaint" in error for error in errors))
        self.assertTrue(any("conflicts[0]" in error for error in errors))
        self.assertTrue(any("provenance.document" in error for error in errors))

    def test_markdown_renders_sections_gaps_and_conflicts(self):
        markdown = render_markdown(self.record)
        for heading in ("## 就诊时间线", "### 主诉", "### 出院诊断", "### 出院带药",
                        "### 缺口", "### 冲突（保留双方，待人工判定）",
                        "### 出院时间的多来源记录", "## 院外既往诊疗"):
            self.assertIn(heading, markdown)
        self.assertIn("待人工核对", markdown)
        self.assertIn("未用其他来源补造", markdown)
        self.assertIn("出院时间：", markdown)
        self.assertIn("- 出院带药：采集到的文书中没有该字段", markdown)
        self.assertNotIn("discharge.datetime", markdown)
        self.assertNotIn("None", markdown)


class ProblemListTests(unittest.TestCase):
    def setUp(self):
        self.record = load_record()
        self.analysis = build_problem_list(self.record)

    def test_identical_diagnoses_are_grouped_across_encounters(self):
        labels = {problem["label"]: problem for problem in self.analysis["problems"]}
        self.assertEqual(labels["示例慢性病丙"]["encounters"], [1, 2])
        self.assertEqual(labels["示例慢性病丙"]["encounter_count"], 2)
        self.assertTrue(labels["示例慢性病丙"]["in_latest_encounter"])
        self.assertEqual(labels["示例诊断甲"]["encounters"], [1])

    def test_differently_worded_diagnoses_are_not_merged(self):
        labels = {problem["label"] for problem in self.analysis["problems"]}
        self.assertIn("示例诊断甲（术后）", labels)
        self.assertIn("示例诊断甲", labels)
        pairs = {(pair["a"], pair["b"]) for pair in self.analysis["needs_confirmation"]}
        self.assertTrue(any("示例诊断甲" in a and "示例诊断甲" in b for a, b in pairs))

    def test_new_diagnosis_during_stay_is_flagged(self):
        delta = self.analysis["per_encounter_delta"][0]
        self.assertEqual(delta["encounter"], 1)
        self.assertEqual(delta["added_at_discharge"], ["示例新发症己"])
        self.assertEqual(delta["only_in_admission"], ["示例合并症戊"])
        self.assertEqual(len(self.analysis["per_encounter_delta"]), 1)

    def test_diagnoses_absent_from_latest_encounter_are_surfaced_as_one_question(self):
        summary = render_clinical_summary(self.record)
        self.assertIn("最近一次就诊未再出现", summary)
        self.assertIn("示例合并症戊", summary)
        self.assertIn("是已缓解、已治愈，", summary)
        self.assertIn("不能据此判断病情变化", summary)


class ClinicalSummaryTests(unittest.TestCase):
    def setUp(self):
        self.record = load_record()
        self.summary = render_clinical_summary(self.record)

    def test_summary_has_consecutive_numbered_sections(self):
        # No review notes are supplied here, so the reviewer section is empty
        # and omitted; numbering still has to stay consecutive.
        headings = [line for line in self.summary.splitlines() if line.startswith("## ")]
        markers = [line.split("、")[0].removeprefix("## ") for line in headings]
        self.assertEqual(markers, ["一", "二", "三", "四", "五", "六"])

    def test_summary_covers_every_required_part(self):
        for text in ("结构化病历（研究数据集）", "问题清单与需核实项", "病历原文",
                     "出院带药原文", "院外既往诊疗原文", "这份文件是什么"):
            self.assertIn(text, self.summary)
        # Section 1 is the research dataset; the tables a researcher uses must
        # all be present, in the order the schema documents.
        for table in ("受试者（subject）", "就诊（encounter）", "诊断（condition）",
                      "用药（medication）", "检查检验（measurement）",
                      "手术与操作（procedure）"):
            self.assertIn(table, self.summary)
        # Section 2 carries the differences a reader has to judge.
        self.assertIn("采集与记录问题", self.summary)
        self.assertIn("需你判断的差异", self.summary)

    def test_research_dataset_comes_first_and_source_text_last(self):
        lines = self.summary.splitlines()
        ordered = [next(index for index, line in enumerate(lines)
                        if line.startswith("## ") and title in line)
                   for title in ("结构化病历（研究数据集）", "问题清单与需核实项",
                                 "病历原文")]
        self.assertEqual(ordered, sorted(ordered))
        # The source wording a reviewer checks against comes after the tables.
        self.assertLess(ordered[0], self.summary.index("**入院诊断**"))

    def test_each_encounter_lists_admission_and_discharge_diagnoses(self):
        # Each encounter gets its own heading line; the explanatory paragraph
        # mentions the same words inline, so match only line-starting headings.
        encounters = len(self.record["encounters"])
        self.assertEqual(self.summary.count("\n**入院诊断**"), encounters)
        self.assertEqual(self.summary.count("\n**出院诊断**"), encounters)

    def test_summary_does_not_leak_internal_keys_or_empty_values(self):
        for token in ("discharge.datetime", "admission_diagnoses", "None", "null",
                      "{'", "\"items\""):
            self.assertNotIn(token, self.summary)

    def test_summary_states_conflicts_and_questions_without_deciding(self):
        self.assertIn("均已保留，未替你选定", self.summary)
        self.assertIn("需你判断的差异", self.summary)
        self.assertIn("是否同一问题", self.summary)
        self.assertIn("未用其他来源补造", self.summary)

    def test_summary_without_prior_history_keeps_numbering_consecutive(self):
        trimmed = json.loads(json.dumps(self.record))
        trimmed["prior_history_outside_this_hospital"] = []
        summary = render_clinical_summary(trimmed)
        self.assertNotIn("院外既往诊疗", summary)
        headings = [line for line in summary.splitlines() if line.startswith("## ")]
        markers = [line.split("、")[0].removeprefix("## ") for line in headings]
        self.assertEqual(markers, ["一", "二", "三", "四", "五"])


class ReviewNoteTests(unittest.TestCase):
    """Reviewer-supplied notes mark the text they qualify, without deciding it."""

    @staticmethod
    def section(summary: str, title: str) -> list[str]:
        """Return the lines of one ``## …`` section, excluding its heading."""
        lines = summary.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.startswith("## ") and title in line)
        end = next((index for index, line in enumerate(lines[start + 1:], start + 1)
                    if line.startswith("## ")), len(lines))
        return lines[start + 1:end]

    def setUp(self):
        self.record = load_record()
        self.notes = [
            {"title": "示例用药天数与医嘱不符", "level": "高",
             "detail": "示例详情。", "verify": "实际发药天数。",
             "anchor": "示例新发症己"},
            {"title": "示例时间口径", "level": "中", "detail": "示例详情二。",
             "verify": "以哪个为准。", "anchor": "示例诊断甲（术后）"},
            {"title": "无锚点的条目", "level": "低", "verify": "读一遍。"},
        ]
        self.summary = render_clinical_summary(self.record, self.notes)

    def test_notes_are_numbered_and_listed_after_the_record(self):
        self.assertIn("### 1. 示例用药天数与医嘱不符（高）", self.summary)
        self.assertIn("→ 核实：实际发药天数。", self.summary)
        lines = self.summary.splitlines()
        dataset = next(index for index, line in enumerate(lines)
                       if line.startswith("## ") and "结构化病历" in line)
        reviewer = next(index for index, line in enumerate(lines)
                        if line.startswith("### ") and "需人工核实" in line)
        source = next(index for index, line in enumerate(lines)
                      if line.startswith("## ") and "病历原文" in line)
        # Reviewer detail sits between the tables and the source wording.
        self.assertLess(dataset, reviewer)
        self.assertLess(reviewer, source)

    def test_anchor_marks_the_number_next_to_the_source_line(self):
        marked = [line for line in self.summary.splitlines()
                  if line.startswith("> ※需核实")]
        self.assertTrue(marked[0].startswith("> ※需核实 1"))
        self.assertTrue(marked[1].startswith("> ※需核实 2"))

    def test_note_without_anchor_stays_in_the_list_only(self):
        self.assertIn("### 3. 无锚点的条目（低）", self.summary)
        self.assertNotIn("※需核实 3", self.summary)

    def test_table_rows_keep_their_columns_and_marks_follow_the_table(self):
        table = self.notes_table_summary()
        lines = table.splitlines()
        drug_rows = [line for line in lines if line.startswith("| 示例药甲片")]
        self.assertEqual(len(drug_rows), 1)
        columns = drug_rows[0].count("|")
        header = next(line for line in lines if line.startswith("| 药品"))
        self.assertEqual(columns, header.count("|"))
        mark = next(index for index, line in enumerate(lines)
                    if line.startswith("> ※需核实 1"))
        # The mark sits after the last table row, never between rows.
        self.assertFalse(lines[mark - 1].startswith("|"))
        self.assertIn("示例药甲片", lines[mark])

    def notes_table_summary(self) -> str:
        record = json.loads(json.dumps(self.record))
        # The collection pack is flat; the latest encounter keeps the sample
        # medication list so the discharge table renders that row.
        record["encounters"][-1]["discharge_medications"] = [
            "示例药甲片 10 mg 口服 每天一次 总量7片",
        ]
        return render_clinical_summary(
            build_record(record, record_id="demo"),
            [{"title": "示例用药天数与医嘱不符", "level": "高",
              "detail": "示例详情。", "verify": "实际发药天数。",
              "anchor": "| 示例药甲片 | 10mg | 口服 | 每天一次 | 7片 | 已解析 |"}],
        )

    def test_notes_do_not_change_the_compiled_record(self):
        plain = render_clinical_summary(self.record)
        # Notes add mark lines only; the documented source text is unchanged.
        stripped = [line for line in self.section(self.summary, "病历原文")
                    if not line.startswith("> ※需核实")]
        self.assertEqual(self.section(plain, "病历原文"), stripped)

    def test_empty_or_titleless_notes_are_ignored(self):
        self.assertEqual(render_clinical_summary(self.record, []),
                         render_clinical_summary(self.record))
        self.assertEqual(render_clinical_summary(self.record, [{"detail": "x"}]),
                         render_clinical_summary(self.record))


class ReviewNotesFileTests(unittest.TestCase):
    """The CLI reads reviewer items from a private file outside the repo.

    These checks use temporary directories only; no real record is needed to
    prove the path boundary and the list validation.
    """

    def setUp(self):
        import importlib.util
        script = Path(__file__).resolve().parents[1] / "scripts" / "structure_record.py"
        spec = importlib.util.spec_from_file_location("structure_record", script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_repository_paths_are_refused(self):
        root = Path(self.module.__file__).resolve().parents[1]
        with self.assertRaises(ValueError):
            self.module.resolve_private_path(root / "record.json", must_exist=False)

    def test_missing_review_notes_file_is_refused(self):
        with self.assertRaises(ValueError):
            self.module.resolve_private_path(Path("/nonexistent/review-notes.json"),
                                             must_exist=True)

    def test_notes_outside_the_repository_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-notes.json"
            path.write_text("[]", encoding="utf-8")
            # resolve() may rewrite the /var symlink on macOS, so compare
            # resolved forms instead of the raw temporary path.
            self.assertEqual(self.module.resolve_private_path(path, must_exist=True),
                             path.resolve())


if __name__ == "__main__":
    unittest.main()
