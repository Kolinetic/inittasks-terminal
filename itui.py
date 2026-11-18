#!/usr/bin/env python3
"""itui — a terminal UI for init.Tasks, styled after inittasks.com.

Built on Textual. Reuses the API layer in it.py (same token, same config).
Run with the project venv:  .venv/Scripts/python itui.py   (or `it tui`).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

import it as core

# ───────────────────────── design tokens ──────────────────────────────────
# design/tokens.json is the init.Tasks design system's tokens file (pulled from
# inittasks.com: the stylesheet's :root and the app's theme engine). Every theme
# in it becomes a Textual theme; token names match the site's CSS variables.
TOKENS_PATH = Path(__file__).resolve().parent / "design" / "tokens.json"


def _load_design():
    tok = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    themes = [(t["id"], t["name"]) for t in tok["color"]["themes"]]
    first = themes[0][0]
    pal = {tid: {} for tid, _ in themes}
    for t in tok["color"]["tokens"]:
        for tid, _ in themes:
            v = t["value"] if isinstance(t["value"], str) else t["value"].get(tid, t["value"].get(first))
            pal[tid][t["name"]] = v
    for tid in pal:  # resolve "{alias}" values
        for k, v in pal[tid].items():
            seen = 0
            while isinstance(v, str) and v.startswith("{") and seen < 16:
                v, seen = pal[tid].get(v.strip("{}"), "#ff00ff"), seen + 1
            pal[tid][k] = v
    return themes, pal


DESIGN_THEMES, PALETTES = _load_design()
THEME_PREFIX = "init-"
THEMES = [THEME_PREFIX + tid for tid, _ in DESIGN_THEMES]   # cycle order for T
THEME_LABEL = {THEME_PREFIX + tid: name for tid, name in DESIGN_THEMES}
DEFAULT_THEME = THEMES[0]

# The active palette. Mutated in place on theme change so every SITE[...] lookup follows it.
SITE: dict = dict(PALETTES[DESIGN_THEMES[0][0]])


def use_palette(theme_name: str):
    tid = theme_name[len(THEME_PREFIX):] if theme_name.startswith(THEME_PREFIX) else None
    if tid in PALETTES:
        SITE.clear()
        SITE.update(PALETTES[tid])


def _is_dark(hexcol: str) -> bool:
    c = Color.parse(hexcol)
    return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) < 128


def textual_theme(tid: str, p: dict) -> Theme:
    return Theme(
        name=THEME_PREFIX + tid,
        primary=p["accent"], secondary=p["headers"], accent=p["purple"],
        warning=p["warn"], error=p["over"], success=p["accent"],
        foreground=p["text"], background=p["bg"], surface=p["panel"], panel=p["panel-2"], boost=p["border"],
        dark=_is_dark(p["bg"]),
        variables={
            "text-muted": p["muted"],
            "border": p["border"],
            "border-blurred": p["border-dim"],
            "footer-key-foreground": p["accent"],
            "footer-background": p["bg"],
            "footer-description-foreground": p["muted"],
            "input-cursor-background": p["accent"],
            "input-cursor-foreground": p["on-accent"],
            "input-selection-background": p["accent-dim"],
            "block-cursor-background": p["panel-2"],
            "block-cursor-foreground": p["text"],
            "block-cursor-text-style": "none",
            "block-hover-background": p["border-dim"],
            "scrollbar": p["border"],
            "scrollbar-hover": p["tree"],
            "scrollbar-active": p["accent-dim"],
            "scrollbar-background": p["bg"],
            "scrollbar-corner-color": p["bg"],
        },
    )


VIEWS = [  # key, label, hotkey
    ("inbox", "inbox", "1"), ("today", "today", "2"), ("planned", "planned", "3"),
    ("anytime", "anytime", "4"), ("someday", "someday", "5"), ("logbook", "logbook", "6"),
]


class Pal:
    """Colours for Rich Text: the active design-system theme's tokens."""

    def __init__(self, app: App):
        use_palette(app.theme)
        p = SITE
        self.text, self.muted, self.accent, self.head = p["text"], p["muted"], p["accent"], p["headers"]
        self.warn, self.over, self.tag, self.purple, self.tree = p["warn"], p["over"], p["cyan"], p["purple"], p["tree"]


# ───────────────────────── profiles ───────────────────────────────────────

def fetch_profiles() -> list[dict]:
    """The account's profiles in the app's order; [] if the API won't say."""
    try:
        r = core.api("GET", "/profiles")
    except core.ApiError:
        return []
    return (r or {}).get("data") or []


def group_by_profile(items: list[dict], profiles: list[dict]) -> list[tuple[dict | None, list[dict]]]:
    """Split items by their `profile`, in profile order, dropping empty profiles.
    An item with no (or an unknown) profile goes to the fallback profile.
    With one profile or none known, everything is one group with no profile (no header)."""
    if len(profiles) < 2:
        return [(None, items)] if items else []
    known = {pf["id"] for pf in profiles}
    fallback = next((pf["id"] for pf in profiles if pf.get("isFallback")), profiles[0]["id"])
    groups: dict = {pf["id"]: [] for pf in profiles}
    for it in items:
        pid = it.get("profile")
        groups[pid if pid in known else fallback].append(it)
    return [(pf, groups[pf["id"]]) for pf in profiles if groups[pf["id"]]]


# ───────────────────────── formatting ─────────────────────────────────────

def day_label(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso[:10])
    delta = (d - dt.date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    s = d.strftime("%a %b ") + str(d.day)
    if d.year != dt.date.today().year:
        s += f" {d.year}"
    return s.lower()


def late_label(deadline: str) -> str | None:
    days = (dt.date.today() - dt.date.fromisoformat(deadline)).days
    if days <= 0:
        return None
    return f"{days} day late" if days == 1 else f"{days} days late"


def todo_text(store: core.Store, t: dict, pal: Pal, *, show_when=True, show_where=True) -> Text:
    done = t["status"] == "done"
    txt = Text(no_wrap=True, overflow="ellipsis")
    if done:
        txt.append("[✓] ", style=pal.accent)
    elif t["status"] == "someday":
        txt.append("[~] ", style=pal.muted)
    else:
        txt.append("[ ] ", style=pal.muted)
    prio = {"high": ("!!! ", pal.over), "medium": ("!! ", pal.warn), "low": ("! ", pal.head)}.get(t.get("priority"))
    if prio and not done:
        txt.append(*prio)
    txt.append(t["title"], style=pal.muted if done else pal.text)
    for g in t.get("tags") or []:
        txt.append(" [" + g + "]", style=pal.tag)
    meta: list[tuple[str, str]] = []
    subs = t.get("subtasks") or []
    if subs:
        meta.append((f"{sum(1 for s in subs if s.get('done'))}/{len(subs)}", pal.muted))
    if t.get("recurrenceRule"):
        meta.append(("↻", pal.purple))
    if show_when and t.get("whenDate"):
        w = day_label(t["whenDate"]) + (f" {t['whenTime']}" if t.get("whenTime") else "")
        meta.append((w, pal.muted))
    elif t.get("whenTime") and not done:
        meta.append((t["whenTime"], pal.muted))
    if t.get("deadline") and not done:
        late = late_label(t["deadline"])
        meta.append((late if late else "due " + day_label(t["deadline"]), pal.over if late else pal.warn))
    if show_where and t.get("parentId"):
        meta.append((store.crumb(t["parentId"]).split(" › ")[-1].lower(), pal.muted))
    if meta:
        txt.append("  ")
        for i, (m, style) in enumerate(meta):
            if i:
                txt.append(" * ", style=pal.tree)
            txt.append(m, style=style)
    return txt


def section_text(title: str, n: int | None, color: str, pal: Pal) -> Text:
    t = Text(no_wrap=True)
    t.append(title, style=f"bold {color}")
    if n is not None:
        t.append(f" {n}", style=pal.muted)
    return t


# ───────────────────────── quick add parser ───────────────────────────────

@dataclass
class Quick:
    title: str
    body: dict = field(default_factory=dict)
    project: str | None = None
    summary: list[str] = field(default_factory=list)


def _try_date(s: str) -> str | None:
    try:
        d = core.parse_date(s)
        return None if d == "null" else d
    except core.CliError:
        return None


def parse_quick(text: str, store: core.Store) -> Quick:
    """'renew passport 7 oct #paperwork [docs] !! due fri at 17:00 every 2 weeks'."""
    body: dict = {}
    s = " " + text.strip() + " "
    summary = []

    def grab(pattern, fn):
        nonlocal s
        m = re.search(pattern, s, re.I)
        if m:
            fn(m)
            s = s[:m.start()] + " " + s[m.end():]
        return m

    # [tags]
    while grab(r"\s\[([^\]]+)\]", lambda m: body.setdefault("tags", []).append(m.group(1).strip())):
        pass
    # #project (allow #"multi word")
    proj = None

    def set_proj(m):
        nonlocal proj
        proj = m.group(1) or m.group(2)
    grab(r'\s#"([^"]+)"|\s#(\S+)', set_proj)
    # priority
    grab(r"\s(!{1,3})(?=\s)", lambda m: body.__setitem__("priority", ["low", "medium", "high"][len(m.group(1)) - 1]))
    # repeat
    grab(r"\s(every\s+(?:\d+\s+)?(?:day|week|month|year)s?(?::\S+)?|daily|weekly(?::\S+)?|monthly(?::\d+)?|yearly|weekdays)(?=\s)",
         lambda m: body.__setitem__("_repeat", m.group(1)))
    # time
    grab(r"\sat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?=\s)", lambda m: body.__setitem__(
        "whenTime", _time(m.group(1), m.group(2), m.group(3))))
    # deadline: "due <date>"
    date_words = r"(today|tomorrow|tom|tonight|(?:next\s+)?(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|\+\d+[dwm]?|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{1,2}\s+[a-z]{3,9}|[a-z]{3,9}\s+\d{1,2})"

    def set_deadline(m):
        d = _try_date(m.group(1))
        if d:
            body["deadline"] = d
    grab(r"\s(?:due|by)\s+" + date_words + r"(?=\s)", set_deadline)
    # start date: last date-like phrase
    # zero-width lookahead so candidates overlap ("passport 7" must not hide "7 oct")
    cands = [(m.start() + 1, m.group(1)) for m in re.finditer(r"(?=\s" + date_words + r"(?=\s))", s, re.I)]
    for start, word in sorted(cands, key=lambda c: (c[0] + len(c[1]), len(c[1])), reverse=True):
        d = _try_date("today" if word.lower() == "tonight" else word.lower())
        if d:
            body["whenDate"] = d
            s = s[:start] + " " + s[start + len(word):]
            break
    if "_repeat" in body:
        body["recurrenceRule"] = core.parse_repeat(body.pop("_repeat"), body.get("whenDate"))
    title = re.sub(r"\s+", " ", s).strip()
    q = Quick(title=title, body=body)
    if proj:
        try:
            c = core.find_container(store, proj)
            q.body["parentId"] = c["id"]
            q.project = c["name"]
        except core.CliError as e:
            q.summary.append(f"⚠ {e}")
    if body.get("whenDate"):
        q.summary.append(day_label(body["whenDate"]) + (f" {body['whenTime']}" if body.get("whenTime") else ""))
    if body.get("deadline"):
        q.summary.append("due " + day_label(body["deadline"]))
    if q.project:
        q.summary.append(q.project.lower())
    for g in body.get("tags", []):
        q.summary.append(f"[{g}]")
    if body.get("priority"):
        q.summary.append(body["priority"])
    if body.get("recurrenceRule"):
        q.summary.append("↻ " + core.describe_repeat(body["recurrenceRule"]))
    return q


def _time(h, m, ampm):
    h = int(h)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    return f"{h:02d}:{int(m or 0):02d}"


# ───────────────────────── modal screens ──────────────────────────────────

class Prompt(ModalScreen[str | None]):
    """One-line input, the site's '> ' prompt style."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, value: str = "", hint: str = ""):
        super().__init__()
        self.title_, self.value, self.hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(Text.assemble(("> ", "bold " + SITE["accent"]), self.title_), classes="dlg-title")
            yield Input(value=self.value, id="prompt-input")
            if self.hint:
                yield Label(self.hint, classes="hint")

    def on_input_submitted(self, e: Input.Submitted):
        self.dismiss(e.value)

    def action_cancel(self):
        self.dismiss(None)


class Picker(ModalScreen[str | None]):
    """Filterable list (projects / areas)."""

    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("down", "down", show=False),
                Binding("up", "up", show=False)]

    def __init__(self, title: str, items: list[tuple[str, Text | str]]):
        super().__init__()
        self.title_, self.items = title, items

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog tall"):
            yield Label(Text.assemble(("> ", "bold " + SITE["accent"]), self.title_), classes="dlg-title")
            yield Input(placeholder="filter…", id="picker-filter")
            yield OptionList(id="picker-list")

    def on_mount(self):
        self.refill("")

    def refill(self, q: str):
        ol = self.query_one("#picker-list", OptionList)
        ol.clear_options()
        n = core.norm(q)
        ol.add_options([Option(lbl, id=key) for key, lbl in self.items
                        if not n or n in core.norm(str(lbl))])
        if ol.option_count:
            ol.highlighted = 0

    @on(Input.Changed, "#picker-filter")
    def _filter(self, e: Input.Changed):
        self.refill(e.value)

    @on(Input.Submitted, "#picker-filter")
    def _submit(self, e):
        ol = self.query_one("#picker-list", OptionList)
        if ol.highlighted is not None:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    @on(OptionList.OptionSelected, "#picker-list")
    def _chosen(self, e: OptionList.OptionSelected):
        self.dismiss(e.option.id)

    def action_down(self):
        self.query_one("#picker-list", OptionList).action_cursor_down()

    def action_up(self):
        self.query_one("#picker-list", OptionList).action_cursor_up()

    def action_cancel(self):
        self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "close")]
    KEYS = [
        ("navigate", [("j / k  ↑ / ↓", "move"), ("1–6", "inbox · today · planned · anytime · someday · logbook"),
                      ("tab", "sidebar ⇄ list"), ("enter", "open to-do / project"), ("/", "search"),
                      ("r", "refresh"), ("T", "next theme"), ("q", "quit")]),
        ("to-dos", [("a  n", "quick add"), ("space  x", "complete / reopen"), ("u", "undo last complete"),
                    ("e", "rename"), ("w", "when (start date)"), ("t", "do today"), ("d", "deadline"),
                    ("m", "move to project"), ("#", "tags"), ("p", "priority"), ("s", "someday"),
                    ("delete", "trash"), ("o", "open in web app")]),
        ("inbox captures", [("enter", "file as a to-do"), ("x  delete", "archive")]),
        ("quick add", [("renew passport 7 oct #paperwork", ""), ("call mum fri at 5pm [calls] !!", ""),
                       ("water plants every 3 days", ""), ("tax return due 4/15 #taxes", "")]),
    ]

    def compose(self) -> ComposeResult:
        t = Text()
        for i, (sec, rows) in enumerate(self.KEYS):
            if i:
                t.append("\n")
            t.append("> ", style="bold " + SITE["accent"])
            t.append(sec + "\n", style="bold " + SITE["headers"])
            for k, d in rows:
                t.append(f"  {k:<34}", style=SITE["accent"] if d else SITE["text"])
                t.append(d + "\n", style=SITE["muted"])
        with VerticalScroll(classes="dialog tall wide"):
            yield Static(t)

    def action_close(self):
        self.dismiss(None)


class TodoScreen(ModalScreen[bool]):
    """Full to-do editor: title, dates, tags, notes, subtasks."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("ctrl+s", "save", "save"),
                Binding("space", "toggle_sub", "tick subtask", show=False),
                Binding("ctrl+n", "add_sub", "add subtask")]

    def __init__(self, store: core.Store, todo: dict):
        super().__init__()
        self.store, self.t = store, todo
        self.dirty = False

    def compose(self) -> ComposeResult:
        t = self.t
        with Vertical(classes="dialog editor"):
            yield Label(Text.assemble(("> ", "bold " + SITE["accent"]),
                                      (self.store.crumb(t.get("parentId")).lower() or "no project", SITE["muted"])),
                        classes="dlg-title")
            yield Input(t["title"], id="f-title", classes="title-input")
            with Horizontal(classes="fields"):
                with Vertical():
                    yield Label("when", classes="flabel")
                    yield Input(t.get("whenDate") or "", placeholder="today, fri, 7 oct…", id="f-when")
                with Vertical():
                    yield Label("time", classes="flabel")
                    yield Input(t.get("whenTime") or "", placeholder="17:00", id="f-time")
                with Vertical():
                    yield Label("deadline", classes="flabel")
                    yield Input(t.get("deadline") or "", placeholder="none", id="f-deadline")
            with Horizontal(classes="fields"):
                with Vertical():
                    yield Label("tags", classes="flabel")
                    yield Input(", ".join(t.get("tags") or []), placeholder="comma separated", id="f-tags")
                with Vertical():
                    yield Label("repeat", classes="flabel")
                    yield Input(core.describe_repeat(t["recurrenceRule"]) if t.get("recurrenceRule") else "",
                                placeholder="weekly:mon, monthly:15…", id="f-repeat")
                with Vertical(classes="narrow"):
                    yield Label("priority", classes="flabel")
                    yield Input(t.get("priority") or "", placeholder="low/medium/high", id="f-prio")
            yield Label("notes", classes="flabel")
            yield TextArea(t.get("description") or "", id="f-notes", soft_wrap=True)
            yield Label("subtasks  (space tick · ctrl+n add)", classes="flabel")
            yield OptionList(id="f-subs")
            yield Label(Text.assemble(("ctrl+s", SITE["accent"]), (" save   ", SITE["muted"]),
                                      ("esc", SITE["accent"]), (" close", SITE["muted"])), classes="hint")

    def on_mount(self):
        self.render_subs()
        self.query_one("#f-title", Input).focus()

    def render_subs(self):
        ol = self.query_one("#f-subs", OptionList)
        hi = ol.highlighted
        ol.clear_options()
        for st in self.t.get("subtasks") or []:
            ol.add_option(Option(Text.assemble(("[✓] " if st.get("done") else "[ ] ",
                                                SITE["accent"] if st.get("done") else SITE["muted"]),
                                               (st["title"], SITE["muted"] if st.get("done") else SITE["text"])),
                                 id=st["id"]))
        if not self.t.get("subtasks"):
            ol.add_option(Option(Text("no subtasks — ctrl+n adds one", style=SITE["muted"]), disabled=True))
        elif hi is not None:
            ol.highlighted = min(hi, ol.option_count - 1)

    def action_toggle_sub(self):
        ol = self.query_one("#f-subs", OptionList)
        if self.focused is not ol or ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        st = next((s for s in self.t["subtasks"] if s["id"] == opt.id), None)
        if st:
            self.toggle_sub(st)

    @work(exclusive=False)
    async def toggle_sub(self, st):
        st["done"] = not st.get("done")
        self.render_subs()
        try:
            await asyncio.to_thread(core.api, "PATCH", f"/todos/{self.t['id']}/subtasks/{st['id']}",
                                    None, {"done": st["done"]})
            self.dirty = True
        except Exception as e:
            st["done"] = not st["done"]
            self.render_subs()
            self.notify(str(e), severity="error")

    def action_add_sub(self):
        def done(title):
            if title:
                self.add_sub(title)
        self.app.push_screen(Prompt("new subtask"), done)

    @work
    async def add_sub(self, title):
        try:
            r = await asyncio.to_thread(core.api, "POST", f"/todos/{self.t['id']}/subtasks", None, {"title": title})
            t = r if isinstance(r, dict) and "subtasks" in r else await asyncio.to_thread(
                core.api, "GET", f"/todos/{self.t['id']}")
            self.t["subtasks"] = t.get("subtasks", [])
            self.dirty = True
            self.render_subs()
        except Exception as e:
            self.notify(str(e), severity="error")

    def action_save(self):
        self.save()

    @work(exclusive=True)
    async def save(self):
        t, b = self.t, {}
        v = lambda i: self.query_one(i, Input).value.strip()
        try:
            if v("#f-title") and v("#f-title") != t["title"]:
                b["title"] = v("#f-title")
            for fid, key in (("#f-when", "whenDate"), ("#f-deadline", "deadline")):
                raw = v(fid)
                new = (None if core.parse_date(raw) == "null" else core.parse_date(raw)) if raw else None
                if new != t.get(key):
                    b[key] = new
            tm = v("#f-time") or None
            if tm != t.get("whenTime"):
                m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", tm or "", re.I)
                b["whenTime"] = _time(*m.groups()) if m else None
            tags = [x.strip().strip("[]#") for x in v("#f-tags").split(",") if x.strip()]
            if tags != (t.get("tags") or []):
                b["tags"] = tags
            prio = v("#f-prio").lower() or None
            if prio != t.get("priority"):
                if prio not in (None, "low", "medium", "high"):
                    raise core.CliError("priority must be low, medium or high")
                b["priority"] = prio
            rep = v("#f-repeat")
            cur = core.describe_repeat(t["recurrenceRule"]) if t.get("recurrenceRule") else ""
            if rep != cur:
                b["recurrenceRule"] = core.parse_repeat(rep, b.get("whenDate", t.get("whenDate"))) if rep else None
            notes = self.query_one("#f-notes", TextArea).text
            if notes != (t.get("description") or ""):
                b["description"] = notes or None
            if b:
                if t.get("recurrenceRule") and ("whenDate" in b or "recurrenceRule" in b):
                    b["scope"] = "this_and_future"
                await asyncio.to_thread(core.api, "PATCH", f"/todos/{t['id']}", None, b)
                self.dirty = True
            self.dismiss(self.dirty)
        except (core.CliError, core.ApiError) as e:
            self.notify(str(e), severity="error", timeout=6)

    def action_close(self):
        self.dismiss(self.dirty)


# ───────────────────────── main app ───────────────────────────────────────

@dataclass
class Row:
    kind: str  # todo | capture | project
    id: str


class InitTasks(App):
    TITLE = "init.tasks"
    CSS = """
    Screen { background: $background; }
    #top { height: 1; padding: 0 2; background: $background; }
    #main { height: 1fr; }
    #sidebar { width: 30; height: 1fr; border: none; border-right: solid $border; background: $background; padding: 0 0; }
    #sidebar:focus { border: none; border-right: solid $primary 50%; }
    #sidebar > .option-list--option { padding: 0 1; }
    #list { border: none; background: $background; padding: 0 1; height: 1fr; }
    #list:focus { border: none; background-tint: transparent; }
    OptionList { scrollbar-size-vertical: 1; }
    OptionList > .option-list--option-highlighted { background: $panel; text-style: none; }
    OptionList:focus > .option-list--option-highlighted { background: $panel; text-style: none; }
    OptionList > .option-list--option-disabled { color: $foreground; }
    #list-wrap { width: 1fr; }
    #view-title { height: 1; padding: 0 2; margin-top: 1; }
    #quick { height: 3; padding: 0 1; display: none; border-top: solid $border; }
    #quick.show { display: block; }
    #quick Input { border: none; background: $background; height: 1; padding: 0 1; }
    #quick-preview { height: 1; padding: 0 2; color: $text-muted; }
    #detail { width: 46; border-left: solid $border; padding: 1 2; background: $background; display: none; }
    #detail.show { display: block; }
    Footer { background: $background; }
    .dialog { width: 64; height: auto; max-height: 90%; background: $surface; border: round $border;
              padding: 1 2; }
    .dialog.tall { height: 80%; }
    .dialog.wide { width: 80; }
    .dialog.editor { width: 90; height: 90%; }
    ModalScreen { align: center middle; background: $background 60%; }
    .dlg-title { margin-bottom: 1; }
    .hint { color: $text-muted; margin-top: 1; }
    .flabel { color: $text-muted; margin-top: 1; }
    .fields { height: auto; }
    .fields > Vertical { height: auto; width: 1fr; padding-right: 1; }
    .dialog Input { border: tall $border; background: $background; }
    .dialog Input:focus { border: tall $primary; }
    .title-input { text-style: bold; }
    #f-notes { height: 8; border: tall $border; background: $background; }
    #f-notes:focus { border: tall $primary; }
    #f-subs { height: 1fr; min-height: 3; border: tall $border; background: $background; }
    #f-subs:focus { border: tall $primary; }
    #picker-list { height: 1fr; border: none; background: $surface; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("a,n", "quick_add", "add"),
        Binding("space,x", "toggle_done", "done"),
        Binding("w", "when", "when"),
        Binding("t", "do_today", "today", show=False),
        Binding("d", "deadline", "deadline", show=False),
        Binding("m", "move", "move"),
        Binding("e", "rename", "rename", show=False),
        Binding("number_sign", "tags", "tags", show=False),
        Binding("p", "priority", "priority", show=False),
        Binding("s", "someday", "someday", show=False),
        Binding("delete,backspace", "trash", "trash", show=False),
        Binding("u", "undo", "undo", show=False),
        Binding("o", "open_web", "web", show=False),
        Binding("i", "toggle_detail", "details"),
        Binding("slash", "search", "search"),
        Binding("r", "refresh", "refresh", show=False),
        Binding("T", "next_theme", "theme"),
        Binding("tab", "swap_focus", "pane", show=False),
        Binding("j", "down", show=False), Binding("k", "up", show=False),
        Binding("escape", "escape", show=False),
        *[Binding(h, f"view('{k}')", show=False) for k, _, h in VIEWS],
    ]

    def __init__(self):
        super().__init__()
        self.store = core.Store()
        self.view = "today"
        self.project_id: str | None = None
        self.search_q: str | None = None
        self.rows: list[Row | None] = []
        self.counts = {}
        self.profiles: list[dict] = []  # GET /profiles, in the app's order
        self.captures: dict = {}
        self.last_done: str | None = None
        self.loading = False

    # ── layout ──
    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="main"):
            yield OptionList(id="sidebar")
            with Vertical(id="list-wrap"):
                yield Static(id="view-title")
                yield OptionList(id="list")
                with Vertical(id="quick"):
                    yield Input(placeholder="renew passport 7 oct #paperwork [docs] !!", id="quick-input")
                    yield Static(id="quick-preview")
            yield VerticalScroll(Static(id="detail-body"), id="detail")
        yield Footer()

    def on_mount(self):
        for tid, _ in DESIGN_THEMES:
            self.register_theme(textual_theme(tid, PALETTES[tid]))
        cfg = core.load_config()
        saved = cfg.get("tui_theme") or ""
        # older configs stored "init" or a Textual built-in name
        saved = {"init": DEFAULT_THEME, "gruvbox": THEME_PREFIX + "gruvbox-dark"}.get(saved, saved)
        if not saved.startswith(THEME_PREFIX):
            saved = THEME_PREFIX + saved
        self.theme = saved if saved in THEMES else DEFAULT_THEME
        use_palette(self.theme)
        if self.size.width >= 130:
            self.query_one("#detail").add_class("show")
        self.render_top("loading…")
        self.query_one("#list").focus()
        self.reload()

    @property
    def pal(self) -> Pal:
        return Pal(self)

    def render_top(self, status: str = ""):
        p = self.pal
        t = Text(no_wrap=True)
        t.append("> ", style=f"bold {p.accent}")
        t.append("init", style=f"bold {p.text}")
        t.append(".tasks", style=f"bold {p.accent}")
        t.append("  " + dt.date.today().strftime("%a %b %d").lower(), style=p.muted)
        if status:
            t.append("  · " + status, style=p.muted)
        self.query_one("#top", Static).update(t)

    # ── data ──
    @work(exclusive=True, group="load")
    async def reload(self, keep_cursor: bool = False):
        self.loading = True
        self.render_top("syncing…")
        s = core.Store()
        today = dt.date.today().isoformat()
        try:
            todos, conts, heads, tv, iv, profiles = await asyncio.gather(
                asyncio.to_thread(lambda: s.todos),
                asyncio.to_thread(lambda: s.containers),
                asyncio.to_thread(lambda: s.headings),
                asyncio.to_thread(core.api, "GET", "/views/today", {"today": today}),
                asyncio.to_thread(core.api, "GET", "/views/inbox", {"today": today}),
                asyncio.to_thread(fetch_profiles),
            )
        except (core.ApiError, core.CliError) as e:
            self.render_top("offline")
            self.notify(str(e), severity="error", timeout=8)
            self.loading = False
            return
        self.store = s
        self.profiles = profiles
        self.counts = {"today": len(tv["overdue"]) + len(tv["fromEarlier"]) + len(tv["todayItems"]),
                       "inbox": len(iv["unsortedTodos"]) + len(iv["inboxCaptures"]),
                       "overdue": len(tv["overdue"])}
        self.render_sidebar()
        await self.load_view(keep_cursor=keep_cursor, prefetched={"today": tv, "inbox": iv})
        self.render_top("synced " + dt.datetime.now().strftime("%H:%M"))
        self.loading = False

    def render_sidebar(self):
        p, s = self.pal, self.store
        sb = self.query_one("#sidebar", OptionList)
        hi = sb.highlighted
        sb.clear_options()
        for key, label, hot in VIEWS:
            t = Text(no_wrap=True)
            t.append(label, style=p.text)
            n = self.counts.get(key)
            if n:
                t.append(f" {n}", style=p.over if key == "today" and self.counts.get("overdue") else p.muted)
            sb.add_option(Option(t, id=f"view:{key}"))
        live = [c for c in s.live_containers() if c.get("status") != "archived"]
        ids = {c["id"] for c in live}
        kids: dict = {}
        for c in live:
            kids.setdefault(c.get("parentId") if c.get("parentId") in ids else None, []).append(c)
        counts = self.open_counts()

        def walk(pid, prefix, roots=None):
            # areas first, then loose projects; each area's projects nest under it
            ch = sorted(roots if roots is not None else kids.get(pid, []),
                        key=lambda c: (c["kind"] != "area", c.get("sortKey") or "", c["name"]))
            for i, c in enumerate(ch):
                last = i == len(ch) - 1
                t = Text(no_wrap=True, overflow="ellipsis")
                if pid is not None:
                    t.append(prefix + ("└ " if last else "├ "), style=p.tree)
                is_area = c["kind"] == "area"
                t.append(c["name"].lower(), style=f"bold {p.text}" if is_area else p.text)
                n = counts.get(c["id"], 0)
                if n:
                    t.append(f" {n}", style=p.muted)
                sb.add_option(Option(t, id=f"proj:{c['id']}"))
                walk(c["id"], prefix + ("" if pid is None else ("  " if last else "│ ")))

        tops = kids.get(None, [])
        for prof, members in group_by_profile(tops, self.profiles):
            sb.add_option(None)
            if prof is None:
                head = Text.assemble(("projects", f"bold {p.head}"))
            else:
                col = prof.get("colorHex") or p.head
                head = Text(no_wrap=True, overflow="ellipsis")
                head.append("● ", style=col)
                head.append(prof["name"].lower(), style=f"bold {col}")
            sb.add_option(Option(head, id=f"profile:{prof['id'] if prof else ''}", disabled=True))
            walk(None, "", roots=members)
        if hi is not None and hi < sb.option_count:
            sb.highlighted = hi

    def open_counts(self) -> dict:
        s = self.store
        direct: dict = {}
        for t in s.todos.values():
            if t["status"] == "open" and not t.get("trashedAt") and t.get("parentId"):
                direct[t["parentId"]] = direct.get(t["parentId"], 0) + 1
        total = dict(direct)
        for cid, n in direct.items():
            p = s.containers.get(cid, {}).get("parentId")
            seen = set()
            while p and p not in seen:
                seen.add(p)
                total[p] = total.get(p, 0) + n
                p = s.containers.get(p, {}).get("parentId")
        return total

    async def load_view(self, keep_cursor=False, prefetched=None):
        prefetched = prefetched or {}
        p, s = self.pal, self.store
        today = dt.date.today().isoformat()
        sections: list[tuple[Text | None, list]] = []
        title = Text()

        def todos(ids):
            return [s.todos[i] if i in s.todos else core.api("GET", f"/todos/{i}") for i in ids]

        try:
            if self.search_q:
                r = await asyncio.to_thread(core.api, "GET", "/search", {"q": self.search_q, "limit": 100})
                hits = [h for h in r["data"] if h["kind"] == "todo"]
                others = [h for h in r["data"] if h["kind"] == "container"]
                title = section_text(f"search: {self.search_q}", len(hits), p.head, p)
                sections.append((None, [("todo", s.todos[h["id"]]) for h in hits if h["id"] in s.todos]))
                if others:
                    sections.append((section_text("projects", len(others), p.head, p),
                                     [("project", s.containers[h["id"]]) for h in others if h["id"] in s.containers]))
            elif self.project_id:
                c = s.containers.get(self.project_id)
                if not c:
                    self.project_id = None
                    return await self.load_view()
                r = await asyncio.to_thread(core.api, "GET", "/todos",
                                            {"parent_id": c["id"], "group_by": "heading", "limit": 200})
                rows = [t for t in r["data"] if t["status"] != "done" and not t.get("trashedAt")]
                heads = {h["id"]: h for h in r.get("headings", [])}
                title = section_text(c["name"].lower(), len(rows), p.head, p)
                if c.get("description"):
                    title.append("\n" + c["description"].splitlines()[0], style=p.muted)
                groups: dict = {}
                for t in rows:
                    groups.setdefault(t.get("headingId"), []).append(t)
                sections.append((None, [("todo", t) for t in groups.pop(None, [])]))
                for hid, ts in groups.items():
                    sections.append((section_text(heads.get(hid, {}).get("name", "—"), len(ts), p.text, p),
                                     [("todo", t) for t in ts]))
                subs = [x for x in s.live_containers() if x.get("parentId") == c["id"]]
                if subs:
                    cnt = self.open_counts()
                    sections.append((section_text("projects", len(subs), p.head, p),
                                     [("project", x) for x in subs]))
                done = [t for t in s.todos.values() if t.get("parentId") == c["id"] and t["status"] == "done"
                        and not t.get("trashedAt")]
                done.sort(key=lambda t: t.get("completedAt") or "", reverse=True)
                if done:
                    sections.append((section_text("done", len(done), p.muted, p), [("todo", t) for t in done[:10]]))
            elif self.view == "today":
                v = prefetched.get("today") or await asyncio.to_thread(core.api, "GET", "/views/today", {"today": today})
                title = section_text("today", len(v["todayItems"]) + len(v["fromEarlier"]) + len(v["overdue"]), p.head, p)
                if v["overdue"]:
                    sections.append((section_text("overdue", len(v["overdue"]), p.over, p),
                                     [("todo", t) for t in await asyncio.to_thread(todos, v["overdue"])]))
                if v["fromEarlier"]:
                    sections.append((section_text("from earlier", len(v["fromEarlier"]), p.warn, p),
                                     [("todo", t) for t in await asyncio.to_thread(todos, v["fromEarlier"])]))
                sections.append((section_text("today", len(v["todayItems"]), p.head, p) if sections else None,
                                 [("todo", t) for t in await asyncio.to_thread(todos, v["todayItems"])]))
                if v["doneToday"]:
                    sections.append((section_text("done today", len(v["doneToday"]), p.muted, p),
                                     [("todo", t) for t in await asyncio.to_thread(todos, v["doneToday"])]))
            elif self.view == "inbox":
                v = prefetched.get("inbox") or await asyncio.to_thread(core.api, "GET", "/views/inbox", {"today": today})
                caps = await asyncio.gather(*[asyncio.to_thread(core.api, "GET", f"/inbox/{c}") for c in v["inboxCaptures"]])
                self.captures = {c["id"]: c for c in caps}
                title = section_text("inbox", len(v["unsortedTodos"]) + len(caps), p.head, p)
                sections.append((None, [("todo", t) for t in await asyncio.to_thread(todos, v["unsortedTodos"])]))
                if caps:
                    sections.append((section_text("captures", len(caps), p.text, p), [("capture", c) for c in caps]))
            elif self.view == "planned":
                v = await asyncio.to_thread(core.api, "GET", "/views/planned", {"today": today})
                n = 0
                for b in v["plannedDays"] + v["plannedLater"]:
                    if not b["items"]:
                        continue
                    n += len(b["items"])
                    d = dt.date.fromisoformat(b["date"])
                    lbl = day_label(b["date"])
                    head = Text(no_wrap=True)
                    head.append(d.strftime("%a %b ").lower() + str(d.day), style=f"bold {p.text}")
                    if lbl in ("tomorrow",):
                        head.append("  " + lbl, style=p.muted)
                    sections.append((head, [("todo", t) for t in await asyncio.to_thread(todos, b["items"])]))
                title = section_text("planned", n, p.head, p)
            elif self.view == "anytime":
                v = await asyncio.to_thread(core.api, "GET", "/views/anytime", {"today": today})
                title = section_text("anytime", len(v["anytime"]), p.head, p)
                sections.append((None, [("todo", t) for t in await asyncio.to_thread(todos, v["anytime"])]))
            elif self.view == "someday":
                v = (await asyncio.to_thread(core.api, "GET", "/views/someday", {"today": today}))["somedayV2"]
                n = len(v["standalone"]) + sum(len(g["todos"]) for g in v["groups"])
                title = section_text("someday", n, p.head, p)
                sections.append((None, [("todo", t) for t in await asyncio.to_thread(todos, v["standalone"])]))
                for g in v["groups"]:
                    sections.append((section_text(s.crumb(g["container"]).lower(), len(g["todos"]), p.text, p),
                                     [("todo", t) for t in await asyncio.to_thread(todos, g["todos"])]))
            elif self.view == "logbook":
                frm = (dt.date.today() - dt.timedelta(days=13)).isoformat()
                v = await asyncio.to_thread(core.api, "GET", "/views/completed", {"today": today, "from": frm})
                n = sum(len(g["ids"]) for g in v["completedDays"])
                title = section_text("logbook", n, p.head, p)
                title.append("  last 14 days", style=p.muted)
                for g in v["completedDays"]:
                    sections.append((Text(day_label(g["date"]), style=f"bold {p.text}"),
                                     [("todo", t) for t in await asyncio.to_thread(todos, g["ids"])]))
        except (core.ApiError, core.CliError) as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        self.query_one("#view-title", Static).update(title)
        self.fill_list(sections, keep_cursor)
        self.mark_sidebar()

    def fill_list(self, sections, keep_cursor=False):
        p, s = self.pal, self.store
        ol = self.query_one("#list", OptionList)
        prev = ol.highlighted
        prev_row = self.rows[prev] if prev is not None and prev < len(self.rows) else None
        ol.clear_options()
        self.rows = []
        in_today = self.view == "today" and not self.project_id and not self.search_q
        in_proj = bool(self.project_id)
        cnt = self.open_counts()
        for i, (head, items) in enumerate(sections):
            if head is not None:
                if self.rows:
                    ol.add_option(Option(Text(""), disabled=True))
                    self.rows.append(None)
                ol.add_option(Option(head, disabled=True))
                self.rows.append(None)
            for kind, obj in items:
                if kind == "todo":
                    by_day = self.view in ("today", "planned", "logbook") and not in_proj
                    txt = todo_text(s, obj, p, show_when=not by_day or bool(self.search_q),
                                    show_where=not in_proj)
                elif kind == "capture":
                    txt = Text(no_wrap=True, overflow="ellipsis")
                    txt.append("↓ ", style=p.purple)
                    label = obj.get("title") or obj.get("text") or obj.get("url") or obj["kind"]
                    txt.append(label.splitlines()[0] if label else obj["kind"], style=p.text)
                    if obj.get("url"):
                        host = re.sub(r"^https?://(www\.)?", "", obj["url"]).split("/")[0]
                        txt.append(" — " + host, style=p.muted)
                else:  # project
                    txt = Text(no_wrap=True)
                    txt.append("▸ ", style=p.accent)
                    txt.append(obj["name"].lower(), style=f"bold {p.text}")
                    if cnt.get(obj["id"]):
                        txt.append(f" {cnt[obj['id']]}", style=p.muted)
                ol.add_option(Option(txt))
                self.rows.append(Row(kind, obj["id"]))
        if not any(self.rows):
            ol.add_option(Option(Text("  nothing here. ✨", style=p.muted), disabled=True))
            self.rows.append(None)
        # restore cursor
        target = None
        if keep_cursor and prev_row is not None:
            for idx, r in enumerate(self.rows):
                if r and r.id == prev_row.id:
                    target = idx
                    break
            if target is None and prev is not None:
                target = min(prev, len(self.rows) - 1)
        if target is None or self.rows[target] is None:
            nxt = [i for i, r in enumerate(self.rows) if r and (target is None or i >= target)]
            target = nxt[0] if nxt else None
        if target is not None:
            ol.highlighted = target
        self.update_detail()

    def mark_sidebar(self):
        sb = self.query_one("#sidebar", OptionList)
        key = f"proj:{self.project_id}" if self.project_id else f"view:{self.view}"
        if self.search_q:
            return
        for i in range(sb.option_count):
            if sb.get_option_at_index(i).id == key:
                if sb.highlighted != i:
                    sb.highlighted = i
                break

    # ── selection helpers ──
    def current(self) -> Row | None:
        ol = self.query_one("#list", OptionList)
        if ol.highlighted is None or ol.highlighted >= len(self.rows):
            return None
        return self.rows[ol.highlighted]

    def current_todo(self) -> dict | None:
        r = self.current()
        if r and r.kind == "todo":
            return self.store.todos.get(r.id)
        return None

    def need_todo(self) -> dict | None:
        t = self.current_todo()
        if not t:
            self.notify("select a to-do first", severity="warning", timeout=2)
        return t

    @on(OptionList.OptionHighlighted, "#list")
    def _hl(self, e):
        self.update_detail()

    def update_detail(self):
        det = self.query_one("#detail")
        if not det.has_class("show"):
            return
        p, s = self.pal, self.store
        body = self.query_one("#detail-body", Static)
        r = self.current()
        if not r:
            body.update("")
            return
        if r.kind == "capture":
            c = self.captures.get(r.id, {})
            t = Text()
            t.append(f"{c.get('kind', '')} capture\n\n", style=p.muted)
            t.append((c.get("title") or c.get("text") or "") + "\n", style=f"bold {p.text}")
            if c.get("url"):
                t.append(c["url"] + "\n", style=p.tag)
            if c.get("text") and c.get("title"):
                t.append("\n" + c["text"], style=p.text)
            t.append("\n\nenter", style=p.accent)
            t.append(" file as to-do   ", style=p.muted)
            t.append("x", style=p.accent)
            t.append(" archive", style=p.muted)
            body.update(t)
            return
        if r.kind == "project":
            c = s.containers.get(r.id, {})
            body.update(Text.assemble((c.get("name", "") + "\n", f"bold {p.text}"),
                                      (c.get("description") or "", p.muted)))
            return
        td = s.todos.get(r.id)
        if not td:
            return
        t = Text()
        crumb = s.crumb(td.get("parentId")).lower()
        t.append((crumb or "no project") + "\n\n", style=p.muted)
        t.append(td["title"] + "\n", style=f"bold {p.text}")
        if td.get("subtitle"):
            t.append(td["subtitle"] + "\n", style=p.muted)
        t.append("\n")

        def row(k, v, style=None):
            t.append(f"{k:<9}", style=p.muted)
            t.append(f"{v}\n", style=style or p.text)
        if td.get("whenDate"):
            row("when", day_label(td["whenDate"]) + (f" {td['whenTime']}" if td.get("whenTime") else "")
                + (f" ({td['durationMin']}m)" if td.get("durationMin") else ""))
        if td.get("deadline"):
            late = late_label(td["deadline"])
            row("deadline", day_label(td["deadline"]) + (f" · {late}" if late else ""), p.over if late else p.warn)
        if td.get("recurrenceRule"):
            row("repeats", core.describe_repeat(td["recurrenceRule"]), p.purple)
        if td.get("priority"):
            row("priority", td["priority"], {"high": p.over, "medium": p.warn}.get(td["priority"], p.head))
        if td.get("tags"):
            row("tags", " ".join(f"[{g}]" for g in td["tags"]), p.tag)
        if td.get("completedAt"):
            row("done", td["completedAt"][:16].replace("T", " "), p.accent)
        if td.get("description"):
            t.append("\n" + td["description"].strip() + "\n", style=p.text)
        if td.get("subtasks"):
            t.append("\n")
            for st in td["subtasks"]:
                t.append("[✓] " if st.get("done") else "[ ] ", style=p.accent if st.get("done") else p.muted)
                t.append(st["title"] + "\n", style=p.muted if st.get("done") else p.text)
        body.update(t)

    # ── navigation ──
    @on(OptionList.OptionSelected, "#sidebar")
    def _side(self, e: OptionList.OptionSelected):
        kind, _, val = (e.option.id or "").partition(":")
        if kind == "view":
            self.action_view(val)
        elif kind == "proj":
            self.open_project(val)
        self.query_one("#list").focus()

    @on(OptionList.OptionSelected, "#list")
    def _open(self, e: OptionList.OptionSelected):
        r = self.current()
        if not r:
            return
        if r.kind == "todo":
            self.edit_todo(self.store.todos[r.id])
        elif r.kind == "project":
            self.open_project(r.id)
        elif r.kind == "capture":
            self.file_capture(r.id)

    def action_view(self, key: str):
        self.view, self.project_id, self.search_q = key, None, None
        self.run_worker(self.load_view(), exclusive=True, group="view")

    def open_project(self, cid: str):
        self.project_id, self.search_q = cid, None
        self.run_worker(self.load_view(), exclusive=True, group="view")

    def action_swap_focus(self):
        sb, ol = self.query_one("#sidebar"), self.query_one("#list")
        (ol if self.focused is sb else sb).focus()

    def action_down(self):
        if isinstance(self.focused, OptionList):
            self.focused.action_cursor_down()

    def action_up(self):
        if isinstance(self.focused, OptionList):
            self.focused.action_cursor_up()

    def action_escape(self):
        if self.query_one("#quick").has_class("show"):
            self.close_quick()
        elif self.search_q:
            self.search_q = None
            self.run_worker(self.load_view(), exclusive=True, group="view")

    def action_refresh(self):
        self.reload(keep_cursor=True)

    def action_help(self):
        self.push_screen(Help())

    def action_toggle_detail(self):
        self.query_one("#detail").toggle_class("show")
        self.update_detail()

    def action_next_theme(self):
        i = THEMES.index(self.theme) if self.theme in THEMES else -1
        self.theme = THEMES[(i + 1) % len(THEMES)]
        use_palette(self.theme)
        cfg = core.load_config()
        cfg["tui_theme"] = self.theme
        core.save_config(cfg)
        self.notify(f"theme: {THEME_LABEL[self.theme]}", timeout=1.5)
        self.render_top()
        self.render_sidebar()
        self.run_worker(self.load_view(keep_cursor=True), exclusive=True, group="view")

    def action_search(self):
        def done(q):
            if q:
                self.search_q = q.strip()
                self.run_worker(self.load_view(), exclusive=True, group="view")
        self.push_screen(Prompt("search", hint="to-dos, projects, notes, tags"), done)

    # ── quick add ──
    def action_quick_add(self):
        q = self.query_one("#quick")
        q.add_class("show")
        inp = self.query_one("#quick-input", Input)
        if self.project_id and not inp.value:
            c = self.store.containers.get(self.project_id, {})
            inp.value = f' #"{c.get("name", "")}"' if " " in c.get("name", "") else f" #{c.get('name', '')}"
            inp.cursor_position = 0
        inp.focus()
        self.preview_quick(inp.value)

    def close_quick(self):
        self.query_one("#quick").remove_class("show")
        self.query_one("#quick-input", Input).value = ""
        self.query_one("#list").focus()

    @on(Input.Changed, "#quick-input")
    def _qchange(self, e: Input.Changed):
        self.preview_quick(e.value)

    def preview_quick(self, value: str):
        p = self.pal
        prev = self.query_one("#quick-preview", Static)
        if not value.strip():
            prev.update(Text("dates, #project, [tag], !!, at 5pm, due fri, every 2 weeks — enter adds, esc cancels",
                             style=p.muted))
            return
        q = parse_quick(value, self.store)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("[ ] ", style=p.muted)
        t.append(q.title or "…", style=p.text)
        if q.summary:
            t.append("  " + " * ".join(q.summary), style=p.muted)
        if not q.body.get("parentId") and not q.body.get("whenDate") and self.view != "today":
            t.append("  → inbox", style=p.muted)
        prev.update(t)

    @on(Input.Submitted, "#quick-input")
    def _qsubmit(self, e: Input.Submitted):
        if not e.value.strip():
            return self.close_quick()
        q = parse_quick(e.value, self.store)
        if not q.title:
            return self.notify("a to-do needs a title", severity="warning")
        body = {"title": q.title, **q.body}
        if self.view == "today" and not self.project_id and "whenDate" not in body:
            body["whenDate"] = dt.date.today().isoformat()
        self.query_one("#quick-input", Input).value = ""
        self.create_todo(body)

    @work(group="write")
    async def create_todo(self, body):
        try:
            t = await asyncio.to_thread(core.api, "POST", "/todos", None, body)
        except (core.ApiError, core.CliError) as e:
            return self.notify(str(e), severity="error", timeout=6)
        self.store.todos[t["id"]] = t
        self.notify(f"added: {t['title']}", timeout=2)
        self.reload(keep_cursor=True)

    # ── mutations ──
    async def _call(self, *a, label=None, **kw):
        try:
            return await asyncio.to_thread(core.api, *a, **kw)
        except core.ApiError as e:
            if e.code == "scope_required":
                self.notify("repeating to-do — use the CLI with --scope, or edit it with enter",
                            severity="warning", timeout=6)
            else:
                self.notify(str(e), severity="error", timeout=6)
        except core.CliError as e:
            self.notify(str(e), severity="error", timeout=6)
        return None

    def action_toggle_done(self):
        r = self.current()
        if r and r.kind == "capture":
            return self.archive_capture(r.id)
        t = self.need_todo()
        if t:
            self.toggle_done(t)

    @work(group="write")
    async def toggle_done(self, t):
        done = t["status"] == "done"
        # optimistic: redraw the row ticked
        t["status"] = "open" if done else "done"
        self.redraw_current(t)
        out = await self._call("POST", f"/todos/{t['id']}/{'uncomplete' if done else 'complete'}", None, {})
        if out is None:
            t["status"] = "done" if done else "open"
            self.redraw_current(t)
            return
        if not done:
            self.last_done = t["id"]
            if out.get("status") == "open" and out.get("whenDate"):
                self.notify(f"↻ next: {day_label(out['whenDate'])}", timeout=2.5)
        self.reload(keep_cursor=True)

    def redraw_current(self, t):
        ol = self.query_one("#list", OptionList)
        for i, r in enumerate(self.rows):
            if r and r.id == t["id"]:
                ol.replace_option_prompt_at_index(i, todo_text(self.store, t, self.pal,
                                                               show_when=self.view != "today",
                                                               show_where=not self.project_id))

    def action_undo(self):
        if not self.last_done:
            return self.notify("nothing to undo", timeout=1.5)
        tid, self.last_done = self.last_done, None
        self.undo(tid)

    @work(group="write")
    async def undo(self, tid):
        if await self._call("POST", f"/todos/{tid}/uncomplete", None, {}) is not None:
            self.notify("reopened", timeout=1.5)
            self.reload(keep_cursor=True)

    def patch_current(self, body: dict, msg: str):
        t = self.need_todo()
        if t:
            self.patch(t, body, msg)

    @work(group="write")
    async def patch(self, t, body, msg):
        if t.get("recurrenceRule") and any(k in body for k in ("whenDate", "deadline")):
            body = {**body, "scope": "this"}
        if await self._call("PATCH", f"/todos/{t['id']}", None, body) is not None:
            self.notify(msg, timeout=1.5)
            self.reload(keep_cursor=True)

    def _date_prompt(self, title, key, current):
        t = self.need_todo()
        if not t:
            return

        def done(v):
            if v is None:
                return
            try:
                d = core.parse_date(v) if v.strip() else "null"
            except core.CliError as e:
                return self.notify(str(e), severity="error")
            d = None if d == "null" else d
            self.patch(t, {key: d}, f"{title}: {day_label(d) or 'cleared'}")
        self.push_screen(Prompt(title, current or "", "today, tomorrow, fri, next fri, +3, 7 oct, 2026-11-20 · empty clears"), done)

    def action_when(self):
        t = self.current_todo()
        self._date_prompt("when", "whenDate", t and t.get("whenDate"))

    def action_deadline(self):
        t = self.current_todo()
        self._date_prompt("deadline", "deadline", t and t.get("deadline"))

    def action_do_today(self):
        self.patch_current({"whenDate": dt.date.today().isoformat()}, "→ today")

    def action_priority(self):
        t = self.need_todo()
        if t:
            order = [None, "low", "medium", "high"]
            nxt = order[(order.index(t.get("priority")) + 1) % 4]
            self.patch(t, {"priority": nxt}, f"priority: {nxt or 'none'}")

    def action_rename(self):
        t = self.need_todo()
        if t:
            self.push_screen(Prompt("rename", t["title"]),
                             lambda v: v and v != t["title"] and self.patch(t, {"title": v}, "renamed"))

    def action_tags(self):
        t = self.need_todo()
        if t:
            def done(v):
                if v is not None:
                    tags = [x.strip().strip("[]#") for x in re.split(r"[,\s]+", v) if x.strip()]
                    self.patch(t, {"tags": tags}, "tags updated")
            self.push_screen(Prompt("tags", " ".join(t.get("tags") or []), "space or comma separated"), done)

    def project_items(self, include_none=True):
        p, s = self.pal, self.store
        items = []
        if include_none:
            items.append(("__none__", Text("no project", style=p.muted)))
        # top-level holders decide the profile; list profile by profile, then by path
        def root(c):
            seen = set()
            while c.get("parentId") in s.containers and c["id"] not in seen:
                seen.add(c["id"])
                c = s.containers[c["parentId"]]
            return c
        live = s.live_containers()
        by_root = {c["id"]: root(c) for c in live}
        order = []
        for prof, roots in group_by_profile(list({r["id"]: r for r in by_root.values()}.values()), self.profiles):
            ids = {r["id"] for r in roots}
            members = sorted((c for c in live if by_root[c["id"]]["id"] in ids),
                             key=lambda c: s.crumb(c["id"]).casefold())
            order += [(prof, c) for c in members]
        for prof, c in order:
            crumb = s.crumb(c["id"]).lower()
            parts = crumb.split(" › ")
            t = Text(no_wrap=True)
            if prof is not None:
                t.append("● ", style=prof.get("colorHex") or p.muted)
            if len(parts) > 1:
                t.append(" › ".join(parts[:-1]) + " › ", style=p.muted)
            t.append(parts[-1], style=f"bold {p.text}" if c["kind"] == "area" else p.text)
            items.append((c["id"], t))
        return items

    def action_move(self):
        t = self.need_todo()
        if not t:
            return

        def done(cid):
            if cid:
                self.move(t, None if cid == "__none__" else cid)
        self.push_screen(Picker("move to", self.project_items()), done)

    @work(group="write")
    async def move(self, t, cid):
        body = {"parentId": cid}
        if t.get("recurrenceRule"):
            body["scope"] = "this_and_future"
        if await self._call("POST", f"/todos/{t['id']}/move", None, body) is not None:
            self.notify("moved → " + (self.store.crumb(cid).lower() or "no project"), timeout=1.5)
            self.reload(keep_cursor=True)

    def action_someday(self):
        t = self.need_todo()
        if t:
            self.simple(t, "someday" if t["status"] != "someday" else "anytime",
                        "→ someday" if t["status"] != "someday" else "→ anytime")

    def action_trash(self):
        r = self.current()
        if r and r.kind == "capture":
            return self.archive_capture(r.id)
        t = self.need_todo()
        if not t:
            return
        if t.get("recurrenceRule"):
            def done(scope):
                if scope:
                    self.simple(t, "trash", "trashed", {"scope": scope})
            return self.push_screen(Picker("trash which dates?", [
                ("this", "only this date"), ("this_and_future", "this and future dates"),
                ("all", "every date, done ones too")]), done)
        self.simple(t, "trash", "trashed — restore from the app's trash")

    @work(group="write")
    async def simple(self, t, verb, msg, body=None):
        if await self._call("POST", f"/todos/{t['id']}/{verb}", None, body or {}) is not None:
            self.notify(msg, timeout=2)
            self.reload(keep_cursor=True)

    def action_open_web(self):
        t = self.current_todo()
        webbrowser.open(f"{core.APP}/today/t/{t['id']}" if t else f"{core.APP}/today")

    def edit_todo(self, t):
        def done(changed):
            if changed:
                self.reload(keep_cursor=True)
        self.push_screen(TodoScreen(self.store, json.loads(json.dumps(t))), done)

    # ── captures ──
    def file_capture(self, cid):
        def done(target):
            if target:
                self._file(cid, None if target == "__none__" else target)
        self.push_screen(Picker("file capture into", self.project_items()), done)

    @work(group="write")
    async def _file(self, cid, parent):
        body = {"parentId": parent} if parent else {}
        if await self._call("POST", f"/inbox/{cid}/move", None, body) is not None:
            self.notify("filed", timeout=1.5)
            self.reload(keep_cursor=True)

    @work(group="write")
    async def archive_capture(self, cid):
        if await self._call("POST", f"/inbox/{cid}/archive", None, {}) is not None:
            self.notify("capture archived", timeout=1.5)
            self.reload(keep_cursor=True)


def main():
    if not core.has_token():
        if not core.interactive():
            print("error: no API key yet. Run `it login` in a terminal (or set INITTASKS_TOKEN).")
            raise SystemExit(1)
        try:
            ok = core.setup_wizard()
        except core.CliError as e:
            print(f"error: {e}")
            ok = False
        if not ok:
            raise SystemExit(1)
    InitTasks().run()


if __name__ == "__main__":
    main()
