"""The ``trail`` command-line client.

A first-class layer of the system, not a debug script: in the compose topology
the client is its own service boundary (INTERFACES §8), and it is the seam where
telephony and audio attach later. Today it reads a typed line from a keyboard
and prints the agent's Portuguese to a terminal; tomorrow the same loop reads a
transcript off an ASR stream and hands a sentence to a synthesiser. Nothing
below the seam changes, which is the point of BLUEPRINT §8's cascaded design:
the disclosures and the balance pass a text checkpoint before anything is
It speaks the same published HTTP contract to the agent that the eval harness
speaks — the harness reaches for one endpoint more, to stage a call nobody
answered — so anything that works here works for a call placed over a phone
line.

The implementation lives in :mod:`trail.client.cli`, whose
:func:`~trail.client.cli.main` is the console-script entry point
(``trail = "trail.client.cli:main"``). Nothing is re-exported here: the package
is imported for its module, and a side-effect import would pull ``httpx`` and
``rich`` into every process that merely touches the namespace.
"""
