"""Run 5-point site UI/KVM qualification stress suite and optionally promote observed dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from med_autopractice.deployment import _build_driver  # noqa: E402
from med_autopractice.domain.task import SubjectRef  # noqa: E402
from med_autopractice.medicine.proposals import NOTES  # noqa: E402
from med_autopractice.platform.security import verify_private_directory, verify_private_file  # noqa: E402
from med_autopractice.storage.evidence_store import ManagedEvidenceDriver, build_evidence_cipher  # noqa: E402
from med_autopractice.workflows.catalog import LOCAL_WORKFLOWS  # noqa: E402
from med_autopractice.workflows.site_playbook import SitePlaybook  # noqa: E402
from med_autopractice.workflows.site_qualify import run_qualification_suite  # noqa: E402


def _build_sandbox_driver_and_injector(evidence_dir: Path, config: dict):
    from examples.sandbox_his.sandbox_engine import SyntheticHISSandbox
    from med_autopractice.adapters.drivers.browser_cdp import BrowserCDPDriver
    from med_autopractice.adapters.drivers.desktop_win import WindowsUIADriver
    from med_autopractice.adapters.drivers.kvm_driver import KVMUIDriver
    from med_autopractice.adapters.drivers.text_entry import PureHIDSegmentedStrategy

    sandbox = SyntheticHISSandbox(site_id=str(config["site_id"]))
    kind = (config.get("driver") or {}).get("kind", "windows_uia")
    if kind == "browser_cdp":
        driver = BrowserCDPDriver(backend=sandbox, screenshot_dir=evidence_dir)
    elif kind == "ip_kvm":
        ui_cfg = config.get("ui_profile") or {}
        driver = KVMUIDriver(
            sandbox,
            screenshot_dir=evidence_dir,
            long_text_qualified=bool(ui_cfg.get("long_text_qualified", True)),
            text_strategy=PureHIDSegmentedStrategy(allow_unicode_hid=True),
        )
    else:
        driver = WindowsUIADriver(backend=sandbox, screenshot_dir=evidence_dir)
    return driver, lambda flag: setattr(sandbox, "human_interrupted", flag)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, type=Path, help="Private site directory outside the repo")
    parser.add_argument("--patient-id", default="synthetic-p01")
    parser.add_argument("--encounter-id", default="synthetic-e01")
    parser.add_argument("--capability-id", default="write_inpatient_notes")
    parser.add_argument("--report", type=Path, default=None, help="Output qualification_report.json path")
    parser.add_argument("--promote", action="store_true", help="Promote observed dependencies only if all 5 checks pass")
    parser.add_argument("--dependency", action="append", default=[], help="Dependency ID to promote")
    parser.add_argument(
        "--handoff-mode", choices=("interactive", "synthetic", "none"), default="interactive",
        help="How to verify human interrupt & recovery (default: interactive prompt)",
    )
    parser.add_argument("--handoff-timeout", type=float, default=30.0, help="Timeout seconds per handoff stage")
    parser.add_argument("--sandbox", action="store_true", help="Use local SyntheticHISSandbox instead of real device")
    args = parser.parse_args(argv)

    if args.capability_id not in NOTES:
        parser.error("this long-text qualification suite supports note draft workflows")
    if args.handoff_mode == "synthetic" and not args.sandbox:
        parser.error("synthetic handoff-mode requires --sandbox")
    if args.promote and (args.sandbox or args.handoff_mode != "interactive"):
        parser.error("site promotion requires a real driver and interactive handoff verification")
    workflow = LOCAL_WORKFLOWS[args.capability_id]
    allowed_dependencies = (workflow.write_dependency, workflow.readback_dependency)
    dep_ids = tuple(args.dependency or allowed_dependencies)
    if not set(dep_ids) <= set(allowed_dependencies):
        parser.error("dependencies must match the selected note draft workflow")

    try:
        site_dir = verify_private_directory(args.site, ROOT)
        config = json.loads(verify_private_file(site_dir / "deployment.json"))
        site_id = str(config["site_id"])
        bindings = (config.get("playbooks") or {}).get(args.capability_id) or {}
        books = {book.playbook_id: book for book in SitePlaybook.load_directory(site_dir / "playbooks")}
        collect_ids = bindings.get("collect") or ()
        collect_id = collect_ids if isinstance(collect_ids, str) else next(iter(collect_ids), None)
        collect_book = books.get(collect_id)
        write_book = books.get(bindings.get("write"))
        readback_book = books.get(bindings.get("readback"))
        if not all((collect_book, write_book, readback_book)):
            raise ValueError("qualification requires configured collect, draft write, and readback playbooks")
        defaults = "qualification_synthetic_report.json" if args.sandbox else "qualification_report.json"
        report_path = (args.report or site_dir / defaults).expanduser()
        if report_path.is_symlink():
            raise ValueError("qualification report must be a regular file")
        report_path = report_path.resolve()
        verify_private_directory(next(parent for parent in report_path.parents if parent.exists()), ROOT)
        if report_path.exists():
            previous = json.loads(verify_private_file(report_path))
            if not isinstance(previous, dict) or previous.get("schema_version") != "map-qualification-report.v1":
                raise ValueError("qualification report path contains a different file")

        evidence_dir = site_dir / "evidence"
        if args.sandbox:
            evidence_dir = evidence_dir / "synthetic-qualification"
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.sandbox:
            driver, sandbox_injector = _build_sandbox_driver_and_injector(evidence_dir, config)
            interrupt_injector = sandbox_injector if args.handoff_mode != "none" else None
        else:
            cipher = build_evidence_cipher(config.get("evidence_security"), site_dir, repo_root=ROOT)
            driver = _build_driver(config.get("driver") or {}, evidence_dir)
            if cipher is not None:
                driver = ManagedEvidenceDriver(driver, evidence_dir, cipher=cipher, site_id=site_id)
            interrupt_injector = None

        report = run_qualification_suite(
            site_dir,
            driver=driver,
            subject=SubjectRef(site_id, args.patient_id, args.encounter_id),
            collect_playbook=collect_book,
            write_playbook=write_book,
            readback_playbook=readback_book,
            interrupt_injector=interrupt_injector,
            interactive_handoff=(args.handoff_mode == "interactive" and interrupt_injector is None),
            handoff_timeout_sec=args.handoff_timeout,
            promote=args.promote,
            dependency_ids=dep_ids,
            report_path=report_path,
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False))
        return 1

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
