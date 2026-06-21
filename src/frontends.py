"""Front doors — transport only.

The bot loop talks to a front door through two methods: `read()` for the next
user utterance and `say()` for Aria's line. Phase 0 is a text REPL; Phase 1
swaps in a voice front door with the SAME interface, so voice is a transport
swap, not a rewrite. A scripted front door drives verification.
"""

from __future__ import annotations

from collections.abc import Iterable


class TextFrontend:
    """A plain text REPL (stdin/stdout)."""

    def read(self) -> str | None:
        try:
            return input("\nyou> ")
        except EOFError:
            return None

    def say(self, text: str) -> None:
        print(f"aria> {text}")


class ScriptedFrontend:
    """Feeds queued inputs and captures Aria's lines — for verification runs."""

    def __init__(self, inputs: Iterable[str]) -> None:
        self._inputs = list(inputs)
        self._i = 0
        self.said: list[str] = []

    def read(self) -> str | None:
        if self._i >= len(self._inputs):
            return None
        val = self._inputs[self._i]
        self._i += 1
        print(f"\nyou> {val}")
        return val

    def say(self, text: str) -> None:
        self.said.append(text)
        print(f"aria> {text}")
