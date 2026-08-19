"""Loader for the approved collections script.

The protocol is a **git-versioned file mounted into the agent, not a service**
(BLUEPRINT §8). A microservice serving static approved text is invented
complexity, and worse, it removes the regulated content from code review — which
is the only review process that catches a missing mini-Miranda or a sentence
that promises authority the agent does not have.

What this module guarantees:

* **Verbatim delivery, up to declared slots.** :meth:`Protocol.text_for`
  returns the contents of a step's ```spoken`` block exactly as written — no
  reformatting, no paraphrase, and never a round trip through a model. The agent
  *reads* the approved script; it never generates it. A disclosure the model
  rewrote in its own words is a missing disclosure, and "the model almost always
  gets it right" is not an acceptable standard for a sentence that has to be
  said.
* **Deterministic slot rendering.** :meth:`Protocol.render` substitutes the
  customer-specific values a block declares, by exact string replacement, from
  values the caller supplies. No slot value ever originates from a model. See
  below — this is the one place the port had to extend the architecture it came
  from.
* **Fail fast at boot.** :func:`load_protocol` raises :class:`ValueError` if the
  version header is missing, if the two version declarations disagree, if any
  :class:`~trail.models.Step` has no approved text, or if a block declares a
  malformed slot. A gap must stop the process from starting rather than surface
  mid-call, where the only remaining options are silence or improvisation.

**Why slots exist here and did not exist in the healthcare original.** The
pre-operative protocol this loader is ported from was strictly
patient-independent: every approved block was the same sentence for every
caller, so ``text_for`` could be verbatim in the strongest possible sense and
the compliance allowlist could be exact string equality against the file. A
collections call cannot be customer-independent. Step three of the conversation
*is* the customer's balance and due date (BLUEPRINT §3), and a wrong balance
spoken aloud is a zero-tolerance, release-blocking failure (BLUEPRINT §5).

Two obvious ways out, both worse. **Refusing to speak the number** — pointing
the customer at the app and never saying the amount — would keep the file
customer-independent, but it gives up the call: an agent that will not say what
is owed cannot take a meaningful promise to pay it, and it hands the customer
back the lookup the call existed to save. **Letting the model say the number**
is the failure BLUEPRINT §5 blocks release on, stated as a design.

The slot mechanism is the third answer, and it is a stronger claim than either.
A block may declare ``{balance}``; the value is produced by a deterministic
formatter in :mod:`trail.money` from an
:class:`~trail.models.AccountProfile` field that came out of the system of
record; :meth:`render` substitutes it by literal string replacement. Because the
compliance allowlist verifies the **rendered** form — the same slot mapping is
handed to ``assert_agent_text_is_approved`` — the utterance is checked against
the very values the system of record supplied, one layer after rendering. So the
agent speaks an amount, and the amount provably did not come from the model.

:meth:`text_for` is unchanged on a slotted block, deliberately: it returns the
raw template, braces and all. Speaking that unrendered fails the allowlist,
because ``"o saldo é de {balance}"`` is not an approved utterance — it is a
template. A caller that forgets to render therefore produces a compliance
violation caught before the words leave the service, rather than a literal brace
read out to a customer. Fail closed, and fail where the tests can see it.

Reviewer notes in the file are prose for humans and are never returned: only the
```spoken`` fences are approved utterances. That separation is what lets the
compliance reviewer explain *why* the wording is what it is, in the same
document, without any risk of the explanation being read to a customer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

from trail.models import Step

__all__ = ["Protocol", "load_protocol"]


# The authoritative version marker. The YAML front matter carries the same
# value for human readers and tooling; the two are cross-checked below.
_VERSION_COMMENT_RE = re.compile(r"<!--\s*protocol_version:\s*(.+?)\s*-->")

# Front matter is a leading `---` fence. Parsed with a regex rather than a YAML
# dependency: one scalar is needed from it, and adding PyYAML to read a single
# `version:` line is a dependency that earns nothing.
_FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?$", re.DOTALL | re.MULTILINE
)
_FRONT_MATTER_VERSION_RE = re.compile(r'^version:[ \t]*"?(.+?)"?[ \t]*$', re.MULTILINE)

_H2_PREFIX = "## "
_FENCE = "```"
_SPOKEN_INFO_STRING = "spoken"

# A slot occurrence inside an approved block. The captured group is `[^{}]*` on
# purpose: it can never span a brace, so a stray unmatched `{` in prose cannot
# swallow the rest of the paragraph and arrive here as a slot name.
_SLOT_RE = re.compile(r"\{([^{}]*)\}")

# What a slot name may look like. Deliberately narrower than a Python
# identifier — lower-case ASCII, no accents — so the name a compliance reviewer
# writes in the approved file and the key the caller passes are the same string,
# with no case folding or Unicode normalisation in between to disagree about.
_SLOT_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class Protocol:
    """The parsed protocol: a version, one approved utterance per step, and the
    slots each of those utterances declares.

    Immutable by construction — the approved text cannot be edited at runtime,
    only by editing the file and restarting, which is what makes
    ``protocol_version`` on a :class:`~trail.models.CallRecord` sufficient to
    replay a call against the exact approved text it was given.

    ``slots`` is derived from ``texts`` at load time rather than declared beside
    it, and it defaults to empty so that a :class:`Protocol` built by hand — in
    a test, or by a caller assembling one directly — still constructs. Such a
    protocol declares no slots, so :meth:`render` refuses every non-empty slot
    mapping it is handed. That is the fail-closed direction: the failure mode of
    a hand-built protocol is a refusal to speak, never a wrong number spoken.
    """

    version: str
    texts: Mapping[Step, str]
    slots: Mapping[Step, frozenset[str]] = MappingProxyType({})

    def text_for(self, step: Step) -> str:
        """Return the VERBATIM approved utterance for ``step``.

        On a slotted block this returns the **raw template**, ``{balance}``
        included, and that is intentional rather than an oversight. A template
        is not an approved utterance: speaking it unrendered fails the
        compliance allowlist, so a caller that reaches for ``text_for`` where it
        owed ``render`` is caught by the gate every outbound utterance already
        passes through. The alternative — quietly rendering here with whatever
        values happen to be around — would make the mistake invisible.

        Raises:
            KeyError: if the step has no approved text. :func:`load_protocol`
                makes this unreachable for a protocol it returned — every
                :class:`~trail.models.Step` is validated at load time — so it
                can only fire on a :class:`Protocol` built by hand.
        """
        try:
            return self.texts[step]
        except KeyError:
            raise KeyError(f"no approved text for step {step.value!r}") from None

    def slots_for(self, step: Step) -> frozenset[str]:
        """Return the slot names ``step``'s approved block declares.

        Empty for every block that says the same sentence to every customer,
        which in protocol 1.0.0 is every block but ``state_balance``.

        Parsed from the block itself, never from a table kept beside it. A
        hand-maintained ``SLOTS_BY_STEP`` constant in code was the alternative
        and it would drift the moment a compliance reviewer edited the file
        without touching Python: the block would ask for a value nobody
        supplies, or the code would supply a value the block no longer mentions,
        and neither drift announces itself. Here the approved text *is* the
        declaration, so the two cannot disagree.
        """
        return self.slots.get(step, frozenset())

    def render(self, step: Step, slots: Mapping[str, str]) -> str:
        """Return ``step``'s approved text with its declared slots substituted.

        The supplied keys must equal the declared set **exactly**, in both
        directions. A missing slot would leave a brace in something about to be
        spoken to a customer. An unknown slot means the caller believes the
        block says something it does not — the approved text was edited and the
        call site was not — and the only safe reading of that disagreement is
        that neither side currently knows what is about to be said.

        ``str.format`` was the obvious implementation and is wrong twice over:
        it ignores extra keys silently, so a caller still passing a ``{fee}``
        that the block stopped mentioning would never find out, and it raises on
        any brace the template uses for anything else. This walks the declared
        names and replaces ``{name}`` literally, in sorted order, so the result
        is a pure function of the template and the mapping — no dict ordering,
        no hash seed, no locale.

        The walk is single-pass per name and does not re-scan what it wrote.
        Slot values come from the deterministic formatters in
        :mod:`trail.money`, applied to
        :class:`~trail.models.AccountProfile` fields from the system of
        record; none of them can contain a brace, so no substitution can
        manufacture a slot for a later iteration to fill. A slot value sourced
        from anywhere else — customer speech, a model — would have to
        re-establish that property, and would be the more urgent problem anyway.

        Raises:
            ValueError: if the supplied slot names are not exactly the declared
                ones. The message names both sets, and the difference in each
                direction, because that difference is the whole diagnosis.
            KeyError: if the step has no approved text (see :meth:`text_for`).
        """
        template = self.text_for(step)
        declared = self.slots_for(step)
        supplied = frozenset(slots)

        if supplied != declared:
            raise ValueError(
                f"step {step.value!r} declares slots {_format_names(declared)} "
                f"but was given {_format_names(supplied)}; the two must match "
                f"exactly. Missing: {_format_names(declared - supplied)}. "
                f"Unknown: {_format_names(supplied - declared)}."
            )

        rendered = template
        for name in sorted(declared):
            rendered = rendered.replace(f"{{{name}}}", slots[name])
        return rendered


@cache
def load_protocol(path: Path) -> Protocol:
    """Parse the protocol file at ``path``.

    Cached on ``path``: the file is approved content mounted into a container
    and cannot change under a running process. Tests that rewrite a protocol
    fixture must call ``load_protocol.cache_clear()``.

    Raises:
        ValueError: if the ``<!-- protocol_version: ... -->`` header is missing,
            if the front matter declares a different version, if a step section
            carries more than one ```spoken`` block, if a block declares a
            malformed slot name, or if any :class:`~trail.models.Step` lacks
            approved text.
        FileNotFoundError: if ``path`` does not exist.
    """
    text = path.read_text(encoding="utf-8")
    version = _parse_version(text, path)
    blocks = _parse_spoken_blocks(text, path)

    texts: dict[Step, str] = {}
    slots: dict[Step, frozenset[str]] = {}
    missing: list[str] = []
    for step in Step:
        spoken = blocks.get(step.value, [])
        if len(spoken) > 1:
            raise ValueError(
                f"{path}: step '{step.value}' has {len(spoken)} `spoken` blocks; "
                "exactly one is approved text and more than one is ambiguous"
            )
        if not spoken or not spoken[0]:
            missing.append(step.value)
            continue
        texts[step] = spoken[0]
        slots[step] = _parse_slots(spoken[0], step, path)

    if missing:
        raise ValueError(
            f"{path}: no approved `spoken` text for step(s): {', '.join(missing)}. "
            "Every Step must have approved text — the agent has no fallback and "
            "must not improvise collections language."
        )

    return Protocol(
        version=version,
        texts=MappingProxyType(texts),
        slots=MappingProxyType(slots),
    )


def _parse_version(text: str, path: Path) -> str:
    """Return the protocol version, checking both declarations agree.

    The HTML comment is authoritative. The YAML front matter, when present, must
    carry the same value: the file states that the two are changed together, and
    a half-applied edit to regulated content is exactly the kind of thing that
    should stop a boot rather than stamp the wrong version onto every record for
    a week.
    """
    match = _VERSION_COMMENT_RE.search(text)
    if match is None:
        raise ValueError(
            f"{path}: missing version header. The file must contain "
            "'<!-- protocol_version: ... -->'."
        )
    version = match.group(1)

    front_matter = _FRONT_MATTER_RE.match(text)
    if front_matter is not None:
        declared = _FRONT_MATTER_VERSION_RE.search(front_matter.group(1))
        if declared is not None and declared.group(1) != version:
            raise ValueError(
                f"{path}: front matter version {declared.group(1)!r} disagrees with "
                f"protocol_version {version!r}. They are the same value and must be "
                "changed together."
            )
    return version


def _parse_spoken_blocks(text: str, path: Path) -> dict[str, list[str]]:
    """Map each ``##`` heading to the ```spoken`` blocks inside its section.

    A single line-by-line pass, tracking fence state, so that Markdown syntax
    appearing inside a fenced block is never mistaken for structure. Headings
    that do not name a :class:`~trail.models.Step`, all other heading levels,
    fences with another info string, and every line outside a ```spoken`` fence
    are prose for human reviewers and are dropped here.
    """
    blocks: dict[str, list[str]] = {}
    heading: str | None = None
    fence_info: str | None = None
    fence_line = 0
    body: list[str] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()

        if fence_info is not None:
            if stripped == _FENCE:
                if fence_info == _SPOKEN_INFO_STRING:
                    if heading is None:
                        raise ValueError(
                            f"{path}: `spoken` block at line {fence_line} appears "
                            "before any '## <step>' heading, so it belongs to no step"
                        )
                    blocks.setdefault(heading, []).append("\n".join(body).strip())
                fence_info = None
                body = []
            else:
                body.append(line)
        elif stripped.startswith(_FENCE):
            fence_info = stripped[len(_FENCE) :].strip()
            fence_line = lineno
        elif line.startswith(_H2_PREFIX):
            heading = line[len(_H2_PREFIX) :].strip()

    if fence_info is not None:
        raise ValueError(f"{path}: unterminated ``` block opened at line {fence_line}")

    return blocks


def _parse_slots(block: str, step: Step, path: Path) -> frozenset[str]:
    """Return the slot names ``block`` declares, refusing to load a malformed one.

    Every matched ``{...}`` pair in approved text is a slot. There is no escape
    for a literal brace and none is offered: no Brazilian Portuguese utterance
    contains one, so a brace pair whose contents are not a valid slot name is a
    typo in reviewed content — ``{Balance}``, ``{ balance }``, ``{saldo!}`` —
    and it stops the boot. The alternative is five literal characters read out
    to a customer, or worse, a block that quietly renders with one fewer number
    in it than the reviewer thought they approved.

    An unmatched brace is not a slot and is not an error: ``_SLOT_RE`` cannot
    match across one, so prose keeps working. That is also the reason
    :meth:`Protocol.render` does not go through ``str.format``, which would
    raise on exactly that text.
    """
    names: set[str] = set()
    for match in _SLOT_RE.finditer(block):
        name = match.group(1)
        if _SLOT_NAME_RE.fullmatch(name) is None:
            raise ValueError(
                f"{path}: step '{step.value}' declares '{{{name}}}', which is not "
                f"a valid slot name (it must match {_SLOT_NAME_RE.pattern!r}). "
                "Approved text carries no literal braces, so this is a typo in "
                "reviewed content and it stops the boot rather than being spoken."
            )
        names.add(name)
    return frozenset(names)


def _format_names(names: frozenset[str]) -> str:
    """Render a set of slot names for an error message.

    Sorted, so two runs of the same mistake produce the same sentence and the
    message is diffable in a log.
    """
    if not names:
        return "{}"
    return "{" + ", ".join(sorted(names)) + "}"
