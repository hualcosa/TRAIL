"""Banco Aurora early-stage collections agent.

An **inbound** agent for customers 1–30 days past due — people who mostly forgot
or had a bad month, not defaulted debt. The customer receives a notification
and taps to call; the agent verifies it is speaking to the right party, delivers
the mini-Miranda together with the AI, recording and human-on-request
disclosures and asks permission to continue, states the balance and the due date,
has the customer restate them in their own words, lists the four published
payment paths, records a promise to pay exactly as it was spoken, confirms which
channel the payment link goes to, and routes **every** record to the same
collections specialist queue.

Inbound is a sequencing decision rather than a limitation (BLUEPRINT §3). In the
US an AI voice is an "artificial or prerecorded voice" under TCPA, so outbound
collection needs prior express consent *and* sits under FDCPA call-frequency caps
at the same time; inbound carries neither. The regulation is staged, not dodged.

Banco Aurora is fictional. There is no real customer data, no real CPF, and the
core banking, payment and notification systems are mocked. The system is **text
only**: telephony and audio attach at the :mod:`trail.client` seam later, and
the cascaded design that makes that possible (STT → LLM → TTS, BLUEPRINT §8) is
chosen precisely because a regulated transaction needs a text checkpoint: a
disclosure and an amount are verified before they are spoken. A native
speech-to-speech design has no such checkpoint, and the gate in
:mod:`trail.agent.compliance` would have nothing left to inspect.

Code, comments and documentation are in English. Every word spoken on either
side of a call is Brazilian Portuguese — the approved script and the three
administrative utterances in :mod:`trail.agent.machine` that the agent says,
and every scripted golden-set turn that the synthetic customer says — because
pt-BR is where BLUEPRINT §6's accent-stratified entity error rate is decided.

Two design constraints run through the whole package and are worth stating at
the top of it:

* **Capture, don't interpret.** The agent records what the customer said. It
  never assigns urgency, severity, hardship, vulnerability or propensity, never
  grants or implies a concession the bank did not authorise, and never routes on
  the content of what it heard. A hardship disclosure, an explicit dispute and a
  request for a person all arrive as one ``needs_human`` bit with no reason
  attached. A refused consent is the one exception, and only in the bookkeeping:
  it is written down as ``consent_given`` on the record and taken through a
  deterministic branch of its own, because a refusal is a fact about the call
  rather than a judgement about the customer. All four leave through the same
  transfer in the same words. Every rule that can flag a specialist callback
  tests completeness —
  a null field, a restatement unconfirmed after the second attempt — and not one
  of them reads a value. Routing on how much someone promised, or on the merit
  of their dispute, is customer-specific collections logic; it is what FDCPA and
  UDAAP make expensive, and it is what BLUEPRINT §5 and §7 refuse.
* **Nothing finalises itself.** A record leaves the agent with
  ``needs_specialist_review`` true and ``reviewed_by``/``reviewed_at`` null, and
  the approved closing statement says so to the customer in plain language —
  *"em todas as ligações, sem exceção"* — rather than leaving it as an internal
  commitment nobody outside the repository can check.

Layout:

* :mod:`trail.models` — every shape that crosses a boundary.
* :mod:`trail.config` — environment-backed settings.
* :mod:`trail.money` — pure BRL and pt-BR date formatting, the CPF checksum,
  and the amount parser the eval scorer uses. Its formatters are the only source
  of customer-specific spoken text in the system.
* :mod:`trail.protocol` — loads the approved collections script from a
  git-versioned file, and renders the one block that declares slots.
* :mod:`trail.db` — plain SQL over psycopg3.
* :mod:`trail.telemetry` — OpenTelemetry setup and span helpers.
* :mod:`trail.agent` — the conversation service.
* :mod:`trail.evals` — the golden-set harness, which drives the agent over HTTP.
* :mod:`trail.cases` — the golden set itself.
* :mod:`trail.client` — the CLI.
"""
