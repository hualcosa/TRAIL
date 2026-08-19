# Build report — porting the Meridian Health platform to Banco Aurora

**Date:** 14–15 August 2026. **Method:** parallel agents against a pinned contract, every wave
followed by an adversarial review pass. **Source:** `../healthcare-preop-preparation-calls`.

This file records what was built, what the reviews caught, and what the first runs found. It is
not a results publication — see §5.

**§1 is a snapshot dated 15 August and is deliberately not kept current.** A later change added the
`ui` service — the browser demo and its streamed turn pipeline — which moved the counts it records:
five services rather than four, 563 unit tests rather than 552. The figures below are left as they
were measured, because a build report that silently absorbs later work stops being a record of a
build and becomes an unsourced description of the present. §6 and §7 are written for a reader
running this today and do reflect the `ui` service.

---

## 1. Where it landed

| | |
|---|---|
| `src/` | 12,206 lines |
| `tests/` | 9,770 lines — **552 unit, 14 integration** |
| Approved protocol + schema | 734 lines |
| Unit suite | **552 green, offline**: no Docker, no database, no network, no API key |
| Integration suite | **14 green** against the live compose stack |
| Lint | `ruff format` + `ruff check` clean |
| Stack | `docker compose` up, four services healthy, real call completed |
| Golden-set runs | 2, both completed, **`compliance_violations = 0` on both** |
| Documentation | fact-checked claim by claim against the code; 28 false statements corrected |

Against the mechanical-port baseline: 42 files changed, +11,890 / −6,156.

## 2. What was reused, and what that bought

The healthcare repository was not "a colonoscopy agent". It was a **regulated-domain capture
agent**: a deterministic state machine reading reviewed text verbatim, an LLM used only to
transcribe what the other party said, a refusal to make the judgement the regulator cares about,
and a human on every record. Collections is that shape with a different regulator.

| Tier | Content | Outcome |
|---|---|---|
| Copy + rename | telemetry, config, protocol loader, db, eval runner/app/report, agent app, CLI, LLM plumbing, Docker/compose/Makefile/pyproject | Ported in half a day. `make test` green before one domain word changed — the gate that proved the mechanical pass broke nothing |
| Skeleton kept, rules rewritten | models, machine, compliance, metrics, prompts, schema | The expensive half, and where every defect below lived |
| New | protocol text (PT-BR), golden set, `money.py`, slot rendering, docs | Genuinely new work |

**The decisions were the asset, not the code.** Allowlist over denylist. Deterministic gate with
LLM capture underneath. Evals over HTTP rather than imports. Scripted customers over LLM-generated
ones. OTel and evaluation as separate services. Nullable metrics over sentinels. Protocol as a
reviewed file rather than a service. Each cost a day to get right in the first repository and five
minutes to carry over.

## 3. The one structural addition: slot rendering

Healthcare's protocol was strictly patient-independent, so its compliance allowlist was exact
string equality against the file. A collections call **must** speak a customer-specific amount
(BLUEPRINT §3), and BLUEPRINT §5 makes a wrong balance spoken aloud a zero-tolerance failure.

`state_balance` therefore declares four slots. Values are produced by deterministic formatters in
`trail/money.py` from `AccountProfile` fields that came out of the system of record, through
exactly one call site (`machine.slots_for_call`), and `assert_agent_text_is_approved` builds its
approved set by **rendering**. The agent speaks a number and the number provably did not come from
the model: an utterance carrying a different balance matches nothing and is refused before the
words leave the service.

That is a stronger claim than healthcare's, which refused to speak the customer-specific value at
all.

## 4. What the adversarial reviews caught

Every wave was written by parallel agents and then attacked by independent reviewers told to
refute conformance. Four blocking defects, all of which would have passed a green test suite.

**The gate was inverted.** `_approved_texts` fell back to the raw template when slots were not
supplied — so `"O valor em aberto é de {balance}"` was a *member of the approved set* and passed,
while the correctly rendered utterance carrying the real balance matched nothing and was refused.
An allowlist that admits the one string the system must never speak and refuses the one string it
exists to speak is worse than no allowlist, and it failed silently in both directions. Slotted
blocks with no slots now contribute nothing.

**Two concession patterns never fired.** `condição especial` — the phrase the approved capability
statement itself names, and the one a model reaches for when denied "desconto" — could not match,
because the character class spelled `õ` and `o` but not `ã`. And `parcelar em seis vezes a sua
dívida`, the sentence a collections agent actually says, exceeded the debt-object window.

**Disputes were a dead letter.** `_listen` transferred on `needs_human` before any rule ran, and
only the capture rule wrote disputes — so *"esse valor está errado, eu já paguei"* was said on
precisely the turn that deleted it, and the specialist phoned to ask what the customer had already
answered. Capture now happens before the branch. It changes no routing; it stops the exit being
lossy.

**The scorer was blind to the failure it guards.** `commitment_entity_accuracy` put both sides
through `parse_brl`, so an agent that normalised `"mil e duzentos"` into `"R$ 1.200,00"` — the one
act the capture architecture forbids — scored 3/3 with zero findings. Under verbatim capture the
record holds the spoken form, so the tolerance could only ever have absolved the agent, never the
customer. Scoring is now string equality; *pairing* stays money-aware, so a normalisation produces
one precise `wrong_value` instead of an omission-plus-fabrication cascade.

**And one inherited from the original: the suite was never hermetic.** The offline guard set
`TRAIL_ANTHROPIC_API_KEY`; `Settings` reads `TRAIL_LLM_API_KEY`. pydantic-settings discarded
the placeholder and every "offline" run read the developer's real key out of `.env`. The mirror
failure is the one that matters to a reader: on a clean checkout, where `.env` does not exist
because it is gitignored, `Settings()` raised instead of skipping — so "make test needs no
credentials" was false in both directions at once. The healthcare repository has the identical
`PREOP_` mismatch. Fixed, and proven by running the suite with `.env` moved out of the tree.

Two locale defects were found the same way. The identity gate took the **last** name token as the
surname, which works where names are given-name-plus-one-surname; Brazilian names are routinely
given plus maternal plus paternal with particles between, and the same person answers as "Marina
Rocha" or "Marina Santos". Demanding the final token would have sent a large and entirely
legitimate slice of customers to `not_right_party`, hardest to the people with the longest names.
And the tokeniser returned a `set`, so "the given name" was whichever token the hash landed on.

## 5. The first two runs — and why this is not a result

`make eval`, twice, against `gpt-5.6-luna`, fifteen scripted customers.

| Metric | Bar | Run 1 | Run 2 |
|---|---|---|---|
| `fully_automated_rate` | ≥ 0.30 | 0.400 | 0.333 |
| `promise_capture_rate` | ≥ 0.118 | 0.467 | 0.467 |
| `commitment_entity_accuracy` | ≥ 0.95 | 0.242 | **0.393** |
| `terms_confirmation_rate` | ≥ 0.70 | 0.727 | 0.727 |
| `false_terms_confirmations` | = 0 | 0 | 0 |
| `compliance_violations` | = 0 | **0** | **0** |
| `cost_per_fully_automated_call_usd` | ≤ 1.84 | 0.0045 | 0.0055 |
| `p95_turn_latency_ms` | ≤ 1500 | 3195 | 3244 |

**These are validation runs for the harness, not a published result.** No eval run is published in
this repository; §7 of the README is the pre-registration and it stands unedited.

The harness earned its keep immediately, finding three routing defects that 552 passing unit tests
did not:

- **`not_right_party` was reached zero times.** The extraction prompt listed "they say they are not
  the right party" among the `needs_human` triggers, and `_listen` transfers on that bit before any
  rule runs — so the hard identity gate was unreachable whenever a caller announced they were the
  wrong person, which is the ordinary way it happens. The measurement loss is the smaller half. The
  real cost is that it took the one caller the third-party-disclosure rule exists to protect and
  routed them *toward* a human who could disclose, instead of ending the call with no message left.
  Fixed; the state went 0 → 1.
- **Every discount request transferred.** The prompt listed it among the things the approved script
  cannot answer, but the script answers it: the capability statement in `offer_payment_path` is read
  to every customer and says exactly that the agent cannot offer one. A question the script answers
  is answered however many times it is asked, and transferring instead would hand a specialist most
  of the book.
- **The capture boundary was never specified.** The agent wrote `"os oitocentos e quarenta e sete e
  trinta e dois"`, article and all, where the golden set holds the figure. Entity accuracy 0.242 →
  0.393 once the prompt said where a value starts and ends; fabrications 5 → 0.

**What was deliberately not done: tuning until the numbers looked good.** Three cases still miss
their expected terminal state and entity accuracy is 0.393 against a pre-registered 0.95. Those are
findings, not embarrassments — they are what posts 3 and 4 are about — and no threshold moved to
meet them. The `p95` miss was pre-registered before the first run: an HTTP round trip to a hosted
model is not a voice-latency configuration.

The result that matters is `compliance_violations = 0` on both runs, across fifteen cases including
the discount trap asked three times.

## 6. What is still open

- **The outcomes layer is not built.** BLUEPRINT §5's primary metric is incremental cash within 30
  days against a holdout; this repository measures execution. The layer needs a Brazilian 1–30 DPD
  self-cure curve, which BLUEPRINT §10 still lists as an open question, and building it on an
  invented curve would break this repository's own rule that every baseline figure be traceable to
  a published source. Deferred for that reason and not for effort. See `PORT_PLAN.md` §3.1.
- Three golden cases miss their expected terminal state (`asks_for_discount`, `elderly_slow_speech`,
  `amount_edge_case`). Unresolved by design — they are the material.
- `commitment_entity_accuracy` is far below its bar and the gap is not yet characterised by
  failure mode.
- Audio, telephony, outbound and consent management, auth, migrations, an operator console and an
  eval dashboard are all deliberately absent. README §9 gives the reasons. The `ui` service added
  after this report is a **demo of one call**, not an operator surface: no queue, no assignment, no
  sign-off on a record, no auth.

## 7. Reproducing

```bash
cp .env.example .env          # set TRAIL_LLM_API_KEY
make test                     # 563 unit tests, offline, no credentials needed
make up                       # five services
make chat                     # hold a call from the terminal
make eval                     # the golden set and the report
```

Ports collide with the healthcare stack if both run at once. Override:
`make up AGENT_PORT=8010 EVALS_PORT=8011 POSTGRES_PORT=55432 LANGFUSE_WEB_PORT=3100`.
If `LANGFUSE_WEB_PORT` changes, `TRAIL_LANGFUSE_UI_BASE_URL` must change with it or the
deep link points at the wrong stack — the compose default already interpolates it, so this
only bites someone who pinned the URL in `.env`.
