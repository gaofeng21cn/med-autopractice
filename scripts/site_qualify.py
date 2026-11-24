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
from med_autopractice.workflows.site_playbook import SitePlaybook  # noqa: E402
from med_autopractice.workflows.site_qualify import run_qualification_suite  # noqa: E402


def _build_sandbox_driver_and_injector(site_dir: Path, evidence_dir: Path, config: dict):
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

    site_dir = args.site.expanduser().resolve()
    if site_dir.is_relative_to(ROOT):
        parser.error("--site must live outside the repository")
    config = json.loads((site_dir / "deployment.json").read_text(encoding="utf-8"))
    site_id = str(config["site_id"])
    driver_cfg = config.get("driver") or {}
    evidence_dir = site_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True, mode=0o700)

    interrupt_injector = None
    if args.sandbox:
        driver, sb_injector = _build_sandbox_driver_and_injector(site_dir, evidence_dir, config)
        if args.handoff_mode != "none":
            interrupt_injector = sb_injector
    else:
        driver = _build_driver(driver_cfg, evidence_dir)
        if args.handoff_mode == "synthetic":
            print(json.dumps({"passed": False, "error": "synthetic handoff-mode requires --sandbox"}, ensure_ascii=False))
            return 1

    bindings = (config.get("playbooks") or {}).get(args.capability_id) or {}
    books = {b.playbook_id: b for b in SitePlaybook.load_directory(site_dir / "playbooks")} if (site_dir / "playbooks").is_dir() else {}
    collect_ids = bindings.get("collect") or ()
    collect_id = collect_ids[0] if isinstance(collect_ids, list) and collect_ids else collect_ids if isinstance(collect_ids, str) else None

    dep_ids = tuple(args.dependency) if args.dependency else ("save_inpatient_note_draft", "readback_inpatient_note")
    try:
        report = run_qualification_suite(
            site_dir,
            driver=driver,
            subject=SubjectRef(site_id, args.patient_id, args.encounter_id),
            collect_playbook=books.get(collect_id) if collect_id else None,
            write_playbook=books.get(bindings.get("write")) if bindings.get("write") else None,
            readback_playbook=books.get(bindings.get("readback")) if bindings.get("readback") else None,
            interrupt_injector=interrupt_injector,
            interactive_handoff=(args.handoff_mode == "interactive" and interrupt_injector is None),
            handoff_timeout_sec=args.handoff_timeout,
            promote=args.promote,
            dependency_ids=dep_ids,
            report_path=args.report or (site_dir / "qualification_report.json"),
        )
    except ValueError as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False))
        return 1

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
