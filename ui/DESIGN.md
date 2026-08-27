# The browser surface

The demo UI for TRAIL. Audience: engineers evaluating whether this scaffold is
worth building on, and technical leadership deciding the same thing one level up.

Single job: **make the machinery visible without making it the subject.**

The conversation is the subject. Under every answer sits the rail — which gates
ran, which were switched off, how long the model took, what it cost, and a link
to the span tree. That is the claim: an agent's behaviour should be arguable,
not merely plausible.

---

## Pass 4 — the conversation redesign

> Passes 1–3 designed a different interface for a different product: an agent
> whose spoken words were pre-approved text, rendered as a form with a tear-off
> compliance panel. That product no longer exists in this repository, and the
> document that argued for it asserted the opposite of what is now true. Rather
> than patch it, this replaces it. The reasoning that survived is folded in
> below and marked.

### What changed, and why the old thesis inverted

The old premise was **"the model listens, it never speaks"** — every customer-
facing sentence came verbatim from an approved protocol, and the interface was
built to make that legible: serif for spoken words, a stamp reading
`TEXTO APROVADO`, a form-field treatment instead of chat bubbles.

The runtime is now generic. `turns.py` returns the last assistant message, so
**the model speaks and only speaks**. Every visual device built to argue the old
thesis is now an argument for something false, and the empty-state paragraph
that said so out loud went from honesty to a lie on a screen whose whole point
is not claiming what it cannot source.

What survives is the *reflex* rather than the conclusion: say what is measured,
show what was skipped, and never render a number nothing produced.

---

## Layout

```
┌────────────┬─────────────────────────────────────────────────┐
│ + Nova     │  TRAIL  trail_guide  guardrails both  ec652a56 ☾│
│            ├─────────────────────────────────────────────────┤
│ o que é a  │   ◆ AGENTE                                      │
│ esteira…   │   A esteira é a pipeline rail do TRAIL…         │
│            │   ┌──────────────────────────┐  ← fenced block, │
│ quais      │   │ ASCII diagram            │    scrolls itself │
│ serviços…  │   └──────────────────────────┘                  │
│            │   ▪entrada 0ms ▪modelo 1.2s ▪search_docs 3ms    │
│            │   ▪saída 0ms · 1.0k in · 35 out · US$ 0.0002    │
│            │   ver trace ↗                                    │
│            │                    ╭──────────────────────────╮ │
│            │                    │ e os guardrails?         │ │
│            │                    ╰──────────────────────────╯ │
│            │   ┌─────────────────────────────────────────┐   │
│  memory:   │   │ Pergunte sobre o TRAIL…            [↑] │   │
│  não persiste  └─────────────────────────────────────────┘   │
└────────────┴─────────────────────────────────────────────────┘
```

**Three regions, no fourth.** An earlier plan gave the rail a collapsible right
drawer. It was cut: the rail belongs under the answer it measures, because data
next to its own effect needs no correlating. A panel would have made the reader
match a row to a message by eye.

**Below 1024px** the sidebar overlays instead of splitting the width, and it
starts closed every time — the persisted preference governs the wide layout
only. A phone that restored "open" would greet its reader with a list covering
the thing they came to read.

---

## Messages, asymmetric

The user is bubbled; the agent is not. That is what ChatGPT and Claude do, and
copying it is not imitation: an unbubbled agent reads as *the system's output
surface* rather than as *another person's turn*, which is the honest description.
A bubble on both sides asserts two speakers.

> *Carried from Pass 2.* The original rejected bubbles outright, for a reason
> that was correct then — bubbles say two people are talking, and one side was a
> form being read aloud. The asymmetric form keeps the distinction and drops the
> premise that no longer holds.

**A refused turn is the third treatment and gets the most.** Red mark, red rule
down the left of the text, the label `RECUSADO` instead of `AGENTE`, and every
violation in full — check, rule, detail, and the offending evidence. This is the
moment the interface justifies itself; collapsing it to the word "blocked" would
waste it.

---

## The rail

Three rules, each of which has been got wrong at least once:

**A skipped stage renders struck through, never hidden.** A guardrail switched
off and a guardrail that ran and passed must not look alike — and a hidden cell
reads as neither, it reads as nothing.

**Durations are nanoseconds on the wire and scaled at the last moment.** One
rail spans four orders of magnitude: a regex gate runs in microseconds, a model
call in seconds. This was milliseconds, truncated to an integer, and it rendered
every guardrail in the system as `0 ms` — true, useless, and easily read as "did
not run" for the one kind of step whose cheapness is the entire argument.

```
▪entrada 134 µs  ▪modelo→stack_status 1.51 s  ▪stack_status 1.9 ms
▪modelo→resposta 1.88 s  ▪saída 110 µs  ▪fim
1.5k in · 198 out · US$ 0.0005 · total 3.43 s
```

**A model call is named for the job it did.** An agent loop calls the model more
than once per turn: the first decides which tool to reach for, the last writes
the answer. Both used to read `modelo`, which put two cells with different
durations on the rail and no way to tell which was which. The name is derived
from what came back — tool calls or content — so it stays right for an agent
that takes four rounds, or none.

**One hue per kind: gate, model, tool.** Purple, cyan, amber, on the mark rather
than the whole cell so the label keeps full contrast. It is what lets the eye
separate a guardrail's cost from the model flow at a glance, which is the reason
the rail is one line. Colour is never the only carrier — the labels differ too —
and a blocked cell's status overrides its kind, because which of those a reader
needs first is not in question.

The `total` is the turn's own wall time and is deliberately **not** the sum of
the cells — those add to 3.16 s. The 40 ms of daylight is what the graph spends
between steps, and it is only visible because both numbers are on the row.

**A blocked stage gets its own glyph, not only its own colour.** `✗` against
`▪` and `▫`. Colour is lost in a screenshot, in a printout, and to a reader who
cannot distinguish red from grey.

**The order is arrival order. Nothing sorts it.** The service emits a
switched-off gate's skip from the hook where that gate would have run,
specifically so no client has to reconstruct the sequence — and any sort able to
reposition a skip is also able to scramble the real interleaving of model and
tool calls, which is the ordering a reader is reading for.

The rail is a **list, not a grid**. An earlier version was six fixed cells,
which was possible when the pipeline had six named stages. A tool contributes
its own name and fires once per call; the model fires once per tool round. The
count is unbounded and the vocabulary is the agent's.

**No label is looked up here.** `label` arrives on the wire. A dictionary in the
frontend is how the previous version of this interface ended up knowing one
agent's words and no other's.

---

## Color

`light-dark()` per token, so a colour is one line and the two themes cannot
drift apart. Both are shipped; neither is a retrofit.

| token | light | dark | where it lands |
| --- | --- | --- | --- |
| `--bg` | `#FAF5FF` | `#14101F` | page ground |
| `--surface` | `#FFFFFF` | `#1C1730` | sidebar, composer, fenced blocks |
| `--surface-2` | `#EDE4FF` | `#251E3D` | the user's bubble, inline code, hover |
| `--text` | `#1E1B4B` | `#EDE9FE` | everything read as prose |
| `--muted` | `#475569` | `#A5A0C0` | the rail, meta, placeholders |
| `--border` | `#DDD6FE` | `#2E2748` | separators **only** |
| `--border-strong` | `#8B5CF6` | `#7E6FB8` | anything a pointer aims at |
| `--primary` | `#7C3AED` | `#A78BFA` | the agent's mark, send, active thread |
| `--accent` | `#0E7490` | `#22D3EE` | rail marks, the trace link |
| `--danger` | `#B91C1C` | `#F87171` | a refusal, a violation, the dial when off |

**Three of these are not the obvious value, and each is a measurement.**

`--accent` in light is cyan-700, not the cyan-600 that reads brighter: `#0891B2`
measures **3.68:1** on the page ground and fails as text.

`--danger` in light started at `#DC2626`, which measures 4.52:1 — passing, with
no margin, on 11.5px violation text.

`--border-strong` in dark started at `#6B5CA5`, which measures **3.05:1**.
Passing, and by so little that any later change to the surface would break it
silently.

The two-border split is itself a contrast decision: `--border` is 1.39:1 in
light, invisible as a control boundary and correct as a separator. Every value
above was measured, both themes, 28 pairs.

## Type

Two families, two jobs. The three-family split of the previous design carried
an argument — *serif means someone said this aloud* — that no longer has a
referent.

- **Inter**, 300–700. Every word.
- **IBM Plex Mono**, 400–600, with `tabular-nums`. Every machine value:
  milliseconds, tokens, cost, thread ids, violation fields. The tabular figures
  are not aesthetic — a column of millisecond counts that reflows as its digits
  change is a column nobody can scan.

---

## Honesty notes, carried into the code

**The typewriter is cosmetic and says so.** The service sends the answer whole;
there is no token stream to follow. `runtime/turns.py` already requests the
`messages` channel from the graph specifically so that adding one later is a
client change rather than a contract change. Until then the reveal is an
animation, the animated copy is `aria-hidden`, and the full text sits in an
`sr-only` span so a screen reader hears the answer once, complete.

**A reopened conversation shows no rail.** The frames were live measurements and
are not stored. An empty rail is honest; reconstructing one would be invention.

**The greeting has no metrics.** No model ran, so there is no latency and no
cost. Rendering `0 ms · US$ 0.0000` would be a measurement of something never
measured — the same reason an unpriced model reports `—` rather than zero.

**Nothing in the header is unsourced.** Every field is a value the service
returned. An earlier version displayed a protocol version no endpoint served,
which put a number on screen that nothing could contradict.

---

## Verified, not assumed

Both themes at 375 / 768 / 1024 / 1440. Contrast measured on the rendered
tokens rather than the intended ones. The full script driven in a real browser:
an ASCII diagram scrolling inside its own block without moving the page, a
prompt injection refused with `modelo` and `saída` struck through, a
conversation reopened from the sidebar and continued with its memory intact.

`prefers-reduced-motion` switches off every transition through the blanket rule,
and the typewriter through `useReducedMotion` — CSS cannot reach a JavaScript
timer, which is the one thing the media query alone would miss.
