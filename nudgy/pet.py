"""The little round pet that stands in for the whole list.

How it looks and moves follows how urgent things are:
- calm (nothing needs you now): mint, bobs gently, hops, winks, yawns, looks around; sleeps at night
- busy (things in Now): yellow, a worried brow, paces from side to side, the odd sweat drop
- alarm (something overdue or due now): coral, wide eyes, bounces with a "!" above its head
- cheer (you ticked something off): arms up, sparkles, a hop
It follows the pointer with its eyes, gets hearts when you "pet" it (wiggle the pointer over it),
and speaks in a bubble now and then about what is next.

When the focus check (watch.py) decides you are drifting it gets judgy: a side-eye and a question,
then a sulk with crossed arms, then it hops to the middle of the screen and turns its back. It hops
home and cheers when you get back to work.

Drag it anywhere; click it to open or close the list. Hidden, it waits in the top bar next to the
Wi-Fi icon; Super+T (or `nudgy toggle`) brings it back.
"""
from __future__ import annotations

import logging
import math
import random
import subprocess
import threading
import time
import warnings
from datetime import datetime

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .model import data_dir  # noqa: E402

log = logging.getLogger(__name__)

SIZE = 84                 # window size; the body is drawn in an 84-unit square
LONG_PRESS_MS = 500       # hold the pet this long to talk to it
FRAME_MS = 100           # 10 frames a second is smooth enough and halves the CPU
SHORTCUT = "Super+T"
REMIND_BUSY = 30 * 60     # seconds between "what's next" bubbles when things are in Now
REMIND_ALARM = 15 * 60    # ... when something is overdue
MINT = (0.49, 0.88, 0.71)
COLORS = {"happy": MINT, "busy": (1.0, 0.83, 0.47), "alarm": (1.0, 0.54, 0.50),
          "sleep": (0.62, 0.68, 0.85), "cheer": MINT, "love": (1.0, 0.70, 0.80),
          "sideeye": (0.95, 0.80, 0.50), "sulk": (0.85, 0.62, 0.62), "back": (0.80, 0.66, 0.66),
          "surprised": (0.55, 0.85, 0.95), "listen": (0.55, 0.85, 0.95), "think": (0.78, 0.74, 0.98),
          "talk": MINT, "yawn": MINT, "wink": MINT, "hop": MINT, "look": MINT}
JUDGE_FACES = {1: "sideeye", 2: "sulk", 3: "back"}
BUBBLE_CSS = b"""
.pet-bubble { color: #202124; padding: 10px 12px; }
.pet-bubble label { color: #202124; font-size: 12px; }
.pet-bubble button { background: #eceff1; border: none; box-shadow: none; border-radius: 8px;
                     padding: 2px 10px; min-height: 22px; color: #202124; font-size: 12px; }
.pet-bubble button:hover { background: #cfd8dc; }
"""


class Mood:
    """Everything that decides the pet's face. The urgency comes from the list; actions are short
    reactions (a hop, a wink, hearts); judge is the focus check's verdict level (0 to 3)."""

    def __init__(self):
        self.pressing = 0                      # items in Now
        self.alarm = False                     # something overdue or due right now
        self.top: list[str] = []               # titles of what needs you now, most pressing first
        self.when: list[str] = []              # when each of those is due, in words
        self.action = ""
        self.action_until = 0.0
        self.judge = 0
        self.voice = ""                        # "listen", "think" or "talk" while you talk to it
        self.timer_end = 0.0                   # a focus timer: when it ends, and how long it was
        self.timer_total = 0.0
        self.level = 0.0                       # how loud you are while it listens, 0..1
        self.look = (0.0, 0.0)                 # where the eyes point, -1..1 each way

    def act(self, name: str, seconds: float) -> None:
        self.action, self.action_until = name, time.time() + seconds

    @property
    def cheer_until(self) -> float:
        return self.action_until if self.action == "cheer" else 0.0

    @cheer_until.setter
    def cheer_until(self, until: float) -> None:
        self.action, self.action_until = "cheer", until

    def base(self) -> str:
        if self.alarm:
            return "alarm"
        if self.pressing:
            return "busy"
        hour = datetime.now().hour
        return "sleep" if hour >= 23 or hour < 7 else "happy"

    def face(self, moment: float | None = None) -> str:
        moment = moment or time.time()
        if self.voice:
            return self.voice
        if self.judge:
            return JUDGE_FACES[min(self.judge, 3)]
        if self.action and moment < self.action_until:
            return self.action
        return self.base()


# ---- drawing -------------------------------------------------------------------------------------
def _motion(face: str, phase: float, progress: float) -> tuple[float, float, float, float]:
    """Body offset and squash for a face: (dx, dy, scale_x, scale_y)."""
    if face in ("cheer", "hop"):
        lift = abs(math.sin(phase * 3)) if face == "cheer" else math.sin(min(1.0, progress) * math.pi)
        squash = 1 - lift
        return 0, -lift * 9, 1 + 0.06 * squash, 1 - 0.06 * squash
    if face == "busy":                        # pacing side to side
        return math.sin(phase * 0.9) * 6, abs(math.sin(phase * 1.8)) * -1.5, 1, 1
    if face == "alarm":                       # bouncing, with a little shake
        lift = abs(math.sin(phase * 2.6))
        return math.sin(phase * 14) * 0.8, -lift * 6, 1 + 0.05 * (1 - lift), 1 - 0.05 * (1 - lift)
    if face == "sulk":
        return math.sin(phase * 9) * 0.7, 2, 1.03, 0.97
    if face == "back":                        # sassy wiggle, and a stomp every few seconds
        beat = phase % 3.2
        stomp = math.sin(beat / 0.35 * math.pi) if beat < 0.35 else 0.0
        squash = 0.05 if 0.35 <= beat < 0.5 else 0.0
        return math.sin(phase * 3.2) * 1.6, 1 - stomp * 4, 1 + squash, 1 - squash
    if face == "sleep":
        breath = math.sin(phase * 0.8)
        return 0, 2, 1 + 0.02 * breath, 1 + 0.03 * breath
    if face == "love":
        return math.sin(phase * 2) * 2.5, math.sin(phase * 4), 1, 1
    if face == "surprised":
        return 0, -3, 0.96, 1.05
    if face == "listen":                      # leaning in
        return 0, -1 + math.sin(phase * 2) * 0.6, 1.0, 1.03
    if face == "think":
        return math.sin(phase * 0.8) * 2, math.sin(phase * 1.6) * 0.8, 1, 1
    if face == "talk":
        bob = abs(math.sin(phase * 4))
        return 0, -bob * 1.5, 1 + 0.02 * bob, 1 - 0.02 * bob
    return 0, math.sin(phase) * 1.5, 1, 1


def _heart(cr, x: float, y: float, size: float) -> None:
    cr.move_to(x, y + size * 0.35)
    cr.curve_to(x - size, y - size * 0.4, x - size * 0.35, y - size, x, y - size * 0.35)
    cr.curve_to(x + size * 0.35, y - size, x + size, y - size * 0.4, x, y + size * 0.35)
    cr.close_path()
    cr.fill()


def draw(cr, size: float, mood: Mood, phase: float = 0.0, blink: bool = False, badge: bool = True,
         still: bool = False) -> None:
    """Draws the pet into a size×size square. phase drives the movement; still=True for icons."""
    face = mood.face()
    progress = 1 - max(0.0, mood.action_until - time.time()) / 0.6 if face == "hop" else 0
    dx, dy, sx, sy = (0, 0, 1, 1) if still else _motion(face, phase, progress)
    cr.save()
    cr.scale(size / SIZE, size / SIZE)
    cx, cy, radius = 42 + dx, 47 + dy, 25
    r, g, b = COLORS.get(face, MINT)
    ink = (0.13, 0.15, 0.18)

    # shadow shrinks as it lifts off the ground
    cr.save()
    cr.translate(42 + dx, 77)
    cr.scale(1, 0.22)
    cr.arc(0, 0, max(8, 19 + dy * 0.8), 0, 2 * math.pi)
    cr.restore()
    cr.set_source_rgba(0, 0, 0, 0.18)
    cr.fill()

    # body, squashed from its base
    cr.save()
    cr.translate(cx, cy + radius)
    cr.scale(sx, sy)
    cr.translate(0, -radius)
    cr.save()
    cr.scale(1, 1.08)
    cr.arc(0, 0, radius, 0, 2 * math.pi)
    cr.restore()
    shade = cairo.RadialGradient(-8, -12, 4, 0, 0, 31)
    shade.add_color_stop_rgb(0, min(1, r + 0.15), min(1, g + 0.15), min(1, b + 0.15))
    shade.add_color_stop_rgb(1, r * 0.85, g * 0.85, b * 0.85)
    cr.set_source(shade)
    cr.fill_preserve()
    cr.set_source_rgb(r * 0.55, g * 0.55, b * 0.55)
    cr.set_line_width(1.6)
    cr.stroke()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)

    if face == "back":                        # turned away: a tuft, crossed arms poking out, a little butt
        edge = (r * 0.55, g * 0.55, b * 0.55)
        cr.set_source_rgb(*edge)
        cr.set_line_width(1.8)
        cr.move_to(-2, -26)
        cr.curve_to(-4, -32, 2, -33, 1, -27)
        cr.stroke()
        for side in (-1, 1):                  # elbows of arms crossed in front
            cr.save()
            cr.translate(side * 24, 1)
            cr.scale(1, 0.8)
            cr.arc(0, 0, 4.2, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgb(r * 0.85, g * 0.85, b * 0.85)
            cr.fill_preserve()
            cr.set_source_rgb(*edge)
            cr.set_line_width(1.4)
            cr.stroke()
        wag = math.sin(phase * 3.2) * 1.2     # the butt sways a little more than the body
        cr.set_source_rgba(1.0, 0.55, 0.6, 0.35)
        for side in (-1, 1):                  # blushing cheeks
            cr.arc(side * 6 + wag, 19, 4.6, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgb(*edge)
        cr.set_line_width(1.6)
        cr.move_to(-11 + wag, 17)             # the round bottom of each cheek
        cr.curve_to(-10 + wag, 25.5, -2 + wag, 25.5, wag, 20)
        cr.curve_to(2 + wag, 25.5, 10 + wag, 25.5, 11 + wag, 17)
        cr.stroke()
        cr.move_to(wag, 20)                   # the crease
        cr.curve_to(wag - 0.5, 16, wag + 0.3, 13, wag, 11)
        cr.stroke()
        cr.restore()
        _extras(cr, face, phase, cx, cy, ink, mood.level)
        _timer_ring(cr, mood, cx, cy)
        cr.restore()
        return

    look_x, look_y = (0, 0) if still else mood.look
    if face == "sideeye":
        look_x, look_y = 1, 0.3
    elif face == "think":
        look_x, look_y = 0.7, -1
    elif face == "listen":
        look_x, look_y = 0, 0
    ex_l, ex_r, eye_y = -9, 9, -4
    cr.set_source_rgb(*ink)

    def dot_eye(x: float) -> None:
        cr.save()
        cr.translate(x + look_x * 2, eye_y + look_y * 1.5)
        cr.scale(1, 1.3)
        cr.arc(0, 0, 3.2, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.arc(x + look_x * 2 + 1.1, eye_y + look_y * 1.5 - 1.8, 1.1, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(*ink)

    def arc_eye(x: float, up: bool) -> None:        # ^ when up, u when down
        cr.set_line_width(2)
        if up:
            cr.arc_negative(x, eye_y + 2, 3.5, -0.15 * math.pi, -0.85 * math.pi)
        else:
            cr.arc(x, eye_y - 1, 3.5, 0.15 * math.pi, 0.85 * math.pi)
        cr.stroke()

    def wide_eye(x: float) -> None:
        cr.set_source_rgb(1, 1, 1)
        cr.arc(x, eye_y, 5, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(*ink)
        cr.arc(x + look_x * 1.5, eye_y + 0.5 + look_y * 1.5, 2.6, 0, 2 * math.pi)
        cr.fill()

    if blink and face not in ("sleep", "cheer", "love", "yawn"):
        cr.set_line_width(2)
        for x in (ex_l, ex_r):
            cr.move_to(x - 3.5, eye_y)
            cr.line_to(x + 3.5, eye_y)
            cr.stroke()
    elif face in ("sleep", "yawn"):
        arc_eye(ex_l, False)
        arc_eye(ex_r, False)
    elif face == "cheer":
        arc_eye(ex_l, True)
        arc_eye(ex_r, True)
    elif face == "love":
        cr.set_source_rgb(0.9, 0.25, 0.4)
        _heart(cr, ex_l, eye_y, 4.2)
        _heart(cr, ex_r, eye_y, 4.2)
        cr.set_source_rgb(*ink)
    elif face == "wink":
        dot_eye(ex_l)
        arc_eye(ex_r, True)
    elif face in ("alarm", "surprised"):
        wide_eye(ex_l)
        wide_eye(ex_r)
    elif face in ("sideeye", "sulk"):
        for x in (ex_l, ex_r):                # half-closed lids
            dot_eye(x)
            cr.set_source_rgb(r * 0.9, g * 0.9, b * 0.9)
            cr.rectangle(x - 5, eye_y - 7, 10, 6)
            cr.fill()
            cr.set_source_rgb(*ink)
            cr.set_line_width(1.6)
            cr.move_to(x - 4, eye_y - 1)
            cr.line_to(x + 4, eye_y - 1)
            cr.stroke()
    else:
        dot_eye(ex_l)
        dot_eye(ex_r)

    # brows
    cr.set_line_width(1.6)
    if face in ("busy", "alarm"):             # inner ends raised: concerned, not cross
        lift = 0.8 if face == "busy" else 1.6
        for x, side in ((ex_l, 1), (ex_r, -1)):
            cr.move_to(x - 4, eye_y - 8 + side * lift)
            cr.line_to(x + 4, eye_y - 8 - side * lift)
            cr.stroke()
    elif face == "sulk":                      # inner ends down: cross
        for x, side in ((ex_l, 1), (ex_r, -1)):
            cr.move_to(x - 4, eye_y - 9 - side * 1.6)
            cr.line_to(x + 4, eye_y - 9 + side * 1.6)
            cr.stroke()
    elif face == "sideeye":                   # one brow up
        cr.move_to(ex_l - 4, eye_y - 8)
        cr.line_to(ex_l + 4, eye_y - 8)
        cr.move_to(ex_r - 4, eye_y - 10)
        cr.line_to(ex_r + 4, eye_y - 12)
        cr.stroke()

    # mouth
    mouth_y = 7
    cr.set_line_width(2)
    if face in ("happy", "hop", "look", "wink", "love"):
        cr.arc(0, mouth_y - 3, 6, 0.2 * math.pi, 0.8 * math.pi)
        cr.stroke()
    elif face == "cheer":
        cr.move_to(-7, mouth_y - 1)
        cr.curve_to(-6, mouth_y + 8, 6, mouth_y + 8, 7, mouth_y - 1)
        cr.close_path()
        cr.fill_preserve()
        cr.stroke()
        cr.set_source_rgb(1.0, 0.55, 0.6)
        cr.arc(0, mouth_y + 3.5, 2.6, 0, math.pi)
        cr.fill()
        cr.set_source_rgb(*ink)
    elif face == "busy":                      # a wobbly, nervous line
        cr.move_to(-5, mouth_y + 1)
        cr.curve_to(-2, mouth_y - 1, 2, mouth_y + 3, 5, mouth_y + 1)
        cr.stroke()
    elif face in ("alarm", "surprised"):
        cr.save()
        cr.translate(0, mouth_y + 1)
        cr.scale(1, 1.3)
        cr.arc(0, 0, 2.6, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    elif face == "yawn":
        cr.save()
        cr.translate(0, mouth_y + 2)
        cr.scale(1, 1.4)
        cr.arc(0, 0, 4.2, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    elif face == "talk":                      # opening and closing as it speaks
        open_ = abs(math.sin(phase * 9)) * 0.8 + 0.2
        cr.save()
        cr.translate(0, mouth_y + 1)
        cr.scale(1, open_)
        cr.arc(0, 0, 4, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    elif face == "listen":                    # a small "o"
        cr.set_line_width(1.8)
        cr.arc(0, mouth_y + 1, 2.2, 0, 2 * math.pi)
        cr.stroke()
    elif face == "think":
        cr.move_to(-3, mouth_y + 2)
        cr.line_to(4, mouth_y)
        cr.stroke()
    elif face == "sideeye":
        cr.move_to(-4, mouth_y + 1)
        cr.line_to(5, mouth_y)
        cr.stroke()
    elif face == "sulk":
        cr.arc(0, mouth_y + 5, 5, 1.2 * math.pi, 1.8 * math.pi)
        cr.stroke()
    else:                                     # sleeping
        cr.arc(0, mouth_y + 1, 1.8, 0, 2 * math.pi)
        cr.fill()

    if face in ("happy", "cheer", "sleep", "love", "wink", "hop", "talk"):       # cheeks
        cr.set_source_rgba(1.0, 0.45, 0.5, 0.45 if face == "love" else 0.35)
        for x in (-15, 15):
            cr.arc(x, 3, 3.5, 0, 2 * math.pi)
            cr.fill()

    if face == "cheer":                       # hands up
        for x, y in ((-26, -14), (26, -14)):
            cr.arc(x, y, 4.5, 0, 2 * math.pi)
            cr.set_source_rgb(r * 0.8, g * 0.8, b * 0.8)
            cr.fill_preserve()
            cr.set_source_rgb(r * 0.55, g * 0.55, b * 0.55)
            cr.set_line_width(1.2)
            cr.stroke()
    elif face == "sulk":                      # arms folded across the tummy
        for start, end in (((-20, 14), (8, 20)), ((20, 14), (-8, 20))):
            cr.move_to(*start)
            cr.line_to(*end)
            cr.set_source_rgb(r * 0.55, g * 0.55, b * 0.55)
            cr.set_line_width(8.5)
            cr.stroke()
            cr.move_to(*start)
            cr.line_to(*end)
            cr.set_source_rgb(r * 0.8, g * 0.8, b * 0.8)
            cr.set_line_width(6)
            cr.stroke()
    cr.restore()

    _extras(cr, face, phase, cx, cy, ink, mood.level)
    _timer_ring(cr, mood, cx, cy)
    if badge and mood.pressing:
        bx, by = 68, 18
        cr.set_source_rgb(0.93, 0.27, 0.27)
        cr.arc(bx, by, 9, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        text = str(mood.pressing) if mood.pressing < 10 else "9+"
        cr.set_font_size(11 if len(text) == 1 else 9)
        extents = cr.text_extents(text)
        cr.move_to(bx - extents.width / 2 - extents.x_bearing, by - extents.height / 2 - extents.y_bearing)
        cr.show_text(text)
    cr.restore()


def _timer_ring(cr, mood: Mood, cx: float, cy: float) -> None:
    """A focus timer shows as a ring that empties around the pet."""
    if not mood.timer_end or not mood.timer_total:
        return
    left = max(0.0, mood.timer_end - time.time()) / mood.timer_total
    cr.save()
    cr.set_line_width(2.6)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgba(0.5, 0.5, 0.55, 0.25)
    cr.arc(cx, cy + 1, 33, 0, 2 * math.pi)
    cr.stroke()
    cr.set_source_rgba(0.95, 0.45, 0.35, 0.9)
    cr.arc(cx, cy + 1, 33, -math.pi / 2, -math.pi / 2 + 2 * math.pi * left)
    cr.stroke()
    cr.restore()


def _extras(cr, face: str, phase: float, cx: float, cy: float, ink, level: float = 0.0) -> None:
    """Things floating around the body: z's, hearts, sparkles, a "!", sweat, a cross mark."""
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    if face == "sleep":
        cr.set_source_rgba(0.40, 0.48, 0.90, 0.9)     # readable on light and dark desktops
        for n in range(2):
            drift = (phase * 2 + n * 3) % 6
            cr.set_font_size(8 + n * 2)
            cr.move_to(cx + 18 + n * 5, cy - 18 - drift - n * 6)
            cr.show_text("z")
    if face in ("love", "cheer"):
        cr.set_source_rgba(0.95, 0.3, 0.45, 0.85)
        for n in range(3):
            rise = (phase * 6 + n * 7) % 20
            _heart(cr, cx - 22 + n * 20, cy - 20 - rise, 3 + n % 2)
    if face == "cheer":
        cr.set_source_rgb(1.0, 0.85, 0.3)
        cr.set_line_width(1.5)
        for x, y in ((12, 18), (74, 24), (72, 58)):
            twinkle = 2.5 + math.sin(phase * 4 + x) * 1.2
            cr.move_to(x - twinkle, y)
            cr.line_to(x + twinkle, y)
            cr.move_to(x, y - twinkle)
            cr.line_to(x, y + twinkle)
            cr.stroke()
    if face == "alarm":
        cr.set_source_rgb(0.9, 0.2, 0.2)
        cr.set_font_size(16)
        cr.move_to(cx - 31, cy - 14 + math.sin(phase * 5) * 1.5)
        cr.show_text("!")
    if face == "alarm" or (face == "busy" and (phase % 12) < 4):
        drop = (phase * 3) % 8
        cr.set_source_rgba(0.45, 0.7, 1.0, 0.85)
        x, y = cx + 20, cy - 16 + drop
        cr.move_to(x, y - 4)
        cr.curve_to(x + 3, y, x + 3, y + 3, x, y + 3)
        cr.curve_to(x - 3, y + 3, x - 3, y, x, y - 4)
        cr.fill()
    if face == "listen":                      # sound waves that grow with your voice
        cr.set_source_rgba(0.25, 0.55, 0.95, 0.9)
        cr.set_line_width(1.8)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for n in range(3):
            size = 5 + n * 5
            cr.set_source_rgba(0.25, 0.55, 0.95, max(0.15, min(0.95, level * 3 - n * 0.6 + 0.3)))
            cr.arc(cx - 26, cy - 4, size, math.pi * 0.75, math.pi * 1.25)
            cr.stroke()
    if face == "think":                       # dots drifting up
        for n in range(3):
            pulse = (math.sin(phase * 3 - n) + 1) / 2
            cr.set_source_rgba(0.45, 0.40, 0.80, 0.4 + 0.5 * pulse)
            cr.arc(cx + 18 + n * 6, cy - 26 - n * 5, 1.6 + n * 0.6, 0, 2 * math.pi)
            cr.fill()
    if face == "back":                        # fuming: an anger mark and puffs of steam
        pulse = 1 + 0.18 * math.sin(phase * 6)
        cr.save()
        cr.translate(cx + 17, cy - 26)
        cr.scale(pulse, pulse)
        cr.set_source_rgb(0.88, 0.16, 0.2)
        cr.set_line_width(1.8)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for angle in (0, 0.5, 1, 1.5):        # four bent strokes around a gap, like a throbbing vein
            cr.save()
            cr.rotate(angle * math.pi)
            cr.move_to(1.6, 4.5)
            cr.curve_to(1.6, 2, 2, 1.6, 4.5, 1.6)
            cr.stroke()
            cr.restore()
        cr.restore()
        for n, side in enumerate((-1, 1)):
            rise = (phase * 1.4 + n * 1.3) % 3.0  # each puff rises, grows and fades
            if rise < 2.2:
                alpha = 0.85 * (1 - rise / 2.2)
                cr.set_source_rgba(0.62, 0.64, 0.68, alpha)
                x = cx + side * (6 + rise * 3)
                y = cy - 30 - rise * 6
                for dx, dy, size in ((0, 0, 3.2), (2.8, -1, 2.5), (-2.6, -1.2, 2.3)):
                    cr.arc(x + dx * (1 + rise / 3), y + dy, size * (1 + rise / 2.5), 0, 2 * math.pi)
                    cr.fill()
    if face == "sulk":
        cr.set_source_rgb(0.85, 0.2, 0.2)
        cr.set_line_width(1.6)
        x, y = cx + 20, cy - 22
        for ax, ay in ((-3, -3), (3, -3), (-3, 3), (3, 3)):
            cr.move_to(x + ax * 0.4, y + ay * 0.4)
            cr.line_to(x + ax, y + ay)
        cr.stroke()


def tip(mood: Mood) -> str:
    if mood.pressing:
        head = f"{mood.pressing} thing{'s' if mood.pressing > 1 else ''} need{'' if mood.pressing > 1 else 's'} you now"
        shown = [f"•  {t if len(t) <= 70 else t[:69] + '…'}" for t in mood.top[:6]]
        if len(mood.top) > 6:
            shown.append(f"   and {len(mood.top) - 6} more")
        return "\n".join([head + ":", *shown, "", "Click to open the list"])
    return "All calm. Nothing needs you right now.\nClick to open the list"


def peek(mood: Mood) -> str:
    """The small hover preview: up to three immediate items, short, with when they're due."""
    if mood.timer_end:
        left = max(0, int((mood.timer_end - time.time()) // 60) + 1)
        return f"⏱ {left} min left" + ("\n" + _peek_items(mood) if mood.top else "")
    return _peek_items(mood)


def _peek_items(mood: Mood) -> str:
    if not mood.top:
        return "All calm 🌿"
    lines = []
    for title, when in list(zip(mood.top, mood.when or [""] * len(mood.top), strict=False))[:3]:
        short = title if len(title) <= 42 else title[:41] + "…"
        lines.append(f"•  {short}" + (f"  · {when}" if when else ""))
    if len(mood.top) > 3:
        lines.append(f"   +{len(mood.top) - 3} more")
    return "\n".join(lines)


# ---- a reply draft you check and send yourself -------------------------------------------------------
class DraftWindow(Gtk.Window):
    """Shows Claude's draft in an editable box with Copy and 'Open in Gmail'. Nudgy never sends anything."""

    def __init__(self, item, sender: str, subject: str, draft: str):
        super().__init__(title="Draft reply")
        self.set_keep_above(True)
        self.set_default_size(460, 340)
        self.item, self.subject, self.sender = item, subject, sender
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        heading = Gtk.Label(xalign=0, wrap=True)
        heading.set_text(f"Reply to {sender or 'them'}: {subject}"[:160])
        box.pack_start(heading, False, False, 0)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=6, right_margin=6, top_margin=6)
        self.text.get_buffer().set_text(draft)
        scroll.add(self.text)
        box.pack_start(scroll, True, True, 0)
        row = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        for label, action in (("Copy", self._copy),
                              ("Open in Gmail" if item.source == "gmail" else "Copy and open chat", self._open),
                              ("Close", lambda *_: self.destroy())):
            button = Gtk.Button(label=label)
            button.connect("clicked", action)
            row.pack_start(button, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.add(box)
        self.show_all()

    def _body(self) -> str:
        buffer = self.text.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _copy(self, *_args) -> None:
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self._body(), -1)

    def _open(self, *_args) -> None:
        import urllib.parse
        from email.utils import parseaddr
        from .app import launch
        self._copy()
        if self.item.source == "gmail":
            address = parseaddr(self.sender)[1]
            subject = self.subject if self.subject.lower().startswith("re:") else "Re: " + self.subject
            url = "https://mail.google.com/mail/?" + urllib.parse.urlencode(
                {"view": "cm", "fs": "1", "to": address, "su": subject, "body": self._body()})
            launch(["xdg-open", url])
        elif self.item.url.startswith("https://"):
            launch(["xdg-open", self.item.url])           # the draft is on the clipboard: paste it there


# ---- speech bubble -------------------------------------------------------------------------------
class Bubble(Gtk.Window):
    """A small speech bubble next to the pet, with up to two buttons."""

    def __init__(self, pet: Pet, text: str, buttons=(), seconds: float | None = 20, judgy=False):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.pet = pet
        visual = self.get_screen().get_rgba_visual()
        if visual and self.get_screen().is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.judgy = judgy
        self.connect("draw", self._paint)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        context = box.get_style_context()
        context.add_class("pet-bubble")
        if judgy:
            context.add_class("judgy")
        label = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_text(text)                  # plain text: it can contain titles from emails
        # Wrapped labels would shrink to one word; ask for the width of the longest line, up to 34.
        label.set_width_chars(min(34, max((len(line) for line in text.splitlines()), default=10)))
        box.pack_start(label, False, False, 0)
        if buttons:
            row = Gtk.Box(spacing=6, halign=Gtk.Align.END)
            for name, action in buttons:
                button = Gtk.Button(label=name, relief=Gtk.ReliefStyle.NONE)
                button.connect("clicked", self._clicked, action)
                row.pack_start(button, False, False, 0)
            box.pack_start(row, False, False, 0)
        click = Gtk.EventBox()
        click.add(box)
        if not buttons:
            click.connect("button-press-event", lambda *_: self.close_bubble() or True)
        self.add(click)
        self.timer = GLib.timeout_add_seconds(int(seconds), self.close_bubble) if seconds else 0
        self.show_all()
        self.resize(1, 1)                     # shrink to the text: wrapped labels ask for too much height
        self.place()

    def _clicked(self, _button, action) -> None:
        self.close_bubble()
        if action:
            GLib.idle_add(lambda: action() and False)

    def _paint(self, _widget, cr) -> bool:
        """A solid rounded card; the children draw on top of it."""
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        width, height, radius = self.get_allocated_width(), self.get_allocated_height(), 12
        cr.new_sub_path()
        cr.arc(width - radius - 1, radius + 1, radius, -math.pi / 2, 0)
        cr.arc(width - radius - 1, height - radius - 1, radius, 0, math.pi / 2)
        cr.arc(radius + 1, height - radius - 1, radius, math.pi / 2, math.pi)
        cr.arc(radius + 1, radius + 1, radius, math.pi, 1.5 * math.pi)
        cr.close_path()
        cr.set_source_rgb(*((1.0, 0.94, 0.92) if self.judgy else (1, 1, 1)))
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.set_line_width(1)
        cr.stroke()
        return False

    def place(self) -> None:
        px, py = self.pet.get_position()
        width, height = self.get_size()
        area = self.pet._area(px + SIZE // 2, py + SIZE // 2)
        x = px + SIZE // 2 - width // 2
        y = py - height + 8 if py - height > area.y else py + SIZE - 8
        self.move(max(area.x, min(x, area.x + area.width - width)), y)

    def close_bubble(self) -> bool:
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.pet.bubble is self:
            self.pet.bubble = None
        self.destroy()
        return False


# ---- the pet window ------------------------------------------------------------------------------
class Pet(Gtk.Window):
    def __init__(self, list_window):
        super().__init__(title="Nudgy pet")
        self.list = list_window
        self.store = list_window.store
        self.mood = Mood()
        self.phase = random.random() * 6
        self.blink_until = 0.0
        self.press: tuple[float, float] | None = None
        self.save_timer = 0
        self.tray: Tray | None = None
        self.watcher = None                   # the focus check, set by the app
        self.voice_worker = None              # speech models, started the first time you talk
        self.chat = None
        self.stop_listening = None
        self.speaking = None                  # the player reading an answer aloud
        self.bubble: Bubble | None = None
        self.home: tuple[int, int] | None = None     # where it was before it walked off to sulk
        self.next_idle = time.time() + random.uniform(8, 20)
        self.last_reminder = time.time()
        self.strokes: list[tuple[float, int]] = []   # (time, direction) of pointer wiggles
        self.last_x: float | None = None

        self.set_decorated(False)
        self.set_keep_above(True)
        self.stick()
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_resizable(False)
        canvas = Gtk.DrawingArea()            # gives the window its size; the window paints
        canvas.set_size_request(SIZE, SIZE)
        self.add(canvas)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual and self.get_screen().is_composited():
            self.set_visual(visual)
        provider = Gtk.CssProvider()
        provider.load_from_data(BUBBLE_CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._pressed)
        self.connect("motion-notify-event", self._motion)
        self.connect("button-release-event", self._released)
        self.connect("configure-event", self._moved)
        self.connect("delete-event", lambda *_: self.hide_pet() or True)
        self._place()
        list_window.on_update = self.update
        list_window.on_finish = self.cheer
        list_window.on_hide = self.list_hidden
        GLib.timeout_add(FRAME_MS, self._frame)

    # ---- placement -------------------------------------------------------------------------------
    def _area(self, x: int, y: int) -> Gdk.Rectangle:
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_point(x, y) or display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_workarea()

    def _place(self) -> None:
        saved = self.store.window
        monitor = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        area = monitor.get_workarea()
        x = saved.get("pet_x", area.x + area.width - SIZE - 24)
        y = saved.get("pet_y", area.y + area.height - SIZE - 24)
        screen_w, screen_h = self.get_screen().get_width(), self.get_screen().get_height()
        self.move(max(0, min(x, screen_w - SIZE)), max(0, min(y, screen_h - SIZE)))

    def _moved(self, *_args) -> bool:
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add(400, self._settled)
        return False

    def _settled(self) -> bool:
        self.save_timer = 0
        if self.home is None:                 # don't remember the sulking spot as its home
            x, y = self.get_position()
            self.store.window.update(pet_x=x, pet_y=y)
            self.store.save()
        if self.list.get_visible():
            self.place_list()                 # the list follows its pet
        if self.bubble:
            self.bubble.place()
        return False

    def place_list(self) -> None:
        """Opens the list beside the pet, on whichever side has room."""
        px, py = self.get_position()
        width, height = self.list.get_size()
        area = self._area(px + SIZE // 2, py + SIZE // 2)
        x = px - width - 6 if px + SIZE / 2 > area.x + area.width / 2 else px + SIZE + 6
        y = py + SIZE - height if py + SIZE / 2 > area.y + area.height / 2 else py
        x = max(area.x, min(x, area.x + area.width - width))
        y = max(area.y, min(y, area.y + area.height - height))
        self.list.move(x, y)

    # ---- showing and hiding ----------------------------------------------------------------------
    def toggle_list(self) -> None:
        if self.list.get_visible():
            self.list.hide_list()
        else:
            self.open_list()

    def open_list(self) -> None:
        if not self.get_visible():
            self.show_pet()
        self.place_list()
        self.list.show_list()
        self.refresh_tip()
        if self.bubble and not self.mood.judge:
            self.bubble.close_bubble()

    def list_hidden(self) -> None:
        self.refresh_tip()

    def refresh_tip(self) -> None:
        """Hovering shows a small peek at what's immediate, but only while the list is closed."""
        self.set_tooltip_text(None if self.list.get_visible() else peek(self.mood))

    def show_pet(self) -> None:
        self.store.window["pet_hidden"] = False
        self.store.save()
        self._place()
        self.show_all()
        self.present()

    def hide_pet(self) -> None:
        self.store.window["pet_hidden"] = True
        self.store.save()
        if self.list.get_visible():
            self.list.hide_list(remember=True)
        if self.bubble:
            self.bubble.close_bubble()
        self.hide()

    def toggle(self) -> None:
        if self.get_visible():
            self.hide_pet()
        else:
            self.show_pet()
            if self.store.window.get("list_open"):
                self.open_list()

    # ---- mood ------------------------------------------------------------------------------------
    def update(self, pressing: int, alarm: bool, top: list) -> None:
        """top: [(title, when)] of what needs you now, most pressing first."""
        titles = [t for t, _ in top]
        before = (self.mood.pressing, self.mood.alarm, self.mood.top)
        self.mood.pressing, self.mood.alarm = pressing, alarm
        self.mood.top, self.mood.when = titles, [w for _, w in top]
        self.refresh_tip()
        if pressing and not before[0]:
            self.mood.act("surprised", 1.2)   # something just became urgent
        if self.tray and before != (pressing, alarm, titles):
            self.tray.refresh()
        if self.watcher:
            self.watcher.set_tasks(list(zip(titles, self.mood.when, strict=False)), alarm)
        self.queue_draw()

    def cheer(self) -> None:
        self.mood.act("cheer", 2.5)
        if self.tray:
            self.tray.refresh()
            GLib.timeout_add(2600, lambda: self.tray.refresh() or False)

    def say(self, text: str, buttons=(), seconds: float | None = 20, judgy=False) -> None:
        if not self.get_visible():
            return
        if self.bubble:
            self.bubble.close_bubble()
        self.bubble = Bubble(self, text, buttons, seconds, judgy)

    def whats_next(self) -> None:
        if not self.mood.top:
            self.say("Nothing urgent right now. 🌿", seconds=6)
            return
        when = self.mood.when[0] if self.mood.when else ""
        self.say(f"Next up: {self.mood.top[0]}" + (f"\n({when})" if when else ""),
                 buttons=(("Open list", self.open_list), ("Later", None)), seconds=20)

    # ---- talking -----------------------------------------------------------------------------------------
    def talk(self) -> None:
        """Listen, understand, answer out loud, and do what you asked. All local; see voice.py."""
        from . import voice
        if self.mood.voice == "listen" and self.stop_listening:
            self.stop_listening.set()
            return
        if self.mood.voice in ("think", "talk"):
            self.hush()
            if self.mood.voice == "think":
                return
        trouble = voice.problem()
        if trouble:
            self.show_pet()
            self.say(trouble, seconds=12)
            return
        self.show_pet()
        if self.voice_worker is None:
            self.voice_worker = voice.Worker()
            self.chat = voice.Chat()
            GLib.timeout_add_seconds(60, lambda: self.voice_worker.tidy() or True)
        self.stop_listening = threading.Event()
        self.mood.voice, self.mood.level = "listen", 0.0
        through = " through your headset" if voice.settings()["microphone"] != "laptop" and voice.headset() else ""
        self.say(f"I'm listening{through}… (click me when you're done)", seconds=None)
        items = self.list.voice_items()
        threading.Thread(target=voice.warm_up, args=(self.voice_worker,), daemon=True).start()
        threading.Thread(target=self._converse, args=(items,), daemon=True).start()

    def _set_level(self, level: float) -> None:
        self.mood.level = self.mood.level * 0.5 + level * 0.5

    def _converse(self, items) -> None:
        from . import voice
        config = voice.settings()
        try:
            audio = voice.listen(self.stop_listening, self._set_level, config["max_seconds"])
        except OSError as error:
            GLib.idle_add(self._voice_done, f"I couldn't open the microphone ({error}).", None)
            return
        if not audio:
            GLib.idle_add(self._voice_done, "I didn't hear anything. Try again?", None)
            return
        GLib.idle_add(self._voice_state, "think", "Thinking…")
        try:
            text, language = voice.transcribe(self.voice_worker, audio)
        except (OSError, RuntimeError, ValueError) as error:
            GLib.idle_add(self._voice_done, f"I couldn't make out the words ({error}).", None)
            return
        if not text:
            GLib.idle_add(self._voice_done, "I didn't catch that. Try again?", None)
            return
        GLib.idle_add(self._voice_state, "think", f"You: “{text}”\nThinking…")
        reply = voice.think(self.chat, text, items, datetime.now().astimezone(), language)
        GLib.idle_add(self._answer, text, reply, language)

    def _voice_state(self, state: str, text: str) -> bool:
        self.mood.voice = state
        self.say(text, seconds=None)
        return False

    def _answer(self, heard: str, reply, language: str) -> bool:
        """Shows and speaks the answer, then does what it said it would."""
        from . import voice
        said = self._do(reply)
        if said:
            reply.say = said
        self.mood.voice = "talk"
        changed = reply.action in ("add_note", "done", "reschedule", "priority", "add_step") or reply.command == "undo"
        buttons = (("Undo", self.list._undo), ("OK", None)) if changed and reply.command != "undo" else \
            (("Talk again", self.talk), ("OK", None))
        self.say(f"You: “{heard}”\n\n{reply.say}", seconds=40, buttons=buttons)
        def speak():
            player = None
            if voice.settings()["speak"]:
                try:
                    player = voice.say_aloud(self.voice_worker, reply.say, language)
                except (OSError, RuntimeError, ValueError):
                    player = None
            self.speaking = player
            if player:
                player.wait()
            else:
                time.sleep(min(6.0, 1.0 + len(reply.say) / 15))       # roughly reading time
            GLib.idle_add(self._voice_done, None, "cheer" if reply.action in ("done", "add_note", "google_task",
                                                                               "google_event") else None)
        threading.Thread(target=speak, daemon=True).start()
        return False

    def _do(self, reply) -> str | None:
        """Carries out what you asked. Returns new words to say when the plan changed (e.g. a time it
        couldn't read), otherwise None. Slow jobs (Claude, Google) report back in their own bubble."""
        from . import voice
        moment = datetime.now().astimezone()
        store, item = self.list.store, reply.item
        if reply.command:
            return self._command(reply)
        if reply.action == "add_note" and reply.note:
            if store.add_note(reply.note, moment):
                self.list._changed()
        elif reply.action == "done" and item:
            self.list._finish(item)
        elif reply.action == "open" and item:
            self.list._open(item)
        elif reply.action == "show_list":
            self.open_list()
        elif reply.action == "reschedule" and item:
            until = voice.when_from_text(reply.when, moment)
            if until is None:
                return f"When should I move {voice.short_title(item.title, 6)} to?"
            store.snooze(item, until)
            self.list._changed()
            when = voice.spoken_when(voice.Item(id="x", source="note", title="", due=until.isoformat()), moment)
            return f"Moved {voice.short_title(item.title, 6)} to {when.removeprefix('on ')}."
        elif reply.action == "priority" and item:
            self.list._set_priority(item, {"medium": "medium", "high": "high", "low": "low"}.get(reply.level, "normal"))
        elif reply.action == "add_step" and item and reply.text:
            if store.add_step(item.id, reply.text):
                self.list._changed()
        elif reply.action == "split" and item:
            threading.Thread(target=self._split, args=(item,), daemon=True).start()
        elif reply.action == "draft" and item:
            threading.Thread(target=self._draft, args=(item, reply.text), daemon=True).start()
        elif reply.action in ("google_task", "google_event"):
            return self._to_google(reply, moment)
        return None

    def _command(self, reply) -> str | None:
        commands = {"undo": self.list._undo, "sync": self.list.sync,
                    "hide": lambda: GLib.timeout_add(2500, self.hide_pet),
                    "stop_timer": self.stop_timer}
        if reply.command in commands:
            commands[reply.command]()
        elif reply.command == "timer":
            from . import voice
            item = voice.best_item(reply.text, self.list.voice_items()) if reply.text else None
            self.start_timer(reply.minutes, item)
            if item:
                return f"{reply.minutes} minutes on {voice.short_title(item.title, 6)}. Go!"
        elif reply.command in ("focus_on", "focus_off", "focus_snooze") and self.watcher:
            if reply.command == "focus_snooze":
                self.watcher.snooze(reply.minutes)
            else:
                self.watcher.set_enabled(reply.command == "focus_on")
        elif reply.command.startswith("focus") and not self.watcher:
            return "The focus check isn't available here."
        return None

    # ---- jobs that take a while ---------------------------------------------------------------------------
    def _later(self, text: str, buttons=(("OK", None),), seconds: float | None = 30) -> None:
        def show():
            self.show_pet()
            self.say(text, buttons=buttons, seconds=seconds)
            return False
        GLib.idle_add(show)

    def _split(self, item) -> None:
        """Claude breaks one item into steps (no tools; only the title and details are sent)."""
        from . import digest, voice
        if not voice.settings().get("claude"):
            self._later("Breaking things into steps uses Claude, which is off. Turn it on with "
                        "\"claude\": true in ~/.config/nudgy/voice.json.")
            return
        try:
            text, _tokens = digest.ask_claude([item], digest.settings()["model"])
            steps = digest.parse(text, {item.id}).get(item.id, [])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._later(f"Claude couldn't help just now ({str(error)[:80]}).")
            return
        if not steps:
            self._later("Claude didn't find steps for that one.")
            return

        def add():
            self.list.store.steps.setdefault(item.id, []).extend(steps)
            self.list._changed()
            names = "\n".join("• " + step["text"].removeprefix("✦ ") for step in steps)
            self.show_pet()
            self.say(f"Steps for {voice.short_title(item.title, 6)}:\n{names}",
                     buttons=(("Undo", self.list._undo), ("OK", None)), seconds=40)
            return False
        GLib.idle_add(add)

    def _draft(self, item, wish: str) -> None:
        """Claude drafts a reply; you read, edit and send it yourself. Nudgy never sends anything."""
        from . import google, voice
        if not voice.settings().get("claude"):
            self._later("Drafting uses Claude, which is off. Turn it on with \"claude\": true in "
                        "~/.config/nudgy/voice.json.")
            return
        sender, subject, body = item.detail, item.title, ""
        if item.source == "gmail":
            try:
                sender, subject, body = google.message_text(item.id)
            except google.NETWORK_ERRORS as error:              # offline or signed out: draft from the subject
                log.info("email text unavailable for the draft: %s", google.describe(error))
        try:
            draft = voice.draft_reply(sender, subject, body, wish)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._later(f"I couldn't write a draft just now ({str(error)[:80]}).")
            return
        GLib.idle_add(lambda: DraftWindow(item, sender, subject, draft) and False)
        self._later("Here's a draft. Check it, then copy it or open it in Gmail to send it yourself.", seconds=12)

    def _to_google(self, reply, moment) -> str | None:
        from . import google, voice
        from .model import parse_quick_add
        if not google.can_write():
            self._later("To add things to Google I need permission to write to your Calendar and Tasks "
                        "(your read-only sign-in stays as it is).",
                        buttons=(("Allow…", lambda: threading.Thread(target=self._allow_google, daemon=True).start()),
                                 ("Not now", None)), seconds=60)
            return "I need your permission first."
        title, due, _priority = parse_quick_add(f"{reply.note} {reply.when}".strip(), moment)
        when = datetime.fromisoformat(due) if due else None
        if reply.action == "google_event" and when is None:
            return f"When is {title}? Say it again with a day or time."
        kind = "task" if reply.action == "google_task" else "event"

        def work():
            try:
                if kind == "task":
                    made = google.add_task(title, when)
                else:
                    all_day = "T" not in due
                    start = when if not all_day else when.replace(tzinfo=moment.tzinfo)
                    made = google.add_event(title, start, reply.minutes or 60, all_day)
            except google.NETWORK_ERRORS as error:
                log.warning("adding to Google failed: %s", google.describe(error))
                self._later(f"Google didn't take it ({google.describe(error)}).")
                return
            where = "Google Tasks" if kind == "task" else "your Google Calendar"
            def undo() -> None:
                threading.Thread(target=google.remove, args=(kind, made), daemon=True).start()
            self._later(f"Added “{title}” to {where}" + (f", {spoken}." if spoken else "."),
                        buttons=(("Undo", undo), ("OK", None)))
        spoken = voice.spoken_when(voice.Item(id="x", source="note", title="", due=due), moment) if due else ""
        spoken = spoken.removeprefix("on ")
        threading.Thread(target=work, daemon=True).start()
        where = "your Google Tasks" if kind == "task" else "your calendar"
        return f"Adding {title} to {where}" + (f", {spoken}." if spoken else ".")

    def _allow_google(self) -> None:
        from . import google
        try:
            google.authorize_write()
        except google.NETWORK_ERRORS as error:
            log.warning("Google write sign-in failed: %s", google.describe(error))
            self._later(f"Sign-in didn't finish ({google.describe(error)}).")
            return
        self._later("Thanks! Ask me again and I'll add it.")

    # ---- focus timer -------------------------------------------------------------------------------------
    def start_timer(self, minutes: int, item=None) -> None:
        self.stop_timer()
        self.mood.timer_end = time.time() + minutes * 60
        self.mood.timer_total = minutes * 60
        self.timer_item = item
        self.timer_id = GLib.timeout_add_seconds(max(1, minutes * 60), self._timer_done)
        self.refresh_tip()

    def stop_timer(self) -> None:
        if getattr(self, "timer_id", 0):
            GLib.source_remove(self.timer_id)
        self.timer_id = 0
        self.mood.timer_end = 0.0
        self.refresh_tip()

    def _timer_done(self) -> bool:
        self.timer_id = 0
        self.mood.timer_end = 0.0
        item = getattr(self, "timer_item", None)
        self.mood.act("cheer", 3)
        self.show_pet()
        buttons = [("5 more minutes", lambda: self.start_timer(5, item))]
        if item:
            buttons.insert(0, ("Done ✓", lambda: self.list._finish(item)))
        self.say("Time's up! How did it go?", buttons=tuple(buttons) + (("OK", None),), seconds=120)
        self.refresh_tip()
        return False

    def _voice_done(self, message: str | None, then: str | None) -> bool:
        self.mood.voice, self.mood.level = "", 0.0
        self.speaking = None
        if message:
            self.say(message, seconds=6, buttons=(("Talk again", self.talk),))
        if then:
            self.mood.act(then, 2.0)
        return False

    def hush(self) -> None:
        """Stops it mid-sentence."""
        if self.speaking and self.speaking.poll() is None:
            self.speaking.terminate()

    # ---- the focus check's verdicts --------------------------------------------------------------
    def judge(self, level: int, text: str, buttons=()) -> None:
        """Level 1: side-eye; 2: sulk; 3: walks to the middle of the screen and turns its back."""
        if not self.get_visible():
            return
        self.mood.judge = level
        if level >= 3 and self.home is None:
            self.home = self.get_position()
            x, y = self.home
            area = self._area(x + SIZE // 2, y + SIZE // 2)
            self.move(area.x + area.width // 2 - SIZE // 2, area.y + area.height - SIZE - 40)
        self.say(text, buttons, seconds=45 if level == 1 else None, judgy=True)
        if self.tray:
            self.tray.refresh()

    def forgive(self, happy: bool = True) -> None:
        was = self.mood.judge
        self.mood.judge = 0
        if self.home is not None:
            self.move(*self.home)
            self.home = None
        if self.bubble:
            self.bubble.close_bubble()
        if was and happy:
            self.mood.act("cheer", 2)
            self.say("Yay, back to it! 💪", seconds=4)
        if self.tray:
            self.tray.refresh()

    # ---- drawing, idle life and input ------------------------------------------------------------
    def _frame(self) -> bool:
        if not self.get_visible():
            return True
        moment = time.time()
        face = self.mood.face(moment)
        speed = {"cheer": 4, "alarm": 2.2, "busy": 1.6, "love": 2.5, "sleep": 0.8}.get(face, 1.6)
        self.phase += FRAME_MS / 1000 * speed
        if moment > self.blink_until + 0.2 and random.random() < 0.012:
            self.blink_until = moment + 0.15
        self._follow_pointer()
        self._idle_life(moment)
        self.queue_draw()
        return True

    def _follow_pointer(self) -> None:
        pointer = self.get_display().get_default_seat().get_pointer()
        _screen, px, py = pointer.get_position()
        x, y = self.get_position()
        dx, dy = px - (x + SIZE / 2), py - (y + SIZE / 2)
        distance = math.hypot(dx, dy)
        target = (0.0, 0.0) if distance > 500 or distance < 1 else (dx / distance, dy / distance)
        if self.mood.action == "look" and time.time() < self.mood.action_until:
            return                            # looking around on its own
        lx, ly = self.mood.look
        self.mood.look = (lx + (target[0] - lx) * 0.25, ly + (target[1] - ly) * 0.25)

    def _idle_life(self, moment: float) -> None:
        """Small things it does on its own, and the occasional reminder of what's next."""
        if self.mood.judge or (self.mood.action and moment < self.mood.action_until):
            return
        base = self.mood.base()
        if moment >= self.next_idle:
            if base == "happy":
                name, seconds = random.choice((("hop", 0.6), ("wink", 0.8), ("look", 2.0), ("yawn", 1.6)))
                self.mood.act(name, seconds)
                if name == "look":
                    self.mood.look = (random.choice((-1.0, 1.0)), 0.0)
            elif base == "busy" and random.random() < 0.3:
                self.mood.act("hop", 0.6)
            self.next_idle = moment + random.uniform(15, 40)
        every = REMIND_ALARM if base == "alarm" else REMIND_BUSY if base == "busy" else None
        if every and moment - self.last_reminder > every and not self.list.get_visible() and not self.bubble:
            self.last_reminder = moment
            self.whats_next()

    def _draw(self, _widget, cr) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        draw(cr, SIZE, self.mood, self.phase, blink=time.time() < self.blink_until)
        return True

    def _pressed(self, _widget, event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if event.button == 1:
            press = self.press = (event.x_root, event.y_root)
            GLib.timeout_add(LONG_PRESS_MS, self._long_press, press)
        elif event.button == 2:
            self.whats_next()
        elif event.button == 3:
            self.menu().popup_at_pointer(event)
        return True

    def _long_press(self, press) -> bool:
        """Holding the left button still on the pet: talk to it."""
        if self.press is press:              # not released or dragged in the meantime
            self.press = None
            self.talk()
        return False

    def _motion(self, _widget, event) -> bool:
        if self.press and math.dist(self.press, (event.x_root, event.y_root)) > 4:
            self.press = None
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
            return True
        if not event.state & Gdk.ModifierType.BUTTON1_MASK:
            self._stroke(event.x_root)
        return True

    def _stroke(self, x: float) -> None:
        """Wiggling the pointer back and forth over it counts as petting."""
        if self.last_x is not None and abs(x - self.last_x) > 2:
            direction = 1 if x > self.last_x else -1
            moment = time.time()
            if not self.strokes or self.strokes[-1][1] != direction:
                self.strokes.append((moment, direction))
            self.strokes = [s for s in self.strokes if moment - s[0] < 1.5]
            if len(self.strokes) >= 4 and not self.mood.judge:
                self.strokes.clear()
                self.mood.act("love", 2.5)
        self.last_x = x

    def _released(self, _widget, event) -> bool:
        if event.button == 1 and self.press:
            self.press = None
            if self.mood.voice == "listen" and self.stop_listening:
                self.stop_listening.set()     # a click while it listens: "I'm done talking"
            elif self.mood.voice == "talk":
                self.hush()
            else:
                self.toggle_list()
        return True

    def menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()
        visible = self.get_visible()
        entries = [("Close the list" if self.list.get_visible() else "Open the list",
                    self.list.hide_list if self.list.get_visible() else self.open_list),
                   ("What's next?", self.whats_next),
                   ("Talk to Nudgy (hold the pet, or Super+Shift+T)", self.talk),
                   ("Sync now", self.list.sync),
                   ("Accounts…", self.list._accounts)]
        for label, action in entries:
            menu.append(self._entry(label, action))
        if self.watcher:
            menu.append(Gtk.SeparatorMenuItem())
            on = self.watcher.enabled()
            menu.append(self._entry("Focus check: on (click to turn off)" if on
                                    else "Focus check: off (click to turn on)",
                                    lambda: self.watcher.set_enabled(not on)))
            if on:
                snooze = Gtk.MenuItem(label="Snooze focus check")
                sub = Gtk.Menu()
                for label, minutes in (("30 minutes", 30), ("1 hour", 60), ("Until tomorrow", None)):
                    sub.append(self._entry(label, lambda m=minutes: self.watcher.snooze(m)))
                snooze.set_submenu(sub)
                menu.append(snooze)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._entry(f"Hide to top bar ({SHORTCUT} brings it back)" if visible
                                else f"Show the pet ({SHORTCUT})", self.hide_pet if visible else self.show_pet))
        menu.show_all()
        menu.attach_to_widget(self, None)
        return menu

    @staticmethod
    def _entry(label: str, action) -> Gtk.MenuItem:
        entry = Gtk.MenuItem(label=label)
        entry.connect("activate", lambda _w: GLib.idle_add(lambda: action() and False))
        return entry


class Tray:
    """The icon in the top bar, next to Wi-Fi. Uses AppIndicator (Ubuntu's top bar supports it);
    falls back to GTK's older tray icon if the AppIndicator library is missing. Clicking it opens a
    small menu: what needs you, show or hide the pet, open the list, sync."""

    def __init__(self, pet: Pet):
        self.pet = pet
        self.folder = data_dir() / "tray"
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.drawn = ""
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as indicator
            self.indicator = indicator.Indicator.new_with_path(
                "nudgy", "nudgy-pet", indicator.IndicatorCategory.APPLICATION_STATUS, str(self.folder))
            self.indicator.set_status(indicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Nudgy")
            self.icon = None
        except (ValueError, ImportError):
            self.indicator = None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.icon = Gtk.StatusIcon()
            self.icon.set_title("Nudgy")
            self.icon.connect("activate", lambda *_: pet.toggle())
            self.icon.connect("popup-menu", self._popup)
        pet.tray = self
        for window in (pet, pet.list):
            window.connect("show", lambda *_: self.refresh())
            window.connect("hide", lambda *_: self.refresh())
        self.refresh()

    def _picture(self, size: int) -> cairo.ImageSurface:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        draw(cairo.Context(surface), size, self.pet.mood, badge=True, still=True)
        return surface

    def refresh(self) -> None:
        if self.icon:
            surface = self._picture(64)
            self.icon.set_from_pixbuf(Gdk.pixbuf_get_from_surface(surface, 0, 0, 64, 64))
            self.icon.set_tooltip_text(tip(self.pet.mood).replace(
                "Click to open the list", f"Click to show or hide the pet ({SHORTCUT})"))
            return
        # The top bar caches icons by name, so each look gets its own file.
        name = f"nudgy2-{self.pet.mood.face()}-{min(self.pet.mood.pressing, 10)}"
        if name != self.drawn:
            path = self.folder / f"{name}.png"
            if not path.exists():
                self._picture(64).write_to_png(str(path))
            self.indicator.set_icon_full(name, tip(self.pet.mood).splitlines()[0])
            self.drawn = name
        self.indicator.set_menu(self._tray_menu())

    def _tray_menu(self) -> Gtk.Menu:
        pet, listing = self.pet, self.pet.list
        menu = Gtk.Menu()
        header = Gtk.MenuItem(label=tip(pet.mood).split(chr(10))[0].rstrip(":"))
        header.set_sensitive(False)
        menu.append(header)
        for title in pet.mood.top[:5]:
            menu.append(Pet._entry(f"•  {title if len(title) <= 60 else title[:59] + '…'}", pet.open_list))
        menu.append(Gtk.SeparatorMenuItem())
        for label, action in (
                (f"Hide the pet ({SHORTCUT})" if pet.get_visible() else f"Show the pet ({SHORTCUT})", pet.toggle),
                ("Close the list" if listing.get_visible() else "Open the list", pet.toggle_list),
                ("Sync now", listing.sync)):
            menu.append(Pet._entry(label, action))
        menu.show_all()
        return menu

    def _popup(self, _icon, button, when) -> None:
        self.menu = self.pet.menu()
        self.menu.popup(None, None, None, None, button, when)
