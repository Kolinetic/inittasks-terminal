# it — init.Tasks CLI

A single-file, stdlib-only Python client for the [init.Tasks](https://inittasks.com) public REST API
(`https://api.inittasks.com/v1`, spec at `https://api.inittasks.com/openapi.json`, docs at devs.inittasks.com).

There is also **`itui`**, a full-screen terminal UI (see [TUI](#tui) below).

## Install

### Linux / macOS

```sh
git clone https://github.com/Kolinetic/inittasks-terminal.git
cd inittasks-terminal && ./install.sh   # first install asks for your api key
```

`install.sh` makes `.venv` (with `uv` if present, else `python3 -m venv`), installs Textual for the TUI,
and writes `it` and `itui` launchers to `~/.local/bin` (`BIN_DIR=… ./install.sh` to change). Needs Python 3.10+.
After a `git pull`, rerun it only if `requirements.txt` changed.

### Windows

`install.sh` is for Linux/macOS. On Windows, from the repo folder:

```powershell
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv\Scripts\python.exe it.py login   # or just run it: the first run asks for your key
.venv\Scripts\python.exe itui.py
```

Put small `it.cmd` / `itui.cmd` shims on your PATH if you want the short names.

### Token

The first time you run `it`, `itui` or `install.sh` without a key, a short setup asks for one: create a
personal access token in init.Tasks under **settings → api keys** (read + write for todos, containers and
inbox; read for tags and profiles), paste it, and it's checked against the API before it's saved. It warns
if the key is missing any of those scopes. Run `it login` any time to replace the key.

`it login` (or `it login KEY`, `it login -f token.txt` for scripts) saves it to `~/.config/inittasks/config.json`
(mode 600), along with the TUI's theme.
`INITTASKS_TOKEN` overrides it. Tokens are personal access tokens (`itk_…`) made in the app; the token is never
stored in this repo, so log in once on each machine.

## Referring to to-dos

Anywhere a command takes `<todo>` you can pass:

| Form | Example |
|---|---|
| number from the last list printed | `it done 3` |
| id prefix (≥4 hex chars) | `it done a1b2c3` |
| full id | `it show A1B2C3D4-…` |
| part of the title | `it done "dentist"` |

Dates: `today`, `tomorrow`/`tom`, `fri`, `next fri`, `+3`, `+2w`, `+1m`, `11-20`, `2026-11-20`, `none` (clears).

## Commands

```text
it                      # = it today
it today [-d]           # Today (+ done today)
it inbox                # unsorted to-dos + inbox captures
it anytime | someday | planned [-w] | logbook [-n DAYS]
it ls [-p PROJECT] [-t TAG] [-s open|someday|done] [-a]
it projects             # areas/projects tree with open counts
it tags
it search TEXT [--trashed]
it show TODO

it add TITLE [-w WHEN] [--at HH:mm] [--duration MIN] [-d DEADLINE] [-p PROJECT] [-H HEADING]
             [-t TAG ...] [-n NOTES] [--priority low|medium|high] [-s SUBTASK]... [--someday]
             [--repeat RULE]
it capture TEXT|URL|-   [--note [TITLE]] [-w WHEN]   # to the inbox
it file N [PROJECT]     # file an inbox capture as a to-do
it archive N            # archive an inbox capture

it done|undone|park|unschedule|rm|restore TODO... [--scope this|future|all]
it when DATE TODO...
it mv PROJECT TODO...   # PROJECT 'none' = no project
it edit TODO [--title] [-w] [--at] [-d] [-n] [-t TAGS...] [-T ADD_TAG] [--rm-tag TAG]
             [--priority] [--repeat RULE] [-p PROJECT] [--scope ...]
it sub TODO [-a TITLE] [-c N...] [-u N...]     # subtasks
it newproject NAME [-k area|project|subproject] [-a AREA] [-d DEADLINE]
it open [TODO]          # open in the web app

it api METHOD PATH [-q k=v]... [-d JSON|-]   # raw escape hatch, e.g. it api GET views/today -q today=2026-11-13
```

Global flags: `--json` (raw JSON output), `-P/--profile NAME` (e.g. `personal`, `work`).

Output follows the init.Tasks design system (`design/tokens.json`, see [TUI](#tui)):
`[ ]`/`[✓]` rows, `[tag]` labels, lowercase headers with counts, ` * `-separated meta, `[ok]`
confirmations, in 24-bit colour from the same theme the TUI last used (`T` in `itui`).
Override per shell with `INITTASKS_THEME=dracula` (any theme id in the tokens).
`NO_COLOR=1` turns colour off; `FORCE_COLOR=1` keeps it on when piping.

### Repeat rules

`--repeat` accepts `daily`, `weekly`, `monthly`, `yearly`, `every 3 days`, `every 2 weeks`,
`weekly:mon,thu`, `weekdays`, `monthly:15`, `none`, or the raw JSON the API stores
(`{"freq":"monthly","interval":1,"byday":[6],"bysetpos":1}` — weekdays are ISO, 1 = Mon … 7 = Sun).
A plain `monthly` uses the day of the `--when` date. Setting a repeat clears the deadline (API rule).

Repeating to-dos need `--scope` for `rm`, `mv` and some edits; the CLI tells you when.

## TUI

`itui` (or `it tui`) is a [Textual](https://textual.textualize.io) app styled on inittasks.com:
lowercase labels, `[ ]`/`[✓]` rows, `[tag]` labels and `*`-separated meta.
It is designed for JetBrains Mono, so set that as your terminal font for the full look.

Its colours come from **`design/tokens.json`**, the tokens file of the init.Tasks design system
(pulled from inittasks.com's stylesheet and the app's theme engine). Each theme in it becomes a TUI theme named `init-<id>`: github dark (default),
ayu light, github light, catppuccin mocha, dracula, nord, tokyo night and gruvbox dark. The colour
token names are the site's CSS variable names (`bg`, `text`, `muted`, `accent`, `headers`, `warn`,
`over`, `cyan`, `tree`, `panel`, …), and the TUI uses them by those names. To pick up changes, replace
`design/tokens.json`; nothing else needs editing.

Setup: `./install.sh` installs Textual into `.venv` (see [Install](#install)).

Layout: sidebar (views + area/project tree with open counts, grouped under each profile) · list · detail pane (`i` toggles;
on by default at 130+ columns).

| Key | Action |
|---|---|
| `1`–`6` | inbox · today · planned · anytime · someday · logbook |
| `j`/`k`, `↑`/`↓`, `tab` | move, switch sidebar ⇄ list |
| `enter` | open the to-do editor (title, dates, time, tags, repeat, priority, notes, subtasks); on a project, open it; on an inbox capture, file it |
| `a` / `n` | quick add, with a live preview |
| `space` / `x` | complete / reopen (`u` undoes the last complete) |
| `w` `t` `d` | when · do today · deadline |
| `m` `#` `p` `e` `s` | move · tags · cycle priority · rename · someday |
| `delete` | trash (asks which dates for a repeating to-do); archives an inbox capture |
| `/` `r` `o` | search · refresh · open in the web app |
| `T` | cycle the design system's themes (remembered) |
| `?` | help |

Quick add understands the app's plain-language syntax:
`renew passport 7 oct #paperwork [docs]`, `call mum fri at 5pm [calls] !!`,
`water plants every 3 days`, `tax return due 4/15 #taxes`, `#"multi word project"`.
`!`/`!!`/`!!!` set low/medium/high priority. In the Today view, an undated quick add lands on today.

## Notes

- The beta API sometimes returns HTTP 500 on parallel reads right after a write; `api()` retries
  GETs (never writes) up to 4 times with backoff.

- Views (`/views/*`) return ids only; the CLI fetches all to-dos once per run (one paged call) to render them.
- `GET /containers` includes trashed containers; the CLI hides them and anything under them.
- Completing a repeating to-do advances it; the CLI shows the next date.
