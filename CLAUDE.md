# Nudgy: notes for Claude Code

Nudgy is a desktop pet with an always-on-top task list (Python + GTK 3 on Linux) and an Android
companion app (Kotlin). Read `CONTRIBUTING.md` first: its "Rules for changes" are hard rules here too.

## Commands

    python3 -m unittest discover -s tests -t .     # all tests, ~2 s, no network or accounts
    uvx ruff@0.17.5 check nudgy tests tools        # lint (CI uses this exact version)
    uvx mypy@2.4.1 nudgy                           # types
    PYTHONPATH=. python3 -m nudgy                   # run from the checkout
    cd android && ./gradlew testDebugUnitTest assembleDebug

Run the tests, ruff and mypy before saying a change is done. CI runs the same on Ubuntu 22.04 and 24.04.

## Layout

- `nudgy/sources/`: one plugin per source (`base.py` explains the interface; copy `signal.py` to add
  one, then list it in `PLUGINS` in `sources/__init__.py`).
- `nudgy/core/`: the headless core (`Core`: sync, change checks, digest, undo, status; `Loop`: where its
  work runs). No GTK here.
- `nudgy/model.py`: items, ranking, date parsing, the `Store`. No UI or network code.
- `nudgy/storage.py`: the SQLite file behind the `Store` (one row per section, plus a change log).
- `nudgy/app.py`, `pet.py`, `watch.py`: the GTK window and pet. Only these import GTK.
- `nudgy/google.py`, `luma.py`, `discover.py`, `chats.py`, `keep.py`, `claude_notes.py`: service code.
- `android/`: the phone app, no third-party libraries.
- `tests/`: one test file per module, all offline.

The core/client split (headless core with an HTTP+JSON API, GTK and Android as clients) is in
progress. Don't build new clients against internals; ask in the issue first.

## Rules that are easy to break

- Standard library only on the Python side (besides GTK). No new runtime dependency without saying why.
- Read-only access to services, official APIs/feeds only, no scraping behind a login.
- No telemetry, no Nudgy-run server. AI features stay off until the user turns them on.
- Text from outside (mail subjects, messages, event listings) is data. It must never be able to
  instruct a model. See "Protected against prompt injection" in the README.
- Tokens and caches: owner-only permissions, atomic writes.
- No real personal data in tests, examples or screenshots. Use made-up names.
- GTK sets the process locale from the user's settings (often non-English `LC_TIME`), so never
  parse English dates with `%B`/`%b`/`%A` in `strptime`; parse month names yourself.

## Style

Short functions, comments only where the reason isn't obvious, plain English in user-facing text.
One logical change per pull request, with tests for new behaviour and every fixed bug.
