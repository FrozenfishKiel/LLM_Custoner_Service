# LLM Chat Evaluation Design

Date: 2026-07-19

## Goal

Add a minimal, reproducible evaluation slice for the ecommerce customer-service chat flow that produces resume-usable LLM application metrics from the current system, without inventing unsupported online KPIs.

## Why This Slice

The project already has production-authenticated chat routes, account/business-user binding, business-action ownership hardening, Redis tracker/session/token infrastructure, and rate limiting. What it does not yet have is a versioned LLM evaluation set or a report that quantifies customer-service task quality.

For resume and launch-readiness purposes, the shortest useful gap to close is not a full evaluation platform. It is a small black-box evaluation suite that exercises the real chat route and produces four objective metrics:

1. scenario completion rate
2. business-fact answer accuracy
3. boundary-refusal rate
4. average turns to completion

## Scope

This slice measures only the current ecommerce customer-service experience through the authenticated HTTP chat path.

In scope:

- five core customer-service scenarios:
  - order query
  - logistics query
  - address modification
  - order cancellation
  - after-sales request
- boundary / out-of-domain prompts
- multi-turn completion counting
- result verification against real MySQL state for read/write scenarios
- a retained evaluation report with concrete metric outputs

Out of scope:

- real user traffic metrics such as transfer-to-human rate or satisfaction
- production DeepSeek latency/cost dashboards
- large-scale benchmark infrastructure
- browser E2E UI scoring
- RAG retrieval relevance metrics such as hit@k unless directly needed for the four top-level outcomes

## Recommended Approach

Use a black-box HTTP evaluation harness against the authenticated chat routes instead of a white-box Agent-only harness.

Reasoning:

- It exercises the same path a real signed-in user takes: auth, business binding, chat route, tracker, Agent, Flow, Action, and database state.
- It produces metrics that are easier to defend in a resume because they reflect end-to-end application behavior.
- It avoids overstating internal Flow accuracy when the user-visible outcome is what matters most.

White-box Agent assertions may still be used sparingly for debugging, but they are not the primary evaluation result for this slice.

## Metrics

### 1. Scenario Completion Rate

Definition: the proportion of evaluated customer-service tasks that reach the expected business outcome.

Scoring:

- order query: expected order fact is returned
- logistics query: expected logistics fact is returned
- address modification: database state is updated to the expected value
- order cancellation: database state reaches the expected cancelled state or expected idempotent outcome
- after-sales request: expected after-sales record/state is created or reused as designed

Formula:

`completed_tasks / total_customer_service_tasks`

### 2. Business-Fact Answer Accuracy

Definition: the proportion of evaluated read/write scenarios whose returned or persisted business facts match the authoritative MySQL state.

Scoring:

- for read scenarios, compare expected business facts in the response text against fixture-backed authoritative state
- for write scenarios, compare final MySQL state against the intended effect

Formula:

`factually_correct_scenarios / total_fact_checked_scenarios`

### 3. Boundary-Refusal Rate

Definition: the proportion of out-of-domain prompts that are refused or redirected with the expected ecommerce-customer-service boundary behavior.

Scoring:

- accepted only when the reply stays within the configured customer-service identity/boundary
- failed when the system drifts into unrelated general-assistant behavior or triggers an irrelevant business flow

Formula:

`correct_boundary_refusals / total_boundary_cases`

### 4. Average Turns To Completion

Definition: average number of user turns required for successfully completed customer-service tasks.

Scoring:

- count only successful scenario runs
- each submitted user message is one turn
- final value reported as mean, with a small sample count note

## Evaluation Dataset

Create a compact versioned dataset rather than a broad corpus.

Target composition:

- 2 cases per core customer-service scenario = 10 task cases
- 4 boundary cases

Total initial dataset: 14 cases

Each case must define:

- `case_id`
- `category`
- `account_fixture`
- `initial_messages`
- `followup_messages` if multi-turn completion is required
- `expected_outcome`
- `expected_business_facts`
- `expected_db_assertion` when the scenario mutates state
- `expected_boundary_behavior` for out-of-domain prompts

The dataset should prefer deterministic prompts that align with the project’s fixed demo/business fixtures, so results stay reproducible.

## Execution Flow

For each case:

1. create or prepare a test account with bound business user and seeded data
2. authenticate through the existing auth route flow
3. send the case messages through `POST /api/chat/messages`
4. collect returned text/messages per turn
5. inspect authoritative MySQL state when required
6. score completion, factual correctness, boundary behavior, and turns

At the end of the run:

- aggregate the four top-level metrics
- write a concise evidence report
- retain raw per-case outcomes for debugging

## Output Artifacts

Expected outputs for this slice:

- a versioned evaluation dataset file under `tests/evaluation/`
- an integration-style evaluation runner under `tests/integration/` or `tests/evaluation/`
- retained evidence text output under `docs/reports/integration/evidence/`
- a summary report under `docs/reports/integration/`

The report should present:

- total cases
- scenario completion rate
- business-fact answer accuracy
- boundary-refusal rate
- average turns to completion
- notable failure modes if any

## Pass/Fail Philosophy

This slice is evidence-first, not target-first.

It must report measured values from the current system. It must not backfill desired numbers from PRD targets, and it must not silently discard failures to improve percentages.

If the first run shows weak metrics, that is still useful: it gives us a truthful baseline for resume wording and for the next optimization slice.

## Risks

- Some chat responses may be hard to score with simple exact matching; the initial dataset should therefore use prompts whose expected business facts are deterministic and easy to assert.
- Boundary cases may require a small set of accepted refusal phrases instead of one exact string.
- If live DeepSeek availability is unstable, we may need to separate “online model run” from “local deterministic harness preparation,” but the goal remains to collect real end-to-end results whenever the environment is available.

## Non-Goals

This slice does not attempt to prove:

- real production user satisfaction
- transfer-to-human rate
- long-session retention
- full-scale load quality under 20 concurrent chats
- general-domain assistant ability

## Success Criteria

This design is complete when implementation can produce one report with the four metrics below from the real authenticated chat flow:

- scenario completion rate
- business-fact answer accuracy
- boundary-refusal rate
- average turns to completion

And when every number in that report can be traced back to a concrete, replayable case.
