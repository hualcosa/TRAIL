"""The golden-set evaluation harness.

This package is the *AI evaluation* half of a split that runs through
BLUEPRINT §6's metric table. What it claims is the **execution** layer — entity
accuracy on amounts and dates, restatement confirmation, disclosure adherence.
It reaches none of the **outcome** layer above that, and says so where it counts
(``golden_v1``: verified incremental cash within 30 days against a holdout is
the north star this scorecard cannot reach, and neither
``fully_automated_rate`` nor ``promise_capture_rate`` is money). The split is
not clean downwards either — ``metrics.turn_latency_percentiles`` is an
infrastructure-layer number computed here — but the *questions* separate
cleanly, and that is the distinction worth keeping. It is deliberately a
separate service from the agent it grades.

**System observability** (OpenTelemetry → Jaeger) answers: how long did it take,
where did it stall, what errored, which span blew p95. It is generic and
industry-standard. **AI evaluation** — this package — answers: was the promised
amount written down as the customer said it, was the restatement genuinely
confirmed rather than falsely confirmed, did the agent speak any of the phrases
a case forbids (``must_not_contain``), and did any call end in a forced transfer
because the runtime allowlist refused an utterance. It is domain-specific, and
no generic APM does it for you. Conflating the two is a common and expensive
mistake: teams buy an APM, believe LLM evaluation is handled, and discover
months later that they have beautiful latency dashboards and no idea whether the
balance being read out is the one the system of record holds.

Note what this package does **not** own. "Is this utterance in the approved
script?" is answered at runtime, inside the agent, by
:func:`~trail.agent.compliance.assert_agent_text_is_approved` — an allowlist,
before the words leave the service. What lives here is the denylist half: a
per-case ``must_not_contain`` list a human wrote in advance, which catches an
off-script phrase only if someone guessed it. A denylist is the weaker
instrument on purpose, and it is not what the safety property rests on; nothing
in this package imports ``trail.agent.compliance`` at all.

The harness drives the agent **over HTTP**, through the agent's public contract
— the same one the CLI speaks (INTERFACES §3) — never by importing agent
internals. It uses a superset of the CLI's endpoints, since only the harness
needs ``POST /calls/{id}/unreachable`` to stage a call nobody answered. An eval
that calls internal functions tests the code; one that calls the interface tests
the *system* — serialisation, timeouts, and the contract the real client sees.

Modules:

* :mod:`trail.evals.runner` — drives every :class:`~trail.models.SyntheticCase`
  against the agent and returns raw per-case outcomes.
* :mod:`trail.evals.metrics` — metric definitions with their denominators
  written down, pre-registered thresholds, the published comparator each metric
  must be read against carrying its evidence grade, the
  omission/fabrication/wrong-value failure taxonomy, and regression detection.
* :mod:`trail.evals.report` — renders a run with ``rich``, alongside the
  published baselines it has to be judged against.
* :mod:`trail.evals.app` — the FastAPI service (``POST /runs``,
  ``GET /runs/{run_id}``, ``GET /runs/latest``).

Nothing is imported here on purpose: importing the package should not pull in
FastAPI, httpx or a database pool.
"""
