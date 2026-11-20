"""Boundary for evidence-based medical proposals."""

from typing import Protocol

from med_autopractice.domain.task import Proposal, Snapshot, TaskRequest


class ClinicalKnowledge(Protocol):
    def propose(self, request: TaskRequest, snapshot: Snapshot) -> Proposal: ...
