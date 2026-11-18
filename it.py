#!/usr/bin/env python3
"""it — a command-line client for init.Tasks (https://inittasks.com).

Stdlib only. Talks to the public REST API at https://api.inittasks.com/v1
with a personal access token (itk_…).

Token lookup order: $INITTASKS_TOKEN, then ~/.config/inittasks/config.json
(written by `it login`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

API = os.environ.get("INITTASKS_API", "https://api.inittasks.com/v1")
APP = "https://app.inittasks.com"
CONFIG_DIR = Path(os.environ.get("INITTASKS_HOME", Path.home() / ".config" / "inittasks"))
CONFIG_FILE = CONFIG_DIR / "config.json"
LAST_FILE = CONFIG_DIR / "last.json"  # ids of the last printed list, for `it done 3`
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

# ───────────────────────────── output helpers ──────────────────────────────

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass
if os.name == "nt":
    os.system("")  # enable ANSI escape handling in the Windows console

USE_COLOR = (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")

# Colours come from the init.Tasks design system (design/tokens.json), in the same
# theme the TUI uses (config "tui_theme", or $INITTASKS_THEME, e.g. "dracula").
# Only foreground colours are set; the terminal keeps its own background.
TOKENS_FILE = Path(__file__).resolve().parent / "design" / "tokens.json"
PALETTE_FALLBACK = {"text": "#e6edf3", "muted": "#747d88", "accent": "#34c48a", "headers": "#58a6ff",
                    "warn": "#d29922", "over": "#ff7b72", "cyan": "#56d4dd", "purple": "#bc8cff",
                    "tree": "#4a5563", "border": "#21262d"}


def load_palette() -> dict:
    try:
        tok = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(PALETTE_FALLBACK)
    ids = [t["id"] for t in tok["color"]["themes"]]
    want = os.environ.get("INITTASKS_THEME")
    if not want:
        try:
            want = json.loads((Path(os.environ.get("INITTASKS_HOME", Path.home() / ".config" / "inittasks"))
                               / "config.json").read_text(encoding="utf-8")).get("tui_theme")
        except (OSError, ValueError):
            want = None
    want = (want or "").removeprefix("init-")
    tid = want if want in ids else ids[0]
    pal = {}
    for t in tok["color"]["tokens"]:
        v = t["value"] if isinstance(t["value"], str) else t["value"].get(tid, t["value"].get(ids[0]))
        pal[t["name"]] = v
    for k, v in pal.items():  # resolve "{alias}"
        for _ in range(16):
            if not (isinstance(v, str) and v.startswith("{")):
                break
            v = pal.get(v.strip("{}"), PALETTE_FALLBACK["text"])
        pal[k] = v
    return {**PALETTE_FALLBACK, **pal}


PAL = load_palette()


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def tok(name: str, s: str, bold_: bool = False) -> str:
    h = PAL[name].lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return c(f"{'1;' if bold_ else ''}38;2;{r};{g};{b}", s)


bold = lambda s: c("1", s)
dim = lambda s: tok("muted", s)        # the app's "comment" layer
green = lambda s: tok("accent", s)     # prompts, ticks, [ok]
red = lambda s: tok("over", s)         # overdue, late, errors
yellow = lambda s: tok("warn", s)      # due dates, warnings
blue = lambda s: tok("headers", s)     # headings
magenta = lambda s: tok("purple", s)   # repeat glyph, captures
cyan = lambda s: tok("cyan", s)        # [tags]
tree = lambda s: tok("tree", s)        # the * separator and rules
head = lambda s: tok("headers", s, True)
OK = lambda: green("[ok]")


class CliError(Exception):
    """A user-facing error. The CLI prints it; the TUI shows it as a notification."""


def die(msg: str, code: int = 1):
    raise CliError(msg)


# ───────────────────────────── config / auth ───────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # created 0600 up front so the token is never briefly readable by others
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def get_token() -> str:
    tok = os.environ.get("INITTASKS_TOKEN") or load_config().get("token")
    if not tok:
        die("no API token. Run `it login` (or set INITTASKS_TOKEN).")
    return tok.strip()


def has_token() -> bool:
    return bool((os.environ.get("INITTASKS_TOKEN") or load_config().get("token") or "").strip())


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# What `it` and `itui` use. A key without some of these still works for the rest.
WANTED_SCOPES = ["todos:read", "todos:write", "containers:read", "containers:write",
                 "inbox:read", "inbox:write", "tags:read", "profiles:read"]


def check_and_save_token(token: str) -> dict:
    """Verify a token against GET /me, then save it. Returns /me; raises CliError if rejected."""
    token = token.strip().strip('"').strip()
    if not token:
        die("no key entered")
    if not token.startswith("itk_"):
        print(yellow("warning: init.Tasks keys start with itk_ — trying it anyway"), file=sys.stderr)
    prev = os.environ.get("INITTASKS_TOKEN")
    os.environ["INITTASKS_TOKEN"] = token
    try:
        me = api("GET", "/me")
    except ApiError as e:
        die("that key was rejected" + (" (wrong or revoked key)" if e.status in (401, 403) else f": {e}"))
    finally:
        # only the saved config should carry the key from here on
        if prev is None:
            os.environ.pop("INITTASKS_TOKEN", None)
        else:
            os.environ["INITTASKS_TOKEN"] = prev
    cfg = load_config()
    cfg["token"] = token
    save_config(cfg)
    return me


def setup_wizard(reason: str = "") -> bool:
    """First-run setup: ask for a personal access token, check it, save it.
    Returns True once a working token is saved. Needs a terminal."""
    import getpass
    print()
    print(head("welcome to it") + dim(" · the init.Tasks command line"))
    if reason:
        print(dim(reason))
    print()
    print("it reaches your account with a personal access token (an api key).")
    print("  1. in init.Tasks, open " + bold("settings → api keys") + " and create a key.")
    print("     " + dim("give it read + write for todos, containers and inbox,"))
    print("     " + dim("and read for tags and profiles."))
    print("  2. copy the key (" + dim("itk_ plus 44 characters, shown only once") + ") and paste it below.")
    print()
    print(dim(f"the key is saved to {CONFIG_FILE}, readable only by you, and only sent to the init.Tasks API."))
    print(dim("press enter on an empty prompt to open the web app, or ctrl-c to skip for now."))
    failures = 0
    while failures < 3:
        try:
            entered = getpass.getpass("\napi key (hidden): ")
        except (EOFError, KeyboardInterrupt):
            print("\n" + dim("skipped. run `it login` when you're ready."))
            return False
        if not entered.strip():
            print(dim(f"opening {APP} …"))
            try:
                webbrowser.open(APP)
            except Exception:
                pass
            continue
        try:
            me = check_and_save_token(entered)
        except CliError as e:
            failures += 1
            print(red("✗ ") + str(e) + (", try again" if failures < 3 else ""))
            continue
        print(OK() + f" connected · saved to {CONFIG_FILE}")
        missing = [s for s in WANTED_SCOPES if s not in set(me.get("scopes") or [])]
        if missing:
            print(yellow("note: ") + "this key is missing " + ", ".join(missing) + ".")
            print(dim("  those parts won't work until you add them under settings → api keys → permissions."))
        print(dim("try: it today · it inbox · itui"))
        print()
        return True
    print(dim("stopping here. run `it login` to try again."))
    return False


# ───────────────────────────── HTTP ─────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, status: int, body):
        self.status, self.body = status, body
        if isinstance(body, dict):
            msg = body.get("detail") or body.get("title") or json.dumps(body)
            if body.get("errors"):
                msg += " " + "; ".join(
                    f"{e.get('pointer', e.get('path', ''))}: {e.get('detail', e.get('message', ''))}"
                    for e in body["errors"] if isinstance(e, dict))
        else:
            msg = str(body)
        super().__init__(f"HTTP {status}: {msg}")

    @property
    def code(self):
        return self.body.get("code") if isinstance(self.body, dict) else None


def api(method: str, path: str, params: dict | None = None, body=None, raw: bool = False):
    url = API + path
    if params:
        q = []
        for k, v in params.items():
            if v is None:
                continue
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                q.append((k, str(item).lower() if isinstance(item, bool) else str(item)))
        if q:
            url += "?" + urllib.parse.urlencode(q)
    data = None
    headers = {"Authorization": f"Bearer {get_token()}", "Accept": "application/json",
               "User-Agent": "it-cli/1.0"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    # Reads are retried on 5xx: the beta API sometimes 500s on concurrent reads right
    # after a write. Writes are never retried (a repeat could complete a to-do twice).
    attempts = 4 if method == "GET" else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = r.read()
                if raw:
                    return payload
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                b = json.loads(payload)
            except Exception:
                b = payload.decode(errors="replace")
            if e.code >= 500 and attempt < attempts - 1:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise ApiError(e.code, b) from None
        except urllib.error.URLError as e:
            die(f"network error: {e.reason}")


def paged(path: str, params: dict | None = None) -> list:
    params = dict(params or {}, limit=200)
    out = []
    while True:
        r = api("GET", path, params)
        out.extend(r.get("data", []))
        cur = r.get("next_cursor")
        if not cur:
            return out
        params["cursor"] = cur


# ───────────────────────────── caches / lookups ────────────────────────────

class Store:
    """Lazy per-invocation caches of todos, containers and headings."""

    def __init__(self, profile: str | None = None):
        self.profile = profile
        self._todos = self._containers = self._headings = None

    @property
    def todos(self) -> dict:
        if self._todos is None:
            self._todos = {t["id"]: t for t in paged("/todos", {"profile": self.profile})}
        return self._todos

    @property
    def containers(self) -> dict:
        if self._containers is None:
            self._containers = {c["id"]: c for c in paged("/containers", {"profile": self.profile})}
        return self._containers

    def live_containers(self):
        trashed = {i for i, c in self.containers.items() if c.get("trashedAt")}

        def dead(c):
            p = c
            while p:
                if p["id"] in trashed:
                    return True
                p = self.containers.get(p.get("parentId"))
            return False
        return [c for c in self.containers.values() if not dead(c)]

    @property
    def headings(self) -> dict:
        if self._headings is None:
            try:
                self._headings = {h["id"]: h for h in paged("/headings")}
            except ApiError:
                self._headings = {}
        return self._headings

    def todo(self, tid: str) -> dict:
        t = self.todos.get(tid)
        if t is None:
            t = api("GET", f"/todos/{tid}")
            self.todos[tid] = t
        return t

    def crumb(self, cid: str | None) -> str:
        names = []
        while cid:
            ct = self.containers.get(cid)
            if not ct:
                break
            names.append(ct["name"])
            cid = ct.get("parentId")
        return " › ".join(reversed(names))


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", s.casefold())


def find_container(store: Store, name: str, kinds=("area", "project", "subproject")) -> dict:
    if UUID_RE.match(name):
        return store.containers.get(name.upper()) or die(f"no container {name}")
    live = [c for c in store.live_containers() if c["kind"] in kinds]
    n = norm(name)
    for pred in (lambda c: c["name"] == name,
                 lambda c: norm(c["name"]) == n,
                 lambda c: norm(c["name"]).startswith(n),
                 lambda c: n in norm(c["name"]),
                 lambda c: c["id"].lower().startswith(name.lower())):
        hits = [c for c in live if pred(c)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # prefer projects over areas when ambiguous by kind
            proj = [h for h in hits if h["kind"] != "area"]
            if len(proj) == 1:
                return proj[0]
            die(f"'{name}' matches several: " + ", ".join(
                f"{h['name']} ({h['kind']}, {h['id'][:6]})" for h in hits))
    die(f"no area/project matching '{name}'")


def load_last() -> list:
    try:
        return json.loads(LAST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_last(ids: list):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_FILE.write_text(json.dumps(ids), encoding="utf-8")
    except OSError:
        pass


def resolve_todo(store: Store, ref: str) -> dict:
    """ref: list index from the last listing, full UUID, id prefix, or title text."""
    if ref.isdigit() and int(ref) < 10000:
        last = load_last()
        i = int(ref)
        if 1 <= i <= len(last):
            return store.todo(last[i - 1])
        die(f"no item #{i} in the last listing ({len(last)} items)")
    if UUID_RE.match(ref):
        return store.todo(ref.upper())
    r = ref.lower()
    if re.fullmatch(r"[0-9a-f-]{4,}", r):
        hits = [t for t in store.todos.values() if t["id"].lower().startswith(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            die(f"id prefix '{ref}' is ambiguous ({len(hits)} matches)")
    live = [t for t in store.todos.values() if not t.get("trashedAt")]
    n = ref.casefold()
    for pred in (lambda t: t["title"].casefold() == n, lambda t: n in t["title"].casefold()):
        hits = [t for t in live if pred(t)]
        open_hits = [t for t in hits if t["status"] != "done"]
        hits = open_hits if len(open_hits) >= 1 and len(hits) > 1 else hits
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            die(f"'{ref}' matches {len(hits)} to-dos:\n" + "\n".join(
                f"  {t['id'][:6]}  {t['title']}" for t in hits[:15]))
    die(f"no to-do matching '{ref}'")


# ───────────────────────────── dates ───────────────────────────────────────

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def today() -> dt.date:
    return dt.date.today()


def parse_date(s: str | None) -> str | None:
    """today, tomorrow, tom, yesterday, +3, +2w, mon…sun, next fri, 11-20, 2026-11-20, none."""
    if s is None:
        return None
    t = s.strip().lower()
    if t in ("none", "clear", "-", "null"):
        return "null"
    d = today()
    if t in ("today", "tod", "now"):
        return d.isoformat()
    if t in ("tomorrow", "tom", "tmr"):
        return (d + dt.timedelta(days=1)).isoformat()
    if t == "yesterday":
        return (d - dt.timedelta(days=1)).isoformat()
    m = re.fullmatch(r"\+?(\d+)([dwm]?)", t)
    if m and (t.startswith("+") or m.group(2)):
        n, unit = int(m.group(1)), m.group(2) or "d"
        if unit == "d":
            return (d + dt.timedelta(days=n)).isoformat()
        if unit == "w":
            return (d + dt.timedelta(weeks=n)).isoformat()
        mo = d.month - 1 + n
        y, mo = d.year + mo // 12, mo % 12 + 1
        import calendar
        return dt.date(y, mo, min(d.day, calendar.monthrange(y, mo)[1])).isoformat()
    m = re.fullmatch(r"(next\s+)?([a-z]{3})[a-z]*", t)
    if m and m.group(2) in WEEKDAYS:
        delta = (WEEKDAYS.index(m.group(2)) - d.weekday()) % 7 or 7
        # "next fri" means the coming Friday, same as "fri" (never today)
        return (d + dt.timedelta(days=delta)).isoformat()
    if t in ("weekend",):
        return (d + dt.timedelta(days=(5 - d.weekday()) % 7 or 7)).isoformat()
    if t in ("nextweek", "next week"):
        return (d + dt.timedelta(days=(0 - d.weekday()) % 7 or 7)).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return dt.date(*map(int, m.groups())).isoformat()
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", t)
    if m:
        cand = dt.date(d.year, int(m.group(1)), int(m.group(2)))
        if cand < d:
            cand = cand.replace(year=d.year + 1)
        return cand.isoformat()
    m = re.fullmatch(r"(\d{1,2})\s*([a-z]{3})[a-z]*\.?|([a-z]{3})[a-z]*\.?\s*(\d{1,2})", t)
    if m:
        day, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if mon in MONTHS:
            cand = dt.date(d.year, MONTHS.index(mon) + 1, int(day))
            if cand < d:
                cand = cand.replace(year=d.year + 1)
            return cand.isoformat()
    die(f"can't understand date '{s}' (try today, tomorrow, fri, +3, 11-20, 2026-11-20, none)")


FREQ = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}


def parse_repeat(spec: str, when: str | None) -> str | None:
    """daily | weekly | monthly | yearly | every 3 days | weekly:mon,thu | monthly:15 | raw JSON | none."""
    t = spec.strip().lower()
    if t in ("none", "-", "off"):
        return None
    if t.startswith("{"):
        json.loads(spec)
        return spec
    t, _, on = t.partition(":")
    m = re.fullmatch(r"every\s+(\d+)?\s*(day|week|month|year)s?", t)
    if m:
        rule = {"freq": FREQ[m.group(2)], "interval": int(m.group(1) or 1)}
    elif t in FREQ.values():
        rule = {"freq": t, "interval": 1}
    elif t in ("weekdays",):
        rule, on = {"freq": "weekly", "interval": 1}, "mon,tue,wed,thu,fri"
    else:
        die(f"can't understand repeat '{spec}' (daily, weekly:mon,thu, monthly:15, every 2 weeks, or JSON)")
    if on:
        if rule["freq"] == "weekly":
            rule["byday"] = [WEEKDAYS.index(d.strip()[:3]) + 1 for d in on.split(",")]
        elif rule["freq"] == "monthly":
            rule["bymonthday"] = int(on)
    elif rule["freq"] == "monthly" and when and when != "null":
        rule["bymonthday"] = int(when[8:10])
    return json.dumps(rule, separators=(",", ":"))


def describe_repeat(raw) -> str:
    try:
        r = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return str(raw)
    n, f = r.get("interval", 1), r.get("freq", "?")
    unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}.get(f, f)
    out = f"every {n} {unit}s" if n != 1 else f
    if r.get("byday"):
        names = [WEEKDAYS[d - 1].capitalize() for d in r["byday"]]
        if r.get("bysetpos"):
            pos = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}.get(r["bysetpos"], r["bysetpos"])
            out += f" on the {pos} {'/'.join(names)}"
        else:
            out += " on " + ", ".join(names)
    if r.get("bymonthday"):
        out += f" on day {r['bymonthday']}"
    return out


def fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso[:10])
    delta = (d - today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    out = (d.strftime("%a %b ") + str(d.day)).lower()
    return out if d.year == today().year else f"{out} {d.year}"


def late_label(deadline: str) -> str | None:
    days = (today() - dt.date.fromisoformat(deadline[:10])).days
    if days <= 0:
        return None
    return "1 day late" if days == 1 else f"{days} days late"


# ───────────────────────────── rendering ───────────────────────────────────

def todo_line(store: Store, t: dict, idx: int | None, show_crumb=True, show_when=True) -> str:
    done = t["status"] == "done"
    parts = []
    if idx is not None:
        parts.append(dim(f"{idx:>3}"))
    parts.append(dim(t["id"][:6]))
    if t.get("trashedAt"):
        box = red("[✗]")
    elif done:
        box = dim("[") + green("✓") + dim("]")
    elif t["status"] == "someday":
        box = dim("[~]")
    else:
        box = dim("[ ]")
    line = " ".join(parts) + " " + box + " "
    prio = {"high": red("!!! "), "medium": yellow("!! "), "low": blue("! ")}.get(t.get("priority"))
    if prio and not done:
        line += prio
    line += dim(t["title"]) if done else t["title"]
    for g in t.get("tags") or []:
        line += " " + cyan(f"[{g}]")
    meta = []
    subs = t.get("subtasks") or []
    if subs:
        meta.append(dim(f"{sum(1 for x in subs if x.get('done'))}/{len(subs)}"))
    if t.get("recurrenceRule"):
        meta.append(magenta("↻"))
    if show_when and t.get("whenDate"):
        meta.append(dim(fmt_date(t["whenDate"]) + (f" {t['whenTime']}" if t.get("whenTime") else "")))
    elif t.get("whenTime") and not done:
        meta.append(dim(t["whenTime"]))
    if t.get("deadline") and not done:
        late = late_label(t["deadline"])
        meta.append(red(late) if late else yellow("due " + fmt_date(t["deadline"])))
    if show_crumb and t.get("parentId"):
        where = store.crumb(t["parentId"]).split(" › ")[-1].lower()
        h = store.headings.get(t.get("headingId")) if t.get("headingId") else None
        if h:
            where += f" › {h['name'].lower()}"
        meta.append(dim(where))
    if meta:
        line += "  " + tree(" * ").join(meta)
    return line


class Printer:
    """Prints sections of to-dos with running indexes and remembers them."""

    def __init__(self, store: Store, as_json=False):
        self.store, self.as_json, self.ids, self.sections = store, as_json, [], []

    def section(self, title: str | None, ids_or_todos, color: str = "headers", count: bool = True,
                note: str = "", **kw):
        """title is plain text, printed lowercase in `color` (a token name) with a muted count."""
        todos = [self.store.todo(x) if isinstance(x, str) else x for x in ids_or_todos]
        self.sections.append((title, todos))
        if self.as_json:
            return
        if not todos:
            return
        if title:
            print(tok(color, title.lower(), True) + (dim(f" {len(todos)}") if count else "")
                  + (dim("  " + note) if note else ""))
        for t in todos:
            self.ids.append(t["id"])
            print(todo_line(self.store, t, len(self.ids), **kw))
        print()

    def finish(self, empty_msg="nothing here."):
        if self.as_json:
            out = {(s or "items"): ts for s, ts in self.sections}
            print(json.dumps(out if len(out) > 1 else next(iter(out.values()), []), indent=2,
                             ensure_ascii=False))
            return
        if not self.ids:
            print(dim(empty_msg))
        save_last(self.ids)


def view_params(args) -> dict:
    return {"today": today().isoformat(), "profile": args.profile}


# ───────────────────────────── commands ────────────────────────────────────

def cmd_login(args):
    token = args.token
    if args.file:
        token = Path(args.file).read_text(encoding="utf-8-sig")
    if not token:
        if not interactive():
            die("no key given. Use `it login KEY`, `it login -f FILE`, or run `it login` in a terminal.")
        if not setup_wizard():
            sys.exit(1)
        return
    me = check_and_save_token(token)
    print(OK() + " saved" + f" to {CONFIG_FILE}  " + dim(f"({len(me.get('scopes', []))} scopes)"))


def cmd_whoami(args):
    me = api("GET", "/me")
    if args.json:
        return print(json.dumps(me, indent=2))
    print(f"subject: {me['sub']}")
    print(f"grant:   {me['grant']['kind']} {me['grant']['id']}")
    print("scopes:  " + ", ".join(sorted(me["scopes"])))


def cmd_today(args):
    s = Store(args.profile)
    v = api("GET", "/views/today", view_params(args))
    p = Printer(s, args.json)
    p.section("overdue", v["overdue"], color="over")
    p.section("from earlier", v["fromEarlier"], color="warn")
    p.section("today", v["todayItems"], show_when=False)
    if args.done:
        p.section("done today", v["doneToday"], color="muted", show_when=False)
    elif v["doneToday"] and not args.json:
        print(dim(f"+ {len(v['doneToday'])} done today") + tree("  // ") + dim("it today -d") + "\n")
    p.finish("nothing for today.")


def cmd_inbox(args):
    s = Store(args.profile)
    v = api("GET", "/views/inbox", view_params(args))
    p = Printer(s, args.json)
    p.section("inbox", v["unsortedTodos"])
    caps = v.get("inboxCaptures") or []
    if caps:
        items = [api("GET", f"/inbox/{cid}") for cid in caps]
        if args.json:
            p.sections.append(("captures", items))
        else:
            print(head("captures") + dim(f" {len(items)}") + tree("  // ") + dim("it file <#> [project]"))
            for it in items:
                p.ids.append("capture:" + it["id"])
                label = it.get("title") or it.get("text") or it.get("url") or it["kind"]
                label = label.splitlines()[0] if label else it["kind"]
                host = re.sub(r"^https?://(www\.)?", "", it["url"]).split("/")[0] if it.get("url") else ""
                print(f"{dim(f'{len(p.ids):>3}')} {dim(it['id'][:6])} {magenta('↓')} {label}"
                      + (dim(" — " + host) if host else ""))
            print()
    p.finish("inbox zero.")


def cmd_anytime(args):
    s = Store(args.profile)
    v = api("GET", "/views/anytime", view_params(args))
    p = Printer(s, args.json)
    p.section("anytime", v["anytime"])
    p.finish()


def cmd_someday(args):
    s = Store(args.profile)
    v = api("GET", "/views/someday", view_params(args))["somedayV2"]
    p = Printer(s, args.json)
    p.section("someday", v["standalone"])
    for g in v["groups"]:
        p.section(s.crumb(g["container"]), g["todos"], color="text", show_crumb=False)
    p.finish()


def cmd_planned(args):
    s = Store(args.profile)
    v = api("GET", "/views/planned", view_params(args))
    p = Printer(s, args.json)
    for b in v["plannedDays"] + (v["plannedLater"] if not args.week else []):
        d = dt.date.fromisoformat(b["date"])
        lbl = fmt_date(b["date"])
        p.section(d.strftime("%a %b ") + str(d.day), b["items"], color="text", count=False,
                  note=lbl if lbl in ("today", "tomorrow") else "", show_when=False)
    p.finish("nothing planned.")


def cmd_logbook(args):
    s = Store(args.profile)
    params = view_params(args)
    params["from"] = (today() - dt.timedelta(days=args.days - 1)).isoformat()
    v = api("GET", "/views/completed", params)
    p = Printer(s, args.json)
    for g in v["completedDays"]:
        p.section(fmt_date(g["date"]), g["ids"], color="text", count=False, show_when=False)
    p.finish(f"nothing completed in the last {args.days} days.")


def cmd_ls(args):
    s = Store(args.profile)
    todos = list(s.todos.values())
    if args.project:
        ct = find_container(s, args.project)
        ids = {ct["id"]}
        changed = True
        while changed:  # include sub-containers
            changed = False
            for c in s.containers.values():
                if c.get("parentId") in ids and c["id"] not in ids:
                    ids.add(c["id"])
                    changed = True
        todos = [t for t in todos if t.get("parentId") in ids]
    if args.tag:
        want = {g.casefold() for g in args.tag}
        todos = [t for t in todos if want & {g.casefold() for g in t.get("tags", [])}]
    if not args.all:
        statuses = {"open", "someday"} if not args.status else {args.status}
        todos = [t for t in todos if t["status"] in statuses]
    if not args.trashed:
        dead = {c["id"] for c in s.containers.values()} - {c["id"] for c in s.live_containers()}
        todos = [t for t in todos if not t.get("trashedAt") and t.get("parentId") not in dead]
    p = Printer(s, args.json)
    if args.project and not args.json:
        # group by heading in page order
        ct = find_container(s, args.project)
        r = api("GET", "/todos", {"parent_id": ct["id"], "group_by": "heading", "limit": 200})
        keep = {t["id"] for t in todos}
        heads = {h["id"]: h for h in r.get("headings", [])}
        groups: dict = {}
        for t in r["data"]:
            if t["id"] in keep:
                groups.setdefault(t.get("headingId"), []).append(t)
        p.section(s.crumb(ct["id"]), groups.pop(None, []), show_crumb=False)
        for hid, ts in groups.items():
            h = heads.get(hid, {})
            p.section(h.get("name") or "—", ts, color="text", show_crumb=False)
        rest = [t for t in todos if t.get("parentId") != ct["id"]]
        if rest:
            p.section("in sub-projects", rest, color="muted")
    else:
        todos.sort(key=lambda t: (s.crumb(t.get("parentId")).casefold(), t.get("sortKey") or ""))
        p.section(None, todos)
    p.finish()


def cmd_projects(args):
    s = Store(args.profile)
    live = s.live_containers()
    if not args.archived:
        live = [c for c in live if c.get("status") != "archived"]
    if args.json:
        return print(json.dumps(live, indent=2, ensure_ascii=False))
    open_counts: dict = {}
    for t in s.todos.values():
        if t["status"] == "open" and not t.get("trashedAt"):
            open_counts[t.get("parentId")] = open_counts.get(t.get("parentId"), 0) + 1
    kids: dict = {}
    for c in live:
        kids.setdefault(c.get("parentId"), []).append(c)
    ids = {c["id"] for c in live}

    def walk(pid, prefix, root):
        ch = sorted(kids.get(pid, []), key=lambda c: (c["kind"] != "area", c.get("sortKey") or "", c["name"]))
        for i, c in enumerate(ch):
            last = i == len(ch) - 1
            joint = "" if root else tree(prefix + ("└ " if last else "├ "))
            name = c["name"].lower()
            name = bold(name) if c["kind"] == "area" else name
            extra = []
            n = open_counts.get(c["id"], 0)
            if n:
                extra.append(dim(str(n)))
            if c.get("deadline"):
                late = late_label(c["deadline"])
                extra.append(red(late) if late else yellow("due " + fmt_date(c["deadline"])))
            if c.get("status") == "archived":
                extra.append(dim("archived"))
            print(joint + name + (" " + tree(" * ").join(extra) if extra else "") + dim(f"  {c['id'][:6]}"))
            walk(c["id"], "" if root else prefix + ("  " if last else "│ "), False)
    for r in [c for c in live if c.get("parentId") not in ids]:
        kids.setdefault("__root__", []).append(r)
    walk("__root__", "", True)


def cmd_tags(args):
    tags = paged("/tags")
    tags = [t for t in tags if not t.get("trashedAt")]
    if args.json:
        return print(json.dumps(tags, indent=2, ensure_ascii=False))
    for t in sorted(tags, key=lambda t: t.get("name", "").casefold()):
        print(cyan("[" + t.get("name", "?") + "]") + dim(f"  {t['id'][:6]}"))
    if not tags:
        print(dim("no tags."))


def cmd_show(args):
    s = Store(args.profile)
    t = resolve_todo(s, args.ref)
    t = api("GET", f"/todos/{t['id']}")
    if args.json:
        return print(json.dumps(t, indent=2, ensure_ascii=False))
    print(todo_line(s, t, None, show_crumb=False))
    print(tree("─" * 60))
    rows = [("id", t["id"]), ("status", t["status"]),
            ("where", s.crumb(t.get("parentId")) or "—"),
            ("when", (t.get("whenDate") or "—") + (f" {t['whenTime']}" if t.get("whenTime") else "")
             + (f" ({t['durationMin']} min)" if t.get("durationMin") else "")),
            ("deadline", t.get("deadline") or "—"),
            ("priority", t.get("priority") or "—"),
            ("tags", ", ".join(t.get("tags") or []) or "—")]
    if t.get("recurrenceRule"):
        rows.append(("repeats", describe_repeat(t["recurrenceRule"])))
    if t.get("reminders"):
        rows.append(("reminders", json.dumps(t["reminders"])))
    if t.get("completedAt"):
        rows.append(("completed", t["completedAt"]))
    rows.append(("created", f"{t['createdAt'][:16].replace('T', ' ')}" + (f" by {t['createdBy']}" if t.get("createdBy") else "")))
    for k, v in rows:
        print(f"{dim(k.rjust(9))}  {v}")
    if t.get("subtitle"):
        print("\n" + t["subtitle"])
    if t.get("description"):
        print("\n" + t["description"])
    if t.get("subtasks"):
        print()
        for st in t["subtasks"]:
            print(("  " + (green("[✓]") if st.get("done") else dim("[ ]"))) + " " + st["title"] + dim(f"  {st['id'][:6]}"))
    try:
        notes = api("GET", f"/todos/{t['id']}/notes").get("data", [])
        if notes:
            print("\n" + dim("linked notes: ") + ", ".join(n.get("title") or n["id"][:6] for n in notes))
    except ApiError:
        pass
    print(dim(f"\n{APP}/today/t/{t['id']}"))


def build_fields(s: Store, args, creating: bool) -> dict:
    b: dict = {}
    if getattr(args, "title", None) and not creating:
        b["title"] = args.title
    for flag, key in (("when", "whenDate"), ("deadline", "deadline")):
        v = getattr(args, flag, None)
        if v is not None:
            d = parse_date(v)
            b[key] = None if d == "null" else d
    if getattr(args, "at", None):
        b["whenTime"] = None if args.at in ("none", "-") else args.at
    if getattr(args, "duration", None):
        b["durationMin"] = args.duration
    if getattr(args, "notes", None) is not None:
        b["description"] = args.notes or None
    if getattr(args, "priority", None):
        b["priority"] = None if args.priority == "none" else args.priority
    if getattr(args, "color", None):
        b["colorHex"] = None if args.color == "none" else args.color
    if getattr(args, "repeat", None):
        b["recurrenceRule"] = parse_repeat(args.repeat, b.get("whenDate"))
    return b


def cmd_add(args):
    s = Store(args.profile)
    title = " ".join(args.title)
    if not title:
        die("a title is required")
    b = {"title": title, **build_fields(s, args, True)}
    if args.project:
        ct = find_container(s, args.project)
        b["parentId"] = ct["id"]
        if args.heading:
            hs = [h for h in paged("/headings", {"project_id": ct["id"]}) if norm(args.heading) in norm(h["name"])]
            if len(hs) != 1:
                die(f"heading '{args.heading}' matched {len(hs)} headings in {ct['name']}")
            b["headingId"] = hs[0]["id"]
    if args.tags:
        b["tags"] = [g.lstrip("#") for g in args.tags]
    if args.someday:
        b["status"] = "someday"
    if args.subtask:
        b["subtasks"] = [{"title": st} for st in args.subtask]
    if args.profile and args.profile != "all":
        b["profile"] = args.profile
    t = api("POST", "/todos", body=b)
    if args.json:
        return print(json.dumps(t, indent=2, ensure_ascii=False))
    s.todos[t["id"]] = t
    print(OK() + " added " + todo_line(s, t, None))
    save_last([t["id"]])


def cmd_capture(args):
    text = " ".join(args.text)
    if text == "-":
        text = sys.stdin.read()
    if re.fullmatch(r"https?://\S+", text.strip()):
        b = {"kind": "url", "url": text.strip()}
    elif args.note:
        b = {"kind": "note", "text": text}
        if args.note is not True:
            b["title"] = args.note
    else:
        b = {"kind": "text", "text": text}
    if args.when:
        d = parse_date(args.when)
        b["whenDate"] = None if d == "null" else d
    if args.profile and args.profile != "all":
        b["profile"] = args.profile
    r = api("POST", "/inbox", body=b)
    if args.json:
        return print(json.dumps(r, indent=2, ensure_ascii=False))
    print(OK() + " captured to inbox " + (r.get("title") or r.get("text") or r.get("url") or "") + dim(f"  {r['id'][:6]}"))


def _scope_retry(method, path, body, scope):
    body = dict(body or {})
    if scope:
        body["scope"] = scope
    try:
        return api(method, path, body=body)
    except ApiError as e:
        if e.code == "scope_required" or "scope" in str(e).lower() and e.status == 422:
            die("this is a repeating to-do — add --scope this|future|all")
        raise


SCOPES = {"this": "this", "future": "this_and_future", "this_and_future": "this_and_future", "all": "all"}


def each_todo(args, fn):
    s = Store(args.profile)
    targets = [resolve_todo(s, r) for r in args.refs]
    for t in targets:
        try:
            out = fn(s, t)
        except ApiError as e:
            print(red("[✗] ") + t["title"] + dim(f"  {e}"), file=sys.stderr)
            continue
        if out is not None:
            s.todos[out["id"]] = out
        yield t, out


def simple_action(verb_path: str, label: str, color=green):
    def run(args):
        scope = SCOPES.get(getattr(args, "scope", None) or "", None)
        for t, out in each_todo(args, lambda s, t: _scope_retry("POST", f"/todos/{t['id']}/{verb_path}", {}, scope)):
            line = OK() + f" {label} " + t["title"]
            if verb_path == "complete" and out and out.get("status") == "open" and out.get("whenDate"):
                line += dim(f"  ↻ next: {fmt_date(out['whenDate'])}")
            print(line)
    return run


def cmd_edit(args):
    s = Store(args.profile)
    t = resolve_todo(s, args.ref)
    b = build_fields(s, args, False)
    if args.add_tag or args.rm_tag or args.tags is not None:
        tags = list(t.get("tags") or []) if args.tags is None else [g.lstrip("#") for g in args.tags]
        tags += [g.lstrip("#") for g in (args.add_tag or []) if g.lstrip("#") not in tags]
        rm = {g.lstrip("#").casefold() for g in (args.rm_tag or [])}
        b["tags"] = [g for g in tags if g.casefold() not in rm]
    if args.subtitle is not None:
        b["subtitle"] = args.subtitle or None
    if not b and not args.project and args.title is None:
        die("nothing to change (see it edit -h)")
    out = t
    if b:
        scope = SCOPES.get(args.scope or "", None)
        out = _scope_retry("PATCH", f"/todos/{t['id']}", b, scope)
    if args.project:
        dest = None if args.project in ("none", "-") else find_container(s, args.project)["id"]
        out = _scope_retry("POST", f"/todos/{t['id']}/move", {"parentId": dest}, SCOPES.get(args.scope or ""))
    if args.json:
        return print(json.dumps(out, indent=2, ensure_ascii=False))
    s.todos[out["id"]] = out
    print(OK() + " updated " + todo_line(s, out, None))


def cmd_move(args):
    s = Store(args.profile)
    dest = None if args.project in ("none", "-") else find_container(s, args.project)["id"]
    for t, out in each_todo(args, lambda s, t: _scope_retry(
            "POST", f"/todos/{t['id']}/move", {"parentId": dest}, SCOPES.get(args.scope or ""))):
        print(OK() + " moved " + t["title"] + dim(" → " + (s.crumb(dest) or "no project")))


def cmd_when(args):
    d = parse_date(args.date)
    body = {"whenDate": None if d == "null" else d}
    scope = SCOPES.get(args.scope or "")
    for t, out in each_todo(args, lambda s, t: _scope_retry("PATCH", f"/todos/{t['id']}", body, scope)):
        print(OK() + " scheduled " + t["title"] + dim("  → " + (fmt_date(body["whenDate"]) or "unscheduled")))


def cmd_sub(args):
    s = Store(args.profile)
    t = resolve_todo(s, args.ref)
    if args.add:
        for title in args.add:
            api("POST", f"/todos/{t['id']}/subtasks", body={"title": title})
            print(OK() + " subtask " + title)
        return
    subs = t.get("subtasks") or []
    if args.check or args.uncheck:
        for ref in (args.check or args.uncheck):
            if ref.isdigit() and 1 <= int(ref) <= len(subs):
                st = subs[int(ref) - 1]
            else:
                hits = [x for x in subs if x["id"].startswith(ref) or ref.casefold() in x["title"].casefold()]
                if len(hits) != 1:
                    die(f"subtask '{ref}' matched {len(hits)}")
                st = hits[0]
            api("PATCH", f"/todos/{t['id']}/subtasks/{st['id']}", body={"done": bool(args.check)})
            print((green("[✓] ") if args.check else dim("[ ] ")) + st["title"])
        return
    for i, st in enumerate(subs, 1):
        print(dim(f"{i:>3} ") + (green("[✓]") if st.get("done") else dim("[ ]")) + " " + st["title"])
    if not subs:
        print(dim("no subtasks.") + tree("  // ") + dim("it sub <todo> -a 'title'"))


def cmd_search(args):
    q = " ".join(args.query)
    r = api("GET", "/search", {"q": q, "limit": args.limit, "profile": args.profile,
                               "include_trashed": "true" if args.trashed else None})
    hits = r.get("data", [])
    if args.json:
        return print(json.dumps(hits, indent=2, ensure_ascii=False))
    ids = []
    s = Store(args.profile)
    for h in hits:
        icon = {"todo": dim("[ ]"), "container": green("▸"), "note": dim("✎"), "inbox": magenta("↓"),
                "attachment": dim("+")}.get(h["kind"], "·")
        idx = ""
        if h["kind"] == "todo":
            ids.append(h["id"])
            idx = dim(f"{len(ids):>3} ")
            t = s.todos.get(h["id"])
            if t and t["status"] == "done":
                icon = dim("[") + green("✓") + dim("]")
        else:
            idx = "    "
        extra = []
        if h["field"] not in ("title", "name", "text"):
            extra.append(dim(f"(in {h['field']})"))
        if h.get("crumb"):
            extra.append(dim(h["crumb"].lower()))
        if h.get("trashed"):
            extra.append(red("trashed"))
        print(f"{idx}{dim(h['id'][:6])} {icon} {h['label']}  " + "  ".join(extra))
    if not hits:
        print(dim(f"no results for '{q}'."))
    save_last(ids)


def cmd_file(args):
    """File an inbox capture into a project (or anytime)."""
    last = load_last()
    ref = args.ref
    cid = None
    if ref.isdigit() and 1 <= int(ref) <= len(last) and last[int(ref) - 1].startswith("capture:"):
        cid = last[int(ref) - 1].split(":", 1)[1]
    else:
        caps = paged("/inbox", {"status": "open"})
        hits = [c for c in caps if c["id"].lower().startswith(ref.lower())
                or ref.casefold() in (c.get("text") or c.get("title") or "").casefold()]
        if len(hits) != 1:
            die(f"capture '{ref}' matched {len(hits)} open inbox items")
        cid = hits[0]["id"]
    body = {}
    if args.project:
        body["parentId"] = find_container(Store(args.profile), args.project)["id"]
    r = api("POST", f"/inbox/{cid}/move", body=body)
    todo = r.get("todo") if isinstance(r, dict) else None
    print(OK() + " filed " + ((todo or {}).get("title") or cid[:6]))


def cmd_archive_capture(args):
    for ref in args.refs:
        last = load_last()
        if ref.isdigit() and 1 <= int(ref) <= len(last) and last[int(ref) - 1].startswith("capture:"):
            cid = last[int(ref) - 1].split(":", 1)[1]
        else:
            cid = ref
        api("POST", f"/inbox/{cid}/archive", body={})
        print(OK() + " archived capture " + cid[:6])


def cmd_project_new(args):
    s = Store(args.profile)
    b = {"kind": args.kind, "name": " ".join(args.name)}
    if args.area:
        b["parentId"] = find_container(s, args.area, kinds=("area", "project"))["id"]
        if args.kind == "project" and s.containers[b["parentId"]]["kind"] != "area":
            b["kind"] = "subproject"
    if args.deadline:
        b["deadline"] = parse_date(args.deadline)
    if args.color:
        b["colorHex"] = args.color
    if args.profile and args.profile != "all":
        b["profile"] = args.profile
    c_ = api("POST", "/containers", body=b)
    print(OK() + f" created {c_['kind']} " + c_["name"] + dim(f"  {c_['id'][:6]}"))


def cmd_open(args):
    if not args.ref:
        url = f"{APP}/today"
    else:
        t = resolve_todo(Store(args.profile), args.ref)
        url = f"{APP}/today/t/{t['id']}"
    print(url)
    webbrowser.open(url)


def cmd_tui(args):
    """Launch the Textual TUI with the project's venv (falls back to this Python)."""
    import subprocess
    here = Path(__file__).resolve().parent
    py = next((str(c) for c in (here / ".venv" / "Scripts" / "python.exe", here / ".venv" / "bin" / "python")
               if c.exists()), sys.executable)
    sys.exit(subprocess.call([py, "-X", "utf8", str(here / "itui.py")]))


def cmd_api(args):
    body = None
    if args.data:
        body = json.loads(sys.stdin.read() if args.data == "-" else args.data)
    params = dict(kv.split("=", 1) for kv in (args.param or []))
    path = args.path.replace("\\", "/")
    # Git Bash rewrites "/tags" into "C:/Program Files/Git/tags"; undo that.
    m = re.match(r"^[A-Za-z]:/.*?/Git(/.*)$", path)
    if m:
        path = m.group(1)
    path = re.sub(r"^(https?://[^/]+)?(/v1)?", "", path)
    r = api(args.method.upper(), path if path.startswith("/") else "/" + path, params, body)
    print(json.dumps(r, indent=2, ensure_ascii=False))


# ───────────────────────────── argparse ────────────────────────────────────

HELP = """\
init.Tasks from the terminal.

views:    today  inbox  anytime  someday  planned  logbook
lists:    ls [-p project] [-t tag]   projects   tags   search <text>
to-dos:   add  show  done  undone  edit  when  mv  sub  park  rm  restore  open
inbox:    capture <text|url>   file <#> [project]   archive <#>
other:    tui  login  whoami  newproject  api

<todo> can be: the number from the last list (it done 3), an id prefix (it done a1b2c3),
a full id, or part of the title (it done "dentist").
Dates: today, tomorrow, fri, next fri, +3, +2w, 11-20, 2026-11-20, none.
"""


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="raw JSON output")
    common.add_argument("-P", "--profile", help="profile name or id (default: all)")

    p = argparse.ArgumentParser(prog="it", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def add(name, fn, help_, aliases=()):
        sp = sub.add_parser(name, help=help_, aliases=list(aliases), parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    sp = add("login", cmd_login, "save your API token")
    sp.add_argument("token", nargs="?")
    sp.add_argument("-f", "--file", help="read the token from a file")
    add("whoami", cmd_whoami, "show the token's scopes")

    sp = add("today", cmd_today, "the Today view", aliases=["t"])
    sp.add_argument("-d", "--done", action="store_true", help="include done today")
    add("inbox", cmd_inbox, "the Inbox view", aliases=["in"])
    add("anytime", cmd_anytime, "the Anytime view", aliases=["any"])
    add("someday", cmd_someday, "the Someday view")
    sp = add("planned", cmd_planned, "the Planned (upcoming) view", aliases=["up", "upcoming"])
    sp.add_argument("-w", "--week", action="store_true", help="only the next six days")
    sp = add("logbook", cmd_logbook, "completed to-dos", aliases=["log", "completed"])
    sp.add_argument("-n", "--days", type=int, default=7)

    sp = add("ls", cmd_ls, "list to-dos with filters", aliases=["list"])
    sp.add_argument("-p", "--project", help="area / project name")
    sp.add_argument("-t", "--tag", action="append")
    sp.add_argument("-s", "--status", choices=["open", "someday", "done"])
    sp.add_argument("-a", "--all", action="store_true", help="every status")
    sp.add_argument("--trashed", action="store_true")
    sp = add("projects", cmd_projects, "areas and projects tree", aliases=["areas", "pj"])
    sp.add_argument("--archived", action="store_true")
    add("tags", cmd_tags, "list tags")

    sp = add("show", cmd_show, "full details of a to-do", aliases=["s"])
    sp.add_argument("ref")

    sp = add("add", cmd_add, "create a to-do", aliases=["a"])
    sp.add_argument("title", nargs="+")
    sp.add_argument("-w", "--when", help="start date (today, fri, +3, 11-20 …)")
    sp.add_argument("--at", help="start time HH:mm")
    sp.add_argument("--duration", type=int, help="minutes")
    sp.add_argument("-d", "--deadline")
    sp.add_argument("-p", "--project")
    sp.add_argument("-H", "--heading")
    sp.add_argument("-t", "--tags", nargs="+")
    sp.add_argument("-n", "--notes")
    sp.add_argument("--priority", choices=["low", "medium", "high"])
    sp.add_argument("--color")
    sp.add_argument("-s", "--subtask", action="append", help="repeatable")
    sp.add_argument("--someday", action="store_true")
    sp.add_argument("--repeat", help="recurrence rule (string or JSON, see devs.inittasks.com)")

    sp = add("capture", cmd_capture, "quick-capture text or a URL to the inbox", aliases=["c"])
    sp.add_argument("text", nargs="+", help="text, a URL, or - for stdin")
    sp.add_argument("-w", "--when")
    sp.add_argument("--note", nargs="?", const=True, help="capture as a note (optional title)")

    for name, path, label, aliases, h in (
            ("done", "complete", "done", ["x", "complete"], "complete to-dos"),
            ("undone", "uncomplete", "reopened", ["reopen"], "un-complete to-dos"),
            ("park", "someday", "parked", [], "move to Someday"),
            ("unschedule", "anytime", "unscheduled", ["anytime-it"], "clear date, move to Anytime"),
            ("rm", "trash", "trashed", ["trash", "del"], "move to trash"),
            ("restore", "restore", "restored", [], "restore from trash")):
        sp = add(name, simple_action(path, label, red if path == "trash" else green), h, aliases=aliases)
        sp.add_argument("refs", nargs="+", metavar="todo")
        sp.add_argument("--scope", choices=["this", "future", "all"], help="for repeating to-dos")

    sp = add("edit", cmd_edit, "change a to-do", aliases=["e"])
    sp.add_argument("ref")
    sp.add_argument("--title")
    sp.add_argument("--subtitle")
    sp.add_argument("-w", "--when")
    sp.add_argument("--at")
    sp.add_argument("--duration", type=int)
    sp.add_argument("-d", "--deadline")
    sp.add_argument("-n", "--notes", help="replace the description ('' clears)")
    sp.add_argument("-t", "--tags", nargs="*", help="replace all tags")
    sp.add_argument("-T", "--add-tag", action="append", dest="add_tag")
    sp.add_argument("--rm-tag", action="append")
    sp.add_argument("--priority", choices=["low", "medium", "high", "none"])
    sp.add_argument("--color")
    sp.add_argument("--repeat")
    sp.add_argument("-p", "--project", help="move to project ('none' for no project)")
    sp.add_argument("--scope", choices=["this", "future", "all"])

    sp = add("when", cmd_when, "set the start date of to-dos", aliases=["sched"])
    sp.add_argument("date")
    sp.add_argument("refs", nargs="+", metavar="todo")
    sp.add_argument("--scope", choices=["this", "future", "all"])

    sp = add("mv", cmd_move, "move to-dos to a project", aliases=["move"])
    sp.add_argument("project")
    sp.add_argument("refs", nargs="+", metavar="todo")
    sp.add_argument("--scope", choices=["this", "future", "all"])

    sp = add("sub", cmd_sub, "list / add / tick subtasks")
    sp.add_argument("ref")
    sp.add_argument("-a", "--add", action="append")
    sp.add_argument("-c", "--check", nargs="+")
    sp.add_argument("-u", "--uncheck", nargs="+")

    sp = add("search", cmd_search, "search everything", aliases=["find", "/"])
    sp.add_argument("query", nargs="+")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--trashed", action="store_true")

    sp = add("file", cmd_file, "file an inbox capture as a to-do")
    sp.add_argument("ref")
    sp.add_argument("project", nargs="?")
    sp = add("archive", cmd_archive_capture, "archive inbox captures")
    sp.add_argument("refs", nargs="+")

    sp = add("newproject", cmd_project_new, "create an area or project", aliases=["np"])
    sp.add_argument("name", nargs="+")
    sp.add_argument("-k", "--kind", choices=["area", "project", "subproject"], default="project")
    sp.add_argument("-a", "--area", help="parent area (or project, for a subproject)")
    sp.add_argument("-d", "--deadline")
    sp.add_argument("--color")

    sp = add("open", cmd_open, "open a to-do in the web app")
    sp.add_argument("ref", nargs="?")

    add("tui", cmd_tui, "full-screen terminal UI")

    sp = add("api", cmd_api, "raw API call: it api GET /views/today -q today=2026-11-13")
    sp.add_argument("method")
    sp.add_argument("path")
    sp.add_argument("-q", "--param", action="append", help="key=value query param")
    sp.add_argument("-d", "--data", help="JSON body, or - for stdin")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = build_parser()
    args = p.parse_args(argv or ["today"])
    if not getattr(args, "fn", None):
        p.print_help()
        return
    try:
        # first run: no key yet → walk through setup, then carry on with the command
        if args.fn not in (cmd_login, cmd_tui) and not has_token():
            if not interactive():
                die("no API key yet. Run `it login` in a terminal (or set INITTASKS_TOKEN).")
            if not setup_wizard():
                sys.exit(1)
        args.fn(args)
    except (ApiError, CliError) as e:
        print(red("error: ") + str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
