"""``trail chat`` — a conversation, and the pipeline behind it.

The point of this client is the rail. Anything can print a model's answer; what
this prints alongside it is what the turn actually did — which gates ran, which
were switched off, how long the model took, what it cost, and a link to the
span tree. That is the claim the repository makes, and a client that showed
only the answer would leave it unverifiable from the terminal.

It speaks HTTP to the service and never imports the agent. Same interface a
browser uses, same one an eval harness would: a client with a private code path
measures a system that does not exist in production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

DEFAULT_BASE_URL = os.environ.get("TRAIL_AGENT_BASE_URL", "http://localhost:8000")

THEME = Theme(
    {
        "agent": "bold cyan",
        "user": "bold white",
        "meta": "dim",
        "ok": "green",
        "skip": "dim strike",
        "blocked": "bold red",
        "rule": "dim",
    }
)

#: A distinct glyph per status, and `blocked` must not share one with `done`.
#: Colour alone is not a difference: it is lost in a pipe, in a screenshot, and
#: to a reader who cannot distinguish red from grey — and a gate that fired
#: rendering identically to a gate that passed is the exact failure this whole
#: design exists to prevent.
MARK = {"done": "▪", "skip": "▫", "blocked": "✗", "start": "▪"}


class CliError(Exception):
    """An error the user can act on, rendered as a message plus a hint."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


async def _iter_sse(lines: Any) -> Any:
    """Yield ``(event, data)`` from an SSE line stream, as they arrive.

    Async and incremental on purpose: buffering the whole response before
    parsing it would make the rail appear all at once, at the end, which is a
    stream indistinguishable from a slow request — the exact failure the
    ``X-Accel-Buffering: no`` header on the server exists to prevent.

    Deliberately minimal otherwise: this endpoint sends only ``event:`` and
    ``data:``, one line of each per frame, and handling ids, retries and
    multi-line data would be handling cases the server never produces.
    """
    event = None
    async for line in lines:
        if not line:
            event = None
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            yield event, json.loads(line[5:].strip())


def _render_rail(console: Console, stages: list[dict[str, Any]]) -> None:
    """Print the rail in arrival order, which is pipeline order.

    Deliberately unsorted. The skips for gates the dial left out are emitted
    from the hook where each would have run, so they already land in place —
    and any sort able to reposition them is also able to scramble the real
    interleaving of model and tool calls, which is the ordering a reader is
    reading for.
    """
    text = Text()
    for stage in stages:
        status = stage["status"]
        style = {"skip": "skip", "blocked": "blocked"}.get(status, "meta")
        text.append(f"{MARK.get(status, '·')}{stage['label']} ", style=style)
        if status == "skip":
            text.append("pulado  ", style="skip")
        elif status == "blocked":
            text.append("BLOQUEADO  ", style="blocked")
        elif stage.get("ms") is not None:
            text.append(f"{stage['ms']}ms  ", style="meta")
    console.print("  ", text)

    for stage in stages:
        for violation in (stage.get("detail") or {}).get("violations", []):
            console.print(
                f"  ↳ [blocked]{violation['check']}[/] · {violation['detail']}"
                f" · [meta]{violation['evidence']}[/]"
            )


def _render_cost(console: Console, stages: list[dict[str, Any]]) -> None:
    tokens_in = tokens_out = 0
    cost: float | None = None
    for stage in stages:
        detail = stage.get("detail") or {}
        if stage["kind"] != "model" or stage["status"] != "done":
            continue
        tokens_in += detail.get("input_tokens") or 0
        tokens_out += detail.get("output_tokens") or 0
        if detail.get("cost_usd") is not None:
            cost = (cost or 0.0) + detail["cost_usd"]
    if not tokens_in and not tokens_out:
        return
    # `—` and not `$0.00`: an unpriced model has an unknown cost, and a
    # confident zero is the most expensive kind of wrong.
    money = f"US$ {cost:.4f}" if cost is not None else "—"
    console.print(f"  [meta]{tokens_in} in · {tokens_out} out · {money}[/]")


async def _turn(
    client: httpx.AsyncClient, console: Console, thread_id: str, message: str
) -> None:
    stages: list[dict[str, Any]] = []
    async with client.stream(
        "POST", f"/threads/{thread_id}/turns/stream", json={"message": message}
    ) as response:
        response.raise_for_status()
        # A live "…" for each stage as it starts, so the wait is visibly the
        # pipeline working rather than the terminal hanging. `start` frames are
        # shown and then discarded; only completed ones join the rail, which is
        # reprinted whole once the answer arrives.
        with console.status("", spinner="dots") as spinner:
            async for event, data in _iter_sse(response.aiter_lines()):
                if event == "stage":
                    if data["status"] == "start":
                        spinner.update(f"[meta]{data['label']}…[/]")
                    else:
                        stages.append(data)
                elif event == "turn":
                    spinner.stop()
                    console.print()
                    console.print(Text(data["text"], style="agent"))
                    console.print()
                    _render_rail(console, stages)
                    _render_cost(console, stages)
                elif event == "error":
                    spinner.stop()
                    console.print(
                        f"  [blocked]falha {data['status']}[/] · {data['detail']}"
                    )
                elif event == "trace" and data.get("trace_url"):
                    console.print(f"  [meta]trace: {data['trace_url']}[/]")


async def chat(base_url: str) -> int:
    console = Console(theme=THEME)
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
        try:
            opened = await client.post("/threads")
            opened.raise_for_status()
        except httpx.HTTPError as exc:
            raise CliError(
                f"não consegui falar com o agente em {base_url}: {exc}",
                hint="a stack está de pé? `make up`",
            ) from exc

        thread = opened.json()
        console.print(
            f"[meta]agente[/] {thread['agent']}  "
            f"[meta]guardrails[/] {thread['guardrails']}  "
            f"[meta]thread[/] {thread['thread_id'][:8]}"
        )
        console.print()
        console.print(Text(thread["greeting"], style="agent"))

        while True:
            console.print()
            try:
                message = console.input("[user]› [/]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            if not message:
                continue
            if message in {"sair", "exit", "quit"}:
                return 0
            await _turn(client, console, thread["thread_id"], message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trail", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    chat_cmd = sub.add_parser("chat", help="hold a conversation with the agent")
    chat_cmd.add_argument("--base-url", default=DEFAULT_BASE_URL)

    health = sub.add_parser("health", help="check that the agent is up")
    health.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser


async def health(base_url: str) -> int:
    console = Console(theme=THEME)
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        try:
            response = await client.get("/healthz")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CliError(f"{base_url} não respondeu: {exc}") from exc
    console.print(f"[ok]ok[/] {base_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = {"chat": chat, "health": health}[args.command]
    try:
        return asyncio.run(runner(args.base_url))
    except CliError as exc:
        console = Console(theme=THEME, stderr=True)
        console.print(f"[blocked]erro[/] {exc}")
        if exc.hint:
            console.print(f"[meta]{exc.hint}[/]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
