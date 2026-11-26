"""Lessons and scenarios as data: load `curriculum/*.toml`, validate, resolve against the user's keys.

`load()` reads every TOML file in `curriculum/` and refuses anything it cannot run:
unknown check or need names, missing or over-long copy, steps naming scenarios that do
not exist, malformed card ids and solution items. `resolve()` binds each card to the user's
chords in today's keys: a scenario with a card that has no key at all becomes a notice step
(the lesson shows how to add the key, and `s` skips it), one the tool is missing for or the
terminal cannot send is inactive, a lesson left with fewer than two active or notice
scenarios is hidden, and `report()` says which and why.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import checks
import desktop_session  # registers the desktop checks
import herdr_backend
import shell_backend
import strata_keys  # registers the strata check
from practice import Card, rows_for, shortcut_text
from router import canonical
from strings import DESKTOP, KEYS, REPORT


FOLDER = Path(__file__).with_name("curriculum")
TOOLS = ("herdr", "yazi", "shell", "strata", "desktop")
TRACKS = {"herdr": "herdr", "yazi": "Yazi", "shell": "Shell", "strata": "Strata", "desktop": "Hyprland"}  # history's track names
KINDS = ("learn", "recall", "build")
FIXTURES = {"herdr": frozenset(herdr_backend.FIXTURES), "desktop": frozenset(desktop_session.FIXTURES),
            "shell": frozenset(shell_backend.FIXTURES), "strata": frozenset({"keys"})}
# (Yazi names its own fixtures; yazi_backend.FIXTURES checks them when a session starts.)
CARD = re.compile(r"(herdr|yazi|shell|strata|desktop):[a-z0-9_]+")
LIMITS = {"goal": 66, "why": 70, "nudge": 80, "done": 66, "title": 40, "intro": 220, "pattern": 110,
          "checklist": 32, "builds": 24}
LESSON_KEYS = {"id": True, "order": True, "title": True, "tool": True, "fixture": True, "intro": True,
               "pattern": True, "steps": True, "builds": False}
SCENARIO_KEYS = {"id": True, "goal": True, "why": True, "nudge": True, "done": True, "cards": True,
                 "checks": True, "solution": True, "fixture": False, "needs": False, "decoy": False,
                 "setup": False, "typing": False}
MOVE_KEYS = {"card", "number", "index", "press", "repeat", "type", "wait"}


class CurriculumError(ValueError):
    """Every problem found, one per line, each naming its file and id."""

    def __init__(self, problems: Sequence[str]):
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Move:
    """One solution, setup or decoy item: a card (the user's chord), a key, typed text or a pause."""

    card: str | None = None
    number: int | None = None  # fills an indexed chord such as alt+1..9
    press: str | None = None
    repeat: int = 1
    text: str | None = None
    wait: float = 0.0


@dataclass(frozen=True)
class Step:
    kind: str  # learn | recall | build
    scenario: str


@dataclass(frozen=True, eq=False)
class Scenario:
    id: str
    tool: str
    goal: str
    why: str
    nudge: str
    done: str
    cards: tuple[str, ...]
    checks: tuple[dict, ...]
    texts: tuple[str | None, ...]  # live checklist label per check (`checklist = "…"` in the file)
    solution: tuple[Move, ...]
    fixture: str | None = None
    needs: tuple[dict, ...] = ()
    decoy: tuple[Move, ...] = ()
    setup: tuple[Move, ...] = ()  # played after the fixture when a rebuild must meet `needs`
    typing: bool = False
    source: str = ""
    template: str = ""  # the goal with «{number}» when the session picks the number (desktop)

    def with_number(self, number: int) -> Scenario:
        """This scenario aimed at `number`: the goal, every numbered move and `number` check."""
        old = next(m.number for m in self.solution if m.card and m.number)

        def moves(items):
            return tuple(replace(m, number=number) if m.card and m.number == old else m for m in items)

        found = tuple({**c, "number": number} if c.get("number") == old else c for c in self.checks)
        return replace(self, goal=self.template.format(number=number), checks=found,
                       solution=moves(self.solution), setup=moves(self.setup), decoy=moves(self.decoy))

    @property
    def played_cards(self) -> tuple[str, ...]:
        """Every card the scenario names, plays or sets up with (decoy included)."""
        found = [*self.cards, *(m.card for m in (*self.solution, *self.setup, *self.decoy) if m.card)]
        return tuple(dict.fromkeys(found))

    @property
    def blueprints(self) -> tuple[str, ...]:
        return tuple(item["blueprint"] for item in self.checks if item["check"] == "blueprint")


@dataclass(frozen=True)
class Lesson:
    id: str
    order: int
    title: str
    tool: str
    fixture: str
    intro: str
    pattern: str
    steps: tuple[Step, ...]
    builds: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class Curriculum:
    lessons: tuple[Lesson, ...]
    scenarios: Mapping[str, Scenario]

    def lesson(self, lesson_id: str) -> Lesson:
        return next(lesson for lesson in self.lessons if lesson.id == lesson_id)


# Needs the herdr lessons add (registered here; the seat may fold them into checks.py).

@checks.need("tab_away", position=checks._int(1, 9), distance=checks._int(1, 8, 2))
def _tab_away(world, earlier, position, distance):
    """Tab `position` exists and is at least `distance` steps from the focused tab, wrapping:
    from next door, «go to tab N» and «next tab» leave the same state (M8)."""
    herdr = checks._herdr(world)
    if herdr.workspace is None or herdr.tab_position is None or len(herdr.workspace.tabs) < position:
        return False
    count, gap = len(herdr.workspace.tabs), abs(herdr.tab_position - position)
    return min(gap, count - gap) >= distance


@checks.need("tab_label_not", label=checks._text())
def _tab_label_not(world, earlier, label):
    """The focused tab does not already carry the label a rename step asks for."""
    herdr = checks._herdr(world)
    return herdr.tab is not None and herdr.tab.label != label


@checks.need("workspace_focused", label=checks._text())
def _workspace_focused(world, earlier, label):
    workspace = checks._herdr(world).workspace
    return workspace is not None and workspace.label == label


@checks.need("workspace_label_not", label=checks._text())
def _workspace_label_not(world, earlier, label):
    herdr = checks._herdr(world)
    return herdr.workspace is not None and herdr.workspace.label != label


# Loading and validation.

def load(folder: Path = FOLDER) -> Curriculum:
    """Read and validate every `*.toml` in the folder; raise CurriculumError listing every problem."""
    return loads({path.name: path.read_text(encoding="utf-8") for path in sorted(Path(folder).glob("*.toml"))})


def loads(files: Mapping[str, str]) -> Curriculum:
    problems: list[str] = []
    lessons: list[Lesson] = []
    scenarios: dict[str, Scenario] = {}
    for name, text in files.items():
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"{name}: not valid TOML: {exc}")
            continue
        extra = set(data) - {"lesson", "scenario"}
        if extra:
            problems.append(f"{name}: unknown top-level table {', '.join(sorted(extra))}")
        for item in data.get("scenario", ()):
            scenario = _scenario(name, item, problems)
            if scenario is None:
                continue
            if scenario.id in scenarios:
                problems.append(f"{name}: scenario {scenario.id}: id already used in {scenarios[scenario.id].source}")
            scenarios[scenario.id] = scenario
        for item in data.get("lesson", ()):
            if (lesson := _lesson(name, item, problems)) is not None:
                lessons.append(lesson)
    seen = {}
    for lesson in lessons:
        where = f"{lesson.source}: lesson {lesson.id}"
        if lesson.id in seen:
            problems.append(f"{where}: id already used in {seen[lesson.id]}")
        seen[lesson.id] = lesson.source
        _fixture(where, lesson.tool, lesson.fixture, problems)
        for step in lesson.steps:
            scenario = scenarios.get(step.scenario)
            if scenario is None:
                problems.append(f"{where}: step {step.kind}:{step.scenario} names no scenario")
            elif scenario.tool != lesson.tool:
                problems.append(f"{where}: scenario {scenario.id} is {scenario.tool}, the lesson is {lesson.tool}")
    orders = [lesson.order for lesson in lessons]
    for order in sorted({o for o in orders if orders.count(o) > 1}):
        problems.append(f"lessons share order {order}: "
                        + ", ".join(lesson.id for lesson in lessons if lesson.order == order))
    if problems:
        raise CurriculumError(problems)
    return Curriculum(tuple(sorted(lessons, key=lambda lesson: lesson.order)), scenarios)


def _copy(where, item, key, required, problems, limit=None):
    value = item.get(key)
    if value is None:
        if required:
            problems.append(f"{where}: missing {key}")
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: {key} must be non-empty text")
        return None
    if not value.isprintable():
        problems.append(f"{where}: {key} has control characters")
    limit = limit or LIMITS.get(key)
    if limit and len(value) > limit:
        problems.append(f"{where}: {key} is {len(value)} characters, at most {limit}")
    return value


def _keys(where, item, allowed, problems):
    for key, required in allowed.items():
        if required and key not in item:
            problems.append(f"{where}: missing {key}")
    extra = set(item) - set(allowed)
    if extra:
        problems.append(f"{where}: unknown key {', '.join(sorted(extra))}")


def _fixture(where, tool, fixture, problems):
    known = FIXTURES.get(tool)
    if fixture is not None and known is not None and fixture not in known:
        problems.append(f"{where}: unknown {tool} fixture {fixture!r}")


def _lesson(name, item, problems):
    where = f"{name}: lesson {item.get('id', '?')}"
    if not isinstance(item.get("id"), str):
        problems.append(f"{where}: missing id")
        return None
    _keys(where, item, LESSON_KEYS, problems)
    copy = {key: _copy(where, item, key, True, problems) for key in ("title", "intro", "pattern")}
    tool = item.get("tool")
    if tool not in TOOLS:
        problems.append(f"{where}: tool must be one of {', '.join(TOOLS)}")
    order = item.get("order")
    if type(order) is not int:
        problems.append(f"{where}: order must be an integer")
    fixture = item.get("fixture")
    if not isinstance(fixture, str):
        problems.append(f"{where}: fixture must be a name")
    steps = []
    raw_steps = item.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        problems.append(f"{where}: steps must be a non-empty list")
        raw_steps = []
    for raw in raw_steps:
        kind, _, scenario = raw.partition(":") if isinstance(raw, str) else ("", "", "")
        if kind not in KINDS or not scenario:
            problems.append(f"{where}: step {raw!r} must be learn:, recall: or build: and a scenario id")
            continue
        steps.append(Step(kind, scenario))
    builds = item.get("builds", [])
    if not isinstance(builds, list) or len(builds) > 3 or not all(isinstance(line, str) for line in builds):
        problems.append(f"{where}: builds must be up to three lines of text")
        builds = []
    for line in builds:
        if len(line) > LIMITS["builds"]:
            problems.append(f"{where}: builds line is {len(line)} characters, at most {LIMITS['builds']}")
    if None in copy.values() or tool not in TOOLS or type(order) is not int or not isinstance(fixture, str):
        return None
    return Lesson(item["id"], order, copy["title"], tool, fixture, copy["intro"], copy["pattern"],
                  tuple(steps), tuple(builds), name)


def _scenario(name, item, problems):
    where = f"{name}: scenario {item.get('id', '?')}"
    if not isinstance(item.get("id"), str):
        problems.append(f"{where}: missing id")
        return None
    _keys(where, item, SCENARIO_KEYS, problems)
    before = len(problems)
    copy = {key: _copy(where, item, key, True, problems) for key in ("goal", "why", "nudge", "done")}
    cards = item.get("cards", [])
    if not isinstance(cards, list) or not cards or not all(isinstance(c, str) for c in cards):
        problems.append(f"{where}: cards must be a non-empty list of card ids")
        cards = []
    for card in cards:
        _card(where, card, problems)
    tools = {card.partition(":")[0] for card in cards}
    if len(tools) > 1:
        problems.append(f"{where}: cards mix tools {', '.join(sorted(tools))}")
    tool = next(iter(tools), "herdr")

    raw_checks = item.get("checks", [])
    if not isinstance(raw_checks, list) or not raw_checks or not all(isinstance(c, dict) for c in raw_checks):
        problems.append(f"{where}: checks must be a non-empty list of tables")
        raw_checks = []
    texts = [_copy(where, c, "checklist", False, problems) for c in raw_checks]
    plain = [{k: v for k, v in c.items() if k != "checklist"} for c in raw_checks]
    needs = item.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(n, dict) for n in needs):
        problems.append(f"{where}: needs must be a list of tables")
        needs = []
    try:
        checks.validate(plain, needs)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")

    solution = _moves(where, "solution", item.get("solution"), problems, required=True)
    setup = _moves(where, "setup", item.get("setup", []), problems)
    decoy = item.get("decoy")
    decoy = _moves(where, "decoy", [decoy] if isinstance(decoy, str) else decoy or [], problems)
    for move in (*solution, *setup, *decoy):
        if move.card and move.card.partition(":")[0] != tool:
            problems.append(f"{where}: {move.card} is not a {tool} card")
    fixture = item.get("fixture")
    if fixture is not None and not isinstance(fixture, str):
        problems.append(f"{where}: fixture must be a name")
        fixture = None
    _fixture(where, tool, fixture, problems)
    typing = item.get("typing", False)
    if type(typing) is not bool:
        problems.append(f"{where}: typing must be true or false")
    goal, template = copy["goal"], ""
    if goal and "{number}" in goal:
        numbers = {m.number for m in solution if m.card and m.number}
        if len(numbers) != 1:
            problems.append(f"{where}: a goal with {{number}} needs one numbered card in the solution")
        else:
            template, goal = goal, goal.format(number=numbers.pop())
            if len(template.format(number=10)) > LIMITS["goal"]:
                problems.append(f"{where}: goal is too long with a two-digit number")
    elif goal and ("{" in goal or "}" in goal):
        problems.append(f"{where}: goal may hold {{number}} and no other braces")
    if len(problems) > before:
        return None
    return Scenario(item["id"], tool, goal, copy["why"], copy["nudge"], copy["done"], tuple(cards),
                    tuple(plain), tuple(texts), solution, fixture, tuple(needs), decoy, setup, typing, name,
                    template)


def _card(where, card, problems):
    if not isinstance(card, str) or not CARD.fullmatch(card):
        problems.append(f"{where}: {card!r} is not a card id (<tool>:<action>, tool herdr | yazi | desktop)")
        return False
    return True


def _moves(where, key, items, problems, required=False):
    if items is None or not isinstance(items, list) or (required and not items):
        problems.append(f"{where}: {key} must be a {'non-empty ' if required else ''}list")
        return ()
    moves = []
    for raw in items:
        if isinstance(raw, str):
            if _card(f"{where}: {key}", raw, problems):
                moves.append(Move(card=raw))
            continue
        if not isinstance(raw, dict) or set(raw) - MOVE_KEYS:
            problems.append(f"{where}: {key} item {raw!r} must be a card id or a table of "
                            f"{', '.join(sorted(MOVE_KEYS))}")
            continue
        kinds = [k for k in ("card", "press", "type", "wait") if k in raw]
        if len(kinds) != 1:
            problems.append(f"{where}: {key} item {raw!r} needs exactly one of card, press, type, wait")
            continue
        number, repeat, wait = raw.get("number"), raw.get("repeat", 1), raw.get("wait", 0.0)
        ok = True
        if number is not None and not (type(number) is int and 1 <= number <= 9 and "card" in raw):
            problems.append(f"{where}: {key} item {raw!r}: number must be 1..9 on a card")
            ok = False
        if not (type(repeat) is int and 1 <= repeat <= 20) or ("repeat" in raw and "press" not in raw):
            problems.append(f"{where}: {key} item {raw!r}: repeat must be 1..20 on a press")
            ok = False
        if not (isinstance(wait, (int, float)) and 0 <= wait <= 5):
            problems.append(f"{where}: {key} item {raw!r}: wait must be 0..5 seconds")
            ok = False
        if "card" in raw and not _card(f"{where}: {key}", raw["card"], problems):
            ok = False
        for text_key in ("press", "type"):
            if text_key in raw and not (isinstance(raw[text_key], str) and raw[text_key].isprintable()
                                        and 0 < len(raw[text_key]) <= 40):
                problems.append(f"{where}: {key} item {raw!r}: {text_key} must be short printable text")
                ok = False
        if ok:
            moves.append(Move(raw.get("card"), number, raw.get("press"), repeat, raw.get("type"), float(wait)))
    return tuple(moves)


# Resolution against the user's keys.

# Per keyboard layout, the punctuation that needs Shift, AltGr or a dead key to type
# (`no`: xkb `symbols/no`). Letters and written-out «Shift + x» chords are the same
# everywhere, so they never count.
LAYOUTS = {
    "no": ("Norwegian", frozenset('!"#$%&()*/:;=>?@[]^_`{}~')),
    "us": ("US", frozenset()),
}
DEFAULT_LAYOUT = "no"


def awkward_on(chord: str, layout: str) -> bool:
    """True when a key of `chord` («prefix+;», Yazi's «g, g») is hard to type on `layout`."""
    hard = LAYOUTS[layout][1]
    return any(canonical(step).removeprefix("prefix+").split("+")[-1] in hard for step in chord.split(", "))


def chord_text(chord: str, prefix: str = "", number: int | None = None) -> str:
    """Spell a rigpage chord for the screen: «Ctrl + Alt + ←», «Ctrl + Space → Shift + c»."""
    def one(part):
        if part.startswith("prefix+"):
            rest = one(part.removeprefix("prefix+"))
            return (one(prefix) + KEYS["then"] + rest) if prefix else rest
        pieces = part.split("+") if not part.endswith("++") else part[:-2].split("+") + ["+"]
        shown = []
        for piece in pieces:
            if piece == "1..9":
                shown.append(str(number) if number else KEYS["range"])
            else:
                shown.append(KEYS.get(piece.lower(), piece) if len(piece) > 1 else piece)
        return KEYS["plus"].join(shown)

    return one(chord.strip())


@dataclass(frozen=True)
class Binding:
    """The user's chords for one card. `chords` are the usable ones (rigpage spelling), taught first."""

    card: str
    chords: tuple[str, ...]
    prefix: str
    practice: Card
    ambiguous: tuple[str, ...] = ()  # bound, but legacy cannot send them apart: never taught
    awkward_on: str = ""  # the layout's name when even the taught chord is hard to type on it

    @property
    def taught(self) -> str:
        return self.chords[0]

    def label(self, number=None, chord=None) -> str:
        return chord_text(chord or self.taught, self.prefix, number)

    def labels(self, number=None) -> tuple[str, ...]:
        return tuple(chord_text(chord, self.prefix, number) for chord in self.chords)


@dataclass(frozen=True)
class ScenarioState:
    scenario: Scenario
    active: bool
    reason: str = ""
    missing: tuple[str, ...] = ()  # cards with no key at all: the step is a notice on how to add one

    @property
    def notice(self) -> bool:
        return not self.active and bool(self.missing)


@dataclass(frozen=True)
class LessonState:
    lesson: Lesson
    visible: bool
    steps: tuple[Step, ...]  # the steps whose scenario is active
    active: int
    total: int
    plan: tuple[Step, ...] = ()  # the steps a lesson run plays: active ones and notices, in order
    notices: int = 0


@dataclass
class Resolved:
    curriculum: Curriculum
    bindings: dict[str, Binding]
    scenarios: dict[str, ScenarioState]
    lessons: list[LessonState] = field(default_factory=list)
    data: Mapping = field(default_factory=dict)  # the keys resolved against (for notices)

    def lesson(self, lesson_id: str) -> LessonState:
        return next(state for state in self.lessons if state.lesson.id == lesson_id)

    @property
    def visible(self) -> list[LessonState]:
        return [state for state in self.lessons if state.visible]

    def active(self, scenario_id: str) -> bool:
        state = self.scenarios.get(scenario_id)
        return bool(state and state.active)

    def actions(self, scenario_ids) -> tuple[str, ...]:
        """The bound cards a private session must bind to run these scenarios."""
        found = [card for sid in scenario_ids for card in self.curriculum.scenarios[sid].played_cards
                 if card in self.bindings]
        return tuple(dict.fromkeys(found))


def _herdr_chords(data, action):
    rows = rows_for(data, "herdr")
    prefix = next((row["name"] for row in rows if row.get("value") == "prefix"), "")
    return tuple(herdr_backend.selected_bindings(rows, {action}).get(action, ())), prefix


def _yazi_chords(data, action):
    try:
        from yazi_backend import yazi_bindings
        found = yazi_bindings(rows_for(data, "yazi")).get(action, ())
    except Exception:  # the Yazi table is another piece's; an unreadable one only deactivates
        found = ()
    return tuple(chord for chord, *_ in found), ""


def _desktop_chords(data, action):
    return desktop_session.desktop_chords(data, action), ""


def _strata_chords(data, action):
    return strata_keys.chords(rows_for(data, "strata"), action), ""


def _shell_chords(data, action):
    return shell_backend.shell_bindings(rows_for(data, "shell")).get(action, ()), ""


RESOLVERS: dict[str, Callable] = {"herdr": _herdr_chords, "yazi": _yazi_chords, "shell": _shell_chords,
                                  "strata": _strata_chords, "desktop": _desktop_chords}


def _bind(data, card, ambiguous, layout):
    tool, _, action = card.partition(":")
    resolver = RESOLVERS.get(tool)
    if resolver is None:
        return None, REPORT["no_tool"].format(tool=tool)
    chords, prefix = resolver(data, action)
    if not chords:
        return None, REPORT["unbound"].format(card=card)
    # Hyprland reads desktop chords itself, never through the terminal: no terminal limit applies.
    # Strata counts Enter for a chord the terminal keeps, so such a chord needs no terminal either.
    kept = strata_keys.TERMINAL_KEEPS if tool == "strata" else frozenset()
    usable = (list(chords) if tool == "desktop"
              else [chord for chord in chords if chord in kept or not ambiguous(chord)])
    if not usable:
        return None, REPORT["ambiguous"].format(card=card, chords=", ".join(chords))
    # Teach a chord that is easy on the user's layout, then the direct one (no prefix), as
    # the keymap's Alt layer does; in legacy keyboard mode an ambiguous chord is never taught
    # and its prefix twin takes its place.
    taught = min(usable, key=lambda chord: (awkward_on(chord, layout), chord.startswith("prefix+")))
    hard = LAYOUTS[layout][0] if awkward_on(taught, layout) else ""
    ordered = (taught, *(chord for chord in usable if chord != taught))
    # History's card key keeps today's spelling (`ctrl+space → h`), so old progress carries over.
    shortcuts = tuple(shortcut_text(chord, prefix) for chord in chords)
    practice = Card(TRACKS[tool], action, action.replace("_", " "), shortcuts)
    return Binding(card, ordered, prefix, practice, tuple(c for c in chords if c not in usable), hard), ""


def _select_quake(curriculum, data):
    """Keep the user's Quake terminal when bound, otherwise teach Omarchy's console."""
    quake = curriculum.scenarios.get("desktop-quake")
    if quake is None or desktop_session.desktop_chords(data, "toggle_quake"):
        return curriculum
    card = "desktop:toggle_qconsole"
    console = replace(quake, goal=DESKTOP["console_goal"], cards=(card,),
                      checks=tuple({**check, "special": "qconsole"} for check in quake.checks),
                      solution=tuple(replace(move, card=card) for move in quake.solution))
    return replace(curriculum, scenarios={**curriculum.scenarios, quake.id: console})


def resolve(curriculum: Curriculum, data: Mapping, ambiguous: Callable[[str], bool] | None = None, *,
            desktop: bool = False, layout: str = DEFAULT_LAYOUT) -> Resolved:
    """Bind every card to the user's chords in a keys payload (`practice.read_keys`, `keyscan.scan`).

    `ambiguous(chord)` is `router.KeyboardMode.ambiguous`: True for a chord a legacy
    terminal cannot send apart; such a chord is never taught and never played. Desktop cards
    are exempt: Hyprland receives their Super chords, not the terminal.
    `desktop` says the user's Hyprland is running (`hypr.HyprObserver.running()`); without it
    every desktop scenario is inactive, so the live lesson is hidden. Any scenario is inactive
    too when the keys have no rows at all for its tool (the tool is missing or unreadable).
    A scenario whose cards include one with no key at all is a notice (`ScenarioState.notice`):
    the lesson shows how to add that key, and a lesson counts its notices toward being shown.
    """
    curriculum = _select_quake(curriculum, data)
    ambiguous = ambiguous or (lambda chord: False)
    bindings, unbound, nokey = {}, {}, set()
    for scenario in curriculum.scenarios.values():
        for card in scenario.played_cards:
            if card in bindings or card in unbound:
                continue
            binding, reason = _bind(data, card, ambiguous, layout)
            if binding:
                bindings[card] = binding
            else:
                unbound[card] = reason
                if reason == REPORT["unbound"].format(card=card):
                    nokey.add(card)
    states = {}
    for scenario in curriculum.scenarios.values():
        needed = [m.card for m in (*scenario.solution, *scenario.setup) if m.card]
        wanted = tuple(dict.fromkeys((*scenario.cards, *needed)))
        missing = next((card for card in wanted if card in unbound), None)
        if scenario.tool == "desktop" and not desktop:
            states[scenario.id] = ScenarioState(scenario, False, REPORT["no_hyprland"])
            continue
        if not rows_for(data, scenario.tool):
            states[scenario.id] = ScenarioState(scenario, False, REPORT["no_keys"].format(tool=TRACKS[scenario.tool]))
            continue
        blocked = any(card in unbound and card not in nokey for card in wanted)  # the terminal, not the user
        states[scenario.id] = ScenarioState(scenario, missing is None, unbound.get(missing, ""),
                                            () if blocked else tuple(card for card in wanted if card in nokey))
    resolved = Resolved(curriculum, bindings, states, data=data)
    for lesson in curriculum.lessons:
        used = list(dict.fromkeys(step.scenario for step in lesson.steps))
        active = [sid for sid in used if states[sid].active]
        notices = [sid for sid in used if states[sid].notice]
        steps = tuple(step for step in lesson.steps if states[step.scenario].active)
        plan = tuple(step for step in lesson.steps if states[step.scenario].active or states[step.scenario].notice)
        resolved.lessons.append(LessonState(lesson, len(active) + len(notices) >= 2, steps, len(active), len(used),
                                            plan, len(notices)))
    return resolved


def report(resolved: Resolved) -> list[str]:
    """Every lesson and scenario, active or inactive with the reason (for `keys-trainer check`)."""
    lines = []
    listed = set()
    for state in resolved.lessons:
        lesson = state.lesson
        lines.append(REPORT["lesson"].format(order=lesson.order, title=lesson.title, tool=lesson.tool,
                                             state=REPORT["visible" if state.visible else "hidden"],
                                             active=state.active, total=state.total))
        for sid in dict.fromkeys(step.scenario for step in lesson.steps):
            listed.add(sid)
            lines.append(_scenario_line(sid, resolved.scenarios[sid]))
    for sid, item in resolved.scenarios.items():
        if sid not in listed:
            lines.append(_scenario_line(sid, item))
    return lines


def _scenario_line(sid, item):
    if item.active:
        return REPORT["active"].format(id=sid)
    return REPORT["notice" if item.notice else "inactive"].format(id=sid, reason=item.reason)
