"""nudgy                  open the window
nudgy toggle           hide the pet to the top bar, or bring it back (bound to Super+T)
nudgy open             open the task list
nudgy talk             talk to the pet (bound to Super+Shift+T); click the pet when you're done
nudgy voice setup      install the local speech parts (Whisper, Piper) and the small model, once
nudgy voice on|off     allow or stop talking to the pet
nudgy voice test       record one sentence and show what it heard, how long each step took
nudgy voice ask TEXT   ask the pet something by typing, and print its answer
nudgy focus            today's focus summary (time on task vs drifting)
nudgy focus test TITLE [APP]   run the focus judges on a window title and show who decided
nudgy focus on|off     turn the pet's focus check on or off
nudgy auth             sign in to Google (read-only)
nudgy link signal      link Signal: scan the QR code with Signal > Settings > Linked devices
nudgy link whatsapp    link WhatsApp: scan with WhatsApp > Linked devices (unofficial, read-only)
nudgy unlink whatsapp  unlink WhatsApp and stop its bridge
nudgy luma LINK        connect Luma with your personal calendar link (Luma > Settings > Calendar Syncing)
nudgy luma off         disconnect Luma
nudgy keep             connect Google Keep through the official API (Workspace accounts)
nudgy keep off         disconnect Google Keep
nudgy accounts         show which services are connected
nudgy list             print the important items in the terminal (--all for everything)
nudgy sync             fetch from every service once and print any problems
nudgy phone FOLDER     share what needs you now with the Android pet through FOLDER (e.g. a Syncthing folder)
nudgy phone off        stop sharing with the phone
nudgy events           search public event listings now (otherwise every 6 hours) and print the best
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from . import chats, google, keep, luma, sources
from .model import Store, bucket, config_dir, data_dir, friendly_time, important, ranked


def private_folders() -> None:
    """Nudgy stores sign-in tokens, mail subjects and chat previews: only you may read them.
    Settings from before the rename (Taskpin) move over the first time."""
    os.umask(0o077)
    for folder in (config_dir(), data_dir()):
        old = folder.with_name("taskpin")
        if old.is_dir() and not folder.exists():
            old.rename(folder)
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        for path in folder.glob("*.json"):          # files from older versions may be group-readable
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)


def voice_command(rest: list[str], moment) -> int:
    import time
    from . import voice
    if rest[:1] == ["setup"]:
        ok = voice.setup()
        if ok:
            voice.save_settings({"enabled": True})
            print("Ready. Talk with Super+Shift+T, or right-click the pet > Talk to Nudgy.")
        return 0 if ok else 1
    if rest[:1] in (["on"], ["off"]):
        voice.save_settings({"enabled": rest[0] == "on"})
        print(f"Talking is {rest[0]}.")
        return 0
    store = Store()
    scored = ranked(store.visible(moment), moment, store.overrides, store.places)
    items = [(i, bucket(v)) for i, v in scored]
    if rest[:1] == ["ask"] and len(rest) > 1:
        started = time.time()
        reply = voice.think(voice.Chat(), " ".join(rest[1:]), items, moment)
        details = {k: getattr(reply, k) for k in ("note", "when", "level", "text", "minutes", "command")
                   if getattr(reply, k)}
        print(f"{reply.say}\n  action: {reply.action}" + (f" -> {reply.item.title[:60]}" if reply.item else "") +
              (f" {details}" if details else "") + f"\n  ({time.time() - started:.1f} s, {voice.settings()['model']})")
        return 0
    if rest[:1] == ["test"]:
        import threading
        trouble = voice.problem()
        if trouble and "off" not in trouble:
            print(trouble)
            return 1
        worker = voice.Worker()
        print("Say something (it stops when you pause)…")
        started = time.time()
        try:
            audio = voice.listen(threading.Event())
        except OSError as error:
            print(f"Couldn't open the microphone: {error}")
            return 1
        heard = time.time()
        if not audio:
            print("Heard nothing. Check the microphone in Settings > Sound > Input.")
            return 1
        text, language = voice.transcribe(worker, audio)
        understood = time.time()
        print(f"Heard ({language}, {len(audio) / 32000:.1f} s of audio): {text}")
        reply = voice.think(voice.Chat(), text, items, moment, language)
        answered = time.time()
        print(f"Nudgy: {reply.say}  [{reply.action}]")
        player = voice.say_aloud(worker, reply.say, language)
        spoke = time.time()
        if player:
            player.wait()
        worker.stop()
        print(f"Times: listening {heard - started:.1f} s, words {understood - heard:.1f} s, "
              f"answer {answered - understood:.1f} s, voice {spoke - answered:.1f} s")
        return 0
    print("Usage: nudgy voice setup | on | off | test | ask TEXT")
    return 2


def main() -> int:
    private_folders()
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    moment = datetime.now().astimezone()
    if command == "run":
        from .app import run
        return run()
    if command in ("toggle", "open", "talk"):
        from .app import remote
        return remote({"toggle": "toggle", "open": "open-list", "talk": "talk"}[command])
    if command == "voice":
        return voice_command(sys.argv[2:], moment)
    if command == "focus":
        from . import watch
        rest = sys.argv[2:]
        if rest[:1] in (["on"], ["off"]):
            watch.save_config({"enabled": rest[0] == "on"})
            print(f"Focus check {rest[0]}. Restart Nudgy or use the pet's menu for it to take effect now.")
            return 0
        if rest[:1] == ["test"] and len(rest) >= 2:
            store = Store()
            items = ranked(store.visible(moment), moment, store.overrides, store.places)
            tasks = [(i.title, friendly_time(i, moment)) for i, v in items if bucket(v) == "Now"]
            if not tasks:
                print("Nothing is urgent right now, so the pet would not judge. Testing against your top 3 items.")
                tasks = [(i.title, friendly_time(i, moment)) for i, _ in items[:3]]
            judge = watch.Judge()
            app = rest[2] if len(rest) > 2 else "firefox"
            verdict = judge.decide(app, rest[1], tasks)
            print(f"{verdict.verdict} ({verdict.confidence:.2f}) by {verdict.judge}: {verdict.reason}")
            if verdict.task:
                print(f"Task: {verdict.task}")
            if judge.claude_calls:
                print(f"Claude calls: {judge.claude_calls}, tokens: {judge.claude_tokens}")
            return 0
        print(watch.summary())
        return 0
    if command == "auth":
        google.authorize()
        print("Signed in.")
        return 0
    if command == "sync":
        store = Store()
        items, errors = sources.sync_all(moment)
        store.synced.update(items)
        store.save()
        for name, group in items.items():
            print(f"{name}: {len(group)} items")
        for name, reason in errors.items():
            print(f"{name}: FAILED: {reason}", file=sys.stderr)
        return 1 if errors else 0
    if command == "accounts":
        for name, state in sources.status().items():
            print(f"{name:9} {state}")
        return 0
    if command == "link" and sys.argv[2:3] == ["signal"]:
        cli = chats.signal_cli()
        if not cli:
            print("signal-cli is not installed. See the README.", file=sys.stderr)
            return 1
        # In a terminal signal-cli prints the QR code itself.
        return subprocess.call([cli, "link", "-n", "Nudgy"])
    if command == "link" and sys.argv[2:3] == ["whatsapp"]:
        print("Scan with WhatsApp > Settings > Linked devices > Link a device.")
        chats.link_whatsapp(lambda qr: subprocess.run(
            chats.bridge_command("qr-terminal", qr), env=chats.bridge_env()))
        print("Linked. The bridge now runs in the background.")
        return 0
    if command == "unlink" and sys.argv[2:3] == ["whatsapp"]:
        chats.unlink_whatsapp()
        print("Unlinked.")
        return 0
    if command == "luma" and len(sys.argv) == 3:
        if sys.argv[2] == "off":
            luma.forget_feed()
            print("Luma disconnected.")
            return 0
        luma.save_feed(sys.argv[2])
        events = luma.fetch_luma(moment)
        print(f"Luma connected: {len(events)} upcoming events in the next {luma.DAYS_AHEAD} days.")
        return 0
    if command == "keep":
        if sys.argv[2:3] == ["off"]:
            keep.disconnect()
            print("Google Keep disconnected.")
            return 0
        try:
            count = keep.connect()
        except (keep.KeepRefused, google.NeedsSignIn) as error:
            keep.disconnect()
            print(f"Google refused Keep access: {error}", file=sys.stderr)
            return 1
        print(f"Google Keep connected: {count} notes readable.")
        return 0
    if command == "whatsapp-bridge":
        os.execve(chats.bridge_command("run")[0], chats.bridge_command("run"), chats.bridge_env())
    if command == "phone":
        from . import phone
        rest = sys.argv[2:]
        if rest[:1] == ["off"]:
            phone.set_folder(None)
            print("No longer sharing with the phone.")
        elif rest:
            target = os.path.abspath(os.path.expanduser(rest[0]))
            phone.set_folder(target)
            print(f"Nudgy will keep {target}/{phone.FILE} up to date. Share that folder with your phone,\n"
                  "then pick it in the Nudgy app. Restart Nudgy (or wait for the next update) to write it.")
        else:
            print(f"Sharing through {phone.folder()}" if phone.folder() else "Not sharing with a phone.")
        return 0
    if command == "events":
        from . import discover
        found, applied = discover.fetch(moment, force=True, applied=google.luma_applications)
        found = discover.drop_known(found, [], applied)
        for item in found:
            print(f"{'*' if 'top' in item.tags else ' '} {friendly_time(item, moment):16} {item.title[:60]}\n"
                  f"{'':19}{item.detail}  {item.url}")
        print(f"{len(found)} events. Sources: ~/.config/nudgy/discover.json")
        return 0
    if command == "list":
        store = Store()
        everything = "--all" in sys.argv[2:]
        scored = ranked(store.visible(moment), moment, store.overrides, store.places)
        shown = [(i, v) for i, v in scored if everything or important(i, v)]
        for item, value in shown:
            print(f"[{bucket(value):5}] {item.source:8} {item.title}  {friendly_time(item, moment)}")
            for step in store.steps.get(item.id, []):
                print(f"{'':17}[{'x' if step['done'] else ' '}] {step['text']}")
        if len(shown) < len(scored):
            print(f"({len(scored) - len(shown)} less urgent items hidden; nudgy list --all shows them)")
        return 0
    print(__doc__)
    return 0 if command in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
