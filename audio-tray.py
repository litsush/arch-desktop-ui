#!/usr/bin/python3
"""Volume icon in swaybar's tray (a StatusNotifierItem over D-Bus).

Left-click opens the mixer (pavucontrol), or closes it if it's already focused; scrolling
changes the default output's volume; right- or middle-click toggles mute. The icon is drawn
here in white, as the theme's volume icons are dark and vanish on a dark bar. Volume changes
made anywhere (keyboard keys, the mixer) update it through `pactl subscribe`.

Started by windows-like.conf; a new instance replaces the running one.
"""
import json
import os
import subprocess
import sys

import cairo
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.desktopui.AudioTray"  # owned so a new instance can replace the old one
SINK = "@DEFAULT_AUDIO_SINK@"
STEP = "5%"
MAX_VOLUME = "1.0"  # scrolling stops at 100%
MIXER_APP_ID = "org.pulseaudio.pavucontrol"
ICON_SIZES = (16, 22, 24, 32, 48)
LOGFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "audio-tray.log")

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewTitle"/>
  </interface>
</node>
"""


def run(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def read_volume():
    """(volume as 0..1+, muted) for the default output, or (0, True) if there's none."""
    words = run("wpctl", "get-volume", SINK).split()  # "Volume: 0.65" or "Volume: 0.65 [MUTED]"
    try:
        return float(words[1]), "[MUTED]" in words
    except (IndexError, ValueError):
        return 0.0, True


def draw_icon(size, volume, muted):
    """A white speaker with one to three sound waves for the level, or a cross when muted.
    Returned as SNI pixmap data: ARGB32 in network byte order."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.scale(size / 24, size / 24)  # drawn on a 24x24 grid
    cr.set_source_rgba(1, 1, 1, 0.55 if muted else 1)
    # Speaker: a box with a cone opening to the right.
    cr.move_to(3, 9)
    cr.line_to(7, 9)
    cr.line_to(12, 4.5)
    cr.line_to(12, 19.5)
    cr.line_to(7, 15)
    cr.line_to(3, 15)
    cr.close_path()
    cr.fill()
    cr.set_line_width(2)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    if muted:
        cr.move_to(15.5, 9)
        cr.line_to(21.5, 15)
        cr.move_to(21.5, 9)
        cr.line_to(15.5, 15)
        cr.stroke()
    else:
        waves = 0 if volume <= 0 else 1 if volume < 0.34 else 2 if volume < 0.67 else 3
        for radius in (3.5, 6.5, 9.5)[:waves]:
            cr.arc(12, 12, radius, -0.85, 0.85)
            cr.stroke()
    surface.flush()
    data = bytearray(surface.get_data())
    # cairo stores each pixel native-endian (BGRA bytes here); SNI wants ARGB bytes.
    for i in range(0, len(data), 4):
        data[i:i + 4] = data[i:i + 4][::-1]
    return size, size, bytes(data)


class AudioTray:
    def __init__(self, bus):
        self.bus = bus
        self.volume, self.muted = read_volume()
        self.refresh_pending = False
        info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        bus.register_object("/StatusNotifierItem", info.interfaces[0],
                            self.on_call, self.on_get_property, None)
        # swaybar hosts the watcher and restarts on every sway reload: register each time.
        Gio.bus_watch_name_on_connection(bus, "org.kde.StatusNotifierWatcher",
                                         Gio.BusNameWatcherFlags.NONE, self.register, None)
        self.watch_changes()

    def register(self, *_):
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                      "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.bus.get_unique_name(),)), None,
                      Gio.DBusCallFlags.NONE, -1, None, None)

    # --- D-Bus ------------------------------------------------------------

    def title(self):
        return "Volume muted" if self.muted else f"Volume {round(self.volume * 100)}%"

    def on_get_property(self, _bus, _sender, _path, _iface, name):
        if name == "IconPixmap":
            icons = [draw_icon(s, self.volume, self.muted) for s in ICON_SIZES]
            return GLib.Variant("a(iiay)", icons)
        return {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "desktopui-audio"),
            "Title": GLib.Variant("s", self.title()),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", ""),  # IconPixmap is used instead
            "ItemIsMenu": GLib.Variant("b", False),
        }.get(name)

    def on_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        if method == "Activate":
            self.toggle_mixer()
        elif method in ("SecondaryActivate", "ContextMenu"):
            run("wpctl", "set-mute", SINK, "toggle")
        elif method == "Scroll":
            delta, orientation = params.unpack()
            if orientation == "vertical" and delta:
                # swaybar sends a negative delta for scrolling up.
                change = STEP + ("+" if delta < 0 else "-")
                run("wpctl", "set-volume", "-l", MAX_VOLUME, SINK, change)
        invocation.return_value(None)
        self.refresh()

    # --- volume -------------------------------------------------------------

    def toggle_mixer(self):
        """Open the mixer; focus it if it's open elsewhere; close it if it's already focused."""
        mixers = []

        def walk(node):
            if node.get("app_id") == MIXER_APP_ID:
                mixers.append(node)
            for child in node.get("nodes", []) + node.get("floating_nodes", []):
                walk(child)
        walk(json.loads(run("swaymsg", "-t", "get_tree")))
        if not mixers:
            run("swaymsg", "exec", "pavucontrol")
        elif any(m["focused"] for m in mixers):
            run("swaymsg", f'[app_id="{MIXER_APP_ID}"] kill')
        else:
            run("swaymsg", f'[app_id="{MIXER_APP_ID}"] focus')

    def refresh(self):
        """Re-read the volume once things settle and update the icon if it changed."""
        if not self.refresh_pending:
            self.refresh_pending = True
            GLib.timeout_add(30, self.do_refresh)

    def do_refresh(self):
        self.refresh_pending = False
        state = read_volume()
        if state != (self.volume, self.muted):
            self.volume, self.muted = state
            for signal in ("NewIcon", "NewTitle"):
                self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem",
                                     signal, None)
        return False

    def watch_changes(self):
        """Follow volume changes made anywhere through `pactl subscribe`."""
        try:
            proc = Gio.Subprocess.new(["pactl", "subscribe"], Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error:
            GLib.timeout_add_seconds(5, lambda: self.watch_changes() and False)
            return
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line, stream)

    def on_line(self, stream, result, _data):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            # pactl went away (the sound server restarted): start it again shortly.
            GLib.timeout_add_seconds(2, lambda: self.watch_changes() and False)
            return
        if " sink " in line or " server " in line:
            self.refresh()
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line, stream)


def main():
    sys.stderr = open(LOGFILE, "a", buffering=1)
    loop = GLib.MainLoop()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    state = {}

    def acquired(*_):
        state["tray"] = AudioTray(bus)

    Gio.bus_own_name_on_connection(
        bus, BUS_NAME,
        Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
        acquired, lambda *_: loop.quit())  # replaced by a newer instance
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, loop.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, loop.quit)
    loop.run()


if __name__ == "__main__":
    main()
