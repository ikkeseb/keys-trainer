"""Play the resolved private curriculum and score only observed changes to its worlds."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep

import checks
from curriculum import Resolved
from practice import rows_for
from session import HerdrSession, perform
from shell_backend import ShellSession
from strata_keys import KeysSession
from strings import SELFTEST as TEXT
from world import describe
from yazi_backend import YaziSession

POLL = 0.02
MOVE_GAP = 0.3
SOLUTION_TIMEOUT = 4.0
DECOY_TIMEOUT = 1.5


@dataclass(frozen=True)
class Result:
    """passed means the expected outcome occurred, including rejection of a decoy."""

    scenario: str | None  # a chain covers the whole lesson; on failure, its offending step
    lesson: str
    kind: str
    passed: bool
    seconds: float
    detail: str
    rebuilds: int = 0  # excludes the initial lesson fixture


def _session(tool, rows):
    herdr = rows_for(rows, "herdr")
    if tool == "strata":
        return KeysSession(rows_for(rows, "strata"))
    if tool == "yazi":
        return YaziSession(rows_for(rows, "yazi"), herdr)
    if tool == "shell":
        return ShellSession(rows_for(rows, "shell"), herdr)
    return HerdrSession(herdr)


def _play(session, resolved, move):
    chord = None
    if move.card:
        binding = resolved.bindings.get(move.card)
        if not binding or binding.taught not in session.chords(move.card):
            raise ValueError(TEXT["no_taught"].format(card=move.card))
        chord = binding.taught
    perform(session, move, chord)


def _poll(session, trail, duration):
    """Keep transitions only, including a return to an earlier world."""
    start = monotonic()
    while True:
        world = session.world()
        if world != trail[-1]:
            trail.append(world)
        elapsed = monotonic() - start
        yield elapsed
        if elapsed >= duration:
            return
        sleep(min(POLL, duration - elapsed))


def _setup(session, resolved, scenario, trail):
    for index, move in enumerate(scenario.setup, 1):
        _play(session, resolved, move)
        final = index == len(scenario.setup)
        for elapsed in _poll(session, trail, SOLUTION_TIMEOUT if final else MOVE_GAP):
            if final and elapsed >= MOVE_GAP and not checks.unmet(scenario.needs, trail[-1], trail[:-1]):
                return
    missing = checks.unmet(scenario.needs, trail[-1], trail[:-1])
    if missing:
        raise ValueError(TEXT["needs_unmet"].format(missing=missing))


def _attempt(session, resolved, scenario, trail, decoy):
    start = len(trail) - 1
    moves = scenario.decoy if decoy else scenario.solution
    timeout = DECOY_TIMEOUT if decoy else SOLUTION_TIMEOUT
    for index, move in enumerate(moves, 1):
        _play(session, resolved, move)
        final = index == len(moves)
        for elapsed in _poll(session, trail, timeout if final else MOVE_GAP):
            progress = checks.evaluate(scenario.checks, trail[start], trail[start + 1:], trail[:start])
            if progress.done and decoy:
                raise ValueError(TEXT["decoy_passed"].format(index=index))
            if final and progress.done and elapsed >= MOVE_GAP:
                return TEXT["solved"].format(total=progress.total)
    if decoy:
        return TEXT["decoy_rejected"].format(latched=progress.latched, total=progress.total)
    change = describe(trail[start], trail[-1]) or TEXT["no_change"]
    raise ValueError(TEXT["unsolved"].format(latched=progress.latched, total=progress.total, change=change))


def _run(resolved, rows, lesson, steps, kind):
    chained = kind == "chained"
    rebuilds = 0
    sid = steps[0].scenario
    started = monotonic()
    try:
        fixture = lesson.fixture
        if not chained:
            fixture = resolved.scenarios[sid].scenario.fixture or fixture
        with _session(lesson.tool, rows) as session:
            actions = resolved.actions(step.scenario for step in steps)
            trail = [session.start(actions, fixture)]
            for step in steps:
                sid = step.scenario
                scenario = resolved.scenarios[sid].scenario
                if chained and checks.unmet(scenario.needs, trail[-1], trail[:-1]):
                    trail = [session.apply_fixture(scenario.fixture or lesson.fixture)]
                    rebuilds += 1
                    _setup(session, resolved, scenario, trail)
                elif not chained:
                    _setup(session, resolved, scenario, trail)
                detail = _attempt(session, resolved, scenario, trail, kind == "decoy")
        if chained:
            sid, detail = None, TEXT["chain_done"].format(count=len(steps))
        passed = True
    except (OSError, RuntimeError, ValueError) as exc:
        passed, detail = False, " ".join(str(exc).splitlines())
    return Result(sid, lesson.id, kind, passed, monotonic() - started, detail, rebuilds)


def solve_all(resolved: Resolved, rows) -> list[Result]:
    """rows is the same rigpage keys export passed to curriculum.resolve."""
    results = []
    tested = set()
    for state in resolved.lessons:
        lesson = state.lesson
        if lesson.tool not in ("herdr", "yazi", "shell", "strata") or not state.steps:
            continue
        for step in state.steps:
            if step.scenario in tested:
                continue
            tested.add(step.scenario)
            results.append(_run(resolved, rows, lesson, (step,), "solution"))
            if resolved.scenarios[step.scenario].scenario.decoy:
                results.append(_run(resolved, rows, lesson, (step,), "decoy"))
        results.append(_run(resolved, rows, lesson, state.steps, "chained"))
    return results


def lines(results) -> list[str]:
    output = []
    for row in results:
        target = row.lesson + (f"/{row.scenario}" if row.scenario else "")
        rebuilds = TEXT["rebuilds"].format(count=row.rebuilds) if row.kind == "chained" else ""
        output.append(TEXT["line"].format(status=TEXT["ok" if row.passed else "failed"], target=target,
                                          kind=TEXT["kinds"][row.kind], seconds=f"{row.seconds:.2f}",
                                          detail=row.detail, rebuilds=rebuilds))
    output.append(TEXT["total"].format(passed=sum(r.passed for r in results), total=len(results)))
    return output
