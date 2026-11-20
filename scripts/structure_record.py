"""Clean one private collection pack into a structured clinical record.

Reads a collection JSON produced while reading hospital documents (stored
outside this repository) and writes a validated structured record plus a
review copy in Markdown. The command prints counts only, never patient
content, so its output is safe to keep in a task log.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.records.clinical_record import (  # noqa: E402
    build_record,
    build_problem_list,
    render_clinical_summary,
    render_markdown,
    validate_record,
)
from med_autopractice.records.research_dataset import (  # noqa: E402
    TABLES,
    build_dataset,
    render_table_csv,
    table_summary,
)


def resolve_private_path(argument: Path, *, must_exist: bool) -> Path:
    path = argument.expanduser().resolve()
    if path.is_relative_to(ROOT.resolve()):
        raise ValueError("collection packs and records must live outside this repository")
    if must_exist and not path.is_file():
        raise ValueError(f"collection pack does not exist: {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", required=True, type=Path,
                        help="private collection JSON outside this repository")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="private directory for the generated record")
    parser.add_argument("--record-id", default=None,
                        help="record identifier; defaults to the collection file stem")
    parser.add_argument("--formats", default="json,md,summary,csv",
                        help="comma separated subset of json,md,summary,csv")
    parser.add_argument("--tables-dir", type=Path, default=None,
                        help="private directory for the research tables as CSV; "
                             "defaults to <out-dir>/tables")
    parser.add_argument("--review-notes", type=Path, default=None,
                        help="optional private JSON list of items to verify; "
                             "each may carry an 'anchor' copied from the record "
                             "so the number is also marked next to that text")
    args = parser.parse_args()

    formats = {item.strip() for item in args.formats.split(",") if item.strip()}
    unknown = formats - {"json", "md", "summary", "csv"}
    if unknown:
        parser.error(f"unsupported formats: {', '.join(sorted(unknown))}")

    try:
        collection_path = resolve_private_path(args.collection, must_exist=True)
        out_dir = resolve_private_path(args.out_dir, must_exist=False)
        notes_path = (resolve_private_path(args.review_notes, must_exist=True)
                      if args.review_notes else None)
        tables_dir = (resolve_private_path(args.tables_dir, must_exist=False)
                      if args.tables_dir else out_dir / "tables")
    except ValueError as error:
        parser.error(str(error))

    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    notes = (json.loads(notes_path.read_text(encoding="utf-8"))
             if notes_path else None)
    if notes is not None and not isinstance(notes, list):
        parser.error("review notes must be a JSON list of objects")
    record_id = args.record_id or collection_path.stem
    record = build_record(collection, record_id=record_id)
    errors = validate_record(record)

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs: list[str] = []
    if not errors:
        if "json" in formats:
            target = out_dir / f"{record_id}.structured.json"
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
            target.chmod(0o600)
            outputs.append(str(target))
        if "md" in formats:
            target = out_dir / f"{record_id}.structured.md"
            target.write_text(render_markdown(record), encoding="utf-8")
            target.chmod(0o600)
            outputs.append(str(target))
        if "summary" in formats:
            target = out_dir / f"{record_id}.summary.md"
            target.write_text(render_clinical_summary(record, notes), encoding="utf-8")
            target.chmod(0o600)
            outputs.append(str(target))
        if "csv" in formats:
            dataset = build_dataset(record)
            tables_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tables_dir.chmod(0o700)
            for name in TABLES:
                target = tables_dir / f"{name}.csv"
                target.write_text(render_table_csv(name, dataset[name]), encoding="utf-8")
                target.chmod(0o600)
                outputs.append(str(target))

    analysis = build_problem_list(record)
    dataset = build_dataset(record)
    summary = {
        "ok": not errors,
        "record_id": record_id,
        "encounters": len(record.get("encounters") or []),
        "sections": sum(len(encounter.get("sections") or {})
                        for encounter in record.get("encounters") or []),
        "missing": sum(len(encounter.get("missing") or [])
                       for encounter in record.get("encounters") or []),
        "conflicts": sum(len(encounter.get("conflicts") or [])
                         for encounter in record.get("encounters") or []),
        "medication_lines": sum(
            len((encounter.get("sections") or {}).get("discharge_medications", {}).get("items") or [])
            for encounter in record.get("encounters") or []
        ),
        "unparsed_medication_lines": sum(
            len((encounter.get("sections") or {}).get("discharge_medications", {}).get("unparsed") or [])
            for encounter in record.get("encounters") or []
        ),
        "problems": len(analysis["problems"]),
        "similar_problem_pairs": len(analysis["needs_confirmation"]),
        "diagnosis_deltas": len(analysis["per_encounter_delta"]),
        "review_notes": len(notes or []),
        "tables": table_summary(dataset),
        "outputs": outputs,
        "validation_errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
