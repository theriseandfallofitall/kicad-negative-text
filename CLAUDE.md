# CLAUDE.md

KiCad 10 action plugin: turns text into negative (etched out of a pour) or positive copper. User guide in `docs/usage.md`; pcbnew API traps in `docs/implementation-notes.md` - read those before touching zone or group code.

- `negative_text.py` is the core (no wx); `action_negative_text.py` is the toolbar action and the modeless wx dialog.
- The dialog only works inside pcbnew (`pcbnew.GetCurrentSelection()` and the editor refresh calls). Do not open it from a standalone Python: it pops errors on the user's screen. Test core functions with KiCad's bundled Python and `pcbnew.LoadBoard()` instead.
- No test suite yet; verify in pcbnew on a demo board (`share/kicad/demos/ecc83`).

## Repo layout (keep it organised)

Checked by `.githooks/layout_check.py` (settings in `.layout.json`), run by `.githooks/pre-commit` on
every commit (turn on once per clone: `git config core.hooksPath .githooks`). A failure is fixed by
moving or documenting the file, never by loosening the check or committing with `--no-verify`
without the owner's say-so.

- **Root** holds only the files in `.layout.json` `rootFiles`. Everything else goes in a folder. The repo root IS the plugin package (KiCad imports it as `negative_text` via a symlink or junction), so `__init__.py`, `action_negative_text.py` and `negative_text.py` stay at the root and keep their names.
- **Folders:** each has one job, written in README, Repo map. A new top-level folder only when none
  fits, added to the map in the same commit.
- **Docs:** `README.md` stays a short overview, a Repo map and a Docs index; detail goes in `docs/`,
  one doc per area, each with a row in README, Docs. Rule docs UPPER-CASE, the rest lower-case kebab.
  Research in `docs/research/`, finished trackers and task lists in `docs/archive/`. Write about a
  topic in the doc that owns it; link, do not repeat.
- **Open work** lives in the GitHub issue tracker; a one-off task list is
  archived when done.
- **Scripts and tests:** scripts in their group folder (`.layout.json` `groupedDirs`), never loose;
  each named in the code map when there is one. Tests named and placed as `.layout.json` `tests`
  says, every one run by the test command; helpers and fixtures in their subfolders.
- **Generated output and scratch** never go in git; add them to `.gitignore`. Temp files go outside
  the repo.
- **Moving things:** allowed when it makes the layout clearer. In the same commit: update every
  mention (`git grep` the old path; the check fails on a backticked path that no longer exists) and
  add old → new rows to `docs/moves.md`, with what else changes outside the repo. Other repos,
  servers and LLM sessions use that log to fix their paths. A path in another repo is written with
  its repo name (`other-repo/docs/...`).
- **Keep stable across moves:** anything that is an id or a URL rather than just a file (public
  URLs, API paths, ids stored in data, package or module names other code imports, deploy entry
  points). Move the file, keep the id or URL, or add a redirect and a `docs/moves.md` row.
