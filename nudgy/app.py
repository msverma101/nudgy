"""The always-on-top sticky window (GTK 3, X11)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
import shutil
import subprocess
import threading
from dataclasses import replace
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from . import chats, claude_notes, digest, google, keep, luma, phone, sources  # noqa: E402
from .changes import Changes  # noqa: E402
from .model import (LATER, LEVELS, NOW, SOON, Item, Store, bucket, friendly_time, place_between,  # noqa: E402
                    important, place_key, ranked, search, ticked_claude)

log = logging.getLogger(__name__)

CHECK_SECONDS = 60      # how often sources are asked whether anything new arrived
DIGEST_SECONDS = 3600   # how often Claude turns new jotted notes into steps
AUTO_CLOSE = 8          # seconds the open list may sit unused before it closes
WIDTH, HEIGHT = 340, 520
SOURCE_LABELS = {"note": "Note", "task": "Task", "calendar": "Calendar", "gmail": "Gmail", "linkedin": "LinkedIn",
                 "google": "Google", "signal": "Signal", "luma": "Luma",
                 "claude": "Claude", "keep": "Keep", "potential": "Event idea",
                 "anthropic": "Claude (optional)"}
# Order of the clusters in the "by source" view.
DRAG_TARGET = "NUDGY_ITEM"
LEVEL_FOR_SECTION = {NOW: "high", SOON: "medium", LATER: "low"}
CLUSTERS = (("note", "Notes"), ("claude", "Claude sessions"), ("calendar", "Calendar"), ("luma", "Luma"),
            ("potential", "Potential events"), ("task", "Google Tasks"), ("keep", "Google Keep"),
            ("gmail", "Gmail"), ("linkedin", "LinkedIn"), ("signal", "Signal"))
OPENS_IN = {"calendar": "Google Calendar", "luma": "Luma", "task": "Google Tasks", "gmail": "Gmail",
            "linkedin": "Gmail", "signal": "Signal Desktop", "note": "your browser",
            "claude": "your notes", "keep": "Google Keep", "potential": "your browser"}
LUMA_STEPS = (
    "Nudgy reads your personal Luma calendar link.\n\n"
    "1. Open luma.com, click your profile picture > Settings.\n"
    "2. Scroll to Calendar Syncing (under Third Party Accounts) and choose Add iCal Subscription.\n"
    "3. Copy the link it shows and paste it below.\n\n"
    "It covers events you host, registered for, are waitlisted or pending for, or were invited to. "
    "Keep the link private: anyone with it can see your events.")
GOOGLE_STEPS = (
    "Google needs a one-time setup (about 10 minutes):\n\n"
    "1. At console.cloud.google.com create a project.\n"
    "2. Enable the Google Calendar, Google Tasks and Gmail APIs.\n"
    "3. Under Google Auth Platform set the audience to External, add your Gmail address as a test "
    "user, then choose Publish app.\n"
    "4. Under Clients create a Desktop app client and download its JSON file.\n\n"
    "Then choose that file here. Access is read-only.")
CLAUDE_STEPS = (
    "Nudgy runs everything on this computer. If you connect Claude, it is used for writing reply drafts, "
    "breaking notes into steps and the focus check's unsure cases, and only the text of that one job is sent "
    "to Anthropic. Talking to the pet always stays here. Paste an API key from the Anthropic Console (it is "
    "stored only on this computer), or use your Claude Code login.")
CSS = b"""
window.nudgy { background-color: #1f2329; color: #e8eaed; }
.nudgy-head { background-color: #2a2f37; padding: 6px 8px; }
.nudgy-title { font-weight: bold; font-size: 13px; }
.nudgy-status { color: #9aa0a6; font-size: 11px; }
.nudgy-status.problem { color: #f6b26b; }
.nudgy-section { color: #9aa0a6; font-size: 11px; font-weight: bold; padding: 8px 10px 2px 10px; }
.nudgy-section.now { color: #ff8a80; }
.nudgy-section.soon { color: #ffd479; }
.nudgy-bullets { background-color: #262a31; border-left: 3px solid #ff6b6b; border-radius: 4px; padding: 4px 0; }
.nudgy-bullet { color: #ff6b6b; font-size: 16px; font-weight: bold; }
.nudgy-bullet-text { font-size: 13px; color: #f1f3f4; }
.nudgy-handle { color: #5f6670; font-size: 13px; padding: 0 2px; }
.nudgy-handle:hover { color: #e8eaed; }
.nudgy-cluster { color: #c4c7cc; font-size: 11px; font-weight: bold; padding: 8px 10px 2px 10px; }
.nudgy-cluster-urgent { color: #ff8a80; font-size: 11px; padding: 8px 0 2px 0; }
list, list row { background-color: transparent; }
list row:hover { background-color: #2a2f37; }
.nudgy-item { font-size: 13px; }
.nudgy-meta { color: #9aa0a6; font-size: 11px; }
.nudgy-step { font-size: 12px; color: #c4c7cc; }
.nudgy-step.done { color: #6d7580; }
.nudgy-step-x { min-height: 16px; min-width: 16px; font-size: 11px; color: #6d7580; }
.nudgy-flat { background: none; border: none; box-shadow: none; color: #c4c7cc;
              min-height: 22px; min-width: 22px; padding: 0; }
.nudgy-flat:hover { color: #ffffff; background-color: #3a404a; }
.nudgy-flat:disabled { color: #4a5059; }
.nudgy-dot { font-size: 14px; }
.nudgy-dot.now { color: #ff6b6b; }
.nudgy-dot.soon { color: #ffc14d; }
.nudgy-dot.later { color: #6d7580; }
entry.nudgy-entry { background-color: #2a2f37; color: #e8eaed; border: none; border-radius: 0;
                    box-shadow: none; padding: 8px 10px; }
"""


# Links that open things on this computer. Only your own notes and Claude notes may carry them; anything
# that came from outside (mail, chats, event feeds) opens only if it's an https:// web link.
LOCAL_LINKS = ("resume:", "file://", "obsidian://")
OWN_SOURCES = ("note", "claude")


def _trusted_link(item: Item) -> bool:
    if item.url.startswith(LOCAL_LINKS):
        return item.source in OWN_SOURCES
    return item.url.startswith("https://") or not item.url


def launch(argv: list[str]) -> None:
    """Starts an app in its own systemd scope. Started directly, it would belong to Nudgy's
    service, and every Nudgy restart would kill it too (this is what crashed Obsidian)."""
    if shutil.which("systemd-run"):
        argv = ["systemd-run", "--user", "--scope", "--collect", "--quiet", *argv]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def now() -> datetime:
    return datetime.now().astimezone()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, store: Store):
        super().__init__(application=app, title="Nudgy")
        self.store = store
        self.syncing = False
        self.problem = ""
        self.last_sync: datetime | None = None
        self.undo_timer = 0
        self.history: list[str] = []      # snapshots before each change, newest last
        self.redo_stack: list[str] = []
        self.current = self.store.snapshot()
        self.save_timer = 0
        self.accounts_grid = None
        self.pending: str | None = None      # a sync asked for while another was running
        self.on_update = self.on_finish = self.on_hide = None   # the pet listens to these
        self._build()
        self._place()
        self.get_child().show_all()          # the window itself opens from the pet
        self.undo.set_sensitive(False)                  # nothing to undo yet
        if self.store.window.get("collapsed"):          # old setting from the removed ▾ button
            self.store.window.pop("collapsed")
        self.idle_seconds = 0
        self.idle_timer = 0
        self.dragging = False
        self.render()
        self.changes = Changes()
        self.sync(manual=False)
        GLib.timeout_add_seconds(60, self._tick)
        if digest.due():
            GLib.timeout_add_seconds(90, lambda: self.digest() and False)
        GLib.timeout_add_seconds(CHECK_SECONDS, self._check_tick)
        GLib.timeout_add_seconds(DIGEST_SECONDS, self._digest_tick)

    def _build(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.get_style_context().add_class("nudgy")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.stick()                                # every workspace
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.connect("delete-event", lambda *_: True)   # Alt+F4 must not lose the list
        self.connect("configure-event", self._moved)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        drag = Gtk.EventBox()
        drag.connect("button-press-event", self._drag)
        head = Gtk.Box(spacing=6)
        head.get_style_context().add_class("nudgy-head")
        drag.add(head)
        outer.pack_start(drag, False, False, 0)
        self.title_label = Gtk.Label(label="Tasks", xalign=0)
        self.title_label.get_style_context().add_class("nudgy-title")
        head.pack_start(self.title_label, False, False, 0)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("nudgy-status")
        head.pack_start(self.status, True, True, 0)
        self.undo = self._button("↶", "Undo the last change (Ctrl+Z). Ctrl+Shift+Z redoes", self._undo)
        self.connect("key-press-event", self._key)
        head.pack_end(self._button("↻", "Sync now", lambda *_: self.sync()), False, False, 0)
        head.pack_end(self._button("⇅", "Re-rank: let the local model suggest what needs you now, soon or later",
                                   lambda *_: self.rerank()), False, False, 0)
        self.view_button = self._button("▤", "", self._toggle_view)
        head.pack_end(self.view_button, False, False, 0)
        self.focus_button = self._button("◎", "", self._toggle_focus)
        head.pack_end(self.focus_button, False, False, 0)
        head.pack_end(self._button("👤", "Accounts: sign in to Google, link Signal",
                                   lambda *_: self._accounts()), False, False, 0)
        head.pack_end(self._button("🔍", "Search everything in Nudgy (Ctrl+F)", lambda *_: self._open_search()),
                      False, False, 0)
        head.pack_end(self.undo, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text="Search tasks, notes, mail, steps…", no_show_all=True)
        self.search.get_style_context().add_class("nudgy-entry")
        self.search.connect("search-changed", lambda *_: self.render())
        self.search.connect("stop-search", lambda *_: self._close_search())
        outer.pack_start(self.search, False, False, 0)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.body, True, True, 0)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.add(self.rows)
        self.body.pack_start(scroller, True, True, 0)
        self.scroller = scroller
        self.scroll_speed = 0.0
        self.scroll_timer = 0
        # Dragging an item near the top or bottom edge scrolls the list. The scroller listens too, so it
        # also works over headings and gaps, where no list takes drops.
        edge = Gtk.TargetEntry.new(DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)
        scroller.drag_dest_set(0, [edge], Gdk.DragAction.MOVE)
        scroller.connect("drag-motion", lambda w, _c, _x, y, _t: self._edge_scroll(y) and False)
        scroller.connect("drag-leave", lambda *_: self._stop_edge_scroll())
        self.entry = Gtk.Entry(placeholder_text="Add a note… (! = high, today, tomorrow)")
        self.entry.get_style_context().add_class("nudgy-entry")
        self.entry.connect("activate", self._add)
        footer = Gtk.Box()
        footer.pack_start(self.entry, True, True, 0)
        grip = Gtk.EventBox(tooltip_text="Drag to resize")
        grip.add(Gtk.Label(label="◢", margin_start=6, margin_end=4))
        grip.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "se-resize")))
        grip.connect("button-press-event", self._resize)
        footer.pack_end(grip, False, False, 0)
        self.body.pack_start(footer, False, False, 0)

    def _button(self, text: str, tip: str, handler) -> Gtk.Button:
        button = Gtk.Button(label=text, tooltip_text=tip, relief=Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("nudgy-flat")
        button.connect("clicked", handler)
        return button

    def _place(self) -> None:
        saved = self.store.window
        monitor = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        area = monitor.get_workarea()
        width = max(260, min(saved.get("width", WIDTH), area.width))
        height = max(200, min(saved.get("height", HEIGHT), area.height))
        self.set_default_size(width, height)
        x = saved.get("x", area.x + area.width - width - 16)
        y = saved.get("y", area.y + 16)
        # A saved position may belong to a monitor that has since been unplugged.
        screen_w, screen_h = self.get_screen().get_width(), self.get_screen().get_height()
        self.move(max(0, min(x, screen_w - 80)), max(0, min(y, screen_h - 40)))

    def _drag(self, _widget, event) -> bool:
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return True

    def _resize(self, _widget, event) -> bool:
        if event.button == 1:
            self.begin_resize_drag(Gdk.WindowEdge.SOUTH_EAST, event.button,
                                   int(event.x_root), int(event.y_root), event.time)
        return True

    def _moved(self, *_args) -> bool:
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add(800, self._save_geometry)
        return False

    def _save_geometry(self) -> bool:
        self.save_timer = 0
        x, y = self.get_position()
        width, height = self.get_size()
        self.store.window.update(x=x, y=y, width=width, height=height)
        self.store.save()
        return False

    def render(self) -> None:
        moment = now()
        for child in self.rows.get_children():
            self.rows.remove(child)
        everything = ranked(self.store.visible(moment), moment, self.store.overrides, self.store.places)
        focus = self.store.window.get("focus", True)
        scored = [(i, v) for i, v in everything if important(i, v)] if focus else everything
        hidden = len(everything) - len(scored)
        self.focus_button.set_label("◎" if focus else "◉")
        self.focus_button.set_tooltip_text(
            f"Important only: {hidden} less urgent items hidden. Click to show everything" if focus
            else "Showing everything. Click to show only what needs attention today")
        self.groups: dict[str, tuple[str, list]] = {}     # item id -> (group, its entries in order)
        by_source = self.store.window.get("view", "source") == "source"
        self.view_button.set_tooltip_text("Showing groups by source. Click to sort by urgency instead"
                                          if by_source else "Showing by urgency. Click to group by source instead")
        query = self.search.get_text().strip() if self.search.get_visible() else ""
        if query:
            # Search looks through everything, including what "Important only" hides.
            found = search(everything, query, self.store.steps, SOURCE_LABELS)
            self.rows.pack_start(self._section(f"Results for “{query[:30]}”", len(found), "search"), False, False, 0)
            if found:
                self.rows.pack_start(self._listing(found, moment, "search"), False, False, 0)
            else:
                none = Gtk.Label(label="No matches. Ticked-off items aren't searched.", margin=16, wrap=True)
                none.get_style_context().add_class("nudgy-meta")
                self.rows.pack_start(none, False, False, 0)
        elif by_source:
            pressing_now = [(i, v) for i, v in scored if bucket(v) == NOW]
            if pressing_now:
                self.rows.pack_start(self._section("Needs you now", len(pressing_now), "now"), False, False, 0)
                self.rows.pack_start(self._bullets(pressing_now, moment), False, False, 0)
            self._render_clusters(scored, moment)
        else:
            self._render_urgency(scored, moment)
        if hidden and not query:
            more = Gtk.EventBox(tooltip_text="Show everything (◎ in the top bar switches back)")
            more.add(Gtk.Label(label=f"+ {hidden} less urgent · show all", margin=8))
            more.get_child().get_style_context().add_class("nudgy-meta")
            more.connect("button-press-event", lambda *_: GLib.idle_add(self._toggle_focus) and True)
            self.rows.pack_start(more, False, False, 0)
        if not scored and not hidden and not query:
            empty = Gtk.Label(label="Nothing here yet.", margin=24)
            empty.get_style_context().add_class("nudgy-meta")
            self.rows.pack_start(empty, False, False, 0)
        self.rows.show_all()
        pressing = sum(1 for _, value in scored if bucket(value) == NOW)
        self.title_label.set_text(f"Tasks · {pressing} now" if pressing else "Tasks")
        self._show_status()
        if self.on_update:
            now_items = [(item, value) for item, value in scored if bucket(value) == NOW]
            self.on_update(pressing, any(value >= 90 for _, value in now_items),
                           [(item.title, friendly_time(item, moment)) for item, _ in now_items])
        try:
            phone.export(scored, moment)           # for the pet on your phone, if you set a folder
        except OSError as error:
            self.problem = f"Phone file: {error.strerror or error}"

    def show_list(self) -> None:
        self.store.window["list_open"] = True
        self.store.save()
        self.show()
        self.present()
        self.idle_seconds = 0
        if not self.idle_timer:
            self.idle_timer = GLib.timeout_add_seconds(1, self._idle_check)

    def _busy(self) -> bool:
        """Something in the list is in use, so it must not close by itself."""
        if self.dragging or Gtk.grab_get_current() is not None:        # menus and drags grab
            return True
        if self.entry.get_text().strip() or self.search.get_text().strip():
            return True
        if any(w.get_visible() and w is not self and w.get_transient_for() is self
               for w in Gtk.Window.list_toplevels()):                      # dialogs, Accounts
            return True
        pointer = self.get_display().get_default_seat().get_pointer()
        _screen, px, py = pointer.get_position()
        x, y = self.get_position()
        width, height = self.get_size()
        return x <= px < x + width and y <= py < y + height

    def _idle_check(self) -> bool:
        """Runs every second while the list is open; closes it after AUTO_CLOSE unused seconds."""
        if self.get_visible():
            self.idle_seconds = 0 if self._busy() else self.idle_seconds + 1
            if self.idle_seconds < AUTO_CLOSE:
                return True
            self.hide_list()
        self.idle_timer = 0
        return False

    def hide_list(self, remember: bool = False) -> None:
        """remember: hidden along with the pet, so it reopens when the pet comes back."""
        self.store.window["list_open"] = remember
        self.store.save()
        self.hide()
        if self.on_hide:
            self.on_hide()

    def _listing(self, entries, moment: datetime, group: str) -> Gtk.ListBox:
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        for item, value in entries:
            listing.add(self._row(item, bucket(value), moment))
            self.groups[item.id] = (group, entries)
        target = Gtk.TargetEntry.new(DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)
        listing.drag_dest_set(Gtk.DestDefaults.ALL, [target], Gdk.DragAction.MOVE)
        listing.connect("drag-motion", self._drag_motion, entries)
        listing.connect("drag-leave", lambda lb, *_: self._drag_left(lb))
        listing.connect("drag-data-received", self._dropped, group, entries)
        return listing

    # ---- moving items by hand --------------------------------------------------------------------
    @staticmethod
    def drop_zone(row_y: int, row_height: int, y: int) -> str:
        """Top third of a row: before it. Middle third: becomes its step. Bottom third: after it."""
        offset = y - row_y
        return "before" if offset < row_height / 3 else "after" if offset > row_height * 2 / 3 else "inside"

    def _drop_target(self, listing: Gtk.ListBox, y: int) -> tuple[int, str]:
        row = listing.get_row_at_y(y)
        if row is None:
            return len(listing.get_children()), "after"
        allocation = row.get_allocation()
        zone = self.drop_zone(allocation.y, allocation.height, y)
        return row.get_index() + (1 if zone == "after" else 0), zone

    @staticmethod
    def edge_speed(y: float, height: float, zone: float = 48, fastest: float = 14) -> float:
        """Pixels to scroll per tick while dragging: negative near the top, positive near the bottom,
        faster the closer to the edge, 0 elsewhere."""
        zone = min(zone, height / 5)            # a short list keeps most of its height still
        if y < zone:
            return -fastest * (zone - max(y, 0)) / zone
        if y > height - zone:
            return fastest * (min(y, height) - (height - zone)) / zone
        return 0.0

    def _edge_scroll(self, y: float) -> None:
        self.scroll_speed = self.edge_speed(y, self.scroller.get_allocated_height())
        if self.scroll_speed and not self.scroll_timer:
            self.scroll_timer = GLib.timeout_add(30, self._scroll_tick)

    def _scroll_tick(self) -> bool:
        if not self.dragging or not self.scroll_speed:
            self.scroll_timer = 0
            return False
        adjustment = self.scroller.get_vadjustment()
        top = adjustment.get_upper() - adjustment.get_page_size()
        adjustment.set_value(min(max(adjustment.get_value() + self.scroll_speed, 0), top))
        return True

    def _stop_edge_scroll(self) -> None:
        self.scroll_speed = 0.0

    def _drag_motion(self, listing, _context, x, y, _time, entries: list) -> bool:
        where = listing.translate_coordinates(self.scroller, x, y)
        if where:
            self._edge_scroll(where[1])
        row = listing.get_row_at_y(y)
        if row:
            listing.drag_highlight_row(row)
            _index, zone = self._drop_target(listing, y)
            hint = (f"Drop to make it a step of: {entries[row.get_index()][0].title[:40]}" if zone == "inside"
                    else "Drop to move it here")
            if self.problem != hint:
                self.problem = hint
                self._show_status()
        return False

    def _drag_left(self, listing) -> None:
        listing.drag_unhighlight_row()
        if self.problem.startswith("Drop to"):
            self.problem = ""
            self._show_status()

    def _dropped(self, listing, _context, _x, y, data, _info, _time, group: str, entries: list) -> None:
        item_id = data.get_data().decode()
        index, zone = self._drop_target(listing, y)
        self._drag_left(listing)
        if zone == "inside":
            parent = entries[index][0]
            GLib.idle_add(lambda: (self._nest(item_id, parent.id), False)[1])
        else:
            GLib.idle_add(lambda: (self._move(item_id, group, entries, index), False)[1])

    def _nest(self, item_id: str, parent_id: str) -> None:
        item = next((i for i in self.store.visible(now()) if i.id == item_id), None)
        if item and self.store.nest(item, parent_id, now()):
            self._changed()

    def _choose_parent(self, item: Item) -> None:
        """Right-click > Make a step of…: pick the item it should go under."""
        moment = now()
        candidates = [i for i, v in ranked(self.store.visible(moment), moment, self.store.overrides,
                                           self.store.places)
                      if i.id != item.id and (i.source == "note" or bucket(v) in (NOW, SOON))]
        dialog = Gtk.Dialog(title="Make a step of…", transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.set_default_size(360, 380)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Move here", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        heading = Gtk.Label(xalign=0, wrap=True, max_width_chars=40, margin=8)
        heading.set_text(f"Put “{item.title[:80]}” under:")
        area.add(heading)
        search = Gtk.SearchEntry(placeholder_text="Search", margin_start=8, margin_end=8)
        area.add(search)
        listing = Gtk.ListBox(activate_on_single_click=False)
        for candidate in candidates:
            label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, margin=6)
            label.set_text(f"{SOURCE_LABELS[candidate.source]} · {candidate.title}")
            row = Gtk.ListBoxRow()
            row.add(label)
            row.candidate = candidate
            listing.add(row)
        listing.set_filter_func(lambda row: search.get_text().lower() in row.candidate.title.lower())
        search.connect("search-changed", lambda *_: listing.invalidate_filter())
        listing.connect("row-activated", lambda *_: dialog.response(Gtk.ResponseType.OK))
        scroller = Gtk.ScrolledWindow(vexpand=True, margin=8)
        scroller.add(listing)
        area.pack_start(scroller, True, True, 0)
        dialog.show_all()
        response = dialog.run()
        chosen = listing.get_selected_row()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and chosen:
            self._nest(item.id, chosen.candidate.id)

    def _move(self, item_id: str, group: str, entries: list, index: int) -> None:
        """Puts the item at `index` of the group's list (counted before it was taken out)."""
        item = next((i for i in self.store.visible(now()) if i.id == item_id), None)
        if item is None:
            return
        value = next((v for i, v in entries if i.id == item_id), None)     # None: came from another group
        by_source = self.store.window.get("view", "source") == "source"
        if group == "search":
            self.problem = "Clear the search to move items around"
            self._show_status()
            GLib.timeout_add_seconds(4, self._clear_hint, self.problem)
            return
        if by_source and item.source != group:
            self.problem = "Items stay in their own source group. Use ▤ to sort across sources"
            self._show_status()
            GLib.timeout_add_seconds(4, self._clear_hint, self.problem)
            return
        if not by_source and value is None:
            # Dragged into another of Now / Soon / Later: that becomes its priority.
            self.store.overrides[item.id] = LEVEL_FOR_SECTION[group]
        before_count = sum(1 for i, _ in entries[:index] if i.id != item_id)
        others = [place_key(i, v, self.store.places) for i, v in entries if i.id != item_id]
        before = others[before_count - 1] if before_count > 0 else None
        after = others[before_count] if before_count < len(others) else None
        self.store.places[item.id] = place_between(before, after)
        self._changed()

    def _move_by(self, item: Item, where: str) -> None:
        group, entries = self.groups[item.id]
        index = next(n for n, (i, _) in enumerate(entries) if i.id == item.id)
        target = {"top": 0, "up": max(index - 1, 0), "down": min(index + 2, len(entries)),
                  "bottom": len(entries)}[where]
        self._move(item.id, group, entries, target)

    def _handle(self, item: Item, row: Gtk.ListBoxRow) -> Gtk.EventBox:
        handle = Gtk.EventBox(tooltip_text="Drag to move", valign=Gtk.Align.START)
        label = Gtk.Label(label="⠿")
        label.get_style_context().add_class("nudgy-handle")
        handle.add(label)
        target = Gtk.TargetEntry.new(DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)
        handle.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target], Gdk.DragAction.MOVE)
        handle.connect("drag-data-get", lambda _w, _c, data, _i, _t: data.set(data.get_target(), 8,
                                                                              item.id.encode()))
        handle.connect("drag-begin", lambda w, context: self._drag_started(row, context))
        handle.connect("drag-end", lambda *_: (setattr(self, "dragging", False), self._stop_edge_scroll()))
        handle.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "grab")))
        return handle

    def _drag_started(self, row: Gtk.ListBoxRow, context) -> None:
        self.dragging = True
        self._drag_icon(row, context)

    @staticmethod
    def _drag_icon(row: Gtk.ListBoxRow, context) -> None:
        """Drag a picture of the row, so it's clear what is moving."""
        import cairo
        allocation = row.get_allocation()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, allocation.width, allocation.height)
        cr = cairo.Context(surface)
        cr.set_source_rgba(0.16, 0.18, 0.22, 0.95)
        cr.paint()
        row.draw(cr)
        Gtk.drag_set_icon_surface(context, surface)

    def _render_urgency(self, scored, moment: datetime) -> None:
        groups: dict[str, list[tuple[Item, int]]] = {NOW: [], SOON: [], LATER: []}
        for item, value in scored:
            groups[bucket(value)].append((item, value))
        for name, entries in groups.items():
            if entries:
                self.rows.pack_start(self._section(name, len(entries)), False, False, 0)
                self.rows.pack_start(self._bullets(entries, moment) if name == NOW
                                     else self._listing(entries, moment, name), False, False, 0)

    def _render_clusters(self, scored, moment: datetime) -> None:
        """One cluster per source, most pressing first inside each. Click a heading to fold it."""
        folded = set(self.store.window.get("folded", []))
        for source, label in CLUSTERS:
            entries = [(item, value) for item, value in scored if item.source == source]
            if not entries:
                continue
            urgent = sum(1 for _, value in entries if bucket(value) == NOW)
            is_folded = source in folded
            heading = Gtk.Box(spacing=6)
            title = Gtk.Label(label=f"{'▸' if is_folded else '▾'} {label.upper()} ({len(entries)})", xalign=0)
            title.get_style_context().add_class("nudgy-cluster")
            heading.pack_start(title, False, False, 0)
            if urgent:
                badge = Gtk.Label(label=f"{urgent} now")
                badge.get_style_context().add_class("nudgy-cluster-urgent")
                heading.pack_start(badge, False, False, 0)
            clickable = Gtk.EventBox(tooltip_text="Click to fold or unfold")
            clickable.add(heading)
            clickable.connect("button-press-event", lambda *_a, s=source: self._fold(s))
            self.rows.pack_start(clickable, False, False, 0)
            if not is_folded:
                self.rows.pack_start(self._listing(entries, moment, source), False, False, 0)

    def _fold(self, source: str) -> bool:
        folded = set(self.store.window.get("folded", []))
        folded ^= {source}
        self.store.window["folded"] = sorted(folded)
        GLib.idle_add(self._changed)
        return True

    def _toggle_focus(self, *_args) -> bool:
        self.store.window["focus"] = not self.store.window.get("focus", True)
        self._changed()
        return False

    def _toggle_view(self, *_args) -> None:
        current = self.store.window.get("view", "source")
        self.store.window["view"] = "urgency" if current == "source" else "source"
        self._changed()

    def _section(self, name: str, count: int, kind: str = "") -> Gtk.Label:
        label = Gtk.Label(label=f"{name.upper()} ({count})", xalign=0)
        context = label.get_style_context()
        context.add_class("nudgy-section")
        context.add_class(kind or name.lower())
        return label

    def _bullets(self, entries, moment: datetime) -> Gtk.Box:
        """Urgent items as a plain bulleted list, written out in full so they are easy to read.
        Click a bullet to open it, right-click for the usual menu, ✓ to mark it done."""
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=10, margin_end=6, margin_bottom=6)
        block.get_style_context().add_class("nudgy-bullets")
        for item, _value in entries:
            events = Gtk.EventBox()
            events.connect("button-press-event", self._row_click, item)
            opens = f"Click to open in {self._opens_in(item)}" if self._openable(item) else ""
            events.set_tooltip_text("\n\n".join(p for p in (item.title, opens) if p))
            line = Gtk.Box(spacing=8, margin_top=3, margin_bottom=3)
            events.add(line)
            mark = Gtk.Label(label="•", valign=Gtk.Align.START)
            mark.get_style_context().add_class("nudgy-bullet")
            line.pack_start(mark, False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            title = Gtk.Label(xalign=0, wrap=True, max_width_chars=34, lines=4,
                              ellipsize=Pango.EllipsizeMode.END)
            title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            title.set_text(item.title)        # plain text: titles come from emails
            title.get_style_context().add_class("nudgy-bullet-text")
            text.pack_start(title, False, False, 0)
            when = friendly_time(item, moment)
            meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            meta.set_text(" · ".join(p for p in (SOURCE_LABELS[item.source], when,
                                                 self.store.step_progress(item.id)) if p))
            meta.get_style_context().add_class("nudgy-meta")
            text.pack_start(meta, False, False, 0)
            line.pack_start(text, True, True, 0)
            done = self._button("✓", "Done", lambda *_a, i=item: self._finish(i))
            done.set_valign(Gtk.Align.START)
            line.pack_end(done, False, False, 0)
            block.pack_start(events, False, False, 0)
        return block

    def _row(self, item: Item, group: str, moment: datetime) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False)
        events = Gtk.EventBox()
        events.connect("button-press-event", self._row_click, item)
        box = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        events.add(box)
        row.add(events)

        box.pack_start(self._handle(item, row), False, False, 0)
        check = Gtk.CheckButton(tooltip_text="Done", valign=Gtk.Align.START)
        check.connect("toggled", lambda *_: self._finish(item))
        box.pack_start(check, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Plain text on purpose: titles come from emails and must never be parsed as markup.
        title = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END, max_width_chars=30)
        title.set_text(item.title)
        title.get_style_context().add_class("nudgy-item")
        text.pack_start(title, False, False, 0)
        if self._openable(item):
            events.set_tooltip_text(f"Click to open in {self._opens_in(item)}")
        source_label = SOURCE_LABELS[item.source] + (" ↗" if self._openable(item) else "")
        parts = [source_label, friendly_time(item, moment),
                 self.store.step_progress(item.id), item.detail]
        meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        meta.set_text(" · ".join(p for p in parts if p))
        meta.get_style_context().add_class("nudgy-meta")
        text.pack_start(meta, False, False, 0)
        for index, step in enumerate(self.store.steps.get(item.id, [])):
            text.pack_start(self._step_row(item, index, step), False, False, 0)
        box.pack_start(text, True, True, 0)

        dot = self._button("●", "Change priority", lambda *_: self._cycle(item))
        dot.set_valign(Gtk.Align.START)
        context = dot.get_style_context()
        context.add_class("nudgy-dot")
        context.add_class(group.lower())
        box.pack_end(dot, False, False, 0)
        return row

    def _step_row(self, item: Item, index: int, step: dict) -> Gtk.Box:
        line = Gtk.Box(spacing=4)
        check = Gtk.CheckButton(active=step["done"], tooltip_text="Step done")
        check.connect("toggled", lambda *_: self._later(self.store.toggle_step, item.id, index))
        line.pack_start(check, False, False, 0)
        label = Gtk.Label(xalign=0, wrap=True, max_width_chars=28)
        label.set_text(step["text"] + (" ↗" if step.get("url") else ""))         # plain text, never markup
        if step["done"]:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_strikethrough_new(True))
            label.set_attributes(attrs)
        context = label.get_style_context()
        context.add_class("nudgy-step")
        if step["done"]:
            context.add_class("done")
        events = Gtk.EventBox(tooltip_text="Click to open · double-click to edit · right-click for more"
                              if step.get("url") else "Double-click to edit · right-click for more")
        events.add(label)
        events.connect("button-press-event", self._step_click, item, index)
        line.pack_start(events, True, True, 0)
        remove = self._button("×", "Remove step", lambda *_: self._later(self.store.remove_step, item.id, index))
        remove.get_style_context().add_class("nudgy-step-x")
        line.pack_end(remove, False, False, 0)
        return line

    def _step_item(self, step: dict) -> Item:
        """What a step opens: the item it was made from, or its own link."""
        if step.get("from"):
            return Item(**step["from"])
        return Item(id="step", source="note", title=step["text"], url=step.get("url", ""))

    def _step_menu(self, item: Item, index: int, event) -> None:
        step = self.store.steps[item.id][index]
        menu = Gtk.Menu()
        choices: list[tuple[str, Callable[[], object]]] = []
        target = self._step_item(step)
        if step.get("url") and self._openable(target):
            choices.append((f"Open in {self._opens_in(target)}", lambda: self._open(target)))
        choices.append(("Edit step…", lambda: self._edit_step(item, index)))
        choices.append(("Make it a separate item", lambda: self._later(self.store.unnest, item.id, index, now())))
        choices.append(("Remove step", lambda: self._later(self.store.remove_step, item.id, index)))
        for label, action in choices:
            entry = Gtk.MenuItem(label=label)
            entry.connect("activate", lambda _w, action=action: action())
            menu.append(entry)
        menu.show_all()
        menu.attach_to_widget(self)
        menu.popup_at_pointer(event)

    def _step_click(self, _widget, event, item: Item, index: int) -> bool:
        step = self.store.steps[item.id][index]
        if event.button == 3:
            self._step_menu(item, index, event)
            return True
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS and step.get("url"):
            target = self._step_item(step)
            if self._openable(target):
                self._open(target)
            return True
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            self._edit_step(item, index)
        return True        # keep clicks on a step from opening the item's link

    def _edit_step(self, item: Item, index: int) -> None:
        text = self._ask("Edit step", self.store.steps[item.id][index]["text"])
        if text:
            self.store.edit_step(item.id, index, text)
            self._changed()

    def _later(self, action, *args) -> None:
        """Change the store, then rebuild once the clicked widget's signal has finished."""
        action(*args)
        GLib.idle_add(self._changed)

    def _ask(self, title: str, text: str = "") -> str:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=text, activates_default=True, width_chars=36, margin=8)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        response, value = dialog.run(), entry.get_text().strip()
        dialog.destroy()
        return value if response == Gtk.ResponseType.OK else ""

    def _add_steps(self, item: Item) -> None:
        """Enter adds a step and clears the box, so several can be typed in a row."""
        dialog = Gtk.Dialog(title="Add steps", transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.add_buttons("Done", Gtk.ResponseType.CLOSE)
        area = dialog.get_content_area()
        heading = Gtk.Label(xalign=0, wrap=True, max_width_chars=36, margin=8)
        heading.set_text(item.title)
        area.add(heading)
        entry = Gtk.Entry(placeholder_text="Type a step, press Enter", width_chars=36, margin=8)
        area.add(entry)
        added = Gtk.Label(xalign=0, margin=8)
        added.get_style_context().add_class("nudgy-meta")
        area.add(added)

        def add(_entry):
            if self.store.add_step(item.id, entry.get_text()):
                entry.set_text("")
                added.set_text(self.store.step_progress(item.id))
                self._changed()

        entry.connect("activate", add)
        dialog.show_all()
        dialog.run()
        add(entry)          # keep a step typed but not yet entered
        dialog.destroy()

    def _row_click(self, _widget, event, item: Item) -> bool:
        if event.button == 3:
            self._menu(item, event)
            return True
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS and self._openable(item):
            self._open(item)
            return True
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS and item.source == "note" \
                and not item.url:
            self._edit(item)
            return True
        return False

    @staticmethod
    def _openable(item: Item) -> bool:
        return item.url.startswith(("https://", *LOCAL_LINKS)) and _trusted_link(item) or item.source == "signal"

    @staticmethod
    def _opens_in(item: Item) -> str:
        if item.url.startswith("resume:"):
            return "a terminal (resumes the Claude session)"
        if item.url.startswith("obsidian://"):
            return "Obsidian"
        if item.url.startswith("file://"):
            return "your notes file"
        if item.source == "claude":
            return "your browser"
        return OPENS_IN.get(item.source, "browser")

    def voice_items(self, limit: int = 400) -> list[tuple[Item, str]]:
        """What the pet tells the local model about when you talk to it: most pressing first."""
        moment = now()
        scored = ranked(self.store.visible(moment), moment, self.store.overrides, self.store.places)
        return [(item, bucket(value)) for item, value in scored[:limit]]

    def _open(self, item: Item) -> None:
        if not _trusted_link(item):
            self.problem = "That link isn't a normal web link, so it wasn't opened"
            self._show_status()
            return
        if item.url.startswith("resume:"):
            try:
                claude_notes.resume(item.url)
            except (OSError, ValueError) as error:
                self.problem = f"Could not resume: {error}"
                self._show_status()
            return
        if item.url.startswith(("https://", "file://", "obsidian://")):
            try:
                launch(["xdg-open", item.url])
            except OSError as error:
                self.problem = f"Could not open link: {error}"
                self._show_status()
        elif item.source == "signal":
            # Signal Desktop can't be pointed at one chat, so it opens on its chat list.
            try:
                launch(["gtk-launch", "signal-desktop"])
            except OSError:
                self.problem = "Signal Desktop is not installed"
                self._show_status()

    def _menu(self, item: Item, event) -> None:
        menu = Gtk.Menu()
        choices: list[tuple[str, Callable[[], object]]] = []
        if self._openable(item):
            choices.append((f"Open in {self._opens_in(item)}", lambda: self._open(item)))
        if item.source == "note":
            choices.append(("Edit…", lambda: self._edit(item)))
            choices.append(("Change link…" if item.url else "Link to an email or page…",
                            lambda: self._link_note(item)))
        choices.append(("Add steps…", lambda: self._add_steps(item)))
        choices.append(("Make a step of…", lambda: self._choose_parent(item)))
        if any(step["done"] for step in self.store.steps.get(item.id, [])):
            choices.append(("Clear finished steps", lambda: self._clear_done_steps(item)))
        for where in ("top", "up", "down", "bottom"):
            label = {"top": "Move to top", "up": "Move up", "down": "Move down", "bottom": "Move to bottom"}[where]
            choices.append((label, partial(self._move_by, item, where)))
        if item.id in self.store.places:
            choices.append(("Back to automatic position", lambda: self._unplace(item)))
        for level in LEVELS:
            name = {"normal": "automatic", "medium": "medium (Soon)"}.get(level, level)
            choices.append((f"Priority: {name}", partial(self._set_priority, item, level)))
        choices.append(("Done", lambda: self._finish(item)))
        for label, action in choices:
            entry = Gtk.MenuItem(label=label)
            entry.connect("activate", lambda _w, action=action: action())
            menu.append(entry)
        menu.show_all()
        menu.attach_to_widget(self)
        menu.popup_at_pointer(event)

    def _edit(self, note: Item) -> None:
        text = self._ask("Edit note", note.title)
        if text:
            self.store.notes = [replace(n, title=text) if n.id == note.id else n for n in self.store.notes]
            self._changed()

    def _unplace(self, item: Item) -> None:
        self.store.places.pop(item.id, None)
        self._changed()

    def _link_note(self, note: Item) -> None:
        text = self._ask("Link note (paste https:// address, empty removes it)", note.url)
        if text and not text.startswith("https://"):
            self._message("The link must start with https://", Gtk.MessageType.WARNING)
            return
        if text or note.url:
            self.store.notes = [replace(n, url=text) if n.id == note.id else n for n in self.store.notes]
            self._changed()

    def _set_priority(self, item: Item, level: str) -> None:
        if level == "normal":
            self.store.overrides.pop(item.id, None)
        else:
            self.store.overrides[item.id] = level
        self._changed()

    # ---- re-ranking with a model --------------------------------------------------------------------
    def rerank(self) -> None:
        """Asks the model for suggestions in the background, then shows them for you to approve."""
        from . import ai, rerank
        if getattr(self, "reranking", False):
            return
        self.reranking = True
        moment = now()
        scored = ranked(self.store.visible(moment), moment, self.store.overrides, self.store.places)
        self.problem = "Re-ranking with the local model…"
        self._show_status()

        def work():
            result: object
            try:
                result = rerank.suggest(scored, moment)
            except ai.Unavailable as error:
                result = error
            GLib.idle_add(self._reranked, result)
        threading.Thread(target=work, daemon=True).start()

    def _reranked(self, result) -> bool:
        self.reranking = False
        self.problem = ""
        self._show_status()
        if isinstance(result, Exception):
            self._message(f"Re-ranking didn't work: {result}", Gtk.MessageType.WARNING)
            return False
        suggestions, _by, note = result
        if not suggestions:
            self._message(f"{note} Nothing needs to move.")
            return False
        self._approve_ranking(suggestions, note)
        return False

    def _approve_ranking(self, suggestions, note: str) -> None:
        dialog = Gtk.Dialog(title="Re-rank", transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.set_default_size(460, 420)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply ticked", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.add(Gtk.Label(label=note + " Untick any you disagree with.", wrap=True, max_width_chars=56,
                           xalign=0, margin=10))
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        ticks = []
        for suggestion in suggestions:
            tick = Gtk.CheckButton(active=True)
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
            label.set_text(f"{suggestion.item.title[:80]}\n{suggestion.current} → {suggestion.section}"
                           + (f": {suggestion.reason}" if suggestion.reason else ""))
            tick.add(label)
            box.pack_start(tick, False, False, 0)
            ticks.append((tick, suggestion))
        scroll.add(box)
        area.pack_start(scroll, True, True, 0)
        dialog.show_all()
        response = dialog.run()
        chosen = [s for tick, s in ticks if tick.get_active()]
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not chosen:
            return
        from . import rerank
        rerank.apply(self.store.overrides, chosen, now())      # one change for undo, however many move
        self._changed()
        self.problem = f"Moved {len(chosen)} item{'s' if len(chosen) != 1 else ''}. ↶ undoes it."
        self._show_status()

    def _cycle(self, item: Item) -> None:
        current = self.store.overrides.get(item.id, "normal")
        self._set_priority(item, {"normal": "high", "high": "low", "medium": "low", "low": "normal"}[current])

    def _clear_done_steps(self, item: Item) -> None:
        self.store.clear_done_steps(item.id)
        self._changed()

    def _finish(self, item: Item) -> None:
        self.store.finish(item, now())
        if self.on_finish:
            self.on_finish()
        # Defer the rebuild: this runs inside a signal from a widget render() would destroy.
        GLib.idle_add(self._changed)

    def _key(self, _widget, event) -> bool:
        self.idle_seconds = 0
        if event.keyval == Gdk.KEY_Escape:
            if self.search.get_visible():
                self._close_search()       # first Esc closes the search, the next one the list
            else:
                self.hide_list()
            return True
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        key = Gdk.keyval_to_lower(event.keyval)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if key == Gdk.KEY_z and not shift:
            self._undo()
            return True
        if (key == Gdk.KEY_z and shift) or key == Gdk.KEY_y:
            self._redo()
            return True
        if key == Gdk.KEY_f:
            self._open_search()
            return True
        return False

    def _open_search(self) -> None:
        if self.search.get_visible() and self.search.has_focus():
            self._close_search()
            return
        self.search.show()
        self.search.grab_focus()
        self.idle_seconds = 0

    def _close_search(self) -> None:
        self.search.set_text("")
        self.search.hide()
        self.render()

    def _flash_undo(self) -> None:
        """The ↶ button is always there; it is greyed out when there is nothing to undo."""
        self.undo.set_sensitive(True)

    def _hide_undo(self) -> bool:
        self.undo.set_sensitive(False)
        return False

    def _step_history(self, source: list[str], target: list[str], word: str) -> None:
        if not source:
            self.problem = f"Nothing to {word.lower()}"
            self._show_status()
            GLib.timeout_add_seconds(3, self._clear_hint, self.problem)
            return
        target.append(self.store.snapshot())
        previous, self.current = self.current, source.pop()
        self.store.restore(self.current)
        self._write_back(previous, self.current)
        self.store.save()
        self.render()
        if source is self.history and not self.history:
            self._hide_undo()

    def _write_back(self, before: str, after: str) -> None:
        """Mirror ticks of Claude follow-ups into the notes file, so they're closed there too."""
        old, new = ticked_claude(json.loads(before)), ticked_claude(json.loads(after))
        for item_id, done in [(i, True) for i in new - old] + [(i, False) for i in old - new]:
            try:
                if not claude_notes.set_ticked(item_id, done):
                    self.problem = "That item changed in Open Items.md; ticked here only"
                    self._show_status()
            except OSError as error:
                self.problem = f"Could not update Open Items.md: {error}"
                self._show_status()

    def _clear_hint(self, hint: str) -> bool:
        if self.problem == hint:
            self.problem = ""
            self._show_status()
        return False

    def _undo(self, *_args) -> None:
        self._step_history(self.history, self.redo_stack, "Undo")

    def _redo(self, *_args) -> None:
        self._step_history(self.redo_stack, self.history, "Redo")
        if self.history:
            self._flash_undo()

    def _add(self, entry: Gtk.Entry) -> None:
        if self.store.add_note(entry.get_text(), now()):
            entry.set_text("")
            self._changed()

    def _changed(self) -> bool:
        """Every change by hand ends here, so this is where undo history is recorded."""
        snapshot = self.store.snapshot()
        if snapshot != self.current:
            self._write_back(self.current, snapshot)
            self.history = (self.history + [self.current])[-100:]
            self.redo_stack.clear()
            self.current = snapshot
            self._flash_undo()
        self.store.save()
        self.render()
        return False

    def _show_status(self) -> None:
        context = self.status.get_style_context()
        if self.problem:
            context.add_class("problem")
            self.status.set_text(self.problem)
            self.status.set_tooltip_text(self.problem)
            return
        context.remove_class("problem")
        self.status.set_tooltip_text(None)
        if self.syncing:
            self.status.set_text("Syncing…")
        elif self.last_sync:
            self.status.set_text(f"Synced {self.last_sync:%H:%M}")
        else:
            self.status.set_text("")

    def _tick(self) -> bool:
        self.render()       # items change bucket as time passes, even without a sync
        return True

    def _check_tick(self) -> bool:
        """Every minute: ask each source whether anything is new, and fetch only those."""
        if not self.syncing:
            self.syncing = True
            threading.Thread(target=self._check_worker, daemon=True).start()
        return True

    def _check_worker(self) -> None:
        groups = self.changes.due()
        if not groups:
            GLib.idle_add(self._nothing_new)
            return
        moment = now()
        items, errors = sources.sync_groups(moment, groups, calendar=self.store.synced.get("calendar"))
        GLib.idle_add(self._sync_done, items, errors, moment, groups == set(sources.GROUPS), False)

    def _nothing_new(self) -> bool:
        self.syncing = False
        self.last_sync = now()
        self._show_status()
        self._run_pending()
        return False

    def sync(self, manual: bool = True) -> None:
        """Fetches everything now. A sync you start by hand also runs the Claude notes digest."""
        if self.syncing:
            self.pending = self.pending or ("manual" if manual else "auto")   # run it right after
            return
        self.syncing = True
        self._show_status()
        threading.Thread(target=self._sync_worker, args=(manual,), daemon=True).start()

    def _run_pending(self) -> None:
        pending, self.pending = getattr(self, "pending", None), None
        if pending:
            self.sync(manual=pending == "manual")

    def _sync_worker(self, manual: bool) -> None:
        moment = now()
        items, errors = sources.sync_all(moment)
        GLib.idle_add(self._sync_done, items, errors, moment, True, manual)

    def _digest_tick(self) -> bool:
        self.digest()
        return True

    def digest(self) -> None:
        """Claude turns long jotted notes (like Keep event notes) into concrete steps, in the background."""
        if getattr(self, "digesting", False):
            return
        self.digesting = True
        items = self.store.visible(now())
        threading.Thread(target=lambda: GLib.idle_add(self._digest_done, digest.run(items, self.store.steps)),
                         daemon=True).start()

    def _digest_done(self, result: dict) -> bool:
        self.digesting = False
        if result.get("steps"):
            for item_id, steps in result["steps"].items():
                self.store.steps.setdefault(item_id, []).extend(steps)
            self.store.save()
            self.current = self.store.snapshot()     # suggestions are not an undoable change of yours
            self.render()
        if result.get("error"):
            self.problem = f"Notes digest: {result['error']}"
            self._show_status()
        return False

    def _sync_done(self, items: dict, errors: dict, moment: datetime, full: bool = True,
                   manual: bool = False) -> bool:
        self.syncing = False
        if full:
            self.changes.full_done()
        if manual:
            self.digest()
        GLib.idle_add(lambda: self._run_pending() and False)
        self.store.synced.update(items)     # failed sources keep their last good items
        self.store.prune(moment)
        self.store.save()
        self.current = self.store.snapshot()    # tidying after a sync is not an undoable change
        if items:
            self.last_sync = moment
        reasons = set(errors.values())
        if len(reasons) == 1 and not items:
            self.problem = reasons.pop()
        else:
            self.problem = "; ".join(f"{SOURCE_LABELS[k]}: {v}" for k, v in errors.items())
        self.render()
        return False


    # ---- accounts ------------------------------------------------------------------------------
    def _accounts(self) -> None:
        dialog = Gtk.Dialog(title="Accounts", transient_for=self, modal=False)
        dialog.set_keep_above(True)
        dialog.add_buttons("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, _r: d.destroy())
        dialog.connect("destroy", lambda *_: setattr(self, "accounts_grid", None))
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=12)
        dialog.get_content_area().add(grid)
        self.accounts_grid = grid
        self._fill_accounts()
        dialog.show_all()

    def _fill_accounts(self) -> None:
        grid = getattr(self, "accounts_grid", None)
        if grid is None:         # Accounts window is closed
            return
        for child in grid.get_children():
            grid.remove(child)
        state = sources.status()
        rows = [
            ("google", "Calendar, Tasks, Gmail", self._google_action(state["google"])),
            ("linkedin", "Messages, invitations and jobs from LinkedIn's emails. LinkedIn has no inbox API.", None),
            ("keep", "Official Keep API: works for Google Workspace accounts whose admin allows it, "
                     "not personal Gmail. Shows notes with dates, to-dos, urgent words or recent edits.",
             self._keep_action(state["keep"])),
            ("luma", "Events you host, registered for or are waitlisted for. Updated every hour.",
             self._luma_action(state["luma"])),
            ("signal", "Linked like Signal Desktop. Unread messages, until you read or reply.",
             self._signal_action(state["signal"])),
            ("anthropic", "Optional. Lets Claude write drafts, break notes into steps and help the focus check, "
                          "instead of the model on this computer. Talking always stays here.",
             self._claude_action(state["anthropic"])),
        ]
        for line, (key, about, action) in enumerate(rows):
            name = Gtk.Label(xalign=0)
            name.set_markup(f"<b>{SOURCE_LABELS[key]}</b>")
            status = Gtk.Label(label=state[key], xalign=0)
            status.get_style_context().add_class("nudgy-meta")
            info = Gtk.Label(label=about, xalign=0, wrap=True, max_width_chars=40)
            info.get_style_context().add_class("nudgy-meta")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for widget in (name, status, info):
                box.pack_start(widget, False, False, 0)
            grid.attach(box, 0, line, 1, 1)
            if action:
                label, tip, handler = action
                button = Gtk.Button(label=label, valign=Gtk.Align.CENTER, tooltip_text=tip)
                button.set_sensitive(handler is not None)
                if handler:
                    button.connect("clicked", lambda *_a, h=handler: h())
                grid.attach(button, 1, line, 1, 1)
        grid.show_all()

    def _google_action(self, state: str):
        if state == "Signed in":
            return "Sign out", "Forget the Google sign-in on this laptop", self._google_sign_out
        if state == "Signed out":
            return "Sign in", "Opens Google in your browser", self._google_sign_in
        return "Set up…", "One-time Google Cloud setup", self._google_setup

    def _keep_action(self, state: str):
        if state.startswith("Connected"):
            return "Disconnect", "Forget the Keep sign-in on this laptop", self._keep_disconnect
        if not google.client_path().exists():
            return "Connect…", "Set up Google first", None
        return "Connect…", "Sign in to Google with Keep access (read-only)", self._keep_connect

    def _keep_connect(self) -> None:
        text = ("Sign in with the account that holds your Keep notes and allow \"View all your Google Keep "
                "data\".\n\nGoogle only allows this for Google Workspace accounts whose admin permits the "
                "Keep API. If Google refuses, Nudgy shows Google's reason; your other Google sources are "
                "not affected.")
        if self._message(text, buttons=Gtk.ButtonsType.OK_CANCEL) != Gtk.ResponseType.OK:
            return
        self.problem = "Finish the Keep sign-in in your browser…"
        self._show_status()
        result: dict = {}

        def work():
            result["count"] = keep.connect()

        def done(error):
            self.problem = ""
            if error is None:
                self._message(f"Google Keep connected: {result['count']} notes readable.")
            else:
                keep.disconnect()       # a token that can't read Keep is useless; don't keep it
                self._message(f"Google refused Keep access:\n\n{error}", Gtk.MessageType.WARNING)
            self._fill_accounts()
            self.sync()
        self._background(work, done)

    def _keep_disconnect(self) -> None:
        keep.disconnect()
        self.store.synced.pop("keep", None)
        self._changed()
        self._fill_accounts()

    def _luma_action(self, state: str):
        if state == "Connected":
            return "Disconnect", "Forget the Luma calendar link on this laptop", self._luma_forget
        return "Connect…", "Paste your personal Luma calendar link", self._luma_connect

    def _luma_connect(self) -> None:
        dialog = Gtk.Dialog(title="Connect Luma", transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Connect", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.add(Gtk.Label(label=LUMA_STEPS, wrap=True, max_width_chars=48, xalign=0, margin=10))
        open_settings = Gtk.LinkButton(uri="https://luma.com/settings", label="Open Luma settings")
        area.add(open_settings)
        entry = Gtk.Entry(placeholder_text="https://api.lu.ma/ics/… or webcal://…", activates_default=True,
                          width_chars=48, margin=10, visibility=True)
        area.add(entry)
        dialog.show_all()
        response, text = dialog.run(), entry.get_text()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            luma.save_feed(text)
        except ValueError as error:
            self._message(str(error), Gtk.MessageType.WARNING)
            return
        self._fill_accounts()
        self.sync()

    def _luma_forget(self) -> None:
        luma.forget_feed()
        self.store.synced.pop("luma", None)
        self._changed()
        self._fill_accounts()

    def _claude_action(self, state: str):
        if state.startswith("Connected"):
            return "Disconnect", "Stop using Claude; everything runs on this computer again", self._claude_disconnect
        return "Connect…", "Use your Anthropic API key or Claude Code login", self._claude_connect

    def _claude_connect(self) -> None:
        from . import ai, settings
        dialog = Gtk.Dialog(title="Connect Claude (optional)", transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Connect", Gtk.ResponseType.OK)
        if ai.claude_code():
            dialog.add_button("Use my Claude Code login", Gtk.ResponseType.APPLY)
        dialog.set_default_response(Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.add(Gtk.Label(label=CLAUDE_STEPS, wrap=True, max_width_chars=48, xalign=0, margin=10))
        area.add(Gtk.LinkButton(uri="https://console.anthropic.com/settings/keys", label="Get an API key"))
        entry = Gtk.Entry(placeholder_text="sk-ant-…", activates_default=True, width_chars=48, margin=10,
                          visibility=False)
        area.add(entry)
        dialog.show_all()
        response, key = dialog.run(), entry.get_text()
        dialog.destroy()
        if response == Gtk.ResponseType.APPLY:
            settings.update("ai", {"claude": "claude-code"})
            self._fill_accounts()
            return
        if response != Gtk.ResponseType.OK:
            return
        try:
            ai.save_key(key)
        except ValueError as error:
            self._message(str(error), Gtk.MessageType.WARNING)
            return

        def connect() -> None:
            try:
                import anthropic  # noqa: F401
            except ImportError:
                ai.install_library()          # Anthropic's library, only needed for API keys
            settings.update("ai", {"claude": "api"})
        self._background(connect, lambda error: self._after_account_change(error, "Connecting Claude"))

    def _claude_disconnect(self) -> None:
        from . import ai, settings
        ai.forget_key()
        settings.update("ai", {"claude": ""})
        self._fill_accounts()

    def _signal_action(self, state: str):
        if state.startswith("Linked"):
            return "Linked", "To unlink, remove 'Nudgy' in Signal > Settings > Linked devices on your phone", None
        if not chats.signal_cli():
            return "Link…", "Install signal-cli first: see the README", None
        return "Link…", "Show a QR code to scan with your phone", self._signal_link

    def _message(self, text: str, kind=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK) -> int:
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind, buttons=buttons)
        dialog.set_keep_above(True)
        dialog.set_property("text", text)
        response = dialog.run()
        dialog.destroy()
        return response

    def _background(self, work, done) -> None:
        """Runs work() off the GTK thread, then done(error or None) on it."""
        def worker():
            try:
                work()
                error = None
            except Exception as problem:     # shown to the user, never crashes the window
                log.warning("background task failed: %s: %s", type(problem).__name__, problem)
                error = problem
            GLib.idle_add(lambda: (done(error), False)[1])
        threading.Thread(target=worker, daemon=True).start()

    def _after_account_change(self, error, what: str) -> None:
        if error:
            self._message(f"{what} did not work: {error}", Gtk.MessageType.WARNING)
        self._fill_accounts()
        self.sync()

    def _google_setup(self) -> None:
        if self._message(GOOGLE_STEPS, buttons=Gtk.ButtonsType.OK_CANCEL) != Gtk.ResponseType.OK:
            return
        chooser = Gtk.FileChooserNative(title="Choose the Google client JSON", transient_for=self,
                                        action=Gtk.FileChooserAction.OPEN)
        only_json = Gtk.FileFilter()
        only_json.add_pattern("*.json")
        chooser.add_filter(only_json)
        if chooser.run() != Gtk.ResponseType.ACCEPT:
            return
        try:
            google.import_client(Path(chooser.get_filename()))
        except (OSError, ValueError) as error:
            self._message(f"Could not use that file: {error}", Gtk.MessageType.WARNING)
            return
        self._google_sign_in()

    def _google_sign_in(self) -> None:
        self.problem = "Finish signing in in your browser…"
        self._show_status()
        self._background(google.authorize, lambda e: (setattr(self, "problem", ""),
                                                      self._after_account_change(e, "Google sign-in")))

    def _google_sign_out(self) -> None:
        google.sign_out()
        self.store.synced = {k: v for k, v in self.store.synced.items()
                             if k not in ("calendar", "task", "gmail", "linkedin")}
        self._changed()
        self._fill_accounts()

    def _qr_window(self, title: str, steps: str):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_keep_above(True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL)
        area = dialog.get_content_area()
        area.add(Gtk.Label(label=steps, wrap=True, max_width_chars=40, margin=10))
        image = Gtk.Image(margin=10)
        image.set_from_icon_name("content-loading-symbolic", Gtk.IconSize.DIALOG)
        area.add(image)
        area_note = Gtk.Label(wrap=True, max_width_chars=40, margin=10, selectable=True)
        area.add(area_note)
        dialog.show_all()
        area_note.hide()
        folder = Path(tempfile.mkdtemp(prefix="nudgy-qr-"))

        def show(text: str) -> None:     # called from the linking thread
            png = folder / "qr.png"
            try:
                chats.make_qr(text, png)
            except (OSError, subprocess.SubprocessError) as error:
                log.warning("could not draw the linking QR code: %s", error)
                note = ("To see the code here, install qrencode (sudo apt install qrencode), or run "
                        "'nudgy link signal' in a terminal, which shows it there.")
                GLib.idle_add(lambda: (area_note.set_text(note), area_note.show(), False)[2])
                return
            GLib.idle_add(lambda: (image.set_from_file(str(png)), False)[1])
        return dialog, show, folder

    def _link(self, title: str, steps: str, link, what: str) -> None:
        dialog, show, folder = self._qr_window(title, steps)
        running: dict = {}

        def finished(error) -> None:
            for leftover in folder.glob("*"):
                leftover.unlink()
            folder.rmdir()
            cancelled = running.get("cancelled")
            dialog.destroy()
            self._after_account_change(None if cancelled else error, what)

        def cancel(*_args) -> None:
            running["cancelled"] = True
            if "process" in running:
                running["process"].terminate()
        dialog.connect("response", cancel)
        self._background(lambda: link(show, lambda p: running.update(process=p)), finished)

    def _signal_link(self) -> None:
        self._link("Link Signal", "On your phone open Signal > Settings > Linked devices > Link new device, "
                   "and scan this code.", chats.link_signal, "Linking Signal")



class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.nudgy.Nudgy")
        self.window = None
        self.pet = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        for name, handler in (("toggle", lambda *_: self.pet and self.pet.toggle()),
                              ("talk", lambda *_: self.pet and self.pet.talk()),
                              ("open-list", lambda *_: self.pet and self.pet.open_list())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def do_activate(self):
        if self.window is None:
            from .pet import Pet, Tray
            self.window = Window(self, Store())
            self.pet = Pet(self.window)
            self.add_window(self.pet)
            Tray(self.pet)
            from .watch import Watcher
            self.pet.watcher = Watcher(self.pet)
            self.window.render()                  # gives the pet its first mood
            if not self.window.store.window.get("pet_hidden"):
                self.pet.show_all()
                if self.window.store.window.get("list_open"):
                    self.pet.open_list()
        elif self.pet is not None and not self.pet.get_visible():
            self.pet.show_pet()


def run() -> int:
    return App().run([])


def remote(action: str) -> int:
    """Sends an action to the running Nudgy (used by the keyboard shortcut)."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        bus.call_sync("dev.nudgy.Nudgy", "/dev/nudgy/Nudgy", "org.gtk.Actions", "Activate",
                      GLib.Variant("(sava{sv})", (action, [], {})), None, Gio.DBusCallFlags.NO_AUTO_START,
                      5000, None)
    except GLib.Error:
        print("Nudgy is not running. Start it with: systemctl --user start nudgy")
        return 1
    return 0
