"""The terminal loop of one lesson: input in, the engine driven, the native view drawn.

`run_lesson` runs inside the alternate screen (`screen.Terminal`) and the outer keyboard
mode (`router.KeyboardMode`), both owned by the caller. Every input token goes through the
`Router`: the trainer's two keys reach the engine (`hint`, `pause`), everything else is
forwarded to the private session through `engine.forwarded`. About 20 times a second the
engine ticks, its events are applied (router state, history) and the coach bar is drawn
under the private herdr's own terminal, for herdr and Yazi lessons alike. A live desktop
lesson has no private terminal: `LiveView` draws a card with the bar under it instead. A
notice step (no key in the user's setup) draws its card in place of the terminal until `s`.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import replace

import coach
import lesson
from native_view import NativeView
from router import HINT, PAUSE, PROMPT_ACTIONS, QUIT, SKIP, Click, Input, Router, canonical, key_name
from screen import BOLD, GOLD, MINT, MUTED, Card, Text, render_card
from strings import DESKTOP

FRAME = 0.05  # one read and one tick: about 20 Hz
PROMPT_WAIT, PROMPT_POLL = 0.15, 0.01  # how long a forwarded key may take to open a prompt
PROMPT_CARDS = frozenset(action.replace(" ", "_") for action in PROMPT_ACTIONS)


def router_for(plan: lesson.Plan, *, prefix: str = "", extra=()) -> Router:
    """A router for the private session of this plan.

    The session binds only the plan's cards (plus `extra` chords, such as Yazi's moving-about
    keys), so only those chords can take the trainer's `?` and `q` away; the user's other bindings
    do nothing in there. `prefix` is the herdr prefix when the plan's bindings have none.
    """
    bindings = [binding for card, binding in plan.bindings.items() if card in plan.actions]
    prefix = next((b.prefix for b in bindings if b.prefix), prefix)
    chords = [*(c for b in bindings for c in (*b.chords, *b.ambiguous)), *extra]
    direct = [step for chord in chords if not chord.startswith("prefix+") for step in chord.split(", ")]
    prompts = [chord for b in bindings if b.card.partition(":")[2] in PROMPT_CARDS for chord in b.chords]
    return Router(prefix, direct, prompts)


def run_lesson(ui, session, engine: lesson.Engine, router: Router, mode, *, history=None, view=None,
               inputs: Input | None = None, clock=time.monotonic, autoplay: float | None = None,
               sleep=time.sleep):
    """Play `engine`'s plan in `session` until the lesson is done; return its LessonResult.

    `history` (default `engine.history`) gets every step and, for a lesson, the finished run.
    `view` defaults to a `NativeView` over `session.herdr`, or a `LiveView` for a session
    without one (the user's live desktop). `autoplay` (the TTY demo) presses
    the hint key (on a notice step, the skip key) every that many seconds of the user's turn and
    ignores the user's keys but the pause key
    and Ctrl+C. Ctrl+C raises KeyboardInterrupt at once; the caller's context managers put
    the terminal back. `sleep` paces the short wait for a prompt report (`_await_prompt`).
    """
    history = engine.history if history is None else history
    inputs = inputs or Input()
    pending, mode.pending = getattr(mode, "pending", b""), b""
    if view is None:
        view = HerdrView(ui, session.herdr) if getattr(session, "herdr", None) is not None else LiveView(ui)
    native = view
    with native:
        engine.begin(clock())
        _apply(engine.drain(), router, history)
        tokens = list(inputs.events(pending)) if pending else []
        waited = clock()
        reached = None  # when a token last reached the session
        while engine.state != lesson.DONE:
            now = clock()
            engine.tick(now)
            _apply(engine.drain(), router, history)
            if engine.state == lesson.DONE:
                break
            if autoplay is not None:
                if engine.state != lesson.RUNNING:
                    waited = now
                elif now - waited >= autoplay:
                    engine.skip() if engine.notice else engine.hint()
                    waited = now
            bar = replace(engine.bar(typing=router.typing), offer=engine.offer)
            card = engine.notice_card()
            if card is not None and hasattr(native, "show_notice"):
                native.show_notice(card, bar)
            else:
                native.show_bar(bar)
            tokens.extend(inputs.events(ui.read(FRAME, forward_ctrl_c=True)))
            for token in tokens:
                router.skippable = engine.skippable
                if reached is not None and engine.state != lesson.PAUSED and router.claims(token):
                    _await_prompt(session, router, reached + PROMPT_WAIT, clock, sleep)
                if _key(token, engine, router, autoplay is not None):
                    reached = clock()
            tokens = []
            _apply(engine.drain(), router, history)
    return engine.result


def _key(token, engine, router, demo):
    """Route one token and act on it; True when it reached the session."""
    if isinstance(token, Click):
        return False
    if engine.state == lesson.PAUSED:
        name = key_name(token)
        if name == "ctrl+c":
            raise KeyboardInterrupt
        if name == "enter":
            engine.resume()
        elif name == "s":
            engine.skip()
        elif name == "r":
            engine.redo()
        elif name is not None and name == canonical(engine.pause_key):
            engine.quit()
        return False
    router.skippable = engine.skippable
    where, value = router.route(token)
    if where != "trainer":
        if not demo and engine.forwarded(value):
            return True
        router.dropped()  # outside the user's turn: the key went nowhere, so it opened nothing
    elif value == QUIT:
        raise KeyboardInterrupt
    elif value == PAUSE:
        engine.quit() if demo else engine.pause()
    elif value == HINT and not demo:
        engine.hint()
    elif value == SKIP and not demo and engine.state == lesson.RUNNING:
        engine.skip()
    return False


def _await_prompt(session, router, until, clock, sleep):
    """Before the hint or pause key, give the session until `until` (PROMPT_WAIT after the
    last token that reached it) to say a prompt opened or closed: Yazi reports its filter
    some time after `f`, and a `q` typed right behind it, in the same read or the next, is
    filter text. Only while the session, not the router, owns `typing` (herdr's prompts are
    the router's own); returns as soon as the report changes `typing`."""
    if not router.follows_session:
        return
    for _ in range(round(PROMPT_WAIT / PROMPT_POLL) + 2):  # bounded, whatever the clock does
        typing = bool(session.typing())
        if typing != router.typing:
            router.typing = typing
            return
        left = until - clock()
        if left <= 0:
            return
        sleep(min(PROMPT_POLL, left))


def _apply(events, router, history):
    for event in events:
        kind = event[0]
        if kind == "typing":
            router.typing = event[1]
        elif kind == "step":
            router.forget_prefix()
        elif kind == "reset":
            router.reset()
        elif kind == "result" and history is not None:
            history.record_step(event[1])
        elif kind == "done" and history is not None and event[1].lesson is not None:
            run = event[1]
            history.finish_lesson(run.lesson, lesson.scored(run.steps), seconds=run.seconds, complete=run.complete)


LINE_STYLES = {"hint": GOLD, "miss": GOLD, "success": MINT, "checklist": MINT}


def _paint_notice(ui, out, card, bar, size):
    """A notice card centred above the coach bar, both on the trainer's own screen."""
    width, height = size
    count = coach.bar_rows(width, height)
    rows = coach.render(bar, width, height)
    if count:
        painted, margin = render_card(card, width, height - count)
        ui.paint(painted, margin, (width, height))
    else:
        ui.cache, ui.size = {}, None
        out.write("\x1b[2J")
    top = height - len(rows) + 1 if count else 1
    for y, row in enumerate(rows, top):
        out.write(f"\x1b[{y};1H\x1b[2K{row}")
    out.flush()


class HerdrView(NativeView):
    """The private herdr's own terminal above the bar; a notice card takes its place while a
    notice step runs, and the next `show_bar` starts the terminal again (herdr redraws whole)."""

    def __init__(self, ui, source, out=None):
        super().__init__(ui, source)
        self.out = out or sys.stdout
        self.drawn = None

    def show_notice(self, card, bar, size=None):
        size = size or tuple(shutil.get_terminal_size())
        if self.drawn == (card, bar, size):
            return
        if self.active:
            self.close()
        _paint_notice(self.ui, self.out, card, bar, size)
        self.drawn = (card, bar, size)

    def show_bar(self, bar):
        self.drawn = None
        super().show_bar(bar)


class LiveView:
    """The trainer's own screen during a live desktop lesson: a card with the goal and the
    middle line, big enough to read the moment the user is back, and the coach bar at the bottom.

    Rows are painted only when the bar or the size changed; the terminal keeps them while
    another workspace is in front, so the card is there when the user comes back.
    """

    def __init__(self, ui, out=None):
        self.ui = ui
        self.out = out or sys.stdout
        self.drawn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.ui.cache, self.ui.size = {}, None
        self.out.write("\x1b[0m\x1b[2J\x1b[H")
        self.out.flush()

    def show_notice(self, card, bar, size=None):
        size = size or tuple(shutil.get_terminal_size())
        if self.drawn == (card, bar, size):
            return
        _paint_notice(self.ui, self.out, card, bar, size)
        self.drawn = (card, bar, size)

    def show_bar(self, bar: coach.Bar, size=None):
        width, height = size or shutil.get_terminal_size()
        if self.drawn == (bar, width, height):
            return
        count = coach.bar_rows(width, height)
        rows = coach.render(bar, width, height)
        if count:
            lines = [((bar.goal, BOLD),)]
            if bar.chord:
                lines += ["", ((bar.chord, GOLD),)]
            if bar.line:
                lines += ["", Text(bar.line, LINE_STYLES.get(bar.kind, MUTED))]
            lines += ["", Text(DESKTOP["watching"], MUTED)]
            card = Card(DESKTOP["header"], bar.title, tuple(lines), header_style=GOLD)
            painted, margin = render_card(card, width, height - count)
            self.ui.paint(painted, margin, (width, height))
        else:
            self.ui.cache, self.ui.size = {}, None
            self.out.write("\x1b[2J")
        top = height - len(rows) + 1 if count else 1
        for y, row in enumerate(rows, top):
            self.out.write(f"\x1b[{y};1H\x1b[2K{row}")
        self.out.flush()
        self.drawn = (bar, width, height)
