import tempfile
from pathlib import Path
import unittest
from dataclasses import replace

from med_autopractice.adapters.kvm import (
    KVMClinicalSystem, KVMNavigator, UIProfile, UIStateError, VisibleObject,
)
from med_autopractice.agent.task_engine import JsonlAuditSink, TaskEngine
from med_autopractice.domain.task import (
    ClinicianApproval, ExecutionReceipt, Fact, Proposal, Snapshot,
    SourceCoverage, SubjectRef, TaskRequest,
)
from med_autopractice.workflows.catalog import PILOT_WORKFLOWS


SUBJECT = SubjectRef("demo-ward", "synthetic-patient-1", "synthetic-admission-1")


class FakeKVM:
    def __init__(self):
        self.words = [
            {"text": SUBJECT.patient_id, "confidence": 99, "box": [10, 10, 100, 20]},
            {"text": SUBJECT.encounter_id, "confidence": 99, "box": [120, 10, 140, 20]},
            {"text": "查看检验", "confidence": 98, "box": [30, 60, 80, 20]},
        ]
        self.actions = []
        self.wait_status = "matched"
        self.shots = []

    async def observe(self, region=None, max_width=1280, ocr=True, include_image=False):
        self.last_observe = {"region": region, "ocr": ocr, "include_image": include_image}
        return ({"frame_id": "fresh-frame", "captured_at": "2026-01-01T12:00:00+08:00",
                 "original_size": [640, 480], "ocr": {"words": self.words}},
                b"synthetic-image" if include_image else None)

    async def shot(self, region=None, label=None, **kwargs):
        self.shots.append((region, label))
        return {"frame_id": "shot-frame", "captured_at": "2026-01-01T12:00:00+08:00",
                "original_size": [640, 480], "ocr": {"words": self.words},
                "shot_path": "demo://private/shots/orders.jpg", "shot_sha256": "0" * 64}

    async def action(self, frame_id, kind, **kwargs):
        self.actions.append((frame_id, kind, kwargs))
        return ({"frame_id": "next-frame", "captured_at": "2026-01-01T12:00:01+08:00",
                 "original_size": [640, 480], "ocr": {"words": self.words}},
                b"next-image" if kwargs.get("result_mode") != "ocr" else None)

    async def wait(self, region, condition="changed", text=None, **kwargs):
        return ({"frame_id": "wait-frame", "captured_at": "2026-01-01T12:00:01+08:00",
                 "original_size": [640, 480], "ocr": {"words": self.words},
                 "wait": {"status": self.wait_status}},
                b"wait-image" if kwargs.get("result_mode") != "ocr" else None)


class NavigatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_and_unique_anchor_use_fresh_guarded_frame(self):
        client = FakeKVM()
        navigator = KVMNavigator(client)
        navigator.require_visible_identity(await navigator.observe(), SUBJECT)
        await navigator.click_unique_text("查看检验", [0, 40, 180, 100])
        self.assertEqual(client.actions[0][0:2], ("fresh-frame", "click"))
        self.assertEqual(client.actions[0][2]["guard_region"], [0, 40, 180, 100])

    async def test_ambiguous_or_missing_identity_never_clicks(self):
        client = FakeKVM()
        navigator = KVMNavigator(client)
        wrong = SubjectRef(SUBJECT.site_id, "synthetic-other", SUBJECT.encounter_id)
        with self.assertRaises(UIStateError):
            navigator.require_visible_identity(await navigator.observe(), wrong)
        client.words.append(dict(client.words[-1]))
        with self.assertRaises(UIStateError):
            await navigator.click_unique_text("查看检验", [0, 40, 180, 100])
        self.assertEqual(client.actions, [])

    async def test_write_click_checks_identity_and_full_frame(self):
        client = FakeKVM()
        navigator = KVMNavigator(client, expected_size=(640, 480))
        await navigator.click_unique_text("查看检验", [0, 40, 180, 100], subject=SUBJECT)
        self.assertIsNone(client.actions[0][2]["guard_region"])
        self.assertIsNone(client.actions[0][2]["result_region"])
        with self.assertRaises(UIStateError):
            await navigator.click_unique_text(
                "查看检验", [0, 40, 180, 100],
                subject=SubjectRef(SUBJECT.site_id, "synthetic-other", SUBJECT.encounter_id),
            )
        self.assertEqual(len(client.actions), 1)

    async def test_resolution_change_rejects_profile(self):
        with self.assertRaises(UIStateError):
            await KVMNavigator(FakeKVM(), expected_size=(1920, 1080)).observe()

    async def test_routine_observation_keeps_picture_bytes_out_of_context(self):
        client = FakeKVM()
        navigator = KVMNavigator(client)
        screen = await navigator.observe()
        self.assertIsNone(screen.image)
        self.assertEqual(client.last_observe["include_image"], False)
        self.assertTrue(screen.words)
        await navigator.click_unique_text("查看检验", [0, 40, 180, 100])
        self.assertEqual(client.actions[0][2]["result_mode"], "ocr")
        # A read-only click still crops to its region, but that crop is text-only.
        self.assertEqual(client.actions[0][2]["result_region"], [0, 40, 180, 100])

    async def test_screenshot_returns_a_private_reference_not_bytes(self):
        client = FakeKVM()
        screen = await KVMNavigator(client).screenshot([0, 0, 640, 480], label="current orders")
        self.assertIsNone(screen.image)
        self.assertEqual(screen.shot_path, "demo://private/shots/orders.jpg")
        self.assertEqual(client.shots, [([0, 0, 640, 480], "current orders")])

    async def test_ascii_input_and_wait_stop_on_unqualified_text(self):
        client = FakeKVM()
        navigator = KVMNavigator(client, expected_size=(640, 480))
        await navigator.type_ascii("synthetic search", SUBJECT)
        self.assertEqual(client.actions[-1][1], "text")
        with self.assertRaises(ValueError):
            await navigator.type_ascii("病历", SUBJECT)
        self.assertEqual(len(client.actions), 1)
        client.wait_status = "timeout"
        with self.assertRaises(UIStateError):
            await navigator.wait_for_text("完成", [0, 0, 200, 100])


class WardKnowledge:
    def propose(self, request, snapshot):
        if request.capability_id == "write_inpatient_notes":
            payload = {"document_type": "progress_note", "draft": "合成病程记录，待医生补充实际查房内容。"}
        else:
            payload = {"changes": [{"action": "review", "item": "合成医嘱调整", "dose": "待医生确认"}]}
        return Proposal(snapshot.subject, request.capability_id, payload,
                        tuple(fact.fact_id for fact in snapshot.facts),
                        "demo://proposer/ward-v1")


class FakeWardUI:
    def __init__(self):
        self.profile = UIProfile(
            SUBJECT.site_id, "synthetic-ward", "1", "demo-doctor", (640, 480),
            "demo://qualification", frozenset({"write_inpatient_notes", "revise_orders"}), True,
        )
        self.objects = {}
        self.writes = 0
        self.candidates = ()

    def collect(self, subject, capability_id):
        return Snapshot(subject, "synthetic-revision-1", (
            Fact("lab", "合成检验结果", "demo://lab/1", "LIS", "lab/1", "2026-01-01", unit="mmol/L"),
            Fact("nursing", "合成护理记录", "demo://nursing/1", "NIS", "nursing/1", "2026-01-01"),
            Fact("orders", "合成当前医嘱", "demo://orders/1", "HIS", "orders/1", "2026-01-01"),
        ), ("demo://kvm/read",), (
            SourceCoverage("EMR", "patient_context", True),
            SourceCoverage("LIS", "results", True),
            SourceCoverage("NIS", "nursing", True),
            SourceCoverage("HIS", "current_orders", True),
        ))

    def write(self, proposal, existing_object_ref):
        self.writes += 1
        ref = existing_object_ref or f"demo://ui/object/{self.writes}"
        self.objects[ref] = VisibleObject(
            proposal.subject, ref, "saved_draft", dict(proposal.payload),
            "demo://kvm/screenshot", ("demo://kvm/action",), "2026-01-01T12:00:00Z",
        )
        return ExecutionReceipt(ref, "saved", ("demo://kvm/action",))

    def open_object(self, subject, capability_id, object_ref):
        return self.objects[object_ref]

    def find_candidates(self, task):
        return self.candidates


class IncompleteWardUI(FakeWardUI):
    def collect(self, subject, capability_id):
        snapshot = super().collect(subject, capability_id)
        coverage = tuple(item if item.scope != "nursing" else
                         SourceCoverage(item.source_system, item.scope, False, ("next page not read",))
                         for item in snapshot.coverage)
        return Snapshot(snapshot.subject, snapshot.revision, snapshot.facts,
                        snapshot.trace_refs, coverage)


class MismatchedWardUI(FakeWardUI):
    def open_object(self, subject, capability_id, object_ref):
        original = super().open_object(subject, capability_id, object_ref)
        return replace(original, payload={"draft": "not the proposed content"})


class LostResponseWardUI(FakeWardUI):
    def write(self, proposal, existing_object_ref):
        super().write(proposal, existing_object_ref)
        raise ConnectionError("synthetic response lost after GUI save")


class KVMWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ui = FakeWardUI()
        self.engine = TaskEngine(WardKnowledge(), KVMClinicalSystem(self.ui),
            JsonlAuditSink(Path(self.directory.name) / "audit.jsonl"), {
                capability: definition.policy("saved_draft")
                for capability, definition in PILOT_WORKFLOWS.items()
            })

    def test_note_and_order_have_distinct_review_gates(self):
        note = self.engine.start(TaskRequest("note", "write_inpatient_notes", "合成病程", SUBJECT, "demo://authority"))
        self.assertEqual(note.status, "ready_to_submit")
        self.engine.submit(note.task_id)
        self.assertEqual(note.status, "needs_clinician_acceptance")
        self.engine.review(note.task_id, "demo-doctor", accepted=True)
        order = self.engine.start(TaskRequest("order", "revise_orders", "合成调整", SUBJECT, "demo://authority"))
        with self.assertRaises(ValueError):
            self.engine.submit(order.task_id)
        self.assertEqual(self.ui.writes, 1)
        approval = ClinicianApproval(order.task_id, "demo-doctor", order.proposal.digest, order.snapshot.revision)
        self.engine.submit(order.task_id, approval)
        self.assertEqual(order.status, "needs_clinician_acceptance")
        self.engine.review(order.task_id, "demo-doctor", accepted=True)
        self.assertEqual(self.ui.writes, 2)

    def test_clinical_fact_keeps_absence_distinct_from_denial(self):
        absent = Fact("a", "", "demo://record/a", status="not_recorded")
        denied = Fact("b", "患者否认", "demo://record/b", status="denied")
        self.assertNotEqual(absent.status, denied.status)
        with self.assertRaises(ValueError):
            Fact("c", "", "demo://record/c", status="negative")

    def test_ambiguous_visible_recovery_never_replays_order(self):
        self.ui = LostResponseWardUI()
        self.engine.system = KVMClinicalSystem(self.ui)
        order = self.engine.start(TaskRequest("order", "revise_orders", "合成调整", SUBJECT, "demo://authority"))
        approval = ClinicianApproval(order.task_id, "demo-doctor", order.proposal.digest, order.snapshot.revision)
        with self.assertRaises(ConnectionError):
            self.engine.submit(order.task_id, approval)
        self.assertEqual(order.status, "execution_uncertain")
        self.ui.candidates = (
            VisibleObject(SUBJECT, "demo://object/1", "saved_draft", order.proposal.payload,
                          "demo://image/1", ("demo://trace/1",), "2026-01-01T12:00:00Z"),
            VisibleObject(SUBJECT, "demo://object/2", "saved_draft", order.proposal.payload,
                          "demo://image/2", ("demo://trace/2",), "2026-01-01T12:00:00Z"),
        )
        self.engine.reconcile(order.task_id)
        self.assertEqual(order.status, "execution_uncertain")
        self.assertEqual(self.ui.writes, 1)

    def test_revoked_ui_qualification_stops_before_input(self):
        order = self.engine.start(TaskRequest("order", "revise_orders", "合成调整", SUBJECT, "demo://authority"))
        self.ui.profile = replace(self.ui.profile, write_capabilities=frozenset())
        approval = ClinicianApproval(order.task_id, "demo-doctor", order.proposal.digest, order.snapshot.revision)
        self.engine.submit(order.task_id, approval)
        self.assertEqual(order.status, "draft_only")
        self.assertEqual(self.ui.writes, 0)

    def test_incomplete_nursing_page_blocks_order_write(self):
        self.engine.system = KVMClinicalSystem(IncompleteWardUI())
        order = self.engine.start(TaskRequest("order", "revise_orders", "合成调整", SUBJECT, "demo://authority"))
        self.assertEqual(order.status, "needs_information")
        with self.assertRaises(ValueError):
            self.engine.submit(order.task_id)

    def test_visible_content_mismatch_blocks_acceptance(self):
        self.engine.system = KVMClinicalSystem(MismatchedWardUI())
        note = self.engine.start(TaskRequest("note", "write_inpatient_notes", "合成病程", SUBJECT, "demo://authority"))
        self.engine.submit(note.task_id)
        self.assertEqual(note.status, "execution_uncertain")
        with self.assertRaises(ValueError):
            self.engine.review(note.task_id, "demo-doctor", accepted=True)

    def test_unqualified_long_text_never_reaches_ui(self):
        self.ui.profile = replace(self.ui.profile, long_text_qualified=False)
        note = self.engine.start(TaskRequest("note", "write_inpatient_notes", "合成病程", SUBJECT, "demo://authority"))
        self.assertEqual(note.status, "draft_only")
        with self.assertRaises(ValueError):
            self.engine.submit(note.task_id)
        self.assertEqual(self.ui.writes, 0)

    def test_unique_visible_object_reconciles_without_replay(self):
        note = self.engine.start(TaskRequest("note", "write_inpatient_notes", "合成病程", SUBJECT, "demo://authority"))
        self.ui.write(note.proposal, None)
        visible = self.ui.objects["demo://ui/object/1"]
        note.idempotency_key = "internal-only-key"
        note.status = "execution_uncertain"
        self.ui.candidates = (visible,)
        self.engine.reconcile(note.task_id)
        self.assertEqual(note.status, "needs_clinician_acceptance")
        self.assertEqual(self.ui.writes, 1)
