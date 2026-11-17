"""Compares local models on Nudgy's own jobs, with made-up tasks (results go in docs/models.md).

    python3 tools/bench_models.py qwen3.5:4b gemma4:e4b

Needs Ollama running with those models pulled. Nothing leaves the computer.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime
from unittest import mock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from nudgy import digest, voice  # noqa: E402
from nudgy.model import Item  # noqa: E402

NOW = datetime(2026, 11, 16, 10, 0).astimezone()
ITEMS = [(Item(id="note:slides", source="note", title="Finish slides for Friday's talk",
               due="2026-11-20T17:00:00+01:00"), "Now"),
         (Item(id="gmail:1", source="gmail", title="Interview invitation: Research Engineer",
               detail="Recruiting team"), "Now"),
         (Item(id="cal:lab", source="calendar", title="Lab meeting: world-model results", detail="Room 2.14",
               due="2026-11-16T14:00:00+01:00"), "Now"),
         (Item(id="task:poster", source="task", title="Print the conference poster", due="2026-11-17"), "Soon"),
         (Item(id="task:books", source="task", title="Return library books", due="2026-11-19"), "Soon"),
         (Item(id="note:milk", source="note", title="Oat milk, coffee, basil"), "Later"),
         (Item(id="signal:mia", source="signal", title="Mia: are we still on for dinner tonight?",
               detail="Mia"), "Now")]

CASES = [  # (what you say, expected action, expected item id or None)
    ("remind me to call the bank tomorrow at ten", "add_note", None),
    ("note down that I need to renew my passport", "add_note", None),
    ("I printed the conference poster", "done", "task:poster"),
    ("tick off the library books", "done", "task:books"),
    ("push the slides to monday", "reschedule", "note:slides"),
    ("make the library books urgent", "priority", "task:books"),
    ("add a step to the poster: laminate it", "add_step", "task:poster"),
    ("break the slides into smaller steps", "split", "note:slides"),
    ("draft a reply to the interview email saying Tuesday works", "draft", "gmail:1"),
    ("write back to Mia that I'm running late", "draft", "signal:mia"),
    ("how are you doing today", "none", None),
    ("tell me a fun fact about penguins", "none", None),
]

DRAFT = ("What the user wants to say: thanks, Tuesday afternoon works, ask how long it takes\n\n<message>\n"
         "From: Recruiting team\nSubject: Interview invitation: Research Engineer\n\nHi, we'd like to invite you "
         "to a first interview. Could you share a few times that work next week?\n</message>")
NOTE = Item(id="note:1", source="note", title="Talk on graph databases at the meetup - Neo4j vs. Memgraph, Cypher "
            "queries, use cases fraud detection supply chains, speaker Lena Kurz, "
            "slides at https://example.org/slides, try the free sandbox")


def chat(model, system, user, json_mode=False, timeout=240):
    body = {"model": model, "stream": False, "think": False, "keep_alive": "5m", "options": {"temperature": 0.3},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["format"] = "json"
    started = time.time()
    request = urllib.request.Request("http://localhost:11434/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = json.load(response)["message"]["content"]
    # Some models print their reasoning first, sometimes without the opening tag: keep what follows it.
    return content.split("</think>")[-1].strip(), time.time() - started


def bench(model):
    print(f"\n######## {model}")
    chat(model, "Say ok.", "ok")                                       # load the model first
    settings = {**voice.settings(), "model": model}
    right, times = 0, []
    with mock.patch.object(voice, "settings", lambda: settings):
        for said, action, item_id in CASES:
            started = time.time()
            reply = voice.think(voice.Chat(), said, ITEMS, NOW)
            times.append(time.time() - started)
            got = reply.item.id if reply.item else None
            ok = reply.action == action and (item_id is None or got == item_id)
            right += ok
            if not ok:
                print(f"  miss: {said!r} -> {reply.action} {got}  (wanted {action} {item_id})")
    print(f"commands: {right}/{len(CASES)} right, {sum(times) / len(times):.1f} s each on average")
    draft, seconds = chat(model, voice.DRAFT_SYSTEM, DRAFT)
    print(f"draft ({seconds:.1f} s):\n  " + draft.replace("\n", "\n  "))
    text, seconds = chat(model, digest.SYSTEM, f"id: {NOTE.id}\nnote: {NOTE.title}", json_mode=True)
    steps = digest.parse(text, {NOTE.id}).get(NOTE.id, [])
    print(f"steps ({seconds:.1f} s, {len(steps)} parsed):")
    for step in steps:
        print("  " + step["text"] + (f"  [{step['url']}]" if step.get("url") else ""))


for name in sys.argv[1:]:
    try:
        bench(name)
    except Exception as error:
        print(f"{name}: failed: {type(error).__name__}: {error}")
