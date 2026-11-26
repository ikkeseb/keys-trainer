"""The private session a lesson drives (seams § Session protocol), and how a move is played.

`HerdrSession` wraps `HerdrBackend`: card ids in (`herdr:split_vertical`), `World(herdr=…)`
out. The lesson engine and the self-test only use the protocol, so a Yazi session (piece 5)
or a fake in tests can stand in.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Protocol

from curriculum import Move
from herdr_backend import HerdrBackend
from world import World

REPEAT_GAP = 0.05  # between repeated presses (backspace ×8); herdr keeps keys ≥ 20 ms apart


class Session(Protocol):
    def start(self, actions: Iterable[str], fixture: str) -> World: ...
    def apply_fixture(self, name: str) -> World: ...
    def world(self) -> World: ...
    def chords(self, card: str) -> tuple[str, ...]: ...
    def play(self, card: str, **kw) -> str: ...
    def press(self, chord: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def feed(self, raw: bytes) -> None: ...
    def typing(self) -> bool: ...
    def close(self) -> None: ...


def _action(card: str, tool: str = "herdr") -> str:
    prefix, _, action = card.partition(":")
    if prefix != tool or not action:
        raise ValueError(f"{card!r} is not a {tool} card.")
    return action


class HerdrSession:
    """One private herdr per lesson: own socket, home and config, as `HerdrBackend` runs it."""

    tool = "herdr"

    def __init__(self, rows, backend: HerdrBackend | None = None):
        self.herdr = backend if backend is not None else HerdrBackend(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self, actions: Iterable[str], fixture: str) -> World:
        actions = {_action(card) for card in actions if card.startswith("herdr:")}
        return World(herdr=self.herdr.start_session(actions, fixture))

    def apply_fixture(self, name: str) -> World:
        return World(herdr=self.herdr.apply_fixture(name))

    def world(self) -> World:
        return World(herdr=self.herdr.world())

    def chords(self, card: str) -> tuple[str, ...]:
        return self.herdr.chords(_action(card))

    def play(self, card: str, *, index: int = 0, number: int | None = None) -> str:
        return self.herdr.play(_action(card), index=index, number=number)

    def press(self, chord: str) -> None:
        self.herdr.press(chord)

    def type_text(self, text: str) -> None:
        self.herdr.type_text(text)

    def feed(self, raw: bytes) -> None:
        self.herdr.feed(raw)

    def typing(self) -> bool:
        # herdr's own prompts are tracked by the router (rename and close-workspace chords).
        return False

    def close(self) -> None:
        self.herdr.close()


def chord_index(session: Session, card: str, chord: str | None) -> int:
    """Where the taught chord sits in the session's bindings for the card (0 when unknown)."""
    chords = session.chords(card)
    return chords.index(chord) if chord in chords else 0


def perform(session: Session, move: Move, chord: str | None = None, sleep=time.sleep) -> str | None:
    """Play one move into the session; return the chord played for a card move.

    `chord` is the taught chord (rigpage spelling) to play for a card; a wait move only waits.
    """
    if move.card:
        return session.play(move.card, index=chord_index(session, move.card, chord), number=move.number)
    if move.press:
        for count in range(move.repeat):
            if count:
                sleep(REPEAT_GAP)
            session.press(move.press)
    elif move.text:
        session.type_text(move.text)
    elif move.wait:
        sleep(move.wait)
    return None
