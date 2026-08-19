"""The agent service: the conversation, and the boundary it is not allowed to cross.

Four modules, in dependency order:

* :mod:`trail.agent.machine` — a LangGraph ``StateGraph`` over
  :class:`~trail.models.Step`: one node per step the agent listens at, four
  exit nodes for the five terminal states — both completed states leave through
  ``post_outcome`` — and a checkpointer holding every in-flight call. It decides
  *what happens next*; it never composes *what the agent says*, which is either
  :meth:`trail.protocol.Protocol.text_for` read verbatim,
  :meth:`trail.protocol.Protocol.render` fed slot values computed from the
  system of record for the single customer-specific block, or one of three
  administrative constants defined in the module itself — the transfer, the
  wrong-party close and the identity reprompt. Those three carry no approved
  collections content, and the offline compliance suite is what proves they
  carry none rather than a reader's assurance that they look harmless. No model
  is called from inside it, which is what keeps every branch offline-testable.
* :mod:`trail.agent.compliance` — five deterministic assertions, no LLM. The
  concession, classification and disclosure boundaries of BLUEPRINT §5 and §7
  expressed as an allowlist that fails loudly. An allowlist rather than a
  denylist because a denylist is a race against a language model's vocabulary,
  and a safety invariant cannot win that race.
* :mod:`trail.agent.llm` — the only module in the system that calls a model,
  and it only ever reads: write down what the customer said during one step, and
  judge whether a restatement reproduced the amount and the date.
* :mod:`trail.agent.app` — the FastAPI service wiring those three to HTTP,
  Postgres and OpenTelemetry, and the place every outbound utterance is screened
  before it leaves, against an approved set rendered with this call's own
  figures.

The split is the point. A reader should be able to answer "can this system
promise a customer a discount?" by reading ``compliance.py`` and ``machine.py``
alone, without trusting anything about the model.
"""
