"""Makes the pictures in docs/images from a demo Nudgy with made-up tasks.

    python3 tools/screenshots.py

Runs a separate copy of Nudgy (its own app id and settings in a temporary folder, no accounts, no network)
next to yours, fills it with example tasks, draws its windows into images and composes them on a plain
background, so nothing from your own screen or list ends up in the pictures. Needs Pillow for the GIF.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

DEMO = Path(tempfile.mkdtemp(prefix="nudgy-demo-"))
os.environ.update(XDG_CONFIG_HOME=str(DEMO / "config"), XDG_DATA_HOME=str(DEMO / "data"),
                  NUDGY_NOTES=str(DEMO / "notes"))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT))

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from nudgy import app, digest, pet, sources  # noqa: E402
from nudgy.changes import Changes  # noqa: E402
from nudgy.model import Item, Store  # noqa: E402

NOW = datetime.now().astimezone().replace(second=0, microsecond=0)


def at(days: int = 0, hours: float = 0, hour: int | None = None, minute: int = 0) -> str:
    moment = NOW + timedelta(days=days, hours=hours)
    if hour is not None:
        moment = moment.replace(hour=hour, minute=minute)
    return moment.isoformat()


def demo_items() -> dict[str, list[Item]]:
    today = NOW.date().isoformat()
    return {
        "calendar": [
            Item(id="calendar:lab", source="calendar", title="Lab meeting: world-model results",
                 detail="Room 2.14", due=at(hours=0.75), end=at(hours=1.75), url="https://calendar.google.com"),
            Item(id="calendar:dentist", source="calendar", title="Dentist", detail="Weserstraße 12",
                 due=at(days=2, hour=9), end=at(days=2, hour=10), url="https://calendar.google.com"),
        ],
        "gmail": [
            Item(id="gmail:1", source="gmail", title="Interview invitation: Research Engineer",
                 detail="Recruiting team", url="https://mail.google.com", tags=["important"]),
            Item(id="gmail:2", source="gmail", title="Your reviews for the workshop paper are in",
                 detail="OpenReview", url="https://mail.google.com", tags=["starred"]),
        ],
        "task": [
            Item(id="task:poster", source="task", title="Print the conference poster", detail="My Tasks",
                 due=today, url="https://tasks.google.com"),
            Item(id="task:books", source="task", title="Return library books", detail="Errands",
                 due=(NOW + timedelta(days=3)).date().isoformat(), url="https://tasks.google.com"),
        ],
        "luma": [
            Item(id="luma:meetup", source="luma", title="AI Safety meetup Berlin", detail="Mitte",
                 due=at(days=4, hour=19), end=at(days=4, hour=21), url="https://luma.com"),
        ],
        "signal": [
            Item(id="signal:mia", source="signal", title="Mia: are we still on for dinner tonight?",
                 detail="Mia", tags=["direct"]),
        ],
        "potential": [
            Item(id="potential:hack", source="potential", title="Weekend research hackathon",
                 detail="Online · AI safety · prizes", due=at(days=12, hour=10), url="https://example.org",
                 tags=["fit:88", "top"]),
        ],
    }


def seed() -> None:
    store = Store()
    store.notes = [
        Item(id="note:slides", source="note", title="Finish slides for Friday's talk", due=at(hour=17)),
        Item(id="note:milk", source="note", title="Oat milk, coffee, basil"),
    ]
    store.overrides = {"note:slides": "high"}
    store.steps = {"task:poster": [{"text": "Export PDF at 300 dpi", "done": True},
                                   {"text": "Pick it up at the copy shop", "done": False}],
                   "note:slides": [{"text": "✦ Add the ablation plot", "done": False, "ai": True}]}
    store.synced = demo_items()
    store.window = {"view": "source", "important_only": False}
    store.save()


# ---- no network, no background jobs ------------------------------------------------------------------------
sources.sync_groups = lambda now, groups, calendar=None: (demo_items(), {})
Changes.due = lambda self: set()
digest.due = lambda: False


class DemoApp(app.App):
    def __init__(self):
        Gtk.Application.__init__(self, application_id="dev.nudgy.Screenshots")
        self.window = None
        self.pet = None


# ---- drawing helpers -------------------------------------------------------------------------------------------
def settle() -> None:
    for _ in range(50):                     # let GTK lay out and paint what was just shown
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        __import__("time").sleep(0.01)


def snapshot(widget: Gtk.Widget) -> cairo.ImageSurface:
    settle()
    width, height = widget.get_allocated_width(), widget.get_allocated_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    widget.draw(cairo.Context(surface))
    return surface


def pose(face: str, pressing: int = 0, level: float = 0.0) -> pet.Mood:
    mood = pet.Mood()
    mood.pressing = pressing
    if face in ("listen", "think", "talk"):
        mood.voice, mood.level = face, level
    elif face in ("sideeye", "sulk", "back"):
        mood.judge = {"sideeye": 1, "sulk": 2, "back": 3}[face]
    elif face == "busy":
        mood.pressing = max(pressing, 2)
    elif face == "alarm":
        mood.alarm, mood.pressing = True, max(pressing, 1)
    else:
        mood.action, mood.action_until = face, float("inf")
    return mood


def draw_pet(cr, x: float, y: float, size: float, face: str, phase: float = 1.0, **options) -> None:
    cr.save()
    cr.translate(x, y)
    mood = pose(face, options.pop("pressing", 0), options.pop("level", 0.0))
    if face == "hop":
        mood.action_until = __import__("time").time() + 0.3            # mid-jump
    pet.draw(cr, size, mood, phase=phase, badge=options.get("badge", True))
    cr.restore()


def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def shadowed(cr, surface, x, y, radius=12):
    for spread, alpha in ((10, 0.05), (6, 0.07), (3, 0.09)):
        rounded(cr, x - spread / 2, y - spread / 2 + 4, surface.get_width() + spread,
                surface.get_height() + spread, radius + spread / 2)
        cr.set_source_rgba(0, 0, 0, alpha)
        cr.fill()
    cr.set_source_surface(surface, x, y)
    cr.paint()


def text(cr, value, x, y, size=13, colour=(0.2, 0.22, 0.25), bold=False, centre=False):
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    cr.set_source_rgb(*colour)
    if centre:
        x -= cr.text_extents(value).x_advance / 2
    cr.move_to(x, y)
    cr.show_text(value)


def backdrop(cr, width, height):
    gradient = cairo.LinearGradient(0, 0, width, height)
    gradient.add_color_stop_rgb(0, 0.36, 0.42, 0.70)
    gradient.add_color_stop_rgb(1, 0.62, 0.44, 0.66)
    cr.set_source(gradient)
    cr.paint()


def top_bar(cr, width, face, pressing):
    cr.rectangle(0, 0, width, 34)
    cr.set_source_rgb(0.09, 0.09, 0.11)
    cr.fill()
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[NOW.weekday()]
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[NOW.month - 1]
    text(cr, f"{day} {NOW.day} {month}  {NOW:%H:%M}", width / 2, 23, 14, (0.95, 0.95, 0.95), bold=True, centre=True)
    cr.save()
    cr.translate(width - 150, 5)
    pet.draw(cr, 24, pose(face, pressing), still=True)
    cr.restore()
    cr.set_source_rgb(0.85, 0.85, 0.85)                                 # stand-ins for the usual icons
    cr.set_line_width(1.8)
    x = width - 110
    for radius in (3, 6, 9):                                            # wifi
        cr.arc(x, 24, radius, -2.36, -0.79)
        cr.stroke()
    cr.move_to(x + 22, 13)                                              # sound
    cr.line_to(x + 30, 17)
    cr.line_to(x + 22, 21)
    cr.close_path()
    cr.fill()
    cr.arc(x + 34, 17, 5, -0.8, 0.8)
    cr.stroke()
    cr.rectangle(x + 52, 11, 9, 14)                                     # battery
    cr.stroke()
    cr.rectangle(x + 54, 16, 5, 7)
    cr.fill()


# ---- the pictures --------------------------------------------------------------------------------------------
def hero(list_shot, bubble_shot, pressing) -> None:
    width, height = 1280, 820
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    backdrop(cr, width, height)
    top_bar(cr, width, "busy", pressing)
    lx, ly = width - list_shot.get_width() - 40, 60
    shadowed(cr, list_shot, lx, ly)
    pet_size = 120
    px, py = lx - pet_size - 30, ly + list_shot.get_height() - pet_size - 10
    draw_pet(cr, px, py, pet_size, "busy", pressing=pressing)
    shadowed(cr, bubble_shot, px - bubble_shot.get_width() + pet_size * 0.7, py - bubble_shot.get_height() - 4)
    text(cr, "Nudgy", 60, 120, 54, (1, 1, 1), bold=True)
    text(cr, "a tiny pet that keeps what needs you", 62, 160, 21, (0.96, 0.96, 1))
    text(cr, "in one always-on-top list", 62, 188, 21, (0.96, 0.96, 1))
    for n, line in enumerate(("Gmail · Calendar · Tasks · Keep · Luma", "Signal · your notes folder",
                              "click it for the list, talk to it, drag it anywhere")):
        text(cr, line, 62, 236 + n * 28, 16, (0.92, 0.92, 0.98))
    surface.write_to_png(str(OUT / "desktop.png"))


FACES = [("happy", "calm"), ("busy", "things need you"), ("alarm", "something's overdue"),
         ("cheer", "you ticked it off"),
         ("love", "being petted"), ("sleep", "night time"), ("wink", "just because"), ("yawn", "sleepy"),
         ("surprised", "new urgent item"), ("hop", "a little jump"), ("sideeye", "are you drifting?"),
         ("sulk", "still drifting…"), ("back", "fine, ignore me"), ("listen", "listening to you"),
         ("think", "thinking"), ("talk", "answering")]


def faces_sheet() -> None:
    columns, cell_w, cell_h, size = 4, 230, 210, 130
    rows = math.ceil(len(FACES) / columns)
    width, height = columns * cell_w + 40, rows * cell_h + 100
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.97, 0.97, 0.98)
    cr.paint()
    text(cr, "Nudgy's moods", width / 2, 52, 28, (0.15, 0.16, 0.2), bold=True, centre=True)
    for n, (face, label) in enumerate(FACES):
        x, y = 20 + (n % columns) * cell_w, 80 + (n // columns) * cell_h
        rounded(cr, x + 8, y + 4, cell_w - 16, cell_h - 12, 16)
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
        draw_pet(cr, x + (cell_w - size) / 2, y + 6, size, face, phase=1.3,
                 pressing=2 if face in ("busy", "alarm") else 0, level=0.35)
        text(cr, face, x + cell_w / 2, y + cell_h - 40, 15, (0.15, 0.16, 0.2), bold=True, centre=True)
        text(cr, label, x + cell_w / 2, y + cell_h - 21, 12.5, (0.42, 0.44, 0.48), centre=True)
    surface.write_to_png(str(OUT / "moods.png"))


def gif() -> None:
    from PIL import Image
    size, frames = 220, []
    sequence = [("happy", 14), ("hop", 8), ("busy", 16), ("alarm", 16), ("cheer", 16), ("listen", 14),
                ("think", 12), ("talk", 14), ("sideeye", 12), ("back", 24), ("love", 14), ("sleep", 16)]
    phase = 0.0
    for face, count in sequence:
        for step in range(count):
            phase += 0.16
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
            cr = cairo.Context(surface)
            cr.set_source_rgb(1, 1, 1)
            cr.paint()
            mood = pose(face, 2 if face in ("busy", "alarm") else 0, 0.3 + 0.3 * math.sin(step))
            if face == "back":
                phase += 0.1                  # a little faster, so the stomp and steam show
            if face == "hop":
                mood.action_until = __import__("time").time() + 0.6 * (1 - step / count)
            pet.draw(cr, size, mood, phase=phase, blink=(face == "happy" and step in (9, 10)))
            image = Image.frombuffer("RGBA", (size, size), bytes(surface.get_data()), "raw", "BGRA", 0, 1)
            frames.append(image.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=64))
    frames[0].save(OUT / "nudgy.gif", save_all=True, append_images=frames[1:], duration=80, loop=0, optimize=True)


def talk_strip(bubbles) -> None:
    width, height = 1100, 330
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.97, 0.97, 0.98)
    cr.paint()
    steps = [("listen", "1. Hold it and talk"), ("think", "2. It thinks, on your laptop"),
             ("talk", "3. It answers out loud")]
    for n, (face, caption) in enumerate(steps):
        x = 30 + n * 360
        shot = bubbles[n]
        cr.save()
        scale = min(1.0, 320 / shot.get_width())
        cr.translate(x, 20)
        cr.scale(scale, scale)
        shadowed(cr, shot, 0, 0)
        cr.restore()
        draw_pet(cr, x + 110, 150, 110, face, phase=1.1, level=0.5)
        text(cr, caption, x + 165, 300, 15, (0.2, 0.22, 0.26), bold=True, centre=True)
    surface.write_to_png(str(OUT / "talk.png"))


def capture(application: DemoApp) -> bool:
    window, creature = application.window, application.pet
    creature.move(1200, 600)
    creature.open_list()
    window.resize(360, 600)

    def later():
        list_shot = snapshot(window)
        pressing = creature.mood.pressing
        creature.say("Next up: Interview invitation: Research Engineer\n(today)",
                     buttons=(("Open list", None), ("Later", None)), seconds=None)
        bubble_shot = snapshot(creature.bubble)
        talking = []
        for message in ("I'm listening… (click me when you're done)", "You: “what's next?”\nThinking…",
                        "You: “what's next?”\n\nThe lab meeting starts in 45 minutes, then the "
                        "interview email needs a reply."):
            creature.say(message, seconds=None,
                         buttons=(("Talk again", None), ("OK", None)) if "lab" in message else ())
            talking.append(snapshot(creature.bubble))
        creature.bubble.close_bubble()
        hero(list_shot, bubble_shot, pressing)
        faces_sheet()
        talk_strip(talking)
        gif()
        print("Wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))
        application.quit()
        return False

    GLib.timeout_add(2500, later)
    return False


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seed()
    application = DemoApp()
    application.connect("activate", lambda a: GLib.timeout_add(1500, capture, a))
    application.run([])


if __name__ == "__main__":
    main()
