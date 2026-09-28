
from __future__ import annotations

from .ledger import RepairLedger
from .operators import ClosureOperators
from .outcomes import ClosureOutcome, DroppedSlice, UpstreamReferral, Verified
from .owners import Verdict, is_repairable, owner_of, Owner

DEFAULT_REPAIR_BUDGET = 3


def run_closure(
    *,
    environment: object,
    page_specifications: object,
    source_traces: object,
    slice_id: str,
    operators: ClosureOperators,
    ledger: RepairLedger,
    repair_budget: int = DEFAULT_REPAIR_BUDGET,
) -> ClosureOutcome:

    budget = int(repair_budget)
    if budget < 0:
        raise ValueError("repair budget must not be negative")

    obligations = operators.build_obligations(source_traces)
    current = environment
    waves: list[dict[str, object]] = []

    for k in range(budget + 1):
        execution = operators.execute(current, obligations)
        failure = operators.review(source_traces, execution, obligations)
        stage = "review"
        if failure is Verdict.PASS:
            failure = operators.probe(current, page_specifications, obligations)
            stage = "probe"

        if failure is Verdict.PASS:
            waves.append({"wave": k, "result": "pass"})
            ledger.record_outcome(slice_id, "verified")
            return Verified(
                environment=current,
                slice_id=slice_id,
                waves=tuple(waves),
                repairs_consumed=ledger.consumed(slice_id),
                evidence=operators.revision_evidence(current),
            )

        waves.append({"wave": k, "result": "fail", "failure": failure.as_dict(), "stage": stage})

        if owner_of(failure) is Owner.SEMANTICS:
            ledger.record_outcome(slice_id, "upstream")
            return UpstreamReferral(
                failure=failure,
                slice_id=slice_id,
                waves=tuple(waves),
                repairs_consumed=ledger.consumed(slice_id),
            )


        if ledger.remaining(slice_id, budget) == 0 or not is_repairable(failure):
            break

        current, delta = operators.repair(current, failure, page_specifications)
        ledger.record_repair(slice_id)
        operators.invalidate(delta)
        waves[-1]["repaired"] = sorted(delta)

    ledger.record_outcome(slice_id, "dropped")
    return DroppedSlice(
        environment=current,
        failure=failure,
        slice_id=slice_id,
        waves=tuple(waves),
        repairs_consumed=ledger.consumed(slice_id),
        at_cap=ledger.remaining(slice_id, budget) == 0,
    )
