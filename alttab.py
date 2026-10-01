#!/usr/bin/python3
"""Windows-style desktop behaviour for sway.

Alt+Tab: sway binds Alt+Tab / Alt+Shift+Tab to `nop alttab next|prev`. This
daemon watches for those binding events over sway IPC, shows an overlay of
windows in most-recently-used order, and focuses the highlighted one when Alt
is released. The overlay grabs the keyboard while open (so it sees the Alt
release); Tab presses are still consumed by sway's bindings and arrive as
binding events.

Win+arrows: `nop wm up|down|left|right` maximize, snap, restore and minimize.

Title bar buttons: sway can't put buttons in its title bars, so each floating
window gets a layer-shell surface over the right end of its title bar with
move-to-other-desktop, fullscreen, minimize, maximize/restore and close buttons.

Renaming: `nop wm rename` (right-click a title bar, or Win+F2) opens an editable box over the
title bar. The new name is shown with sway's title_format, so the app can't overwrite it; an
empty name goes back to the app's own title.

Win+Tab: `nop desktops next|prev` opens the same switcher over desktops (workspaces) instead of
windows, in most-recently-used order; releasing Win switches to the highlighted one.

Desktops: workspaces 1 and 2 always exist; Win+3-9 open more, and Win+0 / Win+Shift+0 go to /
send a window to desktop 10, "Junk". Desktops are known by number: a named one is called
"N: name" in sway (and so in the bar), which `workspace number N` still matches. F2 or a right-
click in the Win+Tab switcher renames one; Win+Shift+F2 (`nop desktops rename`) renames the
current desktop.

Live wallpapers: a window from one of WALLPAPER_APPS that opens as big as the screen is that
app's wallpaper (its other windows, like settings, are left alone). It's tiled, so every
(floating) window stays above it, and marked _wallpaper. sway puts a window on one desktop
only, so this moves the same window to whichever desktop is shown; it's left out of Alt+Tab.

Bar buttons: swaybar's workspace buttons can't be right-clicked, so while this runs they're
turned off and drawn here instead, in the same place and colours: left-click switches desktop,
scrolling steps through them and right-click renames one in place.
"""
import json
import os
import re
import socket
import struct
import subprocess
import sys
import traceback

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

MAGIC = b"i3-ipc"
HEADER = struct.Struct("=6sII")
RUN_COMMAND, GET_WORKSPACES, SUBSCRIBE, GET_OUTPUTS, GET_TREE, GET_BAR_CONFIG = 0, 1, 2, 3, 4, 6
EVENT_WORKSPACE = 0x80000000
EVENT_WINDOW = 0x80000003
EVENT_BINDING = 0x80000005

SHOW_DELAY_MS = 120  # a quick Alt+Tab tap switches without flashing the overlay
FOCUS_TIMEOUT_MS = 600  # give up if the overlay never receives keyboard focus
THUMB_W, THUMB_H = 280, 175
ICON_SIZE = 20  # app icon beside the title above each thumbnail
PLACEHOLDER_ICON_SIZE = 64  # shown until the window's thumbnail has been captured
# Per switcher mode: the modifier held while tapping Tab, whose release commits the choice.
HOLD_KEYS = {
    "windows": ({Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R},
                Gdk.ModifierType.MOD1_MASK),
    "desktops": ({Gdk.KEY_Super_L, Gdk.KEY_Super_R},
                 Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.SUPER_MASK),
}
DESKTOP_CAPTURE_DELAY_MS = 500  # let a newly shown desktop finish drawing before its snapshot
BUTTON_POLL_MS = 66  # sway has no IPC event for window moves/resizes, so poll geometry
DESKTOPS = ("1", "2")  # desktops are keyed by their number, as a string
JUNK_DESKTOP = "10"
WALLPAPER_MARK = "_wallpaper"
WALLPAPER_APPS = {"salish-pod"}  # app_ids of apps that draw a live desktop background
RENAME_MARK = "_wm_rename"  # the rename binding marks its window; underscore marks aren't drawn
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "sway-alttab.pid")
# sway's exec sends stderr to /dev/null or its console, so tracebacks go here instead.
LOGFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "sway-alttab.log")

CSS = b"""
window { background-color: transparent; }
.panel {
    background-color: rgba(28, 28, 30, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 12px;
    padding: 16px;
}
.panel.hidden { opacity: 0; }
.heading { color: #ffffff; font-size: 12pt; font-weight: bold; margin: 0 4px 12px 4px; }
.item { padding: 10px; border-radius: 8px; border: 2px solid transparent; }
.item.selected { background-color: rgba(255, 255, 255, 0.12); border-color: rgba(255, 255, 255, 0.85); }
.item label { color: #e6e6e6; font-size: 10pt; }
.item .minimized { color: #9a9a9a; font-style: italic; }
.thumb { background-color: rgba(0, 0, 0, 0.35); border-radius: 4px; }
.title-entry {
    background-color: #1c1c1e; color: #ffffff; caret-color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.85); border-radius: 0;
    padding: 0 4px; min-height: 0; box-shadow: none;
}
.heading-entry { font-size: 12pt; font-weight: bold; margin: 0 4px 12px 4px; padding: 2px 4px; }
.title-entry selection { background-color: #3d6fb4; color: #ffffff; }
"""


class SwayIPC:
    def __init__(self):
        path = os.environ.get("SWAYSOCK") or subprocess.check_output(
            ["sway", "--get-socketpath"], text=True).strip()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""

    def send(self, msg_type, payload=""):
        data = payload.encode()
        self.sock.sendall(HEADER.pack(MAGIC, len(data), msg_type) + data)

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("sway IPC socket closed")
            self.buf += chunk

    def recv(self):
        self._fill(HEADER.size)
        _, length, msg_type = HEADER.unpack(self.buf[:HEADER.size])
        self._fill(HEADER.size + length)
        body = self.buf[HEADER.size:HEADER.size + length]
        self.buf = self.buf[HEADER.size + length:]
        return msg_type, json.loads(body)

    def has_message(self):
        if len(self.buf) < HEADER.size:
            return False
        _, length, _ = HEADER.unpack(self.buf[:HEADER.size])
        return len(self.buf) >= HEADER.size + length

    def request(self, msg_type, payload=""):
        self.send(msg_type, payload)
        return self.recv()[1]


def windows_in_focus_order(tree):
    """Leaf windows, ordered by sway's per-container focus stacks."""
    out = []

    def walk(node, workspace):
        if node["type"] == "workspace":
            workspace = node["name"]
        children = node.get("nodes", []) + node.get("floating_nodes", [])
        if not children:
            if "pid" in node:
                out.append(window_info(node, workspace))
            return
        rank = {cid: i for i, cid in enumerate(node.get("focus", []))}
        for child in sorted(children, key=lambda c: rank.get(c["id"], len(rank))):
            walk(child, workspace)

    walk(tree, None)
    return out


def window_info(node, workspace):
    app = node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
    return {
        "id": node["id"],
        "title": node.get("name") or app or "Untitled",
        "app": app,
        "workspace": workspace,
        "minimized": workspace == "__i3_scratch",
        "capture_id": node.get("foreign_toplevel_identifier"),
        "focused": node.get("focused", False),
        "wallpaper": WALLPAPER_MARK in node.get("marks", []),
    }


def desktop_app_info(desktop_id):
    # PyGObject raises, rather than returning None, when there's no such .desktop file.
    try:
        return Gio.DesktopAppInfo.new(desktop_id)
    except TypeError:
        return None


class IconLookup:
    def __init__(self):
        self.cache = {}
        self.by_wm_class = {}
        for info in Gio.AppInfo.get_all():
            wm_class = info.get_startup_wm_class() if isinstance(info, Gio.DesktopAppInfo) else None
            if wm_class and info.get_icon():
                self.by_wm_class.setdefault(wm_class.lower(), info.get_icon())

    def get(self, app):
        if app not in self.cache:
            self.cache[app] = self._find(app)
        return self.cache[app]

    def _find(self, app):
        if app:
            names = [app, app.lower(), app.split(".")[-1].lower()]
            for name in names:
                info = desktop_app_info(name + ".desktop")
                if info and info.get_icon():
                    return info.get_icon()
            if app.lower() in self.by_wm_class:
                return self.by_wm_class[app.lower()]
            for group in Gio.DesktopAppInfo.search(app):
                for desktop_id in group:
                    info = desktop_app_info(desktop_id)
                    if info and info.get_icon():
                        return info.get_icon()
            theme = Gtk.IconTheme.get_default()
            for name in names:
                if theme.has_icon(name):
                    return Gio.ThemedIcon.new(name)
        return Gio.ThemedIcon.new_from_names(
            ["application-x-executable", "application-x-executable-symbolic", "image-missing"])


def find_window(node, match, workspace=None):
    """First window matching `match(node)`, with the name of its workspace."""
    if node["type"] == "workspace":
        workspace = node["name"]
    if "pid" in node and match(node):
        return node, workspace
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find_window(child, match, workspace)
        if found:
            return found
    return None


def outer_geometry(con):
    """Absolute frame of a floating window including its title bar, as `move absolute position`
    and `resize set` use. IPC `rect` excludes the title bar, which sits directly above it."""
    rect, title_height = con["rect"], con["deco_rect"]["height"]
    return rect["x"], rect["y"] - title_height, rect["width"], rect["height"] + title_height


class Snapper:
    """Windows-style maximize / half-screen snap / restore / minimize for floating windows."""

    def __init__(self, ipc):
        self.ipc = ipc
        self.states = {}  # con_id -> {"mode", "saved", "rect"}

    def current_state(self, con):
        state = self.states.get(con["id"])
        if state and state["rect"] is not None and state["rect"] != con["rect"]:
            # Moved or resized by hand since we snapped it: it's a normal window again.
            del self.states[con["id"]]
            state = None
        return state

    def is_maximized(self, con):
        state = self.current_state(con)
        return bool(state and state["mode"] == "max")

    def run(self, action, con_id=None):
        """Apply a Win+arrow action to `con_id`, or to the focused window."""
        match = (lambda n: n["focused"]) if con_id is None else (lambda n: n["id"] == con_id)
        found = find_window(self.ipc.request(GET_TREE), match)
        if not found or found[1] == "__i3_scratch":
            return
        con, ws_name = found
        con_id = con["id"]
        state = self.current_state(con)
        mode = state["mode"] if state else None

        if action == "down":
            if mode:
                self.restore(con_id, state)
            else:
                self.cmd(con_id, "move scratchpad")
            return

        target = {"up": "max", "left": "left", "right": "right"}.get(action)
        if action == "toggle-max":
            target = None if mode == "max" else "max"
            if not target:
                self.restore(con_id, state)
                return
        if not target or target == mode:
            return
        if {mode, target} == {"left", "right"}:
            self.restore(con_id, state)
            return

        if state:
            saved = state["saved"]
        elif con["type"] == "floating_con":
            saved = outer_geometry(con)
        else:
            saved = None  # was tiled; restoring puts it back in the tiling layout
            self.cmd(con_id, "floating enable")

        ws = next(w["rect"] for w in self.ipc.request(GET_WORKSPACES) if w["name"] == ws_name)
        x, y, w, h = ws["x"], ws["y"], ws["width"], ws["height"]
        if target == "left":
            w //= 2
        elif target == "right":
            x, w = x + w // 2, w - w // 2
        self.cmd(con_id, f"resize set {w} px {h} px, move absolute position {x} px {y} px")
        self.states[con_id] = {"mode": target, "saved": saved, "rect": None}
        # Record where the window actually landed once the client has committed its new size.
        GLib.timeout_add(200, self.record_rect, con_id)

    def restore(self, con_id, state):
        del self.states[con_id]
        if state["saved"] is None:
            self.cmd(con_id, "floating disable")
        else:
            x, y, w, h = state["saved"]
            self.cmd(con_id, f"resize set {w} px {h} px, move absolute position {x} px {y} px")

    def record_rect(self, con_id):
        state = self.states.get(con_id)
        if state:
            found = find_window(self.ipc.request(GET_TREE), lambda n: n["id"] == con_id)
            if found:
                state["rect"] = found[0]["rect"]
            else:
                del self.states[con_id]
        return False

    def cmd(self, con_id, command):
        self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] {command}")


def overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def desktop_key(workspace):
    """The desktop a workspace name belongs to: its leading number, as `workspace number` reads it."""
    if workspace == "junk":
        return JUNK_DESKTOP  # the junk desktop's name before desktops were numbered
    match = re.match(r"\d+", workspace)
    return str(int(match.group())) if match else workspace


def other_desktop(workspace):
    return DESKTOPS[1] if desktop_key(workspace) == DESKTOPS[0] else DESKTOPS[0]


def sway_quote(text):
    """Quote an argument for a sway command. sway keeps backslashes literally, so a string can't
    contain both quote characters: double quotes inside become typographic ones."""
    return f"'{text}'" if "'" not in text else '"' + text.replace('"', "\u201d") + '"'


class TitleBarButtons:
    """Other-desktop / fullscreen / minimize / maximize / close buttons: one layer-shell surface
    over a title bar."""

    NAMES = ("desktop", "fullscreen", "minimize", "maximize", "close")

    @classmethod
    def button_width(cls, title_height):
        return round(title_height * 1.6)

    def __init__(self, con_id, on_click):
        self.con_id = con_id
        self.placement = None
        self.look = None  # (focused, maximized, target desktop)
        self.hover = None  # index of the button under the pointer
        self.pressed = None  # index of the button the left mouse button went down on
        self.win = Gtk.Window()
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)
        self.win.set_app_paintable(True)
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "titlebuttons")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_exclusive_zone(self.win, -1)  # position relative to the whole output
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.LEFT, True)
        self.area = Gtk.DrawingArea()
        # The press mask is required too: GTK gives the implicit pointer grab (and so the
        # release) to the nearest window that selects presses, which would otherwise be the
        # toplevel rather than this area.
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.area.connect("draw", self.draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release, on_click)
        self.area.connect("motion-notify-event", lambda _w, e: self.set_hover(self.button_at(e.x)))
        self.area.connect("leave-notify-event", lambda *_: self.set_hover(None))
        self.win.add(self.area)

    def button_at(self, x):
        width = self.area.get_allocated_width() / len(self.NAMES)
        return min(len(self.NAMES) - 1, max(0, int(x // width)))

    def on_press(self, _widget, event):
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.pressed = self.button_at(event.x)
        return True

    def on_release(self, _widget, event, on_click):
        pressed, self.pressed = self.pressed, None
        if event.button != 1 or pressed is None:
            return True
        width, height = self.area.get_allocated_width(), self.area.get_allocated_height()
        inside = 0 <= event.x < width and 0 <= event.y < height
        # Like Windows, only act if the pointer is released over the button it was pressed on.
        if inside and self.button_at(event.x) == pressed:
            on_click(self.con_id, self.NAMES[pressed])
        return True

    def place(self, placement, focused, maximized, target):
        """placement = (monitor, x, y, button_width, height) with x/y relative to the monitor."""
        if placement != self.placement:
            monitor, x, y, button_width, height = placement
            GtkLayerShell.set_monitor(self.win, monitor)
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.LEFT, x)
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP, y)
            self.area.set_size_request(button_width * len(self.NAMES), height)
            self.placement = placement
        if (focused, maximized, target) != self.look:
            self.look = (focused, maximized, target)
            self.area.queue_draw()
        if not self.win.get_visible():
            self.win.show_all()

    def hide(self):
        if self.win.get_visible():
            self.win.hide()
            self.hover = None

    def set_hover(self, index):
        if index != self.hover:
            self.hover = index
            self.area.queue_draw()

    def draw(self, widget, cr):
        focused, maximized, target = self.look or (False, False, DESKTOPS[0])
        h = widget.get_allocated_height()
        bw = widget.get_allocated_width() / len(self.NAMES)
        for i, name in enumerate(self.NAMES):
            x0 = i * bw
            hovered = i == self.hover
            if hovered:
                if name == "close":
                    cr.set_source_rgb(0.77, 0.17, 0.11)  # Windows' red close hover
                else:
                    cr.set_source_rgba(1, 1, 1, 0.18)
                cr.rectangle(x0, 0, bw, h)
                cr.fill()
            shade = 1.0 if focused or hovered else 0.6
            cr.set_source_rgba(shade, shade, shade, 1)
            cr.set_line_width(1.2)
            cx, cy, r = x0 + bw / 2, h / 2, h * 0.22  # glyph centre and half-size
            if name == "desktop":
                self.draw_desktop(cr, cx, cy, r, target)
            else:
                getattr(self, "draw_" + name)(cr, cx, cy, r, maximized)
            cr.stroke()

    @staticmethod
    def draw_desktop(cr, cx, cy, r, target):
        # "→ 2": an arrow followed by the number of the desktop the window will move to.
        cr.select_font_face("Sans", 0, 1)  # normal slant, bold
        cr.set_font_size(r * 2.4)
        extents = cr.text_extents(target)
        cr.move_to(cx + r * 0.5 - extents.x_bearing, cy - extents.y_bearing - extents.height / 2)
        cr.show_text(target)
        cr.new_path()
        tip = cx + r * 0.1
        cr.move_to(cx - r * 1.5, cy)
        cr.line_to(tip, cy)
        cr.move_to(tip - r * 0.6, cy - r * 0.6)
        cr.line_to(tip, cy)
        cr.line_to(tip - r * 0.6, cy + r * 0.6)

    @staticmethod
    def draw_fullscreen(cr, cx, cy, r, _maximized):
        arm = r * 0.6
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = cx + dx * r, cy + dy * r
            cr.move_to(x, y - dy * arm)
            cr.line_to(x, y)
            cr.line_to(x - dx * arm, y)

    @staticmethod
    def draw_minimize(cr, cx, cy, r, _maximized):
        cr.move_to(cx - r, cy + 0.5)
        cr.line_to(cx + r, cy + 0.5)

    @staticmethod
    def draw_maximize(cr, cx, cy, r, maximized):
        if maximized:
            # "Restore": two overlapping squares.
            o = r * 0.4
            cr.rectangle(cx - r, cy - r + o, 2 * r - o, 2 * r - o)
            cr.move_to(cx - r + o, cy - r + o)
            cr.line_to(cx - r + o, cy - r)
            cr.line_to(cx + r, cy - r)
            cr.line_to(cx + r, cy + r - o)
            cr.line_to(cx + r - o, cy + r - o)
        else:
            cr.rectangle(cx - r, cy - r, 2 * r, 2 * r)

    @staticmethod
    def draw_close(cr, cx, cy, r, _maximized):
        cr.move_to(cx - r, cy - r)
        cr.line_to(cx + r, cy + r)
        cr.move_to(cx + r, cy - r)
        cr.line_to(cx - r, cy + r)


class TitleButtons:
    """Keeps title bar buttons over the top-right of every visible, uncovered floating window."""

    def __init__(self, ipc, snapper):
        self.ipc = ipc
        self.snapper = snapper
        self.buttons = {}
        GLib.timeout_add(BUTTON_POLL_MS, self.refresh)

    def refresh(self):
        wanted = {}
        display = Gdk.Display.get_default()
        visible = {w["name"] for w in self.ipc.request(GET_WORKSPACES) if w["visible"]}
        for output in self.ipc.request(GET_TREE)["nodes"]:
            for ws in output["nodes"]:
                if ws["name"] in visible:
                    self.collect(ws, output["rect"], display, wanted)

        for con_id in list(self.buttons):
            if con_id not in wanted:
                self.buttons.pop(con_id).win.destroy()
        for con_id, (placement, focused, maximized, target) in wanted.items():
            if con_id not in self.buttons:
                self.buttons[con_id] = TitleBarButtons(con_id, self.clicked)
            if placement:
                self.buttons[con_id].place(placement, focused, maximized, target)
            else:
                self.buttons[con_id].hide()
        return True

    def collect(self, ws, out_rect, display, wanted):
        def has_fullscreen(node):
            children = node.get("nodes", []) + node.get("floating_nodes", [])
            return any(c.get("fullscreen_mode") or has_fullscreen(c) for c in children)

        # (Workspaces themselves always report fullscreen_mode 1, so only look inside.)
        if has_fullscreen(ws):
            return
        # floating_nodes is in stacking order: later windows are drawn on top.
        stack = ws["floating_nodes"]
        frames = [outer_geometry(c) for c in stack]
        for i, con in enumerate(stack):
            height = con["deco_rect"]["height"]
            if "pid" not in con or height == 0:
                continue  # containers of several windows, or apps drawing their own title bar
            button_width = TitleBarButtons.button_width(height)
            width = button_width * len(TitleBarButtons.NAMES)
            border = con.get("current_border_width", 0)
            rect = con["rect"]
            x, y = rect["x"] + rect["width"] - border - width, rect["y"] - height
            covered = any(overlaps((x, y, width, height), f) for f in frames[i + 1:])
            too_narrow = x < rect["x"] + border
            # A surface entirely off the output gets no frame callbacks, which freezes GTK's
            # frame clock so it never commits the margins that would bring it back. Unmap it
            # instead; showing it again maps a fresh surface.
            off_screen = not overlaps((x, y, width, height), (
                out_rect["x"], out_rect["y"], out_rect["width"], out_rect["height"]))
            monitor = display.get_monitor_at_point(x, y)
            placement = None if covered or too_narrow or off_screen else (
                monitor, x - out_rect["x"], y - out_rect["y"], button_width, height)
            wanted[con["id"]] = (placement, con["focused"], self.snapper.is_maximized(con),
                                 other_desktop(ws["name"]))

    def clicked(self, con_id, name):
        if name == "desktop":
            found = find_window(self.ipc.request(GET_TREE), lambda n: n["id"] == con_id)
            if found:
                # Stays on the current desktop, like Windows' "Move to" menu.
                target = other_desktop(found[1])
                self.ipc.request(RUN_COMMAND,
                                 f"[con_id={con_id}] move container to workspace number {target}")
        elif name == "fullscreen":
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] focus, fullscreen toggle")
        elif name == "minimize":
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] move scratchpad")
        elif name == "maximize":
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] focus")
            self.snapper.run("toggle-max", con_id)
        elif name == "close":
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] kill")
        self.refresh()


class TitleEditor:
    """An editable box over a window's title bar for giving the window its own name."""

    # Kept in the runtime dir so Alt+Tab still shows the names after this daemon restarts.
    STATE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "sway-window-names.json")

    def __init__(self, ipc):
        self.ipc = ipc
        try:
            with open(self.STATE_FILE) as f:
                self.names = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError):
            self.names = {}  # con_id -> name the user gave it
        self.con_id = None  # window being renamed
        self.original = None

        self.win = Gtk.Window()
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "titleedit")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_exclusive_zone(self.win, -1)
        # sway takes on-demand keyboard focus straight back, so this has to be exclusive. Clicking
        # another window still changes sway's focus, and the focus event finishes the edit.
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.LEFT, True)
        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("title-entry")
        self.entry.connect("activate", lambda *_: self.finish(True))
        self.win.connect("key-press-event", self.on_key_press)
        self.win.add(self.entry)

    def title(self, window):
        return self.names.get(window["id"], window["title"])

    def start(self):
        tree = self.ipc.request(GET_TREE)
        found = find_window(tree, lambda n: RENAME_MARK in n.get("marks", []))
        self.ipc.request(RUN_COMMAND, f"[con_mark={RENAME_MARK}] unmark {RENAME_MARK}")
        if not found or found[1] == "__i3_scratch":
            return
        con = found[0]
        height = con["deco_rect"]["height"]
        if height == 0:
            return  # no title bar to edit over
        self.finish(True)
        border = con.get("current_border_width", 0)
        rect = con["rect"]
        x, y = rect["x"] + border, rect["y"] - height
        width = rect["width"] - 2 * border
        if "pid" in con:
            width -= TitleBarButtons.button_width(height) * len(TitleBarButtons.NAMES)
        monitor = Gdk.Display.get_default().get_monitor_at_point(x, y)
        area = monitor.get_geometry()
        GtkLayerShell.set_monitor(self.win, monitor)
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.LEFT, x - area.x)
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP, y - area.y)
        self.entry.set_size_request(max(width, 120), height)

        self.con_id = con["id"]
        self.original = con.get("name") or ""
        self.entry.set_text(self.names.get(self.con_id, self.original))
        self.win.show_all()
        self.entry.grab_focus()  # selects all the text, ready to type over

    def on_key_press(self, _win, event):
        if event.keyval == Gdk.KEY_Escape:
            self.finish(False)
            return True
        return False

    def finish(self, keep):
        con_id, self.con_id = self.con_id, None
        if con_id is None:
            return
        self.win.hide()
        if not keep:
            return
        name = " ".join(self.entry.get_text().split())
        if name == self.names.get(con_id, self.original):
            return
        if name and name != self.original:
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] title_format {sway_quote(name)}")
            self.names[con_id] = name
        else:
            self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] title_format %title")
            self.names.pop(con_id, None)
        self.save()

    def closed(self, con_id):
        if con_id == self.con_id:
            self.finish(False)
        if self.names.pop(con_id, None) is not None:
            self.save()

    def save(self):
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(self.names, f)
        except OSError:
            pass


class Cascader:
    """Opens new floating windows at Windows-style cascading positions instead of all dead-centre."""

    STEP = 40

    def __init__(self, ipc):
        self.ipc = ipc
        self.slot = {}  # workspace name -> index of the next cascade position

    def place(self, con_id):
        found = find_window(self.ipc.request(GET_TREE), lambda n: n["id"] == con_id)
        if not found or found[0]["type"] != "floating_con" or found[0].get("fullscreen_mode"):
            return False
        con, ws_name = found
        workspaces = [w for w in self.ipc.request(GET_WORKSPACES) if w["name"] == ws_name]
        if not workspaces:
            return False
        ws = workspaces[0]["rect"]
        _, _, w, h = outer_geometry(con)
        n = self.slot.get(ws_name, 0)
        x, y = ws["x"] + self.STEP * (n + 1), ws["y"] + self.STEP * (n + 1)
        fits = lambda x, y: x + w <= ws["x"] + ws["width"] and y + h <= ws["y"] + ws["height"]
        if not fits(x, y):
            n = 0  # ran off the screen: start again at the top-left
            x, y = ws["x"] + self.STEP, ws["y"] + self.STEP
        if fits(x, y):
            self.slot[ws_name] = n + 1
        else:
            # Too big for any cascade position: pin it to the top-left without using up a slot.
            x, y = ws["x"], ws["y"]
        self.ipc.request(RUN_COMMAND, f"[con_id={con_id}] move absolute position {x} px {y} px")
        return False


class HomeDesktops:
    """Remembers the desktop each window was last on, so minimized windows (which sway keeps on
    the scratchpad, outside any desktop) still belong to one."""

    # Kept in the runtime dir so it survives a sway reload.
    STATE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "sway-home-desktops.json")

    def __init__(self, ipc):
        self.ipc = ipc
        try:
            with open(self.STATE_FILE) as f:
                self.homes = {int(k): desktop_key(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self.homes = {}  # con_id -> desktop key
        self.update()

    def update(self):
        windows = windows_in_focus_order(self.ipc.request(GET_TREE))
        homes = {w["id"]: desktop_key(w["workspace"]) if not w["minimized"]
                 else self.homes.get(w["id"]) for w in windows}
        homes = {k: v for k, v in homes.items() if v}
        if homes != self.homes:
            self.homes = homes
            try:
                with open(self.STATE_FILE, "w") as f:
                    json.dump(homes, f)
            except OSError:
                pass
        return False

    def on_desktop(self, window, desktop, existing):
        """Whether `window` belongs on `desktop`. A minimized window whose desktop is unknown, or
        gone (sway deletes a desktop once nothing on it is visible), belongs on every desktop."""
        home = self.homes.get(window["id"], desktop)
        return home == desktop or (window["minimized"] and home not in existing)


class DesktopNames:
    """Names the user gives desktops. sway forgets a desktop as soon as it's empty, so names are
    kept here and put back whenever the desktop is created again."""

    STATE_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                              "desktop-ui", "desktop-names.json")
    DEFAULTS = {JUNK_DESKTOP: "Junk"}

    def __init__(self, ipc):
        self.ipc = ipc
        try:
            with open(self.STATE_FILE) as f:
                self.names = {str(k): str(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError):
            self.names = dict(self.DEFAULTS)  # desktop key -> name
        self.apply()

    def label(self, key):
        return self.names.get(key) or (f"Desktop {key}" if key.isdigit() else key)

    def sway_name(self, key):
        return f"{key}: {self.names[key]}" if key in self.names else key

    def apply(self):
        """Give every numbered workspace the sway name its desktop's name calls for."""
        for ws in self.ipc.request(GET_WORKSPACES):
            key = desktop_key(ws["name"])
            if key.isdigit() and ws["name"] != self.sway_name(key):
                self.ipc.request(RUN_COMMAND, f"rename workspace {sway_quote(ws['name'])} "
                                              f"to {sway_quote(self.sway_name(key))}")
        return False

    def rename(self, key, name):
        name = " ".join(name.split())
        if name and name != f"Desktop {key}":
            self.names[key] = name
        else:
            self.names.pop(key, None)  # back to plain "Desktop N"
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, "w") as f:
                json.dump(self.names, f)
        except OSError:
            pass
        self.apply()


def css_color(sway_color):
    """sway's #rrggbb[aa] as CSS; GTK3 doesn't read 8-digit hex."""
    h = sway_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1
    return f"rgba({r}, {g}, {b}, {a:.3f})"


class WorkspaceBar:
    """Workspace buttons over the left end of swaybar, standing in for its own."""

    STATES = ("focused", "active", "inactive", "urgent")
    RECHECK_MS = 1000  # sway sends no event when swaybar takes (or gives back) its space

    def __init__(self, ipc, names):
        self.ipc = ipc
        self.names = names
        self.bar_ids = ipc.request(GET_BAR_CONFIG)
        self.config = ipc.request(GET_BAR_CONFIG, self.bar_ids[0]) if self.bar_ids else None
        self.bars = {}  # output name -> (window, box)
        self.editing = None  # (output name, desktop key, entry) while renaming
        self.recheck = None  # timer while some output's bar has no space yet
        if not self.config:
            return
        self.install_css()
        self.set_swaybar_buttons(False)
        self.refresh()

    def install_css(self):
        colors = self.config["colors"]
        font = Pango.FontDescription.from_string(self.config["font"])
        size = font.get_size() / Pango.SCALE
        unit = "px" if font.get_size_is_absolute() else "pt"
        css = [f"""
            .ws-bar {{ background-color: {css_color(colors["background"])}; }}
            .ws-button, .ws-entry {{
                font-family: "{font.get_family()}"; font-size: {size}{unit};
                padding: 0 5px; border: 1px solid transparent; border-radius: 0;
                min-height: 0; box-shadow: none;
            }}
            .ws-entry {{
                background-color: #1c1c1e; color: #ffffff; caret-color: #ffffff;
                border-color: {css_color(colors["focused_workspace_border"])};
            }}
            .ws-entry selection {{ background-color: #3d6fb4; color: #ffffff; }}
        """]
        for state in self.STATES:
            css.append(f"""
            .ws-button.{state} {{
                border-color: {css_color(colors[state + "_workspace_border"])};
                background-color: {css_color(colors[state + "_workspace_bg"])};
                color: {css_color(colors[state + "_workspace_text"])};
            }}""")
        provider = Gtk.CssProvider()
        provider.load_from_data("".join(css).encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def set_swaybar_buttons(self, on):
        for bar_id in self.bar_ids:
            self.ipc.request(RUN_COMMAND, f"bar {bar_id} workspace_buttons {'yes' if on else 'no'}")

    def bar_height(self, output, workspace):
        """swaybar's height: the strip of the output that workspaces don't cover."""
        if self.config["bar_height"]:
            return self.config["bar_height"]
        out, ws = output["rect"], workspace["rect"]
        if self.config["position"] == "top":
            return ws["y"] - out["y"]
        return out["y"] + out["height"] - ws["y"] - ws["height"]

    def refresh(self):
        if self.editing or not self.config:
            return False  # rebuilt once the rename finishes
        outputs = {o["name"]: o for o in self.ipc.request(GET_OUTPUTS) if o["active"]}
        by_output = {}
        for ws in self.ipc.request(GET_WORKSPACES):
            if ws["output"] in outputs:
                by_output.setdefault(ws["output"], []).append(ws)
        for name in list(self.bars):
            if name not in by_output:
                self.bars.pop(name)[0].destroy()
        waiting = False
        for name, workspaces in by_output.items():
            height = self.bar_height(outputs[name], workspaces[0])
            if height <= 0:
                # swaybar hasn't docked yet (sway just started), or is hidden: check again soon.
                waiting = True
                if name in self.bars:
                    self.bars[name][0].hide()
                continue
            if name not in self.bars:
                self.bars[name] = self.make_bar()
            win, box = self.bars[name]
            rect = outputs[name]["rect"]
            GtkLayerShell.set_monitor(win, Gdk.Display.get_default().get_monitor_at_point(
                rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2))
            for child in box.get_children():
                child.destroy()
            box.set_size_request(-1, height)
            for ws in workspaces:
                box.pack_start(self.make_button(name, ws), False, False, 0)
            win.resize(1, 1)  # let the surface shrink when buttons go away
            win.show_all()
        if waiting and self.recheck is None:
            self.recheck = GLib.timeout_add(self.RECHECK_MS, self.on_recheck)
        return False

    def on_recheck(self):
        self.recheck = None
        self.refresh()
        return False

    def make_bar(self):
        win = Gtk.Window()
        win.get_style_context().add_class("ws-bar")
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_namespace(win, "workspacebuttons")
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_exclusive_zone(win, -1)  # sit on top of swaybar, not beside it
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
        edge = GtkLayerShell.Edge.TOP if self.config["position"] == "top" \
            else GtkLayerShell.Edge.BOTTOM
        GtkLayerShell.set_anchor(win, edge, True)
        GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
        win.add_events(Gdk.EventMask.SCROLL_MASK)
        win.connect("scroll-event", self.on_scroll)
        win.connect("key-press-event", self.on_key_press)
        box = Gtk.Box(spacing=1)
        win.add(box)
        return win, box

    def make_button(self, output, ws):
        label = Gtk.Label(label=ws["name"])
        state = "urgent" if ws["urgent"] else "focused" if ws["focused"] \
            else "active" if ws["visible"] else "inactive"
        for cls in ("ws-button", state):
            label.get_style_context().add_class(cls)
        button = Gtk.EventBox()
        button.add(label)
        button.connect("button-press-event", self.on_press, output, ws["name"])
        return button

    def switch_to(self, name):
        self.ipc.request(RUN_COMMAND, f"workspace {sway_quote(name)}")

    def on_press(self, button, event, output, name):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True  # ignore the extra double-click events
        self.finish(True)
        if event.button == 1:
            self.switch_to(name)
        elif event.button == 3:
            self.start_rename(output, button, name)
        return True

    def on_scroll(self, _win, event):
        # Like swaybar: up goes to the previous desktop on this output, down the next, no wrap.
        step = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction)
        if step is None or self.editing:
            return True
        output = next((o for o, (win, _) in self.bars.items() if win.get_window() == event.window
                       or win.get_window() == event.window.get_toplevel()), None)
        names = [w["name"] for w in self.ipc.request(GET_WORKSPACES) if w["output"] == output]
        current = next((w["name"] for w in self.ipc.request(GET_WORKSPACES)
                        if w["output"] == output and w["visible"]), None)
        if current in names and 0 <= names.index(current) + step < len(names):
            self.switch_to(names[names.index(current) + step])
        return True

    def start_rename(self, output, button, name):
        key = desktop_key(name)
        if not key.isdigit():
            return  # only numbered desktops keep a name
        win = self.bars[output][0]
        entry = Gtk.Entry(text=self.names.names.get(key, ""), placeholder_text=f"Desktop {key}",
                          width_chars=max(12, len(name) + 2))
        entry.get_style_context().add_class("ws-entry")
        entry.connect("activate", lambda *_: self.finish(True))
        button.remove(button.get_child())
        button.add(entry)
        entry.show()
        self.editing = (output, key, entry)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        entry.grab_focus()  # selects all the text, ready to type over

    def on_key_press(self, _win, event):
        if self.editing and event.keyval == Gdk.KEY_Escape:
            self.finish(False)
            return True
        return False

    def finish(self, keep):
        """End a rename; `keep` saves what was typed. Also called when focus moves elsewhere."""
        if not self.editing:
            return
        output, key, entry = self.editing
        self.editing = None
        if output in self.bars:
            GtkLayerShell.set_keyboard_mode(self.bars[output][0], GtkLayerShell.KeyboardMode.NONE)
        if keep:
            self.names.rename(key, entry.get_text())
        self.refresh()


class Wallpapers:
    """Tiles live wallpaper windows and keeps them on the focused desktop."""

    def __init__(self, ipc):
        self.ipc = ipc
        self.follow()

    def adopt(self, con):
        """Called for each new window: if it's a wallpaper, make it the background."""
        if con.get("app_id") not in WALLPAPER_APPS:
            return
        rect = con["rect"]
        # These apps' other windows (the pod window, dialogs) open at ordinary sizes.
        if not any(rect["width"] >= o["rect"]["width"] * 0.9 and
                   rect["height"] >= o["rect"]["height"] * 0.9
                   for o in self.ipc.request(GET_OUTPUTS) if o["active"]):
            return
        self.ipc.request(RUN_COMMAND, f"[con_id={con['id']}] floating disable, border none, "
                                      f"mark --add {WALLPAPER_MARK}")

    def follow(self):
        desktop = next((w["name"] for w in self.ipc.request(GET_WORKSPACES) if w["focused"]), None)
        tree = self.ipc.request(GET_TREE)
        found = find_window(tree, lambda n: WALLPAPER_MARK in n.get("marks", []))
        # (Left alone if minimized: it's on the scratchpad then.)
        if not desktop or not found or found[1] in (desktop, "__i3_scratch"):
            return False
        before = find_window(tree, lambda n: n.get("focused"))
        self.ipc.request(RUN_COMMAND, f"[con_id={found[0]['id']}] move container "
                                      f"to workspace {sway_quote(desktop)}")
        if before:
            # Moving a window can hand it focus; give it back to the window that had it.
            self.ipc.request(RUN_COMMAND, f"[con_id={before[0]['id']}] focus")
        return False


class Switcher:
    def __init__(self):
        self.ipc = SwayIPC()
        self.events = SwayIPC()
        self.icons = IconLookup()
        self.snapper = Snapper(self.ipc)
        self.title_buttons = TitleButtons(self.ipc, self.snapper)
        self.cascader = Cascader(self.ipc)
        self.titles = TitleEditor(self.ipc)
        self.homes = HomeDesktops(self.ipc)
        self.desktop_names = DesktopNames(self.ipc)
        self.bar = WorkspaceBar(self.ipc, self.desktop_names)
        self.wallpapers = Wallpapers(self.ipc)
        self.mru = [w["id"] for w in windows_in_focus_order(self.ipc.request(GET_TREE))]
        self.desktop_mru = [desktop_key(w["name"])
                            for w in self.ipc.request(GET_WORKSPACES) if w["focused"]]
        self.mode = "windows"
        self.active = False
        self.bar_refresh_pending = False
        self.editing = False  # renaming the highlighted desktop in the heading
        self.sticky = False  # once renaming, releasing Win no longer closes the switcher
        self.entries = []
        self.index = 0
        self.items = []
        self.thumb_images = []
        # con_id, or "desktop:<name>", -> last captured thumbnail, shown while a fresh one is taken
        self.thumbs = {}
        self.session = 0  # ignores captures that finish after the switcher was reopened
        self.columns = 1
        self.timers = []

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.win = Gtk.Window()
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)
        self.win.set_app_paintable(True)
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "alttab")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        self.win.connect("focus-in-event", self.on_focus_in)
        self.win.connect("key-press-event", self.on_key_press)
        self.win.connect("key-release-event", self.on_key_release)

        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel.get_style_context().add_class("panel")
        self.heading = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=80)
        self.heading.get_style_context().add_class("heading")
        self.grid = Gtk.Grid(column_spacing=6, row_spacing=6, halign=Gtk.Align.CENTER)
        self.heading_entry = Gtk.Entry(no_show_all=True, halign=Gtk.Align.START, width_chars=32)
        for cls in ("title-entry", "heading-entry"):
            self.heading_entry.get_style_context().add_class(cls)
        self.heading_entry.connect("activate", lambda *_: self.end_rename(True))
        self.panel.pack_start(self.heading, False, False, 0)
        self.panel.pack_start(self.heading_entry, False, False, 0)
        self.panel.pack_start(self.grid, False, False, 0)
        self.win.add(self.panel)

        self.events.request(SUBSCRIBE, json.dumps(["window", "binding", "workspace"]))
        GLib.io_add_watch(self.events.sock.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                          self.on_ipc_readable)

    # --- sway events -------------------------------------------------------

    def refresh_bar(self):
        self.bar_refresh_pending = False
        self.bar.refresh()
        return False

    def on_ipc_readable(self, *_):
        try:
            self.safe_handle_event(*self.events.recv())
            while self.events.has_message():
                self.safe_handle_event(*self.events.recv())
        except ConnectionError:
            Gtk.main_quit()
            return False
        return True

    def safe_handle_event(self, msg_type, event):
        # An exception escaping this watch callback would make GLib drop the watch for good,
        # silently killing Alt+Tab and Win+arrows while the title bar buttons keep working.
        try:
            self.handle_event(msg_type, event)
        except Exception:
            traceback.print_exc()
            if self.active:
                self.finish()

    def handle_event(self, msg_type, event):
        if msg_type == EVENT_WINDOW:
            con_id = event["container"]["id"]
            if event["change"] in ("new", "focus", "move", "floating"):
                self.homes.update()
            if event["change"] == "new":
                self.wallpapers.adopt(event["container"])
                # Give the client a moment to settle on its initial size first.
                GLib.timeout_add(60, self.cascader.place, con_id)
            elif event["change"] == "focus":
                # Like renaming a file in Explorer, clicking away keeps what was typed.
                self.titles.finish(True)
                self.bar.finish(True)
                if con_id in self.mru:
                    self.mru.remove(con_id)
                self.mru.insert(0, con_id)
            elif event["change"] == "mark":
                self.wallpapers.follow()
            elif event["change"] == "close":
                self.snapper.states.pop(con_id, None)
                self.thumbs.pop(con_id, None)
                self.titles.closed(con_id)
                if con_id in self.mru:
                    self.mru.remove(con_id)
        elif msg_type == EVENT_WORKSPACE:
            # Rebuild the bar once sway has settled, rather than once per event of a burst.
            if not self.bar_refresh_pending:
                self.bar_refresh_pending = True
                GLib.idle_add(self.refresh_bar)
            if event["change"] == "init":
                self.desktop_names.apply()
            elif event["change"] == "focus" and event.get("current"):
                self.wallpapers.follow()
                key = desktop_key(event["current"]["name"])
                if key in self.desktop_mru:
                    self.desktop_mru.remove(key)
                self.desktop_mru.insert(0, key)
                GLib.timeout_add(DESKTOP_CAPTURE_DELAY_MS, self.capture_desktop, key)
        elif msg_type == EVENT_BINDING:
            words = event["binding"]["command"].split()
            # The rename binding marks its window first: "mark --add _wm_rename, nop wm rename".
            if event["binding"]["command"].endswith("nop wm rename"):
                self.titles.start()
            elif words[:2] == ["nop", "wm"] and len(words) > 2:
                self.snapper.run(words[2])
            elif words[:2] in (["nop", "alttab"], ["nop", "desktops"]) and len(words) > 2:
                mode = "windows" if words[1] == "alttab" else "desktops"
                if words[2] == "rename":
                    if not self.active:
                        self.start(0, mode)  # opens on the current desktop
                    self.start_rename()
                    return
                step = -1 if words[2] == "prev" else 1
                if not self.active:
                    self.start(step, mode)
                elif mode == self.mode and not self.editing:
                    self.select(self.index + step)

    # --- switcher lifecycle -------------------------------------------------

    def window_entries(self):
        workspaces = self.ipc.request(GET_WORKSPACES)
        desktop = next(desktop_key(w["name"]) for w in workspaces if w["focused"])
        existing = {desktop_key(w["name"]) for w in workspaces}
        windows = [w for w in windows_in_focus_order(self.ipc.request(GET_TREE))
                   if self.homes.on_desktop(w, desktop, existing) and not w["wallpaper"]]
        for w in windows:
            w["title"] = self.titles.title(w)
            w["where"] = "minimized" if w["minimized"] else f"workspace {w['workspace']}"
            w["thumb_key"] = w["id"]
            w["capture"] = ["grim", "-T", w["capture_id"]] if w["capture_id"] else None
        rank = {cid: i for i, cid in enumerate(self.mru)}
        # Stable sort keeps sway's focus order for windows we haven't seen focused.
        windows.sort(key=lambda w: (not w["focused"], rank.get(w["id"], len(rank))))
        return windows

    def desktop_entries(self):
        workspaces = self.ipc.request(GET_WORKSPACES)
        keys = list(DESKTOPS)
        keys += [k for k in (desktop_key(w["name"]) for w in workspaces) if k not in keys]
        windows = windows_in_focus_order(self.ipc.request(GET_TREE))
        rank = {key: i for i, key in enumerate(self.desktop_mru)}
        focused = next((desktop_key(w["name"]) for w in workspaces if w["focused"]), None)
        outputs = {desktop_key(w["name"]): w["output"] for w in workspaces if w["visible"]}
        entries = []
        for key in keys:
            on_it = [w for w in windows
                     if desktop_key(w["workspace"]) == key and not w["wallpaper"]]
            count = f"{len(on_it)} window{'' if len(on_it) == 1 else 's'}" if on_it else "empty"
            entries.append({
                "desktop": key,
                "title": self.desktop_names.label(key),
                "where": count,
                # The most recently used window's icon stands in until there's a snapshot.
                "app": on_it[0]["app"] if on_it else None,
                "minimized": False,
                "thumb_key": "desktop:" + key,
                "focused": key == focused,
                # Desktops on screen right now get a fresh snapshot.
                "capture": ["grim", "-o", outputs[key]] if key in outputs else None,
            })
        # Current desktop first, then most recently used; never-visited ones keep their order.
        entries.sort(key=lambda d: (not d["focused"], rank.get(d["desktop"], len(rank))))
        return entries

    def start(self, step, mode):
        self.titles.finish(True)
        entries = self.window_entries() if mode == "windows" else self.desktop_entries()
        # Nothing to switch to, unless the one entry is a minimized window to bring back.
        if len(entries) < 2 and not any(e["minimized"] for e in entries):
            return
        self.mode = mode
        self.entries = entries
        self.active = True
        self.editing = self.sticky = False
        self.got_focus = False
        self.session += 1
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_window(self.win.get_window()) if self.win.get_window() \
            else display.get_primary_monitor() or display.get_monitor(0)
        self.columns = max(1, (monitor.get_geometry().width - 120) // (THUMB_W + 40))
        self.build()
        for i, w in enumerate(entries):
            if w["capture"]:
                self.capture(w["capture"], w["thumb_key"], i)
        self.select(0 if step == 0 else 1 if step > 0 else len(entries) - 1)
        self.panel.get_style_context().add_class("hidden")
        self.win.resize(1, 1)
        self.win.show_all()
        self.timers = [
            GLib.timeout_add(SHOW_DELAY_MS, self.reveal),
            GLib.timeout_add(FOCUS_TIMEOUT_MS, self.check_focus_arrived),
        ]

    def build(self):
        for child in self.grid.get_children():
            self.grid.remove(child)
        self.items = []
        self.item_labels = []
        self.thumb_images = []
        for i, w in enumerate(self.entries):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.get_style_context().add_class("item")

            header = Gtk.Box(spacing=6)
            header_icon = self.desktop_icon() if "desktop" in w else self.icons.get(w["app"])
            icon = Gtk.Image.new_from_gicon(header_icon, Gtk.IconSize.MENU)
            icon.set_pixel_size(ICON_SIZE)
            label = Gtk.Label(label=w["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END,
                              max_width_chars=1, hexpand=True)
            if w["minimized"]:
                label.get_style_context().add_class("minimized")
            header.pack_start(icon, False, False, 0)
            header.pack_start(label, True, True, 0)

            frame = Gtk.Box()
            frame.get_style_context().add_class("thumb")
            frame.set_size_request(THUMB_W, THUMB_H)
            if w["thumb_key"] in self.thumbs:
                image = Gtk.Image.new_from_pixbuf(self.thumbs[w["thumb_key"]])
            else:
                placeholder = self.desktop_icon() if w["app"] is None else self.icons.get(w["app"])
                image = Gtk.Image.new_from_gicon(placeholder, Gtk.IconSize.DIALOG)
                image.set_pixel_size(PLACEHOLDER_ICON_SIZE)
            frame.set_center_widget(image)

            box.pack_start(header, False, False, 0)
            box.pack_start(frame, False, False, 0)
            ebox = Gtk.EventBox()
            ebox.add(box)
            ebox.connect("button-release-event", self.on_click, i)
            self.grid.attach(ebox, i % self.columns, i // self.columns, 1, 1)
            self.items.append(box)
            self.item_labels.append(label)
            self.thumb_images.append(image)

    @staticmethod
    def desktop_icon():
        return Gio.ThemedIcon.new_from_names(["user-desktop", "computer", "image-missing"])

    def capture_desktop(self, key):
        """Snapshot a desktop while it's on screen, for the Win+Tab switcher to show later."""
        if self.active:
            return False  # the overlay would be in the picture
        output = next((w["output"] for w in self.ipc.request(GET_WORKSPACES)
                       if desktop_key(w["name"]) == key and w["visible"]), None)
        if output:
            self.capture(["grim", "-o", output], "desktop:" + key, None)
        return False

    def capture(self, command, key, index):
        """Grab a window or output with grim, asynchronously, into self.thumbs[key]. If `index`
        is given, the open switcher's entry at that index shows it as soon as it arrives."""
        try:
            proc = Gio.Subprocess.new(command + ["-t", "ppm", "-"],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            return
        proc.communicate_async(None, None, self.on_captured, (index, key, self.session))

    def on_captured(self, proc, result, data):
        index, key, session = data
        try:
            _, stdout, _ = proc.communicate_finish(result)
            if not proc.get_successful() or not stdout:
                return
            loader = GdkPixbuf.PixbufLoader.new_with_type("pnm")
            loader.write(stdout.get_data())
            loader.close()
            full = loader.get_pixbuf()
        except GLib.Error:
            return
        scale = min(THUMB_W / full.get_width(), THUMB_H / full.get_height(), 1.0)
        thumb = full.scale_simple(max(1, int(full.get_width() * scale)),
                                  max(1, int(full.get_height() * scale)),
                                  GdkPixbuf.InterpType.BILINEAR)
        self.thumbs[key] = thumb
        if index is not None and self.active and session == self.session:
            self.thumb_images[index].set_from_pixbuf(thumb)

    def select(self, index):
        self.index = index % len(self.entries)
        for i, box in enumerate(self.items):
            ctx = box.get_style_context()
            (ctx.add_class if i == self.index else ctx.remove_class)("selected")
        w = self.entries[self.index]
        self.heading.set_text(f"{w['title']}  —  {w['where']}")

    def reveal(self):
        self.panel.get_style_context().remove_class("hidden")
        return False

    def check_focus_arrived(self):
        if self.active and not self.got_focus:
            self.finish()
        return False

    def commit(self):
        if not self.active:
            return
        target = self.entries[self.index]
        self.finish()
        Gdk.Display.get_default().flush()
        if self.mode == "desktops":
            key = target["desktop"]
            self.ipc.request(RUN_COMMAND, f"workspace number {key}" if key.isdigit()
                             else f"workspace {sway_quote(key)}")
        else:
            self.ipc.request(RUN_COMMAND, f"[con_id={target['id']}] focus")

    def finish(self):
        if self.editing:
            self.end_rename(False)
        self.active = False
        context = GLib.MainContext.default()
        for t in self.timers:
            if context.find_source_by_id(t):
                GLib.source_remove(t)
        self.timers = []
        self.win.hide()

    # --- renaming desktops ---------------------------------------------------

    def start_rename(self):
        if not self.active or self.mode != "desktops" or self.editing:
            return
        self.editing = self.sticky = True
        self.heading_entry.set_text(self.entries[self.index]["title"])
        self.heading.hide()
        self.heading_entry.show()
        self.heading_entry.grab_focus()  # selects all the text, ready to type over

    def end_rename(self, keep):
        self.editing = False
        entry = self.entries[self.index]
        if keep:
            self.desktop_names.rename(entry["desktop"], self.heading_entry.get_text())
            entry["title"] = self.desktop_names.label(entry["desktop"])
            self.item_labels[self.index].set_text(entry["title"])
        self.heading_entry.hide()
        self.heading.show()
        self.win.set_focus(None)
        self.select(self.index)

    # --- input on the overlay ----------------------------------------------

    def modifier_held(self):
        keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        return bool(keymap.get_modifier_state() & HOLD_KEYS[self.mode][1])

    def on_focus_in(self, *_):
        if self.active and not self.got_focus:
            self.got_focus = True
            # Alt/Win may already be up if the shortcut was tapped quickly; poll as a safety net.
            self.timers.append(GLib.timeout_add(40, self.poll_modifier))
        return False

    def poll_modifier(self):
        if not self.active or self.sticky:
            return False
        if not self.modifier_held():
            self.commit()
            return False
        return True

    def on_key_release(self, _win, event):
        if event.keyval in HOLD_KEYS[self.mode][0] and not self.sticky:
            self.commit()
        return True

    def on_key_press(self, _win, event):
        key = event.keyval
        if self.editing:
            if key == Gdk.KEY_Escape:
                self.end_rename(False)
                return True
            return False  # typing goes to the entry
        if key == Gdk.KEY_F2:
            self.start_rename()
        elif key == Gdk.KEY_Escape:
            self.finish()
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.commit()
        elif key in (Gdk.KEY_Right, Gdk.KEY_Tab):
            self.select(self.index + 1)
        elif key in (Gdk.KEY_Left, Gdk.KEY_ISO_Left_Tab):
            self.select(self.index - 1)
        elif key == Gdk.KEY_Down:
            self.select(self.index + self.columns)
        elif key == Gdk.KEY_Up:
            self.select(self.index - self.columns)
        return True

    def on_click(self, _widget, event, index):
        if self.editing:
            self.end_rename(True)
        self.select(index)
        if event.button == 3:
            self.start_rename()
        else:
            self.commit()
        return True


def replace_running_instance():
    try:
        with open(PIDFILE) as f:
            old = int(f.read().strip())
        with open(f"/proc/{old}/cmdline", "rb") as f:
            if b"alttab.py" in f.read():
                os.kill(old, 15)
                # Let it finish turning swaybar's buttons back on before this one turns them off.
                for _ in range(50):
                    if not os.path.exists(f"/proc/{old}"):
                        break
                    GLib.usleep(20000)
    except (OSError, ValueError):
        pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def main():
    sys.stderr = open(LOGFILE, "a", buffering=1)
    replace_running_instance()
    switcher = Switcher()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)
    Gtk.main()
    try:
        switcher.bar.set_swaybar_buttons(True)
    except (OSError, ConnectionError):
        pass  # sway itself is going away


if __name__ == "__main__":
    sys.exit(main())
