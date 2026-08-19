# Segunda via — design plan

The demo UI for the Banco Aurora 1–30 DPD collections agent. Audience: technical
leadership and compliance reviewers. Single job: make the product's thesis visible —
**the model listens, it never speaks.** Every word the customer hears is
compliance-approved text; every captured field traces back to the customer's own words.

Subject world: Brazilian bank collections paperwork. NCR carbon-copy forms, boleto
security paper, rubber stamps, ballpoint ink. Not a chat app — an instrument panel
wrapped around a conversation.

---

## Pass 1 — the plan

### Color

Fixed by the brief, used with these assignments:

| token | value | where it lands |
| --- | --- | --- |
| `--paper` | `#DCE3D9` | page ground, customer utterance blocks |
| `--paper-2` | `#E9EDE4` | header, agent utterance blocks, composer, active rail cell |
| `--canary` | `#E4D9A8` | the ficha panel, and nothing else |
| `--ink` | `#1D2A2E` | body text, primary buttons |
| `--ink-soft` | `#5C6B6A` | labels, meta, `—` (not asked), evidence quotes |
| `--rule` | `#A9B6A9` | every hairline, the perforation, leader dots |
| `--stamp` | `#B3372A` | `TRANSFERIDO`, errors, `não`, disputes |
| `--stamp-ok` | `#2F6B4F` | `TEXTO APROVADO`, the live status dot |

One derived value: `--canary-deep #D6C88C`, the carbon rule beside a
`source_utterance` and the background of the ficha's new-field flash. Derived rather
than invented, so the ficha stays one material.

Single committed light look, no dark mode.

### Type

Three faces, three jobs, no overlap. The split is the argument the page is making —
**speech is human, everything else is machine.**

- **Archivo** — ALL CAPS, 11px, weight 600, tracking `.12em`. Printed form field
  labels: section headers, row labels, eyebrows, buttons.
- **Newsreader** — 19px/1.5, `optical-sizing: auto`. Spoken words *only*. Agent
  utterances and customer utterances. Nothing else is ever set in it, so the
  reader learns that serif = someone said this out loud. Italic at 15px for a
  `source_utterance`.
- **IBM Plex Mono** — 12–13px. The machine layer: step ids, the stage rail, ms,
  tokens, cost, trace id, extraction values, account numbers, tri-state words.

Every family gets a real fallback stack; one `<link>` to fonts.googleapis.com with
`display=swap`.

### Layout

Header bar, then two columns. Left ~62%: the call — a step line, the transcript
(scrolls), the live stage rail, the composer pinned to the bottom. Right ~38%: FICHA
DA CHAMADA on canary, its own scroll. Under 900px it becomes one column and the ficha
collapses to a summary strip above the composer that expands on tap.

```
┌────────────────────────────────────────────────────────┬───────────────────────┐
│ BANCO AURORA · COBRANÇA 1–30 DPD   protocolo 8F2C · ● ligada                   │
├────────────────────────────────────────────────────────┼───────────────────────┤
│ passo ▸ verify_right_party                             ┊ FICHA DA CHAMADA      │
│ ┌─ AGENTE ─────────────────────────────────┐           ┊ ───────────────────   │
│ │ Bom dia. Aqui é a assistente…            │           ┊ CONTA                 │
│ │                       ╱TEXTO APROVADO╱   │           ┊ TITULAR ···· Beatriz  │
│ └──────────────────────────────────────────┘           ┊ SALDO ······ 847,32   │
│   142 ms · 318 tok · US$ 0,0004 · ver trace ↗          ┊ ───────────────────   │
│ ┌╌ CLIENTE ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┐           ┊ CAPTURA               │
│ ╎ Sou eu sim, Beatriz Almeida…             ╎           ┊ IDENTIDADE ···· sim   │
│ └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘           ┊ CONSENTIMENTO ····· — │
│ ▪extrair ▪julgar ▫avançar ▫gate ▫gravar ▫encerrar      ┊ ───────────────────   │
├────────────────────────────────────────────────────────┤ PROMESSAS             │
│ [ Fale como o cliente…                       ] ENVIAR  ┊ (nenhuma ainda)       │
└────────────────────────────────────────────────────────┴───────────────────────┘
```

### Signature

**O carimbo.** Every agent utterance carries a rubber stamp: outlined box, rotated
-3°, IBM Plex Mono ALL CAPS, `TEXTO APROVADO · §{step}` in `--stamp-ok`, or
`TRANSFERIDO` in `--stamp` when the compliance gate forced a transfer. It lands —
scale 1.35 → 0.96 → 1, opacity 0 → 1, ~180ms ease-out, with a one-frame ink spread
(a `currentColor` overlay at 28% that fades in 220ms). Fully suppressed under
`prefers-reduced-motion`: it appears instantly, untransformed.

This is the one bold move. Everything else stays quiet: no shadows, no border radius,
no gradients beyond the paper hatch, one animation per interaction.

---

## Pass 2 — critique, and what changed

Four things in pass 1 were defaults I would have produced for any similar page.

**1. Speech bubbles.** Pass 1 had utterances as rounded blocks, agent left, customer
right, with an avatar and a timestamp. That is every chat app ever shipped and it
argues the opposite of the thesis — bubbles say *two people are talking*, when the
point is that one side is a form being read aloud.
**Changed to:** utterances are **NCR form fields**. A hairline box with the caption
notched into its top rule (`AGENTE`, `CLIENTE`), fieldset-style. The two are
distinguished by rule *style*, not by side or color: the agent block is **solid** —
pre-printed on the form — and the customer block is **dashed** — filled in by hand.
That encodes something true about which words existed before the call started. No
avatars, no timestamps, no alignment split.

**2. Cards.** Pass 1 gave the ficha a white card with 8px radius and a soft shadow.
Generic SaaS, and it makes the ficha float above a page whose whole subject is paper.
**Changed to:** the ficha is a **tear-off stub**. It sits directly on canary carbon
with no elevation, and the seam between the two columns is a **perforation** — a
repeating dash down the divider, the way a boleto detaches from its receipt. The
right panel is the copy you tear off and file, which is exactly what it is.
Inside, rows are ruled with **leader dots** between label and value
(`TITULAR ········ Beatriz A. N.`), the fill-in-the-dots device off a real form,
which also gives the panel its rhythm without a single box.

**3. Status pills.** Pass 1 had the CAPTURA rows as colored pills, and the terminal
state as a badge. Pills are the house style of every admin dashboard and they smuggle
in a judgement: green *sim* reads as good news, which is not something a collections
record is allowed to imply.
**Changed to:** tri-state rows read as bare mono words — `—`, `sim`, `não`. `—` in
`--ink-soft` means *not asked*, which is a genuinely different fact from *no* and the
brief is right to insist it stay visible. Only `não` takes color (`--stamp`), because
a refusal is the thing a reviewer scans for.

**4. Fabricated header meta.** The wireframe shows `protocolo v1 · gpt-5.6-luna`, but
the service exposes no version endpoint — `/healthz` returns `{"status": "ok"}` and
the protocol and model versions only arrive on `CallRecord` when the call finishes.
Hardcoding `v1` would put a fact on a compliance screen that the screen cannot source,
which is the exact failure this product exists to avoid.
**Changed to:** `protocolo` shows the **call reference** — the Brazilian
customer-service reading of the word, the short form of `call_id`, available the
moment the call opens and the thing a reviewer would actually quote. `modelo` reads
`—` until the record lands and then prints `record.model` verbatim. The header never
displays a value it did not receive.

**One accessory removed.** Pass 1 also had a rotated `2ª VIA` watermark in the header
and ruled writing lines behind the composer textarea. Both were the same joke the
stamp is already telling, told twice more and worse. Cut.

---

## Pass 3 — what looking at it caught

Four things survived the plan and died in the browser. Recorded because three of
them are the kind of bug that only a screenshot finds.

**The opening utterance printed `0 ms · 0 tok · US$ 0,0000`.** The agent speaks
first, no extraction runs, and there is nothing to measure — but the footer was
built from a fixed template, so it filled the gap with zeros. An absent
measurement rendered as a claimed one, on the screen whose entire argument is
that it only shows what it can source. Every `TurnMetrics` field is now nullable
and the footer is assembled from the parts that exist; the opening turn shows
its trace link alone.

**The stamp read `§VERIFY_RIGHT_PARTY`.** `text-transform: uppercase` is right
for TEXTO APROVADO and wrong for a step id — upper-cased, it stops looking like
the identifier a reviewer can grep the traces for. The id now keeps its own case
inside the stamp.

**The status dot said "encerrada" before any call existed.** Two states where
there are three: a page with no call open is not an ended call. Now `sem chamada`
with a hollow dot, `ligada`, `encerrada`.

**The expanded mobile ficha painted over itself.** At 55vh the step line, the
toggle, the panel and the composer together exceeded the column; the transcript's
`1fr` row collapsed to literally zero and the transcript overflowed on top of the
strip's own header. Fixed at both ends — the panel caps at 40vh, and the
transcript row carries a `minmax(72px, 1fr)` floor so no combination of auto rows
can starve it again.

Verified in the browser, not assumed: SSE frames arrive through the Vite proxy
300ms apart rather than in one blob; the parser reassembles frames from 7-byte
chunks that split mid-JSON and mid-UTF-8; the rail renders `julgar` struck
through as `pulado` when the step is not confirm_terms; `prefers-reduced-motion`
leaves the stamp with `animation-name: none`, no scale, and the ink spread at
zero opacity; 360px has no horizontal scroll.

---

## Honesty note, carried into the code

There is no token stream. The LLM never writes a word the customer hears
(`src/trail/agent/llm.py`), so agent utterances are verbatim compliance-approved
protocol text retrieved whole. What streams is the per-turn **pipeline**, as SSE:
six stages, their latency, their tokens, their cost.

The typewriter reveal of the approved text is **client-side and cosmetic**. It is
labelled as such on the empty state, in prose the reviewer reads before the first
call:

> O modelo nunca escreve o que o cliente ouve. O texto falado é aprovado
> previamente; a digitação na tela é apenas um efeito visual.

The stage rail is the opposite: every cell is a real SSE frame with a real
measurement behind it. A skipped stage renders **struck through rather than hidden**,
because which path the turn took is the information, and a hidden cell would let the
`judge` step's absence pass for its success.

One derivation is worth naming. `TurnResponse` carries no extraction, so during a
live call the CAPTURA rows are derived from the `advance` and `judge` stage frames —
the only truthful in-flight signal there is — and then overwritten wholesale from
`record` once the call finishes, which is authoritative. The ficha never shows a
value it inferred once the real one exists.
