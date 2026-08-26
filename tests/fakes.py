"""A chat model that answers from a script, so the unit tier can stay offline.

``make test`` runs with no Docker, no database, no network and a deliberately
invalid API key, and that constraint is what keeps the suite runnable during
review rather than only in CI. Exercising the agent loop under it needs a model
object, and none of LangChain's shipped fakes implement ``bind_tools`` — which
``create_agent`` calls unconditionally, even for an agent with no tools.

So: ten lines. :class:`ScriptedModel` replays a list of messages in order and
accepts tool bindings without doing anything with them, which is exactly right
for a fake — the *model's* tool choice is scripted, and whether the tool
actually runs is the graph's business and therefore the thing under test.

Putting a tool call in the script drives the real tool node, the real
``wrap_tool_call`` hook and the real rail frame. Nothing about the path is
simulated except the model's decision to take it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class ScriptedModel(GenericFakeChatModel):
    """Replays ``messages`` in order; accepts and ignores tool bindings.

    ``disable_streaming`` is set because the base fake builds its stream by
    chunking the message *text*, and a tool call has no text — streaming one
    yields no chunks and raises "No generations found in stream". Turning
    streaming off makes LangChain fall back to a single invoke, which is what a
    scripted model should do anyway: there is nothing to stream from a list.

    The runtime still asks for the ``messages`` channel; a fake that declines
    to use it is not a claim that real models will.
    """

    disable_streaming: bool = True

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self


def scripted(*messages: AIMessage) -> ScriptedModel:
    """A model that will answer with ``messages``, one per model call."""
    return ScriptedModel(messages=iter(messages))


def says(text: str, usage: dict[str, Any] | None = None) -> AIMessage:
    """A plain assistant reply, optionally carrying usage metadata.

    ``usage`` is the LangChain-normalised shape — ``input_tokens``,
    ``output_tokens``, ``input_token_details`` — which is what lets a test
    assert on cost accounting without a provider.
    """
    message = AIMessage(text)
    if usage is not None:
        message.usage_metadata = usage  # type: ignore[assignment]
    return message


def calls(tool: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    """An assistant turn that asks for one tool, driving the real tool node."""
    return AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": args, "id": call_id, "type": "tool_call"}],
    )
