---
version: "1.0.0"
book: "1–30 days past due, personal loan and credit card"
last_reviewed: 2026-08-15
reviewed_by: "Head of Collections Compliance, Banco Aurora"
locale: pt-BR
---

<!-- protocol_version: 1.0.0 -->

# Early-Stage Collections Protocol — Banco Aurora

Approved collections content for the 1–30 days-past-due call. The customer received a notification
(SMS, app or WhatsApp) and tapped to call, so v0 is **inbound**: there is no outbound dialling, no
consent management and no calling-window logic in this document, because BLUEPRINT §3 stages that
risk rather than dodging it.

**This file is the single source of truth for everything the agent says.** The agent reads the
`spoken` block for each step **verbatim**. It does not paraphrase, summarise, shorten, reorder,
translate, soften, or generate a payment term of any kind. A fabricated settlement, waiver or
discount is a zero-tolerance failure (BLUEPRINT §5), and "the model almost always gets it right" is
not an acceptable standard when the sentence is a promise about someone's money. If a step has no
approved text here, the process refuses to start (`load_protocol` raises) rather than let the model
improvise one.

**Three boundaries govern every word below.**

1. **Capture, don't interpret.** The agent records what the customer says. It never classifies it.
   No hardship score, no vulnerability flag, no sentiment, no propensity to pay.
2. **Route everything uniformly.** Every completed record reaches the same specialist queue in the
   same order, regardless of content. There is no risk-based ordering, filtering or prioritisation
   anywhere in this document, because "concerning" and "likely to pay" are both inferred
   customer-specific classifications.
3. **The agent has no authority.** It can state published terms. It cannot grant, reduce, waive,
   forgive or negotiate one, for anybody, ever — including for the customer who most deserves it.

Each section below has exactly one `spoken` block — the approved utterance — and reviewer notes
explaining why the wording is what it is. **Reviewer notes are never spoken.**

**One block is different.** `state_balance` contains `{slot}` placeholders and is the only
customer-specific text in the file. Its reviewer note explains why the healthcare architecture this
system is ported from had to be extended to allow it, and what was built so that the extension does
not become the hole through which fabricated money reaches a customer.

---

## verify_right_party

```spoken
Olá. Aqui é o assistente automático do Banco Aurora.

Estou falando sobre uma pendência na sua conta conosco. Não vou dizer mais nada sobre esse assunto até ter certeza de que estou falando com a pessoa certa.

Por favor, me diga o seu nome completo e o seu CPF. Se você preferir não informar o seu CPF por telefone, me diga a sua data de nascimento no lugar dele.
```

**Reviewer note — what may be said to an unverified party.** Nothing about the debt is disclosed
before identity is confirmed: not the amount, not the product, not the due date, not the word
*atraso*, not the word *dívida*. "Uma pendência na sua conta conosco" is the ceiling, and it is
deliberately vague enough to be uninformative to a stranger and specific enough that the right person
knows why they called. Disclosing a debt to a third party is a direct FDCPA violation and the first
entry on BLUEPRINT §5's zero-tolerance list, which is why this step is a **hard gate**: failure
terminates the call as `not_right_party` and leaves no message, rather than degrading into a partial
conversation that discloses a little.

**Reviewer note — state, don't confirm.** The block asks the caller to *state* two identifiers
rather than confirm ones the agent reads out. "Falo com Marina Rocha?" is the rejected alternative,
and it fails twice over: it discloses the name to whoever picked up, and it reduces verification to a
yes/no that a wrong party answers wrongly — sometimes deliberately, more often because a spouse
genuinely believes they are entitled to handle it. Reading a CPF out loud for confirmation is worse
still: a CPF is a checksummed national identifier, and the agent would be handing an unverified
listener a document number they did not have when the call began. The direction of information flow
across this gate is one-way, and it points at the bank.

**Reviewer note — CPF or date of birth, and why the fallback exists.** Refusing to say a CPF over the
phone is not evasive behaviour; in Brazil it is *good* behaviour, and a script that treats it as a
failure would filter out the most security-literate customers in the book. So date of birth is an
approved substitute for the CPF, never for the name, and never for both. The comparison itself is
deterministic and lives outside the model: the LLM captures `stated_name`, `stated_tax_id` and
`stated_date_of_birth` as spoken, and `identity_matches` decides, with CPF check digits verified
arithmetically. The model transcribes; it never adjudicates identity.

**Reviewer note — the greeting is channel-neutral on purpose.** It does not say "obrigado por
ligar", and it does not say "estou ligando para você". v0 is inbound, but `not_reached` and
`not_right_party` stay first-class terminal states because outbound lands at BLUEPRINT §9's post 8,
and approved text that asserts a channel becomes a false statement the day the channel changes. A
sentence that is true in both directions costs nothing here and saves a protocol version bump later.

---

## disclose_and_consent

```spoken
Obrigado. Agora eu posso falar sobre a sua conta.

Antes de qualquer outra coisa, três avisos.

Primeiro. Esta é uma tentativa de cobrança de uma dívida e qualquer informação obtida será utilizada para esse fim.

Segundo. Eu sou um assistente de inteligência artificial. Eu não sou uma pessoa e não sou um especialista do Banco Aurora. Esta ligação está sendo gravada.

Terceiro. Você pode pedir para falar com uma pessoa a qualquer momento desta ligação, e eu transfiro. Tudo o que eu anotar aqui é revisado por um especialista do Banco Aurora depois que a gente desligar.

Você autoriza que eu continue com esta ligação gravada?
```

**Reviewer note — four disclosures, then the question.** The mini-Miranda, the AI disclosure, the
recording disclosure and the availability of a human all land *before* consent is requested and
before any payment term is mentioned. The rejected alternative — disclose late, once rapport exists —
is how a disclosure becomes a formality read over someone who has already committed to a number, and
it is the exact shape UDAAP calls deceptive. The mini-Miranda is FDCPA §807(11) in origin; Brazil's
CDC art. 42 governs the *manner* of collection rather than mandating a script, so Banco Aurora
adopts the disclosure as policy and states it in the customer's language.

**Reviewer note — "eu não sou uma pessoa" is not politeness.** It is the load-bearing sentence of the
whole call. Everything downstream — the refusal to concede, the transfer on request, the specialist
review — is coherent only if the customer knows they are talking to software. Saying it in the same
breath as the recording disclosure keeps the two facts from being separated by a paraphrase that
never happens, because there is no paraphrase: this text is read verbatim.

**Reviewer note — consent is explicit, and it is a bit, not a mood.** The customer says yes or the
call does not proceed. Extraction records `consent_given` as a boolean and nothing else; a refusal
or a withdrawal sets `needs_human` and transfers, with no attempt to persuade and no second ask. The
agent has no approved text for a second ask, which is the strongest form the rule can take: the
capability simply does not exist rather than being a rule the model is asked to respect.

**Reviewer note — where the human-on-request promise is honoured.** "Você pode pedir para falar com
uma pessoa a qualquer momento" is a promise the state machine keeps unconditionally, from any step,
including this one. It is also the sentence that makes the hardship boundary workable: a customer who
cannot be helped by this script has a stated, always-available exit that costs them nothing to use.
See the `offer_payment_path` note.

---

## state_balance

```spoken
Obrigado. Agora os números, exatamente como eles estão no sistema do Banco Aurora.

O produto é o seu {product}. O valor em aberto é de {balance}. O vencimento foi em {due_date}, e o atraso está em {days_past_due}.

Se esse valor não bate com o que você tem, ou se você já pagou, me diga agora. Eu anoto o que você falar com as suas palavras e um especialista do Banco Aurora confere.
```

**Reviewer note — this is the one place the architecture had to be extended, and why.** The
healthcare system this repository is ported from is strictly patient-independent: every approved
block is identical for every patient, `text_for` returns it verbatim, and the compliance allowlist is
exact string equality against the file. That property is what makes verbatim delivery provably safe,
and it survived the whole of that build. It cannot survive here. A collections call whose entire
purpose is to state an amount cannot be customer-independent, and BLUEPRINT §3 step 3 makes stating
the balance and the due date a required step rather than an optional courtesy.

The rejected alternative was to keep the file customer-independent by *refusing to speak the number*
— point at the app, let the customer read their own balance, never say an amount out loud. It is
tempting, it is safe in the trivial sense, and it is worse. It moves the number to a channel where
nothing verifies that the spoken conversation and the displayed figure agree, it makes `confirm_terms`
meaningless, and it quietly concedes that the system cannot be trusted with the one fact the call
exists to communicate. Refusing to speak is not a safety property; it is an absence of one.

**Reviewer note — deterministic slot rendering, and what it actually claims.** The four placeholders
`{product}`, `{balance}`, `{due_date}` and `{days_past_due}` are the only braces in any `spoken` block in this file. They
are filled by `Protocol.render`, which walks the declared slot set explicitly and requires it to match
exactly — a missing slot and an unknown slot both raise rather than render. The values come from
deterministic formatters in `trail.money` reading `AccountProfile` fields that came from the system
of record. **No slot value ever originates from an LLM**, and the model is never shown a rendered
utterance to approve.

The claim that buys is stronger than "we told the model to be careful about numbers", and it is worth
stating precisely. The compliance allowlist verifies the **rendered** form: the candidate utterance is
compared against this block rendered with *this customer's* slot values. An utterance carrying a
balance that disagrees with the record therefore matches nothing in the approved set and is a
compliance violation **before the words leave the service** — not a defect found afterwards in a
transcript review, and not a judgement about whether the difference was material. That is BLUEPRINT
§5's "wrong balance / fee / date spoken aloud" made structural. It is also why BLUEPRINT §8's
cascaded architecture is the right one: a text checkpoint exists between the decision and the speech,
and this check lives in it.

The corollary is deliberate and should not be treated as a bug. `Protocol.text_for(Step.STATE_BALANCE)`
returns the raw template, braces and all, and speaking it unrendered **fails** the allowlist. A step
that forgot to render is caught by the same mechanism as a step that hallucinated, which is the
correct outcome for both.

**Reviewer note — the day count carries its own unit.** `{days_past_due}` renders the elapsed time
*including* the noun — "1 dia", "12 dias" — rather than a bare integer with "dias" written into the
template. Portuguese number agreement is a deterministic function of an integer and belongs in the
formatter, next to the currency and date formatters, not in a template that would otherwise have to
say "dia(s)" and have a speech synthesiser read the parentheses aloud. Same rule for `{product}`: the
renderer emits the customer-facing product name, so this file never has to branch.

**Reviewer note — the invitation to dispute, and what happens to it.** The last sentence exists
because an omission and a denial are different records. A customer who says "esse valor está errado,
eu já paguei" is captured as a `Dispute`, verbatim, with the utterance attached, and is never
characterised: "já paguei", "esse valor não é meu" and "eu nunca peguei esse empréstimo" are three
different facts for the specialist and the agent is not permitted to assess which is true or which is
serious. A `Dispute` row triggers **no callback of its own** — routing on its content would be the
agent assessing the dispute's merit — but an explicit dispute is also something this script cannot
answer, so it sets `needs_human` and transfers. FDCPA §809(b) cease-collection-on-dispute is the
specialist's action, taken by a person, on a record that reached them like every other record. *Flag
for post 5: this is the clearest example in the system of a duty that is discharged by routing rather
than by classifying.*

---

## confirm_terms

```spoken
Quero ter certeza de que esses dois números chegaram certos até você, porque é neles que o resto da conversa se apoia.

Me diga com as suas palavras: qual é o valor em aberto e qual foi a data de vencimento?
```

**Reviewer note — restatement, not "ficou claro?".** "Ficou claro?" and "alguma dúvida?" are close to
useless as comprehension checks; people answer "sim" and "não" to them almost universally, whether or
not anything was understood, and in a collections call there is an extra reason to say yes: ending
the conversation faster. Teach-back asks for reproduction instead of a self-report. The agent judges
only whether the customer restated the amount **and** the date correctly against the rendered
approved text. On a mismatch it re-reads `state_balance` verbatim and tries once more; after the
second attempt the call still runs to the end and the record is flagged for callback. An unconfirmed
restatement is information for the specialist, not a failure to hide.

**Reviewer note — this is where entity accuracy is actually decided.** The entity that has to survive
this exchange is a number, spoken by a human, over a phone. BLUEPRINT §6 asks for entity error rate
on amounts, dates, account numbers and negation precisely because average WER hides this: a
transcript can be 95% correct word-for-word and still turn "oitocentos e quarenta e sete" into
"oitocentos e quarenta". BLUEPRINT §6 also makes it a fairness question — published Brazilian
Portuguese ASR shows a phoneme error rate an order of magnitude worse for underrepresented Northeast
speech than for São Paulo — and if amount recognition is materially worse for a poorer cohort *in a
debt-collection context*, this step is where the measurement lands.

**Reviewer note — what the judge is not allowed to do.** It returns one boolean. It never corrects
the customer in composed words, never explains the figure differently, never meets them halfway on a
near-miss, and never adapts the approved text to what they said. The only correction available is
re-reading the approved block, because an agent that "helps" the customer converge on a number is
asserting a fact it cannot verify — and the only fact it holds is the one already in the block.

---

## offer_payment_path

```spoken
Existem quatro formas de resolver isso, e elas são as mesmas para todos os clientes do Banco Aurora nesta situação.

A primeira é pagar neste momento, pelo aplicativo do Banco Aurora.

A segunda é receber um link de pagamento no mesmo canal em que você recebeu o nosso aviso.

A terceira é agendar o pagamento para uma data que você escolher.

A quarta é o plano de parcelamento padrão do Banco Aurora. Ele é publicado, vale para todos os clientes nesta mesma situação, e as condições estão no aplicativo.

Não consigo oferecer descontos, abatimentos ou condições diferentes das que acabei de listar. Um especialista do Banco Aurora pode falar sobre outras opções com você.

Qual dessas quatro formas você prefere?
```

**Reviewer note — this is the concession boundary, and it is why this block reads as incomplete.**
The spoken text lists and records. It does not offer a discount, a settlement, a waiver, a fee
reversal or a rate to anybody, ever, and it names no percentage and no amount. That restraint is not
caution about model accuracy; it would apply identically to a perfectly accurate system. Granting a
concession *based on what the customer said* is the agent exercising authority the bank never gave
it — BLUEPRINT §5 lists "fabricated settlement, waiver or discount" as zero-tolerance for exactly
this reason, and a promise made on a recorded call is not undone by explaining afterwards that a
model made it. This is the direct analogue of the healthcare system's "I am not able to tell you to
start, stop, or change anything", one industry over: same sentence shape, same reason, different
regulator.

**Reviewer note — the capability statement is delivered to everyone.** "Não consigo oferecer
descontos, abatimentos ou condições diferentes das que acabei de listar" is read to every customer,
in this position, whether or not they asked for anything. That is what makes it a **capability
statement** rather than a **response**, and the distinction is the whole point. Delivered
universally it is a description of what this system is. Delivered *because* a customer asked for a
discount, it becomes a customer-specific decision about that customer's request — which is the thing
the agent is not permitted to make, and which would also leak, by its presence or absence, whether
the agent judged the request worth answering. The golden set's `asks_for_discount` case asks three
times, politely, escalating; the correct behaviour is this sentence, verbatim and unchanged each
time, and the case is named in advance as the one expected to fail, because *helpful* is the
direction a language model fails in.

**Reviewer note — the instalment plan is published, not negotiated.** It is stated as a standing
universal option with its conditions in the app, and this block names no instalment count, no rate
and no figure. The rejected alternative was to read the plan's terms aloud here, which sounds more
helpful and is not: terms read from an approved-text file drift out of date silently, and a spoken
figure that disagrees with the app is the same UDAAP exposure as a wrong balance, minus the slot
machinery that makes the balance verifiable. Stating a published plan is not a concession. Choosing
who gets it would be.

**Reviewer note — hardship, and why nothing here detects it.** BLUEPRINT §7 refuses to automate
hardship negotiation, and BLUEPRINT §5 makes a missed hardship or vulnerability cue a zero-tolerance
failure under FCA Consumer Duty / CONC. Those pull in opposite directions and the resolution is the
architecture: **the duty is to route to a human, not to classify.** A customer who says "perdi o
emprego" reaches the graph as `needs_human` — a routing bit with no reason attached — and gets the
same transfer, in the same words, as a customer who refused consent or simply asked for a person. The
record never says it was hardship, never carries a severity, and the queue is never reordered by it.
Recording *why* would be the agent classifying a vulnerable customer in a debt-collection context,
which is precisely the thing the section refuses. Brazil's Lei 14.181/2021 puts over-indebtedness
treatment where it belongs — with people, on terms the institution actually approved — and this
script's contribution is to get there quickly and without editorialising. Whether a cue was missed is
measured by golden-set expectation, not by a field on the record.

---

## capture_commitment

```spoken
Agora eu preciso de duas informações para registrar o seu compromisso de pagamento: um valor e uma data.

Qual valor você pode pagar, e em que dia?

Pode falar do jeito que for mais fácil para você. Eu anoto exatamente as suas palavras, sem arredondar o valor e sem transformar o dia em uma data. Quem confere isso depois é um especialista do Banco Aurora.
```

**Reviewer note — verbatim capture, and the error class it avoids.** The amount and the date are
recorded exactly as said, with the utterance stored alongside them. "Mil e duzentos" is stored as
"mil e duzentos" and not as "R$ 1.200,00"; "sexta-feira" is stored as "sexta-feira" and is never
resolved into a calendar date. The temptation to normalise is strong and the argument against it is
the same one that governs the healthcare original: a discharge summary that turned 8 units of insulin
into 80 killed a patient, and inference is where that class of error is manufactured. The collections
version of 8-becomes-80 is "mil e duzentos" arriving as R$ 1.200,00 when the customer meant something
else entirely, or "sexta" resolving to the wrong Friday because the call crossed a weekend. Both are
silent: nothing downstream can tell a confidently normalised value from a correct one.

Normalisation still happens — just deterministically, offline, and outside the agent. `parse_brl`
handles both the digit forms and the spelled-out forms a speech transcript produces, returns `None`
rather than guessing, and is used **only by the eval scorer**. The agent holds no parser. If the
scorer cannot resolve what the customer said, that shows up as an unscored entity in the numbers
rather than as a confident wrong value on a record a specialist will trust.

**Reviewer note — completeness is checked, content never is.** A commitment row missing either the
amount or the date flags the record for callback. The rule reads *nullity*, not the value: it is the
same rule as "a medication row missing dose, unit or frequency" and it carries the same uncomfortable
and correct property. A customer promising R$ 4.000,00 with a date comes out fully automated; a
customer promising R$ 40,00 who will not name a day does not. Deciding which *amounts* deserve a
human is customer-specific logic, and the agent holds no threshold — nor should it, since a threshold
is exactly the "how much is this account worth" judgement that a queue must never be sorted by.

---

## confirm_contact

```spoken
Falta uma coisa. O link de pagamento vai para o mesmo canal em que você recebeu o aviso do Banco Aurora, e eu não vou ler nenhum número nem nenhum endereço em voz alta.

Me diga você: em qual canal você recebeu o nosso aviso? No aplicativo, por mensagem de texto, ou pelo WhatsApp?
```

**Reviewer note — the customer reads it back, the agent never reads it out.** This is the mirror of
the healthcare original's arrival-time argument. The agent has no approved customer-specific text for
a phone number, an e-mail address or a WhatsApp handle, and it will not speak one: a contact detail
is not in this file, cannot be rendered from a declared slot, and a number spoken out of a model's
context window is a fabricated value with a plausible shape. Asking the customer which channel they
already received the notification on establishes the destination without the agent ever asserting it,
and it doubles as a cheap check that the notification actually arrived.

**Reviewer note — this rule fails closed.** `contact_channel_confirmed` must be explicitly true; a
null is treated as unconfirmed and flags the record for callback. "Provavelmente chega no meu
celular" is not a confirmation, and the failure it guards against is a payment link sent to a stale
channel — which reads to the customer as the bank ignoring a promise they just made, and shows up in
the numbers as a repeat contact nobody can explain.

---

## post_outcome

```spoken
Era isso que eu precisava. Vou dizer o que acontece agora.

Tudo o que você me falou nesta ligação vai para um especialista do Banco Aurora. Isso acontece em todas as ligações, sem exceção, e nada do que conversamos aqui está fechado até uma pessoa revisar.

O link de pagamento chega no mesmo canal em que você recebeu o nosso aviso, e o aplicativo do Banco Aurora mostra a sua situação atualizada.

Obrigado pelo seu tempo e tenha um bom dia.
```

**Reviewer note — uniform routing, stated to the customer in plain language.** "Em todas as ligações,
sem exceção" is said out loud because it is the design commitment the whole architecture rests on and
because it is true. There is no prioritisation, no risk ordering and no filtering: the specialist
queue is sorted by call start time, `CallRecord` carries no `priority`, `urgency`, `severity`,
`risk_score`, `hardship`, `vulnerability`, `propensity` or `segment` field and must never gain one,
and `needs_specialist_review` is pinned true in both the Pydantic model and a database `CHECK` so no
code path can mark AI output final. Sorting that queue by how likely someone is to pay is the
collections version of a red-flag detector: it is customer-specific logic dressed as operational
efficiency, and it decides who gets a human by predicting who is worth one.

**Reviewer note — "nada está fechado até uma pessoa revisar" is the honest framing.** It sets the
expectation that a human decides, which is both true and the intended use of this system:
administrative intake and communication about an account, not a credit decision, not a hardship
assessment, not a negotiation. A customer told this at the end of the call has a correct model of
what just happened, which matters more here than in most domains — the alternative is someone hanging
up believing they have an agreement with a bank.

**Reviewer note — no phone number, no URL, no amount.** The closing points at the app and at the
channel the notification arrived on rather than speaking a number or an address. Contact details are
site configuration, not approved content, and a number confidently read out of an approved-text file
that turns out to be wrong is a worried customer dialling a stranger about their debt. There is also
no closing restatement of the balance: the amount is spoken once, in `state_balance`, through
verified slots, and repeating it here would be a second place for it to drift.

**Reviewer note — the agent does not listen after this.** `post_outcome` is excluded from
`LISTENING_STEPS`. The closing statement is delivered and the call ends. The rejected alternative —
one last "mais alguma coisa?" — reopens a conversation after the point at which the agent still has
approved text for anything, and the only honest answers it could give are a transfer or silence.

---

## How this file is consumed

For engineers. This section is prose and is ignored by the parser.

- `trail.protocol.load_protocol(path)` parses this file at agent startup and returns a frozen
  `Protocol`. It fails fast — a missing version, a step with two `spoken` blocks, or any `Step`
  without approved text raises `ValueError` and the process does not start. There is no runtime
  fallback and no default text.
- The `##` headings are the machine contract: their text equals the `Step` enum value exactly, in
  declaration order, which is conversation order — `verify_right_party`, `disclose_and_consent`,
  `state_balance`, `confirm_terms`, `offer_payment_path`, `capture_commitment`, `confirm_contact`,
  `post_outcome`. Do not rename them, do not add prefixes, do not reorder them.
- `Protocol.text_for(step)` returns the contents of that step's `spoken` block **verbatim**, with no
  substitution and no formatting. Reviewer notes, headings and this section are never returned.
- **Slots.** `state_balance` is the only block with placeholders and the only customer-specific text
  in the file. `Protocol.slots_for(Step.STATE_BALANCE)` returns exactly
  `{"product", "balance", "due_date", "days_past_due"}`, parsed from the block itself rather than
  declared anywhere else. `Protocol.render(step, slots)` substitutes deterministically and requires
  the supplied slot set to match the declared one **exactly** — a missing slot and an unknown slot
  both raise `ValueError`. It does not use `str.format`, which would silently accept extra keys and
  choke on any stray brace a future edit introduces; it walks the declared slots explicitly.
- Slot values come from the four deterministic formatters in `trail.money`, reading
  `AccountProfile` fields sourced from the system of record: `format_brl` for `{balance}`,
  `format_date_ptbr` for `{due_date}`, `format_product_ptbr` for `{product}`, and
  `format_days_past_due` for `{days_past_due}` — the last two emitting the customer-facing product
  name and the day count *with its noun*, so this file never has to branch on either.
  **No slot value ever originates from an LLM.** The call site is
  `trail.agent.machine.slots_for_call(profile)`, and it is the single one: the agent and the
  compliance allowlist must build the identical dictionary or the gate would reject the agent's own
  approved text.
- `text_for` on a slotted block returns the raw template. Speaking it unrendered fails the compliance
  allowlist, and that is the intended behaviour — `assert_agent_text_is_approved` builds its approved
  set by rendering slotted blocks with the current call's slot values, so an utterance whose amount
  disagrees with the record matches nothing and is rejected before it leaves the service.
- `version` in the YAML front matter and `protocol_version` in the HTML comment are the same value
  and must be changed together. It is stamped on every `CallRecord` and on every OTel span, so any
  call can be replayed against the exact approved text it was given.
- This file is reviewed as regulated content, in code review, in the same pull request as the code
  that reads it. That is why the protocol is a git-versioned file mounted into the agent and not a
  service: a microservice serving static approved text is invented complexity, and it removes the
  approved wording from the only review process that catches a wrong term.
- Editing any `spoken` block is a compliance change, not a copy edit. Bump `version`, update
  `last_reviewed`, and expect the golden-set expectations and the allowlist fixtures to move with it.
