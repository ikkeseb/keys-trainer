"""Every fixed string the trainer shows, in one table per module (decision 2).

Lesson copy lives in `curriculum/*.toml`; this file holds the rest, so a language swap
is these tables plus the curriculum files. Keys and values are English. Shown quotes are
“…”; terminal terms follow the tools (herdr: pane, tab, workspace, prefix; Yazi: folder,
filter, selection, hovered file; Hyprland: workspace, special workspace).
"""

# What changed between two looks at the private session (`world.describe`).
WORLD = {
    "workspace_new": "A new workspace opened.",
    "workspace_closed": "A workspace closed.",
    "workspace_focus": "You switched workspace.",
    "workspace_label": "The workspace is now called “{label}”.",
    "tab_new": "A new tab opened.",
    "tab_closed": "A tab closed.",
    "tab_focus": "You switched to tab {position}.",
    "tab_label": "The tab is now called “{label}”.",
    "tab_moved": "The tab moved.",
    "split_down": "The pane split into two rows.",
    "split_right": "The pane split into two columns.",
    "pane_new": "A new pane opened.",
    "pane_closed": "A pane closed.",
    "zoom_on": "The pane is zoomed.",
    "zoom_off": "Zoom is off.",
    "focus_left": "Focus moved left.",
    "focus_right": "Focus moved right.",
    "focus_up": "Focus moved up.",
    "focus_down": "Focus moved down.",
    "focus_other": "Focus moved to another pane.",
    "wider": "The pane got wider.",
    "narrower": "The pane got narrower.",
    "taller": "The pane got taller.",
    "shorter": "The pane got shorter.",
    "yazi_folder": "You are now in the {name} folder.",
    "yazi_hover": "The hovered file is now {name}.",
    "yazi_selected": "The selection changed.",
    "yazi_hidden_on": "Hidden files are shown.",
    "yazi_hidden_off": "Hidden files are no longer shown.",
    "yazi_filter": "The filter changed.",
    "yazi_copied": "That copied {text}.",
    "yazi_search": "Search results for “{query}”.",
    "yazi_opened": "That opened {name} ({opener}).",
    "yazi_tabs": "You are in tab {position} of {count}.",
    "shell_cwd": "You are now in the {name} folder.",
    "shell_cmd": "Ran {cmd}.",
    "shell_widget": "Used the {widget} picker.",
    "shell_tool": "{tool} matched {count} files.",
    "shell_opened": "That opened {name} ({opener}).",
    "pressed": "That was {key}.",
    "desktop_workspace": "You are on workspace {number}.",
    "desktop_special_on": "{name} opened.",
    "desktop_special_off": "{name} closed.",
    "special_quake": "The Quake terminal",
    "special_qconsole": "The Quake console",
    "special_other": "The special workspace {name}",
}

# A key-press practice (Strata): the card the lesson shows in place of a terminal.
KEYS_ONLY = {"header": "PRESS THE KEY", "note": "Press it here. Nothing in Strata is opened or changed."}

# How a chord is spelled on screen: «Ctrl + Alt + ←», «Ctrl + Space → h».
KEYS = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super",
    "left": "←", "right": "→", "up": "↑", "down": "↓",
    "enter": "Enter", "esc": "Esc", "space": "Space", "tab": "Tab", "backspace": "Backspace",
    "home": "Home", "end": "End", "pageup": "PgUp", "pagedown": "PgDn", "delete": "Delete",
    "plus": " + ", "then": " → ", "range": "1…9",
}

# The lesson engine's bar copy (lesson.py).
LESSON = {
    "tag_learn": "LEARN",
    "tag_recall": "RECALL",
    "tag_build": "BUILD",
    "tag_done": "✓",
    "tag_pause": "PAUSE",
    "tag_live": "LIVE",
    "live_line": "This happens on your real desktop. Back here: {keys}.",
    "live_only": "This happens on your real desktop.",
    "label_keys": "Show keys",
    "label_show": "Show me",
    "keys": "Keys · {keys}",
    "watch": "Watch: {keys}",
    "miss_here": "{what} Try again from here.",
    "miss_rebuild": "{what} I'll set it up again, then you try.",
    "miss_build": "{what} That won't reach the target. Setting it up again.",
    "miss_unknown": "Something else happened.",
    "enter_fallback": "{line}{gap}Nothing happens? Press Enter.",  # a key the terminal may keep (Strata)
    "or": " or ",
    "then": ", then ",
    "list": ", ",
    "times": " ×{count}",
    "clear": "clear",
    "type": "type {text}",
    "seconds": "{seconds} s",
    "first_clean": "{time} · first time without a hint",
    "record": "{time} · new record · was {best}",
    "also": "Also bound to {keys}",
    "with_help": "Shown this time. It comes back soon for you to do yourself.",
    "shown": "Shown this time.",
    "shown_keys": "Keys · {keys} · {remark}",
    "nudged": "{time} · with a nudge",
    "check_done": "✓",
    "check_todo": "○",
    "next": "Next: {what}",
    "warmup_title": "Warm-up",
    "warmup_done": "{title} done · {clean} of {total} without a hint",
    "round_title": "Today's round",
    "lesson_done": "Lesson complete",
    "pause_goal": "Pause",
    "pause_line": "The terminal stays as you left it.",
    "pause_resume": "Resume",
    "pause_skip": "Skip",
    "pause_redo": "Redo step",
    "pause_quit": "Leave lesson",
    "typing": "Typing · Enter confirms · Esc cancels",
    "awkward": "Awkward on a {layout} keyboard · {key} skips it",
}

# A notice step (lesson.Engine.notice_card and its bar): the lesson reached an action the user
# has no key for. {action} is the action, {tool} herdr, Yazi or Hyprland, {path} the config file.
NOTICE = {
    "tag": "NO KEY",
    "header": "NO KEY IN YOUR SETUP",
    "line": "No key for {actions} in your setup · {key} skips it",
    "missing": "Your {tool} setup has no key for “{action}”, so this step cannot run.",
    "where_herdr": "To add one, put this line under [keys] in {path} (in place of any line for it already there):",
    "where_yazi": "To add one, put this in prepend_keymap under [mgr] in {path}:",
    "desktop": "To add one, add a bind for “{action}” to your Hyprland bindings.",
    "where_strata": "To add one, put this line under [keymap.default] in {path} (or set it in Strata's Settings → Keyboard):",
    "where_shell": "To add one, wire it in {path} (or install the tool):",
    "taken": "{tool}'s own key for it, {chord}, is already used for {taken} here: pick a free key.",
    "placeholder": "Replace <key> with a free key.",
    "after": "The trainer reads your keys when it starts, so the step runs next time.",
    "skip": "{key}  Skip",
    "pause": "{key}  Pause",
    "tools": {"herdr": "herdr", "yazi": "Yazi", "shell": "Shell", "strata": "Strata", "desktop": "Hyprland"},
}

# BUILD next-move phrases, after “Next: ”. {where} names a pane, {text} typed text.
MOVES = {
    "split_horizontal": "split {where} into two rows",
    "split_vertical": "split {where} into two columns",
    "focus": "move focus to {where}",
    "close_pane": "close the focused pane",
    "new_tab": "open a new tab",
    "rename_tab": "name the tab {text}",
    "switch_tab": "jump to tab {number}",
    "next_tab": "go to the next tab",
    "previous_tab": "go to the previous tab",
    "new_workspace": "create a new workspace",
    "rename_workspace": "name the workspace {text}",
    "close_workspace": "close the workspace and confirm",
    "next_workspace": "go to the next workspace",
    "previous_workspace": "go to the previous workspace",
    "zoom": "zoom the pane",
    "last_pane": "jump back to the last pane",
    "resize": "move the divider",
    "other": "{keys}",
}
PLACES = {
    "focused": "this pane",
    "left": "the left pane", "right": "the right pane",
    "top": "the top pane", "bottom": "the bottom pane",
    "top-left": "the top-left pane", "top-right": "the top-right pane",
    "bottom-left": "the bottom-left pane", "bottom-right": "the bottom-right pane",
    "other": "the other pane",
}

# `keys-trainer check`: the curriculum part (curriculum.report).
REPORT = {
    "lesson": "{order}. {title} ({tool}): {state}, {active} of {total} scenarios active",
    "visible": "shown",
    "hidden": "hidden: fewer than two scenarios it can run",
    "active": "  active    {id}",
    "inactive": "  inactive  {id}: {reason}",
    "notice": "  no key    {id}: {reason}; the lesson shows how to add it",
    "unbound": "you have no key for {card}",
    "no_keys": "no {tool} keys were found, so {tool} is missing or unreadable",
    "ambiguous": "{card} has only {chords}, which the terminal cannot tell apart without the kitty protocol",
    "no_tool": "no lookup for the tool {tool} yet",
    "no_hyprland": "Hyprland is not running, so the real desktop cannot be practised here",
}

# The shell: Home, lesson intro, lesson complete, session end, check (trainer.py).
# Counted words are (one, many) pairs.
SHELL = {
    "brand": "KEYS TRAINER",
    "minutes": "≈ {minutes} min",
    "first_right": "first time · ≈ {minutes} min",
    "first_title": "Learn your shortcuts by using them",
    "first_private": ("You practise in a private herdr and a private Yazi. Tabs and panes really change "
                      "in there, but your own session and files are never touched. Strata keys are "
                      "only pressed here."),
    "first_how_label": "How it works:",
    "first_how": ("I show a shortcut  →  you press it  →  you recall it unaided  →  "
                  "you build something real with it."),
    "first_stuck": "Stuck? Press {key} to see the keys. It is always shown at the bottom.",
    "back_title": "Welcome back",
    "last_today": "last practised today",
    "last_yesterday": "last practised yesterday",
    "last_days": "last practised {days} days ago",
    "today": "Today",
    "today_warmup": "Warm-up",
    "today_warmup_what": "{count} {shortcuts} due for review",
    "today_lesson": "Lesson {number}",
    "path": "The path",
    "note_start": "start here",
    "note_next": "next",
    "note_one_star": "one to go for three stars",
    "note_two_stars": "two to go for three stars",
    "note_tool": "needs {tool}",
    "home_start_first": "Enter  Start lesson {number}",
    "home_start": "Enter  Start",
    "home_pick_first": "↑ ↓  Choose",
    "home_pick": "↑ ↓  Choose lesson",
    "home_layouts": "l  Layout workshop",
    "home_quit": "q  Quit",
    "herdr_line": ("You are inside herdr, so the shortcuts would hit your real session. Open a "
                   "terminal without herdr (Super + Enter) and run keys-trainer there."),
    "herdr_window": "Enter  Open in its own window",
    "intro_header": "LESSON {number} OF {total}",
    "intro_pattern": "Pattern",
    "intro_learn": "You learn",
    "intro_build": "Then you build",
    "intro_start": "Enter  Start",
    "intro_test": "t  I know this: test me",
    "intro_back": "Esc  Back",
    "intro_live": ("LIVE · This lesson happens on your real desktop. The keys work as usual; the "
                   "trainer presses nothing, it only watches."),
    "live_now": "Now on your real desktop",
    "live_go": "Enter  Continue",
    "live_skip": "Esc  Skip",
    "round_what": "{count} {shortcuts}",
    "round_build": "{what} and one build",
    "round_only_build": "one build",
    "done_header": "LESSON {number} COMPLETE",
    "done_build": "Build",
    "done_no_key": "No key: {actions}",
    "done_skipped": "skipped",
    "done_clean": "no hint",
    "done_nudged": "with a nudge",
    "done_shown": "shown",
    "done_then": "{first}, then {final}",
    "done_again": "back in the warm-up",
    "star_full": "Three stars. From now on the warm-up keeps them fresh.",
    "star_one_more": "One to go for three stars: “{goal}” without a hint.",
    "star_two_more": "Next star: the whole lesson without “Show me”.",
    "done_next": "Enter  Next: {title} ({minutes})",
    "done_finish": "Enter  Done for today",
    "done_replay": "r  Replay the lesson",
    "done_quit": "q  Done for today",
    "end_header": "DONE FOR TODAY",
    "end_minutes": "{minutes} min",
    "end_today": "Today",
    "end_today_line": "{practised} {shortcuts} · {clean} without a hint · {lessons} {lesson_word} complete",
    "end_new": "New today",
    "end_more": "Needs more work",
    "end_fastest": "Fastest",
    "end_path": "The path",
    "end_path_line": "{dots}   {done} of {total} lessons",
    "end_back": "Coming back",
    "end_next": "Next time",
    "end_quit": "Enter  Quit",
    "due_now": "{count} today",
    "due_tomorrow": "{count} tomorrow",
    "due_days": "{count} in {days} days",
    "next_warmup": "Warm-up ({count}), then lesson {number} · {title}",
    "next_lesson": "Lesson {number} · {title}",
    "error_header": "THE SESSION DID NOT START",
    "error_back": "Enter  Back",
    # Error-card headlines over an exception's own message.
    "error_session": "The private practice session could not run.",
    "error_timeout": "The private practice session stopped answering.",
    "error_layouts": "The layout workshop could not run.",
    "shortcuts": ("shortcut", "shortcuts"),
    "lessons": ("lesson", "lessons"),
    "list": "  ·  ",
    # Errors and the CLI.
    "cli_description": "Learn your own shortcuts, mostly in private herdr and Yazi sessions: Home, lessons, warm-up.",
    "cli_mode": ("layouts: the layout workshop; check: what this terminal and your keys allow; "
                 "demo: watch lesson 1 play itself (piped: one line per scenario)"),
    "cli_keys": "read your keys from a JSON export (rigpage keys format) instead of scanning",
    "cli_lesson": "start at this lesson's intro",
    "cli_all": "with demo: the whole self-test",
    "cli_layout": "your keyboard layout; steps whose keys are awkward on it say so (default: %(default)s)",
    "cli_unknown_mode": "unknown mode {mode} (choose layouts, check or demo)",
    "cli_all_demo": "--all goes with demo",
    "cli_lesson_mode": "--lesson starts the guided path; leave out the mode",
    "needs_tty": "The trainer needs an interactive terminal. Without one: keys-trainer check or keys-trainer demo.",
    "layouts_tty": "The layout workshop needs an interactive terminal.",
    "no_herdr": "herdr is missing. Install it with: sudo pacman -S herdr",
    "no_selftest": "The self-test is not in place yet (selftest.py is missing).",
    "no_lessons": "No lesson fits your shortcuts yet. See keys-trainer check.",
    "unknown_lesson": "There is no lesson called {id}. The lessons: {ids}.",
    "hidden_lesson": "Lesson {id} is hidden with your shortcuts: fewer than two scenarios are active. See keys-trainer check.",
    "check_terminal": "Terminal",
    "check_tools": "Tools",
    "check_tool": "  {tool}: {path}",
    "check_cards": "Shortcuts (the first one is taught)",
    "check_card": "  {card}: {chords}",
    "check_ambiguous": " · needs the kitty protocol: {chords}",
    "check_lessons": "Lessons",
}

# The layout workshop's gallery and puzzle (layout_screen.py). {key}-style fields are the
# player's own keys. The footers' width thresholds in layout_screen.py fit this copy.
LAYOUTS = {
    "brand": "KEYS TRAINER   /   LAYOUTS",
    "brand_short": "KEYS / LAYOUTS",
    "too_small": "Enlarge to 30 × 10",
    "back": "q  Back",
    "back_layouts": "{back}  Layouts",
    "choose": "Choose a layout",
    "built": "{built} / {total} built",
    "panes": "{count} panes",
    "best": "   ·   Best {seconds:.1f} s",
    "footer": "↑ ↓  Choose    Enter  Build    q  Back",
    "footer_short": "← → Choose  Enter Build  q Back",
    "seconds": "{seconds:.1f} s",
    "matched": "{matched} / {total} panes matched",
    "target": "TARGET",
    "yours": "YOUR TAB",
    "play": "[ {key}  Play {guide} ]",
    "footer_solved": "Enter Next   {terminal} Terminal   {reset} Replay   {back} Back",
    "footer_wide": "{terminal} Terminal   {hint} Hint   {undo} Undo   {focus}/click Focus   {reset} Restart   {back} Back",
    "footer_mid": "{terminal} Terminal  {hint} Hint  {undo} Undo  {focus} Focus  {reset} Reset  {back} Back",
    "footer_narrow": "{terminal} Term  {hint} Hint  {undo} Undo  {back} Back",
}

# The coach bar itself (coach.py); lesson text arrives in its model.
COACH = {
    "press": "Press",
    "pause": "Pause",
    "hint": "Hint",
    "skip": "Skip",
    "too_small": "Enlarge the window to 40 × 12",
}

# The live desktop (desktop_session.py, hypr.py) and its card under the bar (runner.py).
DESKTOP = {
    "console_goal": "Open the Quake console, then close it again",
    "no_workspace": "Could not tell which workspace you are on.",
    "no_keys": "The trainer never presses your keys on the real desktop.",
    "no_session": "No Hyprland session found (XDG_RUNTIME_DIR and HYPRLAND_INSTANCE_SIGNATURE are missing).",
    "connected": "The Hyprland observer is already connected.",
    "bad_reply": "Hyprland did not answer with a workspace.",
    "closed": "The Hyprland event stream is closed.",
    "ended": "Hyprland closed the event socket.",
    "header": "DESKTOP · LIVE",
    "watching": "The keys work on your real desktop. The trainer only watches.",
}

# `keys-trainer check`: the terminal part (doctor.py).
DOCTOR = {
    "ambiguous": "Alt + Esc and Alt + Shift + Enter",
    "kitty": "Keyboard protocol: kitty (flags {flags}) · {ambiguous} are told apart",
    "legacy": "Keyboard protocol: not supported · {ambiguous} are practised with the prefix twin",
    "silent": "Keyboard protocol: no answer from the terminal · {ambiguous} are practised with the prefix twin",
    "herdr_inside": "HERDR_ENV: 1 · started inside herdr, so the answer above is herdr's, not the terminal's",
    "herdr_env": "HERDR_ENV: {value}",
    "unset": "not set",
    "size": "Terminal size: {columns} × {rows}",
    "size_small": " · too small, needs at least 40 × 12",
    "size_two": " · the bar at the bottom gets two rows, three from 80 × 20",
    "term": "TERM: {value}",
}

# The start check (doctor.start_check): one plain line per missing or mismatched piece,
# shown before Home and in `keys-trainer check`.
START = {
    "header": "Before you start:",
    "bullet": "- ",
    "prompt": "Press Enter to start, or q and Enter to quit: ",
    "herdr": ("herdr is missing. Every lesson runs in a private herdr session, so the trainer cannot "
              "start without it. Install it with: sudo pacman -S herdr"),
    "yazi": "Yazi is missing, so {lessons} {off}. Install it with: sudo pacman -S yazi",
    "strata": "Strata is missing, so {lessons} {off}. Install it with: sudo pacman -S strata",
    "shell": "bash is missing, so {lessons} {off}. Install it with: sudo pacman -S bash",
    "hyprland": ("Hyprland is not running, so {lessons} {off}. Start the trainer from a terminal on "
                 "your Omarchy desktop to practise it."),
    "foot": ("foot is missing. Started inside herdr, the trainer opens its own foot window, so start it "
             "from a terminal without herdr (Super + Enter), or install foot with: sudo pacman -S foot"),
    "foot_optional": ("foot: missing · only needed when you start the trainer inside herdr. "
                      "Install it with: sudo pacman -S foot"),
    "hyprland_running": "Hyprland: running",
    "lesson": "{order} “{title}”",
    "lessons": ("lesson {lessons}", "lessons {lessons} and {last}"),
    "lesson_start": "Lesson {lesson}",
    "off": ("is off", "are off"),
    "no_key": "{lesson} is off: you have no {tool} key for {actions}.",
    "notice": ("{lesson}: you have no {tool} key for {actions}. Those steps show how to add the key, "
               "and s skips them."),
    "or": " or ",
    "comma": ", ",
    "tools": {"herdr": "herdr", "yazi": "Yazi", "shell": "shell", "strata": "Strata", "desktop": "desktop"},
    "source": "Your keys: {source}",
    "sources": {"scan": "read from your config files", "rigpage": "from rigpage keys",
                "file": "from the file given with --keys"},
    "note": "Keys: {note}",
    # The launcher, before anything else loads.
    "old_python": ("keys-trainer needs Python 3.11 or newer, and this is Python {version}. "
                   "Omarchy ships a new enough one: sudo pacman -S python"),
}

# The private Yazi (yazi_backend.py).
YAZI = {
    "missing": "Install Yazi to practise in a private Yazi.",
    "no_start": "The private Yazi did not start: {detail}",
    "no_report": "The private Yazi did not report the practice folder.",
    "left_tree": "The private Yazi left the practice folder.",
    "not_started": "Start a Yazi session first.",
    "tab_label": "Files",
}

# The private bash shell (shell_backend.py).
SHELL_BACKEND = {
    "missing": "Install bash to practise in a private shell.",
    "no_start": "The private shell did not start: {detail}",
    "no_report": "The private shell did not report its prompt.",
    "not_started": "Start a shell session first.",
    "tab_label": "Shell",
}

# The self-test (selftest.py; `keys-trainer demo` prints its lines).
SELFTEST = {
    "no_taught": "No taught shortcut for {card}",
    "needs_unmet": "Start condition not met: {missing}",
    "decoy_passed": "Decoy accepted after move {index}",
    "solved": "{total}/{total} checks met",
    "decoy_rejected": "Decoy rejected ({latched}/{total} checks)",
    "no_change": "No visible change from the start",
    "unsolved": "{latched}/{total} checks met. {change}",
    "chain_done": "{count} steps complete",
    "rebuilds": " · rebuilds {count}",
    "ok": "OK",
    "failed": "FAIL",
    "kinds": {"solution": "solution", "decoy": "decoy", "chained": "lesson"},
    "line": "{status}  {target}  {kind}  {seconds} s  {detail}{rebuilds}",
    "total": "Self-test: {passed}/{total} passed",
}
