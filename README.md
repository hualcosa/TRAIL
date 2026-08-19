# TRAIL — Traced Runtime for Agents, Instrumented Locally

A local, full-stack scaffold for building agentic systems that can be **argued with**. Five services
under Docker Compose: a browser demo, a conversation service, an evaluation harness, Postgres and
Langfuse. Every turn emits a trace. Every trace carries a cost. A golden set drives the agent over the
same HTTP interface a human drives, so the thing you measure is the thing you shipped.

TRAIL is not an agent framework. It does not want to own your prompts, your graph, or your model
calls — LangGraph, an SDK, or a `while` loop are all fine, and TRAIL never sees inside them. What it
owns is the part nobody enjoys building and everybody needs by week three: **the spine that makes an
agent's behaviour observable, reproducible and falsifiable.**

The name is literal, and it is a debt. TRAIL is the scaffold behind
[**The Audit Trail**](https://github.com/hualcosa) — a series on AI platform architecture and
observability in regulated industries. Every entry in the series ships a working demo, and every demo
is an instance of this repository.

---

> ## Status: this README describes TRAIL as it is meant to be
>
> TRAIL is being extracted from a working system, not designed on a whiteboard. The collections agent
> it came from runs today — 563 unit tests, five services, a fifteen-case golden set. What does not
> exist yet is the *seam*: the code still carries the names of the domain it was born in.
>
> Every claim below is marked:
>
> | Marker | Means |
> |---|---|
> | **`shipped`** | Runs today. You can execute it. Extraction is mechanical renaming. |
> | **`extraction`** | The code exists and works, but is still named for collections. The work is de-doming it. |
> | **`designed`** | Argued for and specified. Not written. |
>
> No section is unmarked. When a marker is wrong, that is a bug in this file.

---

## 1. What TRAIL gives you

Three pillars. The acronym is not decoration — it is the component list.

| | Pillar | What it is | Status |
|---|---|---|---|
| **T·R** | **Traced Runtime** | A FastAPI conversation service where every turn is an OTel span tree, streamed to the browser as it runs. Six pipeline stages, each with its latency, its tokens and its cost. | **`extraction`** |
| **I** | **Instrumentation** | OTel → OTLP/HTTP → self-hosted Langfuse, wired. LLM spans arrive typed as generations with model, tokens and cost, not as anonymous spans. Trace deep links stamped onto API responses, so a record in the UI is one click from the span that produced it. Postgres tables for call records, turn traces and LLM traces. | **`shipped`** |
| **L** | **…Locally** — and the golden set | An evaluation harness that drives your agent over HTTP, computes metrics against **pre-registered thresholds**, classifies failures into a taxonomy, and detects regression against a previous run. | **`extraction`** |

The fourth thing, which does not fit the acronym and matters as much: **a browser demo that shows the
pipeline rather than the tokens.** A stage rail where a skipped stage renders struck through instead
of hidden — because which path the turn took is the information, and a hidden cell lets a skipped
stage pass for a successful one. **`extraction`**

---

## 2. The thesis

Most agent demos prove that a model can talk. That was interesting in 2023. What a technical audience
asks now is narrower and much harder:

**How do you know it did the right thing — and can you show me?**

TRAIL's answer is three commitments, and they are the reason the scaffold is shaped the way it is.

**The eval harness drives the same interface the human drives.** Not a test-only code path, not a
mocked service. `POST /calls/{id}/turns` is what the CLI calls, what the browser calls, and what the
golden set calls. A harness with its own entry point measures a system that does not exist in
production.

**Thresholds are pre-registered in code, before results.** A metric you choose after seeing the run is
a description, not a criterion. TRAIL puts the numbers in a module and makes the harness read them.

**The stream carries the pipeline, not the tokens.** Token streaming is a UX trick that proves
nothing. Streaming *stage, latency, cost, decision* proves the system did work and shows what the work
was. When your agent must not improvise — regulated text, approved copy, a deterministic gate — there
are no tokens to stream anyway, and the pipeline is the only honest thing to show.

---

## 3. Quickstart

**`designed`** — the commands are the collections build's, working today; the package name is not.

```bash
git clone https://github.com/hualcosa/trail && cd trail
cp .env.example .env          # set TRAIL_LLM_API_KEY
make test                     # the unit suite, offline, no credentials needed
make up                       # five services
make chat                     # hold a conversation from the terminal
make eval                     # run the golden set and print the report
```

| Surface | URL |
|---|---|
| **Demo UI** | **http://localhost:5173** |
| **Langfuse** | **http://localhost:3000** |
| Agent OpenAPI | http://localhost:8000/docs |
| Evals OpenAPI | http://localhost:8001/docs |
| Postgres | `localhost:5432` |

Langfuse logs in headlessly with a fixed demo identity — no signup form to click through:
`demo@trail.local` / `trail-demo-password`.

Every port is overridable on the command line, because you will eventually run two instances at once
and they will collide: `make up AGENT_PORT=8010 EVALS_PORT=8011 POSTGRES_PORT=55432 UI_PORT=5273`.
Pass the same values to `make chat` and `make eval`, which build host-side addresses from them.

The first `make up` takes noticeably longer than before — ClickHouse and Prisma migrations run on
the empty Langfuse volumes.

**The unit suite runs with no network and no credentials.** That is a design commitment, not an
accident: it means a reviewer can clone the repository and verify every claim about the deterministic
layer before deciding whether to trust the rest.

---

## 4. Architecture

**`shipped`** — this topology runs today.

```
  browser ──▶  ui :5173  ──nginx, /api/ ──┐
  client (CLI) ──────────────HTTP─────────┤
                                          ├──▶  agent :8000  ──▶  your LLM provider
  evals  :8001 ──────────────HTTP─────────┘        │
                                                   └──  protocol/, read-only bind mount

  agent and evals both write ──▶  postgres :5432   records · turn traces · LLM traces ·
                                                    eval runs · findings
  every service exports spans ──▶  langfuse :3000
```

| Service | What it is |
|---|---|
| `ui` | The demo surface. nginx serving a built Vite bundle, and the reverse proxy that puts the app and the API on **one origin** — which is why there is no CORS middleware anywhere in this repository. |
| `agent` | Your conversation. TRAIL owns the HTTP shell, the streaming, the persistence and the spans. You own what happens between them. |
| `evals` | The golden-set harness. Drives the agent over the same HTTP interface the client uses. |
| `postgres` | Five tables, applied once by the init hook. No migration tool — five tables do not need one, and a migration tool exercised only by a fresh volume is a tool that has never been tested. |
| `langfuse` | Self-hosted v4 — web, worker, ClickHouse, Redis, MinIO and its own Postgres. Six containers, because LLM observability is a different shape of problem from request tracing. The traces are the product, not a debugging aid you add later. |

**One image, three roles.** `agent`, `evals` and the CLI are the same build with a different entry
point, so a dependency drift between the harness and the thing it measures is not possible.

---

## 5. What is yours, and what is TRAIL's

**`designed`** — the seam is specified; drawing it is the extraction work.

TRAIL's bet is that this line is drawable, and that most scaffolds put it in the wrong place by
trying to abstract the *agent*. The agent is exactly the part you should write yourself.

| You write | TRAIL provides |
|---|---|
| Your state machine, graph, or loop | The HTTP shell around it, and the SSE pipeline that reports it |
| Your prompts and model calls | Token and cost accounting, LLM trace persistence, span wrapping |
| Your domain models | The persistence layer, the schema shape, the record contract |
| Your assertions about what must never happen | The gate seam that runs them on every outbound utterance, and fails the turn when one trips |
| Your golden set and your thresholds | The runner, the metrics engine, the failure taxonomy, regression detection, the report |
| Your protocol or approved-content files | Versioned loading, slot rendering, fail-fast on a missing slot |

**What TRAIL deliberately does not abstract:** the compliance gate's *rules*, and the state machine's
*steps*. Both are attempts to generalise a thing that should stay concrete. A pluggable compliance
layer that satisfies two domains prevents nothing in either — the gate only works because it knows
which step it is standing in.

---

## 6. How the seam was found

**`shipped`** — this measurement is real and reproducible from the collections repository's history.

This is the part of TRAIL that is not a taste claim.

The collections build began as a **mechanical port** of a healthcare agent — a different regulator, a
different language, a different conversation. That makes the repository an experiment that already
ran: a scaffold subjected to a full domain change, with the result in git.

The naive question — "what transferred unchanged?" — has a bleak answer. **Zero Python files survived
the port untouched.** All 21 were modified.

The useful signal is not change; it is **deletion**. Added lines are growth. Deleted lines mean the
previous domain's version was *wrong* for the new one. Sorting by lines removed over current size:

| Module | LOC | Deleted since the port |
|---|---:|---:|
| `evals/app.py` | 339 | **1.5%** |
| `db.py` | 472 | **4%** |
| `telemetry.py` | 303 | **4%** |
| `config.py` | 98 | **5%** |
| `protocol.py` | 381 | **7%** |
| `evals/runner.py` | 357 | **10%** |
| — *the seam* — | | |
| `agent/app.py` | 966 | 12% |
| `client/cli.py` | 1360 | 13% |
| `evals/report.py` | 474 | 15% |
| `models.py` | 929 | 17% |
| `evals/metrics.py` | 1313 | 19% |
| `agent/llm.py` | 894 | 20% |
| `agent/machine.py` | 1206 | 21% |
| `agent/compliance.py` | 1085 | **29%** |

There is a clean gap between 10% and 12%. Above it, roughly **1,950 lines** that survived a change of
industry, language and regulator. That is TRAIL, and it was measured rather than chosen.

Below the line is domain, and the two most-deleted modules are exactly the two §5 refuses to
generalise. A 29% rewrite of the compliance gate is what "regulator-independent mechanism,
domain-specific rules" looks like when you count it instead of asserting it.

One honest caveat: the port was mechanical, so churn since then mixes *domain adaptation* with
*features added later*. `agent/app.py`'s 586 added lines are the SSE streaming — new capability, and
generic. Deletion separates the two reasonably well. Not perfectly.

---

## 7. The example agent

**`designed`**

TRAIL ships with the smallest conversation that still exercises every pillar: a three-field intake
agent. It asks for a name, a date and a quantity, restates them, and asks the user to confirm.

It has one rule, and the rule is the domain-free version of every regulated-content thesis TRAIL was
built to serve:

> **Never state a value the user did not say.**

That rule is checkable by a deterministic assertion, which means the example ships with a real gate
rather than a decorative one. Five golden-set cases: one clean run, one correction mid-conversation,
one ambiguous date, one user who never confirms, one who supplies a value the agent must refuse to
invent.

It is deliberately boring. An example that is interesting competes with your agent for attention, and
the first thing you do with a scaffold is delete its example.

---

## 8. Consuming TRAIL

**`designed`** — and the shape is an **open decision**, recorded here rather than hidden.

TRAIL is meant to be instanced, not forked-and-forgotten. Two shapes, and they are different
architectures:

| Shape | What it buys | What it costs |
|---|---|---|
| **Package** — `pip install trail-spine @ git+…@v0.3.0` | Real versioning. A fix reaches every demo. The seam is enforced by the import boundary. | Every seam mistake becomes an API break. You must be right about the interface early. |
| **Template** — clone, rename, diverge | No coupling. Each demo evolves freely. | Fixes never propagate. After four demos you have four scaffolds. |

The pull is toward **package**, because the whole argument of §6 is that the spine is stable enough to
version. The risk is committing to an interface before a second consumer has stressed it. The likely
answer is a package with a deliberately small surface and a `v0.x` that promises nothing.

Undecided, on purpose. It will be settled before the extraction, not during it.

---

## 9. What is deliberately not in TRAIL

**`designed`** — the reasoning is inherited from the collections build, where each of these was
argued and declined.

| Not included | Why |
|---|---|
| **An agent abstraction** | The reason TRAIL exists. A base class for "an agent" is the fastest way to make the scaffold's opinions load-bearing on your design. Write your loop. |
| **A pluggable compliance engine** | See §5. Rules that satisfy every domain constrain none of them. |
| Authentication and multi-tenancy | One local stack, one trust boundary, no real data. Auth without a real identity provider and a real data boundary is theatre, and it models none of what makes auth hard. |
| Database migrations | Five tables. `make clean` drops the volume and the init hook applies the schema again. |
| An operator console | The `ui` service is a demo of one conversation, not a workplace. No queue, no assignment, no sign-off, no auth. Those belong to a product, and the specialist review step they would serve is the one thing a person should do. |
| An eval dashboard | `make eval` renders the metrics and the failure taxonomy legibly in a terminal, with no build step. Charting a run nobody has published is the most visible and least informative thing a repository can contain. |
| Audio, telephony, ASR, TTS | Transport. It attaches at the client boundary, which is why that boundary is a service. Building it first spends week one on codecs and answering-machine detection, and none of the interesting problems live there. |
| A cloud deployment | **`designed`**, and coming — but as a complement, not a default. A 100% local system cannot prove the audit-trail primitives (IAM, network isolation, retained logs), and those primitives are what the series is actually about. Local buys reproducibility; only a real deployment buys evidence. |

---

## 10. Repository layout

**`designed`** — the target shape.

```
README.md                     This file
INTERFACES.md                 The interface reference, written out of shipped code — not proposed to it
docker-compose.yml            The five services
Dockerfile                    One image, three roles
Makefile                      The control surface: up · chat · eval · test · lint · clean
.env.example                  Every variable, with its default and the reason for it
db/schema.sql                 Five tables, applied once by the Postgres init hook

src/trail/
  config.py                   pydantic-settings, TRAIL_ prefix
  db.py                       psycopg3 + pool, plain SQL
  telemetry.py                OTel SDK → OTLP → Langfuse, and the trace deep links
  protocol.py                 Versioned approved content, slot rendering, fail-fast on a gap
  runtime/                    The HTTP shell, the SSE turn pipeline, the gate seam
  evals/                      runner · metrics (thresholds) · report · regression
  client/                     CLI — and where a transport layer attaches later

example/                      The three-field intake agent, and its five golden cases
ui/                           The demo surface: Vite bundle + nginx reverse proxy
tests/                        Unit tests offline; integration tests behind a marker
```

---

## 11. Licence

**Undecided, and load-bearing.** The collections repository it came from has no LICENSE file, which
under default copyright means all rights reserved — appropriate for something published to be read
and argued with, and fatal for something meant to be depended on.

A scaffold that other people cannot legally instance is not a scaffold. This gets a permissive
licence before the first public tag, or TRAIL is just a blog post with a repository attached.

---

## 12. Provenance

TRAIL was extracted from the **Banco Aurora early-stage collections agent**, the second entry in The
Audit Trail — itself a port of a healthcare pre-operative preparation agent, the first. The extraction
method is §6, and it only worked because there were two instances to compare.

That is the honest version of the rule of three: TRAIL is at two. The interface will be wrong in
places that only a third domain will find.
