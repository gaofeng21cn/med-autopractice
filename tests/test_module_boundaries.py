import unittest

from med_autopractice.domain.task import Fact, Proposal, Snapshot, SubjectRef, TaskRequest
from med_autopractice.medicine.router import CapabilityKnowledgeRouter
from med_autopractice.workflows.catalog import PILOT_WORKFLOWS


SUBJECT = SubjectRef("demo-hospital", "synthetic-patient", "synthetic-encounter")
SNAPSHOT = Snapshot(SUBJECT, "demo-revision", (Fact("fact", "demo", "demo://fact"),),
                    ("demo://trace",))


class FixedProposer:
    def __init__(self, generator_ref):
        self.generator_ref = generator_ref

    def propose(self, request, snapshot):
        return Proposal(snapshot.subject, request.capability_id,
                        {"draft": self.generator_ref}, ("fact",), self.generator_ref)


class ModuleBoundaryTests(unittest.TestCase):
    def test_capabilities_can_use_different_medical_proposers(self):
        router = CapabilityKnowledgeRouter({
            "write_inpatient_notes": FixedProposer("demo://model/note-v1"),
            "revise_orders": FixedProposer("demo://model/order-v2"),
        })
        note = router.propose(TaskRequest("note", "write_inpatient_notes", "demo", SUBJECT,
                                          "demo://authority"), SNAPSHOT)
        order = router.propose(TaskRequest("order", "revise_orders", "demo", SUBJECT,
                                           "demo://authority"), SNAPSHOT)
        self.assertNotEqual(note.generator_ref, order.generator_ref)
        self.assertNotEqual(note.digest, order.digest)
        with self.assertRaises(ValueError):
            router.propose(TaskRequest("other", "unconfigured", "demo", SUBJECT,
                                       "demo://authority"), SNAPSHOT)

    def test_workflow_owns_sources_and_review_gate(self):
        note = PILOT_WORKFLOWS["write_inpatient_notes"].policy("saved_draft")
        order = PILOT_WORKFLOWS["revise_orders"].policy("entered")
        self.assertIn("nursing", note.required_scopes)
        self.assertIn("current_orders", order.required_scopes)
        self.assertFalse(note.requires_pre_submit_review)
        self.assertTrue(order.requires_pre_submit_review)
        self.assertEqual(order.expected_system_state, "entered")
        with self.assertRaises(ValueError):
            PILOT_WORKFLOWS["revise_orders"].policy("")


if __name__ == "__main__":
    unittest.main()
