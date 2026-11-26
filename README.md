# Keys Trainer

A terminal trainer that teaches the shortcuts from your own setup (herdr, Yazi, Strata, Hyprland)
as short guided lessons. herdr and Yazi are practised in private sessions that the trainer starts
and stops itself, so your own sessions are never touched. Strata is practised as key presses in the
trainer, and the desktop lesson only watches your real Hyprland.

## Get started on Omarchy

```sh
git clone https://github.com/ikkeseb/keys-trainer.git
cd keys-trainer
./keys-trainer
```

Start it from a plain terminal (Super + Enter), not from inside herdr: herdr would swallow the
chords before the trainer sees them. Started inside herdr, it says so and offers to open itself in
its own `foot` window.

It needs Python 3.11+ (Omarchy has it) and `herdr`; `yazi` and `strata` are needed for their own
lessons. Standard library only, nothing to install with pip.

The private Yazi can only write inside its own temporary folder (Linux Landlock, which Omarchy's
kernel has); without it, or before Linux 6.2, the Yazi lessons do not start.

**Your own keys.** At every launch the trainer reads the shortcuts you actually have: herdr's
config (`~/.config/herdr/config.toml`) plus herdr's defaults for what it leaves alone, Yazi's
keymap (`~/.config/yazi/keymap.toml`) plus Yazi's built-in one, Strata's `[keymap.default]`
(`~/.config/strata/settings.toml`) over its default keys, and Hyprland's live binds. Change
a binding and the next launch teaches the new one. A step whose action you have no key for shows a
short notice with how to add one, and `s` skips it.

**The start check.** Before Home it checks Python, herdr, yazi, Strata, Hyprland and foot. Anything
missing gets one line: what it means, the `pacman` command that installs it, and which lessons are
off because of it. herdr missing stops the trainer; everything else only turns lessons off. With
nothing missing it goes straight in. `./keys-trainer check` prints the full report any time.

### Setting it up with an agent

Point your coding agent at this paragraph: clone `https://github.com/ikkeseb/keys-trainer.git`,
run `./keys-trainer check` in the clone and read its Tools section. For each missing tool, install
it with the `sudo pacman -S <package>` line it prints (ask the user before any `sudo`). Do not
edit the user's herdr, Yazi, Strata or Hyprland config unless they ask; a missing key is shown inside the
trainer with the line to add. Then tell the user to run `./keys-trainer` from a terminal outside
herdr. The trainer never touches the user's own herdr sessions: it starts private ones and stops
them itself.

## A session

1. **Home** shows the lessons, your stars and today's plan. `↑ ↓` picks a lesson, Enter starts,
   `l` opens the layout workshop, `q` ends the day.
2. **Warm-up.** If shortcuts you have met are due for review, a short warm-up runs first, one
   private session per tool.
3. **Lesson.** An intro card, then the lesson in the private herdr's own terminal (for Strata, a
   key card) with a coach bar underneath. Each step is one of three kinds:
   - **LEARN**: the goal and your keys are shown; do it once.
   - **RECALL**: the goal only; do it from memory.
   - **BUILD**: reach a target shape with several moves.

   The trainer checks what actually changed in the session and moves on by itself. Press `?` once to
   see the keys. Press it again and the trainer does the step for you and moves on; that step comes
   back later in the lesson for you to do yourself. `q` pauses (resume, skip, redo or leave).
   **Keyboard layout.** The default is Norwegian (`--layout no`; `us` turns this off). When a
   shortcut has several chords, one that is easy to type on the layout is taught. A step whose
   taught key still needs Shift, AltGr or a dead key says so, and `s` skips it (another free key if
   your session binds `s`).
4. **Lesson complete** shows how each step went and the stars. Enter goes to the next lesson, `r`
   replays, `q` ends the session with a summary of what you practised and what comes back when.

The lessons cover herdr panes, tabs and jumping around; Yazi moving, selecting, copying paths, file
tricks and tabs; Strata navigation; and the desktop.

**Strata** is a window, so the trainer cannot look inside it: a step is done when you press your
key for it. A terminal usually keeps Ctrl + Shift + C for itself, so Enter counts for that chord.
With Strata's type-to-search on (its default), a plain letter such as `y` is no shortcut there.

Lesson 13, “Desktop (live)”, is the exception: it runs on your real Hyprland desktop, marked
`LIVE`. The trainer only watches Hyprland's events there and never sends a key, so `?` shows the
keys but cannot do the step for you. It is hidden when Hyprland is not running.

## Commands

| Command | What it does |
| --- | --- |
| `./keys-trainer` | Home and the guided path. |
| `./keys-trainer layouts` | The layout workshop: pick a blueprint, build it in a private herdr with your splits. |
| `./keys-trainer check` | What this terminal reports, which tools are present, and every card's chords. |
| `./keys-trainer demo` | Lesson 1 plays itself in a terminal. Piped: one result line per scenario solution. |
| `./keys-trainer demo --all` | The whole self-test, decoys and chains included. |
| `./keys-trainer --lesson ID` | Start at one lesson's intro (ids are in the curriculum files, e.g. `split`). |
| `./keys-trainer --keys FILE` | Read your keys from a JSON export (the `rigpage keys` format) instead of scanning. |
| `./keys-trainer --layout us` | Your keyboard layout: `no` (default) or `us`. |

Progress (spaced review per shortcut, lesson stars, layout bests) lives in
`$XDG_STATE_HOME/keys-trainer/practice.sqlite3` (default `~/.local/state`). Older files are
migrated in place; rows are never dropped.

## Curriculum

Lessons are data in `curriculum/*.toml`, one file per tool. A `[[lesson]]` lists its steps; a
`[[scenario]]` has the shown text (English), the cards it practises, a start state (`fixture`,
`needs`), the `checks` that count as done, a `solution` and a `decoy`. A scenario is verified
against the observed state of the private session, not against the keys pressed: the checks in
`checks.py` (herdr) and `checks_yazi.py` (Yazi) are pure functions over that state. The self-test
(`demo`) plays every solution with your real chords and requires it to pass, and plays every decoy
and requires it to fail. Shown copy lives in the curriculum files and `strings.py`.

## Checks

```sh
python3 -m unittest -q
KEYS_TRAINER_TEST_HERDR=1 KEYS_TRAINER_TEST_YAZI=1 python3 -m unittest -q
./keys-trainer demo --all
```

The first is the fast suite. The second adds the live suites, which start private herdr and Yazi
processes (own socket, home and config under `/tmp`) and stop them afterwards.

Hard rule: tests and demos never start a compositor (Hyprland, Sway or any other display server)
and never send keys or API calls to your real herdr session. Hyprland is exercised through fixtures
only.

## License

MIT, see `LICENSE`.
