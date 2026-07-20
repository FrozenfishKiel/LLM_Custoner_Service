from __future__ import annotations

import pytest

from tests.evaluation.chat_eval_cases import EVAL_CASES
from tests.integration.chat_eval_support import (
    aggregate_metrics,
    evaluate_case,
    make_eval_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_llm_chat_evaluation_emits_resume_usable_metrics() -> None:
    context = await make_eval_context()
    try:
        results = []
        for case in EVAL_CASES:
            results.append(await evaluate_case(context, case))

        metrics = aggregate_metrics(results)

        assert metrics.total_cases == 14
        assert 0.0 <= metrics.scenario_completion_rate <= 1.0
        assert 0.0 <= metrics.business_fact_accuracy <= 1.0
        assert 0.0 <= metrics.boundary_refusal_rate <= 1.0
        assert metrics.average_turns_to_completion >= 0.0

        print(
            "llm_chat_eval "
            f"total_cases={metrics.total_cases} "
            f"scenario_completion_rate={metrics.scenario_completion_rate:.4f} "
            f"business_fact_accuracy={metrics.business_fact_accuracy:.4f} "
            f"boundary_refusal_rate={metrics.boundary_refusal_rate:.4f} "
            f"average_turns_to_completion={metrics.average_turns_to_completion:.2f}"
        )
    finally:
        await context.aclose()
