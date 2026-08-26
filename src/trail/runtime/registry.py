"""Which example agent is mounted, resolved from ``TRAIL_AGENT``.

Examples are looked up lazily, by import path, so that adding one is adding a
module rather than editing the runtime. The runtime never imports an example
at module scope: an example is free to depend on things the runtime does not,
and a broken example should fail when it is asked for, not when the service
starts with a different one selected.
"""

from __future__ import annotations

from importlib import import_module

from trail.runtime.agent import AgentSpec

#: Example name → module exposing a zero-argument ``build() -> AgentSpec``.
EXAMPLES: dict[str, str] = {
    "trail_guide": "examples.trail_guide.agent",
}


def load_spec(name: str) -> AgentSpec:
    """The :class:`AgentSpec` registered as ``name``.

    A miss names the valid set. The alternative — an ``ImportError`` mentioning
    a module path the operator never typed — is the same information with the
    useful part removed.
    """
    try:
        module_path = EXAMPLES[name]
    except KeyError:
        valid = ", ".join(sorted(EXAMPLES))
        raise ValueError(
            f"unknown agent {name!r}; TRAIL_AGENT must be one of: {valid}"
        ) from None
    module = import_module(module_path)
    return module.build()
