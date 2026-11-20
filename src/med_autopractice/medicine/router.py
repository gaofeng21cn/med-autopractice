"""Select a medical proposal provider for each clinical capability."""

from collections.abc import Mapping

from med_autopractice.domain.task import Proposal, Snapshot, TaskRequest
from med_autopractice.medicine.ports import ClinicalKnowledge


class CapabilityKnowledgeRouter:
    def __init__(self, providers: Mapping[str, ClinicalKnowledge]):
        self._providers = dict(providers)

    def propose(self, request: TaskRequest, snapshot: Snapshot) -> Proposal:
        try:
            provider = self._providers[request.capability_id]
        except KeyError as error:
            raise ValueError(f"no medical proposer for {request.capability_id}") from error
        return provider.propose(request, snapshot)
