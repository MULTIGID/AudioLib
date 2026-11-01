from __future__ import annotations

import atexit
import ctypes
from datetime import datetime
import json
import math
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from . import __version__

try:
    import pystray
except ImportError:
    pystray = None

from .audio_endpoints import (
    AudioEndpointError,
    CaptureEndpoint,
    WindowsMicrophoneRouting,
    current_default_microphone,
    list_physical_microphones,
    microphone_label,
)
from .library_tools import create_backup, restore_backup
from .loudness import analyze_pcm16_loudness, decode_pcm16_file
from .hotkeys import (
    GlobalHotkeyManager,
    display_hotkey,
    hotkey_from_tk_event,
    parse_hotkey,
)
from .player import PlayerError, WindowsMediaPlayer, list_output_devices
from .startup import register_autostart, unregister_autostart
from .storage import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, LibraryStore, Person, Track
from .virtual_audio import (
    DRIVER_PAGE,
    VirtualAudioError,
    VirtualMicrophoneBridge,
    launch_driver_setup,
    prepare_official_installer,
)
from .windows_file_dialog import FileDialogError, choose_files, choose_save_file


BG = "#111827"
PANEL = "#1f2937"
CARD = "#273449"
ACCENT = "#8b5cf6"
ACCENT_HOVER = "#7c3aed"
ACCENT_PRESETS = {
    "Фіолетовий": ("#8b5cf6", "#7c3aed"),
    "Синій": ("#3b82f6", "#2563eb"),
    "Бірюзовий": ("#0d9488", "#0f766e"),
    "Рожевий": ("#ec4899", "#db2777"),
}
TEXT = "#f8fafc"
MUTED = "#9ca3af"
DANGER = "#dc2626"
FAVORITES_PERSON_ID = -1
FAVORITES_LIMIT = 15
STOP_HOTKEY_ACTION_ID = -2
DEFAULT_STOP_HOTKEY = "Ctrl+Alt+Backspace"
DEFAULT_WINDOW_WIDTH = 1180
DEFAULT_WINDOW_HEIGHT = 760
if pystray is not None:
    class AudioLibTrayIcon(pystray.Icon):
        """Windows tray icon with separate single- and double-click actions."""

        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205

        def __init__(self, *args, on_single_click=None, on_double_click=None, **kwargs):
            self._on_single_click_callback = on_single_click
            self._on_double_click_callback = on_double_click
            self._ignore_next_left_up = False
            super().__init__(*args, **kwargs)

        def _on_notify(self, wparam, lparam):
            if lparam == self.WM_LBUTTONDBLCLK:
                self._ignore_next_left_up = True
                if self._on_double_click_callback is not None:
                    self._on_double_click_callback()
            elif lparam == self.WM_LBUTTONUP:
                if self._ignore_next_left_up:
                    self._ignore_next_left_up = False
                elif self._on_single_click_callback is not None:
                    self._on_single_click_callback()
            elif lparam == self.WM_RBUTTONUP:
                if self._on_single_click_callback is not None:
                    self._on_single_click_callback()
            else:
                super()._on_notify(wparam, lparam)

else:
    AudioLibTrayIcon = None


def format_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_duration(milliseconds: int | None) -> str:
    if not milliseconds:
        return "—"
    seconds = milliseconds // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def render_media_control_image(
    size: int,
    symbol: str,
    foreground: str,
    background: str | None = None,
) -> ImageTk.PhotoImage:
    """Render a smooth media icon (and optional circular button surface)."""
    scale = 4
    canvas_size = max(1, int(size))
    image = Image.new(
        "RGBA",
        (canvas_size * scale, canvas_size * scale),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(image)
    if background is not None:
        inset = scale
        draw.ellipse(
            (inset, inset, image.width - 1 - inset, image.height - 1 - inset),
            fill=background,
        )
    center = canvas_size * scale / 2
    half_height = max(4.0, canvas_size * 0.13) * scale
    if symbol == "▶":
        draw.polygon(
            (
                (center - half_height * 0.55, center - half_height),
                (center - half_height * 0.55, center + half_height),
                (center + half_height * 1.05, center),
            ),
            fill=foreground,
        )
    elif symbol == "■":
        half = half_height * 0.78
        draw.rectangle(
            (center - half, center - half, center + half, center + half),
            fill=foreground,
        )
    elif symbol in {"Ⅱ", "‖"}:
        half = half_height
        bar_width = max(scale * 2, half_height * 0.48)
        gap = max(scale * 1.5, half_height * 0.35)
        draw.rectangle(
            (
                center - gap - bar_width,
                center - half,
                center - gap,
                center + half,
            ),
            fill=foreground,
        )
        draw.rectangle(
            (
                center + gap,
                center - half,
                center + gap + bar_width,
                center + half,
            ),
            fill=foreground,
        )
    image = image.resize(
        (canvas_size, canvas_size),
        Image.Resampling.LANCZOS,
    )
    return ImageTk.PhotoImage(image)


def render_rounded_surface(
    width: int,
    height: int,
    *,
    fill: str,
    radius: int,
    outline: str | None = None,
    outline_width: int = 1,
) -> ImageTk.PhotoImage:
    """Render a seam-free antialiased rounded surface for Tk canvases."""
    scale = 4
    width = max(1, int(width))
    height = max(1, int(height))
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(1, int(outline_width)) * scale
    inset = stroke / 2 if outline else 0
    draw.rounded_rectangle(
        (inset, inset, image.width - 1 - inset, image.height - 1 - inset),
        radius=max(0, int(radius)) * scale,
        fill=fill,
        outline=outline,
        width=stroke if outline else 1,
    )
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(image)


def render_triangle_icon(size: int, color: str, *, direction: str = "down") -> ImageTk.PhotoImage:
    """Render a small antialiased disclosure triangle."""
    scale = 4
    size = max(1, int(size))
    image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = size * scale / 2
    half = size * scale * 0.3
    if direction == "up":
        points = ((center - half, center + half / 2), (center + half, center + half / 2), (center, center - half / 2))
    else:
        points = ((center - half, center - half / 2), (center + half, center - half / 2), (center, center + half / 2))
    draw.polygon(points, fill=color)
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(image)


class FlatScale(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        from_: float,
        to: float,
        command=None,
        width: int = 100,
        height: int = 18,
        background: str = "#0b1220",
        trough_color: str = "#374151",
        fill_color: str = "#64748b",
        active_color: str = ACCENT,
        thumb_radius: int = 4,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=background,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.minimum = float(from_)
        self.maximum = float(to)
        self.value = float(from_)
        self.command = command
        self.trough_color = trough_color
        self.fill_color = fill_color
        self.active_color = active_color
        self.thumb_radius = thumb_radius
        self.hovered = False
        self.dragging = False
        self._surface_image: ImageTk.PhotoImage | None = None
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<B1-Motion>", self._drag, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")

    def set_range(self, maximum: float) -> None:
        self.maximum = max(self.minimum + 1, float(maximum))
        self.value = min(self.value, self.maximum)
        self._draw()

    def set(self, value: float) -> None:
        self.value = min(self.maximum, max(self.minimum, float(value)))
        self._draw()

    def get(self) -> float:
        return self.value

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self._draw()

    def _press(self, event: tk.Event) -> None:
        self.dragging = True
        self._set_from_x(event.x)

    def _drag(self, event: tk.Event) -> None:
        if self.dragging:
            self._set_from_x(event.x)

    def _release(self, event: tk.Event) -> None:
        self._set_from_x(event.x)
        self.dragging = False

    def _set_from_x(self, x: int) -> None:
        width = max(1, self.winfo_width())
        padding = self.thumb_radius + 2
        usable_width = max(1, width - padding * 2)
        ratio = min(1.0, max(0.0, (x - padding) / usable_width))
        self.value = self.minimum + ratio * (self.maximum - self.minimum)
        self._draw()
        if self.command is not None:
            self.command(str(self.value))

    def _draw(self) -> None:
        self.delete("scale")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        padding = self.thumb_radius + 2
        start = padding
        end = max(start + 1, width - padding)
        center_y = height / 2
        value_range = max(1.0, self.maximum - self.minimum)
        ratio = min(1.0, max(0.0, (self.value - self.minimum) / value_range))
        value_x = start + (end - start) * ratio
        active = self.active_color if self.hovered or self.dragging else self.fill_color
        scale = 4
        surface = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        line_width = 4 * scale
        draw.line(
            (start * scale, center_y * scale, end * scale, center_y * scale),
            fill=self.trough_color,
            width=line_width,
        )
        cap_radius = line_width / 2
        for x in (start * scale, end * scale):
            draw.ellipse(
                (x - cap_radius, center_y * scale - cap_radius,
                 x + cap_radius, center_y * scale + cap_radius),
                fill=self.trough_color,
            )
        if value_x > start:
            draw.line(
                (start * scale, center_y * scale, value_x * scale, center_y * scale),
                fill=active,
                width=line_width,
            )
            for x in (start * scale, value_x * scale):
                draw.ellipse(
                    (x - cap_radius, center_y * scale - cap_radius,
                     x + cap_radius, center_y * scale + cap_radius),
                    fill=active,
                )
        if self.thumb_radius:
            radius = self.thumb_radius * scale
            draw.ellipse(
                (value_x * scale - radius, center_y * scale - radius,
                 value_x * scale + radius, center_y * scale + radius),
                fill=active,
            )
        surface = surface.resize((width, height), Image.Resampling.LANCZOS)
        self._surface_image = ImageTk.PhotoImage(surface)
        self.create_image(0, 0, image=self._surface_image, anchor="nw", tags="scale")


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command,
        color: str,
        hover_color: str,
        *,
        radius: int = 9,
        padx: int = 14,
        pady: int = 8,
        font=("Segoe UI Semibold", 10),
        image: ImageTk.PhotoImage | None = None,
    ) -> None:
        self.text = text
        self.button_image = image
        self.command = command
        self.normal_color = color
        self.hover_color = hover_color
        self.radius = radius
        self.horizontal_padding = padx
        self.vertical_padding = pady
        self.button_font = tkfont.Font(root=parent, font=font)
        self.state = "normal"
        self.hovered = False
        self.pressed = False
        self._media_button_image: ImageTk.PhotoImage | None = None
        self._surface_image: ImageTk.PhotoImage | None = None
        width, height = self._requested_size()
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda _event: self.invoke())
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self._draw()

    def _requested_size(self) -> tuple[int, int]:
        if self.button_image is None and self.text in {"▶", "■", "Ⅱ", "‖"}:
            return 40, 40
        image_width = self.button_image.width() if self.button_image else 0
        image_height = self.button_image.height() if self.button_image else 0
        gap = 7 if self.button_image and self.text else 0
        return (
            image_width
            + gap
            + self.button_font.measure(self.text)
            + self.horizontal_padding * 2,
            max(image_height, self.button_font.metrics("linespace"), 18)
            + self.vertical_padding * 2,
        )

    def _background_color(self) -> str:
        if self.state == "disabled":
            return "#374151"
        if self.hovered or self.pressed:
            return self.hover_color
        return self.normal_color

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width(), int(self.cget("width")))
        height = max(1, self.winfo_height(), int(self.cget("height")))
        color = self._background_color()
        symbol_color = "#94a3b8" if self.state == "disabled" else "white"
        if self.button_image is None and self.text in {"▶", "■", "Ⅱ", "‖"}:
            diameter = min(width, height)
            self._media_button_image = render_media_control_image(
                diameter,
                self.text,
                symbol_color,
                color,
            )
            self.create_image(
                width / 2,
                height / 2,
                image=self._media_button_image,
                anchor="center",
            )
            self.configure(cursor="arrow" if self.state == "disabled" else "hand2")
            return
        radius = min(self.radius, width // 2, height // 2)
        self._surface_image = render_rounded_surface(
            width,
            height,
            fill=color,
            radius=radius,
        )
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
        image_width = self.button_image.width() if self.button_image else 0
        gap = 7 if self.button_image and self.text else 0
        text_width = self.button_font.measure(self.text)
        content_width = image_width + gap + text_width
        content_x = (width - content_width) / 2
        icon_y = height / 2
        text_y = height / 2 - 2
        if self.button_image is not None:
            self.create_image(
                content_x + image_width / 2,
                icon_y,
                image=self.button_image,
                anchor="center",
            )
            content_x += image_width + gap
        if self.text:
            self.create_text(
                content_x,
                text_y,
                text=self.text,
                fill=symbol_color,
                font=self.button_font,
                anchor="w",
            )
        self.configure(cursor="arrow" if self.state == "disabled" else "hand2")

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.pressed = False
        self._draw()

    def _press(self, _event=None) -> None:
        if self.state != "disabled":
            self.pressed = True
            self._draw()

    def _release(self, event: tk.Event) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if (
            was_pressed
            and self.state != "disabled"
            and 0 <= event.x <= self.winfo_width()
            and 0 <= event.y <= self.winfo_height()
        ):
            self.invoke()

    def invoke(self) -> None:
        if self.state != "disabled" and self.command is not None:
            self.command()

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        redraw = False
        if "text" in options:
            self.text = str(options.pop("text"))
            width, height = self._requested_size()
            super().configure(width=width, height=height)
            redraw = True
        if "state" in options:
            self.state = str(options.pop("state"))
            redraw = True
        if options:
            result = super().configure(**options)
        else:
            result = None
        if redraw:
            self._draw()
        return result

    config = configure

    def cget(self, key: str):
        if key == "text":
            return self.text
        if key == "state":
            return self.state
        return super().cget(key)


class SmoothRadioButton(tk.Canvas):
    """Antialiased radio control that follows the application palette."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        value: str,
        variable: tk.StringVar,
        command=None,
    ) -> None:
        self.text = text
        self.value = value
        self.variable = variable
        self.command = command
        self.hovered = False
        self.button_font = tkfont.Font(root=parent, font=("Segoe UI", 9))
        self._indicator_image: ImageTk.PhotoImage | None = None
        width = 22 + self.button_font.measure(text)
        super().__init__(
            parent,
            width=width,
            height=22,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self._trace_id = self.variable.trace_add("write", self._changed)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._select)
        self.bind("<space>", self._select)
        self.bind("<Return>", self._select)
        self._draw()

    def _changed(self, *_args) -> None:
        self._draw()

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self._draw()

    def _select(self, _event=None) -> str:
        changed = self.variable.get() != self.value
        self.variable.set(self.value)
        if changed and self.command is not None:
            self.command()
        return "break"

    def _draw(self) -> None:
        self.delete("all")
        scale = 4
        size = 16
        image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        selected = self.variable.get() == self.value
        ring = ACCENT if selected else TEXT if self.hovered else "#64748b"
        stroke = 2 * scale
        inset = stroke / 2
        draw.ellipse(
            (inset, inset, image.width - 1 - inset, image.height - 1 - inset),
            fill=BG,
            outline=ring,
            width=stroke,
        )
        if selected:
            dot = 4 * scale
            center = image.width / 2
            draw.ellipse(
                (center - dot, center - dot, center + dot, center + dot),
                fill=ACCENT,
            )
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        self._indicator_image = ImageTk.PhotoImage(image)
        self.create_image(0, 11, image=self._indicator_image, anchor="w")
        self.create_text(
            21,
            9,
            text=self.text,
            fill=TEXT if self.hovered or selected else MUTED,
            font=self.button_font,
            anchor="w",
        )

    def destroy(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace_id)
        except (tk.TclError, AttributeError):
            pass
        super().destroy()


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.BooleanVar,
        command=None,
    ) -> None:
        super().__init__(
            parent,
            width=46,
            height=24,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self.variable = variable
        self.command = command
        self.enabled = True
        self._surface_image: ImageTk.PhotoImage | None = None
        self._trace_id = self.variable.trace_add("write", self._variable_changed)
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self._draw()

    def _variable_changed(self, *_args) -> None:
        self._draw()

    def _toggle(self, _event=None) -> str:
        if self.enabled:
            self.variable.set(not self.variable.get())
            if self.command is not None:
                self.command()
        return "break"

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(46, self.winfo_width(), int(self.cget("width")))
        height = max(24, self.winfo_height(), int(self.cget("height")))
        active = self.variable.get()
        track_color = (
            ACCENT if active and self.enabled
            else "#475569" if self.enabled
            else "#273449"
        )
        thumb_color = "#f8fafc" if self.enabled else "#64748b"
        radius = height / 2
        thumb_radius = max(3, radius - 3)
        center_x = width - radius if active else radius
        scale = 4
        surface = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle(
            (0, 0, width * scale - 1, height * scale - 1),
            radius=int(radius * scale),
            fill=track_color,
        )
        draw.ellipse(
            ((center_x - thumb_radius) * scale,
             (radius - thumb_radius) * scale,
             (center_x + thumb_radius) * scale,
             (radius + thumb_radius) * scale),
            fill=thumb_color,
        )
        surface = surface.resize((width, height), Image.Resampling.LANCZOS)
        self._surface_image = ImageTk.PhotoImage(surface)
        self.create_image(0, 0, image=self._surface_image, anchor="nw")

    def destroy(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace_id)
        except (tk.TclError, AttributeError):
            pass
        super().destroy()


class RoundedDropdown(tk.Canvas):
    """Dark themed, rounded replacement for the native Windows combobox."""

    _active_dropdown = None

    def __init__(
        self,
        parent: tk.Misc,
        *,
        textvariable: tk.StringVar,
        command=None,
        width: int = 390,
        height: int = 38,
        placeholder: str = "Оберіть значення",
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self.textvariable = textvariable
        self.command = command
        self.placeholder = placeholder
        self.values: list[str] = []
        self.selected_index = -1
        self.popup: tk.Toplevel | None = None
        self._popup_position_job: str | None = None
        self._popup_size = (0, 0)
        self._surface_image: ImageTk.PhotoImage | None = None
        self._arrow_image: ImageTk.PhotoImage | None = None
        self.hovered = False
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._toggle_popup)
        self.bind("<Return>", self._toggle_popup)
        self.bind("<space>", self._toggle_popup)
        self.bind("<Escape>", lambda _event: self._close_popup())
        self.bind(
            "<Destroy>",
            lambda event: self._close_popup(restore_focus=False)
            if event.widget is self
            else None,
            add="+",
        )
        self._draw()

    @staticmethod
    def _rounded_surface(
        width: int,
        height: int,
        *,
        fill: str,
        outline: str | None = None,
        radius: int = 10,
    ) -> ImageTk.PhotoImage:
        """Render one antialiased shape without seams between primitives."""
        return render_rounded_surface(
            width,
            height,
            fill=fill,
            outline=outline,
            radius=radius,
        )

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width(), int(self.cget("width")))
        height = max(1, self.winfo_height(), int(self.cget("height")))
        outline = ACCENT if self.hovered or self.popup is not None else "#64748b"
        self._surface_image = self._rounded_surface(
            width,
            height,
            fill="#334155",
            outline=outline,
        )
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
        label = self.textvariable.get() or self.placeholder
        self.create_text(
            13,
            height / 2 - 2,
            text=label,
            fill=TEXT if self.textvariable.get() else MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        )
        center_x = width - 18
        self._arrow_image = render_triangle_icon(
            16,
            TEXT,
            direction="up" if self.popup is not None else "down",
        )
        self.create_image(
            center_x,
            height / 2,
            image=self._arrow_image,
            anchor="center",
        )

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self._draw()

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if "values" in options:
            self.values = [str(value) for value in options.pop("values")]
            if self.selected_index >= len(self.values):
                self.current(-1)
        result = super().configure(**options) if options else None
        self._draw()
        return result

    config = configure

    def current(self, index: int | None = None) -> int:
        if index is None:
            return self.selected_index
        self.selected_index = index if 0 <= index < len(self.values) else -1
        self.textvariable.set(
            self.values[self.selected_index] if self.selected_index >= 0 else ""
        )
        self._draw()
        return self.selected_index

    def _toggle_popup(self, _event=None) -> str:
        if self.popup is not None:
            self._close_popup()
        elif self.values:
            self._open_popup()
        return "break"

    def _open_popup(self) -> None:
        active = RoundedDropdown._active_dropdown
        if active is not None and active is not self:
            active._close_popup(restore_focus=False)
        self.update_idletasks()
        width = self.winfo_width()
        visible_rows = min(8, len(self.values))
        row_height = 32
        height = visible_rows * row_height + 10
        popup = tk.Toplevel(self)
        owner = self.winfo_toplevel()
        popup.transient(owner)
        popup.overrideredirect(True)
        popup.configure(bg="#111827")
        self.popup = popup
        self._popup_size = (width, height)
        RoundedDropdown._active_dropdown = self
        self._sync_popup_position()
        surface = tk.Canvas(
            popup,
            width=width,
            height=height,
            bg="#111827",
            highlightthickness=0,
            bd=0,
        )
        surface.pack(fill="both", expand=True)
        popup._audiolib_surface_image = self._rounded_surface(
            width,
            height,
            fill=PANEL,
            outline="#64748b",
        )
        surface.create_image(
            0,
            0,
            image=popup._audiolib_surface_image,
            anchor="nw",
        )
        listbox = tk.Listbox(
            popup,
            bg=PANEL,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
            activestyle="none",
            exportselection=False,
            cursor="hand2",
        )
        for value in self.values:
            listbox.insert("end", value)
        if self.selected_index >= 0:
            listbox.selection_set(self.selected_index)
            listbox.activate(self.selected_index)
            listbox.see(self.selected_index)
        listbox.place(x=5, y=5, width=width - 10, height=height - 10)

        def choose(event=None) -> str:
            if event is not None and getattr(event, "y", None) is not None:
                index = listbox.nearest(event.y)
            else:
                selection = listbox.curselection()
                index = selection[0] if selection else listbox.index("active")
            if 0 <= index < len(self.values):
                self._select(index)
            return "break"

        listbox.bind("<ButtonRelease-1>", choose)
        listbox.bind("<Return>", choose)
        listbox.bind("<Escape>", lambda _event: self._close_popup())
        listbox.bind(
            "<MouseWheel>",
            lambda event: listbox.yview_scroll(-1 if event.delta > 0 else 1, "units"),
        )
        popup.bind("<Escape>", lambda _event: self._close_popup())
        popup.bind("<FocusOut>", lambda _event: popup.after(80, self._close_if_unfocused))
        popup.lift()
        listbox.focus_force()
        self._draw()

    def _sync_popup_position(self) -> None:
        popup = self.popup
        if popup is None:
            return
        try:
            owner = self.winfo_toplevel()
            field_x = self.winfo_rootx()
            field_y = self.winfo_rooty()
            field_height = self.winfo_height()
            owner_y = owner.winfo_rooty()
            owner_bottom = owner_y + owner.winfo_height()
            # A dropdown inside a scrolled settings canvas can stay mapped
            # even after it has moved outside the visible window.
            if (
                not self.winfo_viewable()
                or field_y + field_height <= owner_y
                or field_y >= owner_bottom
            ):
                self._close_popup(restore_focus=False)
                return
            width, height = self._popup_size
            popup.geometry(
                f"{width}x{height}+{field_x}+{field_y + field_height + 3}"
            )
            popup.lift()
            self._popup_position_job = self.after(40, self._sync_popup_position)
        except tk.TclError:
            self._close_popup(restore_focus=False)

    def _select(self, index: int) -> None:
        self.current(index)
        self._close_popup()
        self.focus_set()
        if self.command is not None:
            self.command(index)

    def _close_if_unfocused(self) -> None:
        if self.popup is not None and self.popup.focus_get() is None:
            self._close_popup()

    def _close_popup(self, *, restore_focus: bool = True) -> None:
        if self._popup_position_job is not None:
            try:
                self.after_cancel(self._popup_position_job)
            except tk.TclError:
                pass
            self._popup_position_job = None
        popup = self.popup
        self.popup = None
        if RoundedDropdown._active_dropdown is self:
            RoundedDropdown._active_dropdown = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        if restore_focus:
            try:
                owner = self.winfo_toplevel()
                owner.after_idle(owner.lift)
                owner.after_idle(self.focus_set)
            except tk.TclError:
                pass
        try:
            self._draw()
        except tk.TclError:
            pass


class CustomTrackTable(tk.Frame):
    """Virtualized Canvas table with a fixed header and interactive rows."""

    HEADER_HEIGHT = 34
    ROW_HEIGHT = 35
    ACTION_COLUMNS = {"speaker_play", "favorite", "mic_play", "mic_stop", "hotkey"}

    def __init__(
        self,
        parent: tk.Misc,
        *,
        columns: tuple[str, ...],
        show: str = "headings",
        selectmode: str = "browse",
    ) -> None:
        del show, selectmode
        super().__init__(parent, bg=BG, bd=0, highlightthickness=0, padx=0, pady=0)
        self._all_columns = tuple(columns)
        self._display_columns = tuple(columns)
        self._column_options: dict[str, dict[str, object]] = {
            name: {"width": 100, "minwidth": 40, "anchor": "w", "stretch": True}
            for name in columns
        }
        self._heading_options: dict[str, dict[str, object]] = {
            name: {"text": name, "image": None, "command": None, "anchor": "center"}
            for name in columns
        }
        self._rows: list[tuple[str, tuple[object, ...]]] = []
        self._row_lookup: dict[str, int] = {}
        self._selected_iid: str | None = None
        self._focused_iid: str | None = None
        self._hovered: tuple[str, str] | None = None
        self._yscrollcommand = None
        self._xscrollcommand = None
        self._redraw_job: str | None = None
        self._pending_y_fraction: float | None = None
        self._autofit_title = False
        self._row_font = tkfont.Font(root=self, font=("Segoe UI", 10))
        self._header_font = tkfont.Font(root=self, font=("Segoe UI Semibold", 9))
        self._hotkey_font = tkfont.Font(root=self, font=("Segoe UI Semibold", 8))
        self._action_image_cache: dict[
            tuple[str, str, str | None, int], ImageTk.PhotoImage
        ] = {}

        self.header = tk.Canvas(
            self,
            height=self.HEADER_HEIGHT,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.body = tk.Canvas(
            self,
            bg=BG,
            highlightthickness=0,
            bd=0,
            takefocus=True,
            xscrollincrement=20,
            yscrollincrement=self.ROW_HEIGHT,
        )
        self.body.pack(fill="both", expand=True)
        self.body.configure(
            xscrollcommand=self._on_xscroll,
            yscrollcommand=self._on_yscroll,
        )
        self.body.bind("<Configure>", self._on_body_configure, add="+")
        self.body.bind("<Motion>", self._on_motion, add="+")
        self.body.bind("<Leave>", self._on_leave, add="+")
        self.body.bind("<ButtonPress-1>", self._on_press, add="+")
        self.body.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.body.bind("<Shift-MouseWheel>", self._on_shift_mousewheel, add="+")
        self.body.bind("<Up>", lambda _event: self._move_selection(-1), add="+")
        self.body.bind("<Down>", lambda _event: self._move_selection(1), add="+")
        self.body.bind("<Home>", lambda _event: self._select_index(0), add="+")
        self.body.bind(
            "<End>", lambda _event: self._select_index(len(self._rows) - 1), add="+"
        )
        self.header.bind("<ButtonRelease-1>", self._on_heading_click, add="+")
        self._update_geometry()

    def column(self, name: str, **kwargs) -> None:
        if name not in self._column_options:
            return
        self._column_options[name].update(kwargs)
        self._update_geometry()

    def heading(self, name: str, **kwargs) -> None:
        if name not in self._heading_options:
            return
        self._heading_options[name].update(kwargs)
        self._draw_header()

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if "displaycolumns" in options:
            columns = options.pop("displaycolumns")
            self._display_columns = (
                self._all_columns if tuple(columns) == ("#all",) else tuple(columns)
            )
            self._update_geometry()
        if "yscrollcommand" in options:
            self._yscrollcommand = options.pop("yscrollcommand")
        if "xscrollcommand" in options:
            self._xscrollcommand = options.pop("xscrollcommand")
        return super().configure(**options) if options else None

    config = configure

    def cget(self, key: str):
        if key == "columns":
            return self._all_columns
        if key == "displaycolumns":
            return self._display_columns
        return super().cget(key)

    def bind(self, sequence=None, func=None, add=None):
        if sequence and sequence not in {"<Configure>", "<Map>", "<Destroy>"}:
            return self.body.bind(sequence, func, add)
        return super().bind(sequence, func, add)

    def insert(self, parent, index, *, iid: str, values: tuple[object, ...]):
        del parent, index
        key = str(iid)
        if key in self._row_lookup:
            self.delete(key)
        self._rows.append((key, tuple(values)))
        self._row_lookup[key] = len(self._rows) - 1
        self._update_geometry()
        return key

    def replace_rows(
        self, rows: list[tuple[str, tuple[object, ...]]]
    ) -> None:
        """Replace the model in one pass and redraw the table only once."""
        self._rows = [(str(iid), tuple(values)) for iid, values in rows]
        self._row_lookup = {
            iid: index for index, (iid, _values) in enumerate(self._rows)
        }
        if self._selected_iid not in self._row_lookup:
            self._selected_iid = None
        if self._focused_iid not in self._row_lookup:
            self._focused_iid = None
        self._update_geometry()

    def delete(self, *items: str) -> None:
        if not items:
            return
        removing = {str(item) for item in items}
        if self._rows and len(removing) >= len(self._rows):
            current_view = self.body.yview()
            self._pending_y_fraction = float(current_view[0]) if current_view else 0.0
        self._rows = [row for row in self._rows if row[0] not in removing]
        self._row_lookup = {iid: index for index, (iid, _values) in enumerate(self._rows)}
        if self._selected_iid in removing:
            self._selected_iid = None
        if self._focused_iid in removing:
            self._focused_iid = None
        self._update_geometry()

    def get_children(self, item=None) -> tuple[str, ...]:
        del item
        return tuple(iid for iid, _values in self._rows)

    def exists(self, iid: str) -> bool:
        return str(iid) in self._row_lookup

    def selection(self) -> tuple[str, ...]:
        return (self._selected_iid,) if self._selected_iid is not None else ()

    def view_anchor(self) -> tuple[str | None, float, float]:
        """Remember the first visible row instead of an unstable scroll fraction."""
        current_view = self.body.yview()
        fraction = float(current_view[0]) if current_view else 0.0
        if not self._rows:
            return None, 0.0, fraction
        top = max(0.0, self.body.canvasy(0))
        index = max(0, min(len(self._rows) - 1, int(top // self.ROW_HEIGHT)))
        iid = self._rows[index][0]
        offset = top - index * self.ROW_HEIGHT
        return iid, offset, fraction

    def restore_view_anchor(self, anchor: tuple[str | None, float, float]) -> None:
        iid, offset, fallback_fraction = anchor
        self._pending_y_fraction = None
        if not self._rows:
            self.body.yview_moveto(0.0)
            return
        index = self._row_lookup.get(iid or "")
        if index is None:
            self.body.yview_moveto(max(0.0, min(1.0, fallback_fraction)))
        else:
            total_height = max(1, len(self._rows) * self.ROW_HEIGHT)
            target = max(0.0, index * self.ROW_HEIGHT + offset)
            self.body.yview_moveto(target / total_height)
        self._schedule_redraw()

    def selection_set(self, iid: str) -> None:
        key = str(iid)
        if key not in self._row_lookup:
            return
        changed = key != self._selected_iid
        self._selected_iid = key
        self._focused_iid = key
        self._schedule_redraw()
        if changed:
            self.body.event_generate("<<TreeviewSelect>>", when="tail")

    def focus(self, iid: str | None = None):
        if iid is None:
            return self._focused_iid or ""
        key = str(iid)
        if key in self._row_lookup:
            self._focused_iid = key
        return self._focused_iid or ""

    def see(self, iid: str) -> None:
        index = self._row_lookup.get(str(iid))
        if index is None or not self._rows:
            return
        top = index * self.ROW_HEIGHT
        bottom = top + self.ROW_HEIGHT
        visible_top = self.body.canvasy(0)
        visible_bottom = visible_top + self.body.winfo_height()
        total_height = max(1, len(self._rows) * self.ROW_HEIGHT)
        if top < visible_top:
            self.body.yview_moveto(top / total_height)
        elif bottom > visible_bottom:
            target = max(0, bottom - self.body.winfo_height())
            self.body.yview_moveto(target / total_height)
        self._schedule_redraw()

    def identify_region(self, x: int, y: int) -> str:
        return "cell" if self.identify_row(y) and self.identify_column(x) else "nothing"

    def identify_row(self, y: int) -> str:
        canvas_y = self.body.canvasy(y)
        index = int(canvas_y // self.ROW_HEIGHT)
        if 0 <= index < len(self._rows):
            return self._rows[index][0]
        return ""

    def identify_column(self, x: int) -> str:
        canvas_x = self.body.canvasx(x)
        for index, (_name, left, right) in enumerate(self._column_layout(), start=1):
            if left <= canvas_x < right:
                return f"#{index}"
        return ""

    def yview(self, *args):
        if not args:
            return self.body.yview()
        result = self.body.yview(*args)
        # Tk Canvas may expose an area above the scrollregion when its content
        # is shorter than the viewport.  Keep the first record flush with the
        # top edge instead of allowing an empty overscroll area.
        if self.body.canvasy(0) < 0:
            self.body.yview_moveto(0.0)
        self._schedule_redraw()
        return result

    def xview(self, *args):
        if not args:
            return self.body.xview()
        return self.body.xview(*args)

    def _on_xscroll(self, first: str, last: str) -> None:
        self.header.xview_moveto(float(first))
        if self._xscrollcommand is not None:
            self._xscrollcommand(first, last)

    def _on_yscroll(self, first: str, last: str) -> None:
        if self._yscrollcommand is not None:
            self._yscrollcommand(first, last)
        self._schedule_redraw()

    def _on_mousewheel(self, event: tk.Event) -> str:
        if event.delta:
            self.yview("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_shift_mousewheel(self, event: tk.Event) -> str:
        if event.delta:
            self.xview("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_body_configure(self, _event=None) -> None:
        self._update_geometry()

    def _on_press(self, event: tk.Event) -> None:
        self.body.focus_set()
        iid = self.identify_row(event.y)
        if iid:
            self.selection_set(iid)

    def _select_index(self, index: int) -> str:
        if not self._rows:
            return "break"
        index = max(0, min(index, len(self._rows) - 1))
        iid = self._rows[index][0]
        self.selection_set(iid)
        self.see(iid)
        return "break"

    def _move_selection(self, offset: int) -> str:
        current = self._row_lookup.get(self._selected_iid or "", -1)
        if current < 0:
            current = 0 if offset >= 0 else len(self._rows) - 1
        else:
            current += offset
        return self._select_index(current)

    def _on_motion(self, event: tk.Event) -> None:
        iid = self.identify_row(event.y)
        column_id = self.identify_column(event.x)
        column = ""
        if column_id:
            index = int(column_id[1:]) - 1
            if 0 <= index < len(self._display_columns):
                column = self._display_columns[index]
        hovered = (iid, column) if iid else None
        if hovered != self._hovered:
            self._hovered = hovered
            self.body.configure(
                cursor="hand2" if column in self.ACTION_COLUMNS else "arrow"
            )
            self._schedule_redraw()

    def _on_leave(self, _event=None) -> None:
        if self._hovered is not None:
            self._hovered = None
            self.body.configure(cursor="arrow")
            self._schedule_redraw()

    def _on_heading_click(self, event: tk.Event) -> None:
        canvas_x = self.header.canvasx(event.x)
        for name, left, right in self._column_layout():
            if left <= canvas_x < right:
                command = self._heading_options[name].get("command")
                if callable(command):
                    command()
                return

    def _column_layout(self) -> list[tuple[str, int, int]]:
        widths = {
            name: max(
                int(self._column_options[name].get("minwidth", 40)),
                int(self._column_options[name].get("width", 100)),
            )
            for name in self._display_columns
        }
        viewport_width = self.body.winfo_width() if hasattr(self, "body") else 1
        if self._autofit_title and "title" in widths:
            minimum_title_width = max(
                int(self._column_options["title"].get("minwidth", 40)),
                self._row_font.measure("W" * 10) + 20,
            )
            fixed_width = sum(
                width for name, width in widths.items() if name != "title"
            )
            widths["title"] = max(
                minimum_title_width,
                viewport_width - fixed_width,
            )
        extra = max(0, viewport_width - sum(widths.values()))
        stretch_columns = [
            name
            for name in self._display_columns
            if bool(self._column_options[name].get("stretch", True))
        ]
        if extra and stretch_columns:
            share, remainder = divmod(extra, len(stretch_columns))
            for index, name in enumerate(stretch_columns):
                widths[name] += share + (1 if index < remainder else 0)
        result: list[tuple[str, int, int]] = []
        left = 0
        for name in self._display_columns:
            width = widths[name]
            result.append((name, left, left + width))
            left += width
        return result

    def _update_geometry(self) -> None:
        if not hasattr(self, "body"):
            return
        total_width = max(1, sum(right - left for _name, left, right in self._column_layout()))
        total_height = max(
            1,
            self.body.winfo_height(),
            len(self._rows) * self.ROW_HEIGHT,
        )
        self.body.configure(scrollregion=(0, 0, total_width, total_height))
        if not self._rows or self.body.canvasy(0) < 0:
            self.body.yview_moveto(0.0)
        self.header.configure(scrollregion=(0, 0, total_width, self.HEADER_HEIGHT))
        self._draw_header()
        self._schedule_redraw()

    def refresh_style(self) -> None:
        self._draw_header()
        self._schedule_redraw()

    def set_title_autofit(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._autofit_title:
            return
        self._autofit_title = enabled
        self._update_geometry()

    def fit_hotkey_column(self, values, minimum: int = 145) -> None:
        """Keep every assigned shortcut visible without ellipsis."""
        heading = str(self._heading_options.get("hotkey", {}).get("text") or "")
        labels = [str(value) for value in values]
        required = max(
            minimum,
            self._header_font.measure(heading) + 26,
            *(self._hotkey_font.measure(label) + 28 for label in labels),
        )
        options = self._column_options.get("hotkey")
        if options is None:
            return
        if int(options.get("width", 0)) == required and int(
            options.get("minwidth", 0)
        ) == required:
            return
        options["width"] = required
        options["minwidth"] = required
        self._update_geometry()

    def fit_source_column(self, values, minimum: int = 190) -> None:
        """Show complete original file names whenever that column is visible."""
        heading = str(self._heading_options.get("source", {}).get("text") or "")
        labels = [str(value) for value in values]
        required = max(
            minimum,
            self._header_font.measure(heading) + 26,
            *(self._row_font.measure(label) + 20 for label in labels),
        )
        options = self._column_options.get("source")
        if options is None:
            return
        if int(options.get("width", 0)) == required and int(
            options.get("minwidth", 0)
        ) == required:
            return
        options["width"] = required
        options["minwidth"] = required
        self._update_geometry()

    def _draw_header(self) -> None:
        if not hasattr(self, "header"):
            return
        self.header.delete("all")
        layout = self._column_layout()
        total_width = max(1, layout[-1][2] if layout else 1)
        self.header.create_rectangle(
            0, 0, total_width, self.HEADER_HEIGHT, fill=BG, outline=""
        )
        for index, (name, left, right) in enumerate(layout):
            options = self._heading_options[name]
            center = (left + right) / 2
            image = options.get("image")
            text = str(options.get("text") or "")
            if image:
                self.header.create_image(center, self.HEADER_HEIGHT / 2, image=image)
            else:
                self.header.create_text(
                    center,
                    self.HEADER_HEIGHT / 2,
                    text=text,
                    fill=TEXT,
                    font=self._header_font,
                    anchor="center",
                )

    def _schedule_redraw(self) -> None:
        if not hasattr(self, "body") or self._redraw_job is not None:
            return
        self._redraw_job = self.after_idle(self._draw_rows)

    def _fit_text(self, value: object, width: int, font: tkfont.Font) -> str:
        text = str(value)
        available = max(4, width - 20)
        if font.measure(text) <= available:
            return text
        suffix = "…"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if font.measure(text[:middle] + suffix) <= available:
                low = middle
            else:
                high = middle - 1
        return text[:low] + suffix

    def _action_image(
        self,
        symbol: str,
        foreground: str,
        background: str | None = None,
        size: int = 30,
    ) -> ImageTk.PhotoImage:
        key = (symbol, foreground, background, size)
        image = self._action_image_cache.get(key)
        if image is None:
            image = render_media_control_image(
                size,
                symbol,
                foreground,
                background,
            )
            self._action_image_cache[key] = image
        return image

    def _draw_rows(self) -> None:
        self._redraw_job = None
        if self._pending_y_fraction is not None and self._rows:
            fraction = self._pending_y_fraction
            self._pending_y_fraction = None
            self.body.yview_moveto(fraction)
        self.body.delete("row")
        if not self._rows:
            return
        top = max(0.0, self.body.canvasy(0))
        bottom = top + max(1, self.body.winfo_height())
        first = max(0, int(top // self.ROW_HEIGHT) - 1)
        last = min(len(self._rows), int(bottom // self.ROW_HEIGHT) + 2)
        layout = self._column_layout()
        total_width = max(1, layout[-1][2] if layout else 1)
        value_indexes = {name: index for index, name in enumerate(self._all_columns)}
        for row_index in range(first, last):
            iid, values = self._rows[row_index]
            y1 = row_index * self.ROW_HEIGHT
            y2 = y1 + self.ROW_HEIGHT
            selected = iid == self._selected_iid
            hovered_row = self._hovered is not None and self._hovered[0] == iid
            if selected:
                fill = ACCENT
            elif hovered_row:
                fill = "#334155"
            else:
                fill = BG
            self.body.create_rectangle(
                0,
                y1,
                total_width,
                y2,
                fill=fill,
                outline="",
                tags="row",
            )
            for name, left, right in layout:
                value_index = value_indexes[name]
                value = values[value_index] if value_index < len(values) else ""
                width = right - left
                center_x = (left + right) / 2
                center_y = (y1 + y2) / 2
                is_action_hover = self._hovered == (iid, name)
                if name in {"speaker_play", "favorite", "mic_play", "mic_stop"}:
                    symbol = str(value)
                    symbol_color = (
                        "#facc15"
                        if name == "favorite" and symbol == "★"
                        else TEXT
                    )
                    hover_color = (
                        (ACCENT_HOVER if selected else "#475569")
                        if is_action_hover
                        else None
                    )
                    if symbol in {"▶", "■", "Ⅱ", "‖"}:
                        image = self._action_image(
                            symbol,
                            symbol_color,
                            hover_color,
                        )
                        self.body.create_image(
                            center_x,
                            center_y,
                            image=image,
                            anchor="center",
                            tags="row",
                        )
                    else:
                        if hover_color is not None:
                            background = self._action_image(
                                "",
                                symbol_color,
                                hover_color,
                            )
                            self.body.create_image(
                                center_x,
                                center_y,
                                image=background,
                                anchor="center",
                                tags="row",
                            )
                        self.body.create_text(
                            center_x,
                            center_y - 2,
                            text=symbol,
                            fill=symbol_color,
                            font=("Segoe UI Semibold", 10),
                            anchor="center",
                            tags="row",
                        )
                elif name == "hotkey":
                    label = str(value)
                    badge_width = max(34, width - 12)
                    self.body.create_rectangle(
                        center_x - badge_width / 2,
                        center_y - 11,
                        center_x + badge_width / 2,
                        center_y + 11,
                        fill=ACCENT_HOVER if is_action_hover else "#0b1220",
                        outline="#64748b",
                        width=1,
                        tags="row",
                    )
                    self.body.create_text(
                        center_x,
                        center_y - 2,
                        text=label,
                        fill=TEXT if label != "—" else MUTED,
                        font=self._hotkey_font,
                        anchor="center",
                        tags="row",
                    )
                else:
                    anchor = str(self._column_options[name].get("anchor", "w"))
                    if anchor == "center":
                        x, canvas_anchor = center_x, "center"
                    else:
                        x, canvas_anchor = left + 12, "w"
                    self.body.create_text(
                        x,
                        center_y,
                        text=self._fit_text(value, width, self._row_font),
                        fill=TEXT,
                        font=self._row_font,
                        anchor=canvas_anchor,
                        tags="row",
                    )


class AudioLibraryApp:
    def __init__(self, root_dir: Path, start_hidden: bool = False) -> None:
        self.root_dir = root_dir
        self.app_settings_path = root_dir / "data" / "app_settings.json"
        self.app_settings = self._load_app_settings()
        self._apply_saved_accent()
        self.start_hidden = start_hidden
        self.store = LibraryStore(root_dir)
        self.player = WindowsMediaPlayer()
        self.virtual_microphone = VirtualMicrophoneBridge()
        self.player.set_normalization_enabled(
            bool(self.app_settings["normalize_loudness"])
        )
        self.virtual_microphone.set_normalization_enabled(
            bool(self.app_settings["normalize_loudness"])
        )
        self.player.set_output_device(str(self.app_settings["output_device"]))
        self.virtual_microphone.configure(
            latency_profile=str(self.app_settings["latency_profile"]),
            local_monitoring=bool(self.app_settings["local_monitoring"]),
            monitor_output_name=str(self.app_settings["output_device"]),
        )
        self.microphone_routing = WindowsMicrophoneRouting(root_dir)
        self._audio_shutdown_done = False
        self._volume_save_job = None
        self._session_wndproc = None
        self._previous_wndproc = None
        self.root = tk.Tk()
        self.root.title("AudioLib — локальна бібліотека аудіозаписів")
        window_width = DEFAULT_WINDOW_WIDTH
        window_height = DEFAULT_WINDOW_HEIGHT
        window_x = max(0, (self.root.winfo_screenwidth() - window_width) // 2)
        window_y = max(0, (self.root.winfo_screenheight() - window_height) // 2)
        default_geometry = f"{window_width}x{window_height}+{window_x}+{window_y}"
        initial_geometry = self._validated_window_geometry(
            self.app_settings.get("window_geometry"),
            default_geometry,
        )
        self.root.geometry(initial_geometry)
        self.root.minsize(920, 620)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.handle_window_close)
        self._window_mode = "normal"
        self._restore_geometry = self.root.geometry()
        self._drag_offset = (0, 0)
        self._resize_data: tuple[str, int, int, int, int, int, int] | None = None
        self._restore_override_after_map = False
        self._taskbar_style_initialized = False
        self.resize_grips: list[tk.Frame] = []
        self.app_icon: ImageTk.PhotoImage | None = None
        self.tray_icon = None
        self.tray_window: tk.Toplevel | None = None
        self.tray_window_items: tk.Frame | None = None
        self.tray_window_canvas: tk.Canvas | None = None
        self.tray_stop_icon: ImageTk.PhotoImage | None = None
        self._hotkey_events: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._hotkey_reload_results: queue.SimpleQueue[tuple[object, dict[int, str]]] = (
            queue.SimpleQueue()
        )
        self._audio_load_results: queue.SimpleQueue[tuple[object, ...]] = (
            queue.SimpleQueue()
        )
        self._virtual_start_results: queue.SimpleQueue[tuple[object, ...]] = (
            queue.SimpleQueue()
        )
        self._library_task_results: queue.SimpleQueue[tuple[object, ...]] = (
            queue.SimpleQueue()
        )
        self._library_task_in_progress = False
        self._virtual_start_in_progress = False
        self._virtual_start_callback = None
        self._pending_microphone_playback: tuple[object, ...] | None = None
        self._closing_event = threading.Event()
        self._speaker_load_serial = 0
        self._microphone_load_serial = 0
        self._speaker_load_track_id: int | None = None
        self._microphone_load_track_id: int | None = None
        self.hotkey_manager = GlobalHotkeyManager(self._hotkey_events.put)
        self._hotkey_capture_active = False
        self._notice_widget: tk.Canvas | None = None
        self._notice_hide_job: str | None = None
        self._set_app_icon()

        self.selected_person_id: int | None = None
        self.current_track_id: int | None = None
        self.virtual_track_id: int | None = None
        self._last_speaker_table_state: tuple[int | None, str] = (None, "stopped")
        self.tray_favorites: list[tuple[int, str]] = []
        self.current_track_length_ms = 0
        self.photo_refs: list[ImageTk.PhotoImage] = []
        self._photo_cache: dict[tuple[object, ...], ImageTk.PhotoImage] = {}
        self._person_card_widgets: dict[int, tuple[tk.Widget, ...]] = {}
        self.seeking = False
        self.volume_value = int(self.app_settings["volume"])
        self.person_query = tk.StringVar()
        self.track_query = tk.StringVar()
        self.now_playing = tk.StringVar(value="Оберіть запис")
        self.time_text = tk.StringVar(value="0:00 / 0:00")
        self.status_text = tk.StringVar(value="Готово")
        self.track_sort_column = "title"
        self.track_sort_descending = False
        self.source_column_visible = False
        self.track_heading_labels = {
            "number": "",
            "title": "Назва",
            "speaker_play": "",
            "favorite": "",
            "hotkey": "Гаряча клавіша",
            "source": "Початковий файл",
            "duration": "Тривалість",
            "mic_play": "",
            "mic_stop": "",
        }
        self.track_heading_icons = {
            "speaker_play": self._make_track_heading_icon("speaker", centered=True),
            "mic_play": self._make_track_heading_icon("microphone", centered=True),
        }
        self.tray_control_icons = {
            "speaker_play": self._make_track_heading_icon("speaker", centered=True),
            "mic_play": self._make_track_heading_icon("microphone", centered=True),
        }
        self.header_action_icons = {
            "settings": self._make_header_action_icon("settings"),
        }
        self.button_action_icons = {
            "random": self._make_header_action_icon("dice"),
        }
        self._configure_styles()
        self._build_ui()
        self._create_resize_grips()
        self.root.bind("<Map>", self._on_root_map, add="+")
        self.root.after(20, self._ensure_taskbar_presence)
        self.root.after(250, self._ensure_taskbar_presence)
        self.person_query.trace_add("write", self._on_person_query_changed)
        self.track_query.trace_add("write", self._on_track_query_changed)
        startup_person_id = FAVORITES_PERSON_ID
        if self.app_settings["startup_view"] == "last":
            last_person_id = self.app_settings.get("last_person_id")
            if isinstance(last_person_id, int) and self.store.get_person(last_person_id):
                startup_person_id = last_person_id
        self.select_person(startup_person_id)
        self._refresh_tray_favorites()
        self._reload_global_hotkeys()
        self.refresh_tracks()
        self.root.after(10, self._poll_global_hotkeys)
        self.root.after(10, self._poll_audio_load_results)
        self.root.after(10, self._poll_virtual_start_results)
        self.root.after(10, self._poll_library_task_results)
        self._refresh_legacy_metadata_async()
        self._start_tray_icon()
        if self.start_hidden:
            self.root.withdraw()
        if self.app_settings["auto_virtual_microphone"]:
            self.root.after_idle(self._start_virtual_microphone_background)
        self.root.after(400, self._update_progress)
        self._install_session_end_handler()
        atexit.register(self._shutdown_audio_routing)

    def _write_audio_routing_log(self, message: str) -> None:
        try:
            log_path = self.root_dir / "data" / "audio_routing.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass

    def _load_app_settings(self) -> dict[str, object]:
        defaults: dict[str, object] = {
            "volume": 500,
            "autostart_enabled": False,
            "start_minimized": True,
            "auto_virtual_microphone": False,
            "close_action": "tray",
            "startup_view": "favorites",
            "language": "uk",
            "stop_hotkey": DEFAULT_STOP_HOTKEY,
            "last_person_id": None,
            "output_device": "",
            "normalize_loudness": True,
            "local_monitoring": True,
            "latency_profile": "balanced",
            "accent_color": "Фіолетовий",
            "photo_size": 92,
            "list_density": "comfortable",
            "window_geometry": None,
        }
        try:
            payload = json.loads(self.app_settings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                defaults.update(
                    {key: payload[key] for key in defaults.keys() if key in payload}
                )
        except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            defaults["volume"] = max(0, min(1000, int(defaults["volume"])))
        except (ValueError, TypeError):
            defaults["volume"] = 500
        for key, fallback in (
            ("autostart_enabled", False),
            ("start_minimized", True),
            ("auto_virtual_microphone", False),
            ("normalize_loudness", True),
            ("local_monitoring", True),
        ):
            if not isinstance(defaults[key], bool):
                defaults[key] = fallback
        if defaults["close_action"] not in {"tray", "exit"}:
            defaults["close_action"] = "tray"
        if defaults["startup_view"] not in {"favorites", "last"}:
            defaults["startup_view"] = "favorites"
        if defaults["language"] not in {"uk"}:
            defaults["language"] = "uk"
        try:
            defaults["stop_hotkey"] = parse_hotkey(
                str(defaults["stop_hotkey"])
            ).canonical
        except (TypeError, ValueError):
            defaults["stop_hotkey"] = DEFAULT_STOP_HOTKEY
        if not isinstance(defaults["last_person_id"], int):
            defaults["last_person_id"] = None
        if not isinstance(defaults["output_device"], str):
            defaults["output_device"] = ""
        if defaults["latency_profile"] not in {"low", "balanced", "stable"}:
            defaults["latency_profile"] = "balanced"
        if defaults["accent_color"] not in ACCENT_PRESETS:
            defaults["accent_color"] = "Фіолетовий"
        if defaults["photo_size"] not in {72, 92, 108}:
            defaults["photo_size"] = 92
        if defaults["list_density"] not in {"compact", "comfortable", "spacious"}:
            defaults["list_density"] = "comfortable"
        if not isinstance(defaults["window_geometry"], str):
            defaults["window_geometry"] = None
        return defaults

    def _validated_window_geometry(
        self, value: object, fallback: str
    ) -> str:
        candidate = str(value) if isinstance(value, str) else fallback
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", candidate)
        if match is None:
            match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", fallback)
        if match is None:
            return fallback
        width, height, x, y = (int(part) for part in match.groups())
        try:
            user32 = ctypes.windll.user32
            left = int(user32.GetSystemMetrics(76))
            top = int(user32.GetSystemMetrics(77))
            screen_width = max(1, int(user32.GetSystemMetrics(78)))
            screen_height = max(1, int(user32.GetSystemMetrics(79)))
            if screen_width <= 1 or screen_height <= 1:
                raise OSError
        except (AttributeError, OSError):
            left = 0
            top = 0
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
        right = left + screen_width
        bottom = top + screen_height
        width = max(920, min(width, screen_width))
        height = max(620, min(height, screen_height))
        # Keep at least part of the title bar reachable after monitor changes.
        x = min(max(x, left - width + 120), right - 120)
        y = min(max(y, top), bottom - 40)
        return f"{width}x{height}{x:+d}{y:+d}"

    def _default_window_geometry(self) -> str:
        width = DEFAULT_WINDOW_WIDTH
        height = DEFAULT_WINDOW_HEIGHT
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        return self._validated_window_geometry(
            f"{width}x{height}{x:+d}{y:+d}",
            f"{width}x{height}+0+0",
        )

    def _save_window_geometry(self) -> None:
        try:
            geometry = (
                self.root.geometry()
                if self._window_mode == "normal"
                else self._restore_geometry
            )
            self.app_settings["window_geometry"] = self._validated_window_geometry(
                geometry,
                self._default_window_geometry(),
            )
        except (AttributeError, tk.TclError):
            pass

    def _apply_saved_accent(self) -> None:
        global ACCENT, ACCENT_HOVER
        name = str(self.app_settings.get("accent_color", "Фіолетовий"))
        ACCENT, ACCENT_HOVER = ACCENT_PRESETS.get(name, ACCENT_PRESETS["Фіолетовий"])

    def _save_volume_setting(self) -> None:
        try:
            self.app_settings["volume"] = self.volume_value
            self.app_settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.app_settings_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.app_settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.app_settings_path)
        except OSError:
            pass
        self._volume_save_job = None

    def _schedule_volume_save(self) -> None:
        if self._volume_save_job is not None:
            try:
                self.root.after_cancel(self._volume_save_job)
            except tk.TclError:
                pass
        self._volume_save_job = self.root.after(300, self._save_volume_setting)

    def _install_session_end_handler(self) -> None:
        """Restore audio when Windows logs off, restarts, or shuts down."""
        if sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            hwnd = self._native_window_handle()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            )
            user32.SetWindowLongPtrW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
            ]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            ]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            wm_query_end_session = 0x0011
            wm_end_session = 0x0016
            wm_syscommand = 0x0112
            sc_minimize = 0xF020
            sw_minimize = 6

            def session_wndproc(window, message, wparam, lparam):
                if message == wm_query_end_session:
                    self._shutdown_audio_routing()
                    return 1
                if message == wm_end_session and wparam:
                    self._shutdown_audio_routing()
                if message == wm_syscommand and (int(wparam) & 0xFFF0) == sc_minimize:
                    user32.ShowWindow(window, sw_minimize)
                    return 0
                return user32.CallWindowProcW(
                    self._previous_wndproc, window, message, wparam, lparam
                )

            self._session_wndproc = wndproc_type(session_wndproc)
            ctypes.set_last_error(0)
            previous = user32.SetWindowLongPtrW(
                hwnd, -4, ctypes.cast(self._session_wndproc, ctypes.c_void_p)
            )
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            self._previous_wndproc = previous
        except Exception as error:
            self._session_wndproc = None
            self._previous_wndproc = None
            self._write_audio_routing_log(f"SESSION_HANDLER_ERROR {error}")

    def _set_app_icon(self) -> None:
        asset_roots = [self.root_dir / "assets"]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            asset_roots.insert(0, Path(sys._MEIPASS) / "assets")
        for assets in asset_roots:
            png_path = assets / "audiolib_icon.png"
            ico_path = assets / "audiolib_icon.ico"
            try:
                if png_path.exists():
                    self.app_icon = ImageTk.PhotoImage(file=png_path)
                    self.root.iconphoto(True, self.app_icon)
                if ico_path.exists():
                    self.root.iconbitmap(str(ico_path))
                if self.app_icon is not None or ico_path.exists():
                    return
            except (OSError, tk.TclError):
                continue

    def _icon_image_path(self) -> Path | None:
        asset_roots = [self.root_dir / "assets"]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            asset_roots.insert(0, Path(sys._MEIPASS) / "assets")
        return next(
            (assets / "audiolib_icon.png" for assets in asset_roots if (assets / "audiolib_icon.png").exists()),
            None,
        )

    def _make_track_heading_icon(
        self, kind: str, *, centered: bool = False
    ) -> ImageTk.PhotoImage:
        scale = 4
        # У Windows-темы Treeview изображение в заголовке визуально смещается
        # вправо из-за стандартного layout. Небольшое прозрачное поле справа
        # компенсирует этот отступ и совмещает значок с центром ячеек.
        image = Image.new("RGBA", (39 * scale, 20 * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = (248, 250, 252, 255)

        if kind == "speaker":
            draw.polygon(
                [(3 * scale, 8 * scale), (7 * scale, 8 * scale),
                 (12 * scale, 4 * scale), (12 * scale, 16 * scale),
                 (7 * scale, 12 * scale), (3 * scale, 12 * scale)],
                fill=color,
            )
            draw.arc(
                (10 * scale, 5 * scale, 19 * scale, 15 * scale),
                start=-55,
                end=55,
                fill=color,
                width=2 * scale,
            )
            draw.arc(
                (10 * scale, 2 * scale, 23 * scale, 18 * scale),
                start=-55,
                end=55,
                fill=color,
                width=2 * scale,
            )
        else:
            draw.rounded_rectangle(
                (8 * scale, 2 * scale, 16 * scale, 12 * scale),
                radius=4 * scale,
                outline=color,
                width=2 * scale,
            )
            draw.arc(
                (5 * scale, 6 * scale, 19 * scale, 17 * scale),
                start=0,
                end=180,
                fill=color,
                width=2 * scale,
            )
            draw.line(
                (12 * scale, 16 * scale, 12 * scale, 19 * scale),
                fill=color,
                width=2 * scale,
            )
            draw.line(
                (8 * scale, 19 * scale, 16 * scale, 19 * scale),
                fill=color,
                width=2 * scale,
            )

        image = image.resize((39, 20), Image.Resampling.LANCZOS)
        if centered:
            bounds = image.getbbox()
            glyph = image.crop(bounds) if bounds else image
            target = Image.new("RGBA", (20, 18), (0, 0, 0, 0))
            target.alpha_composite(
                glyph,
                (
                    (target.width - glyph.width) // 2,
                    (target.height - glyph.height) // 2,
                ),
            )
            image = target
        return ImageTk.PhotoImage(image)

    def _make_header_action_icon(self, kind: str) -> ImageTk.PhotoImage:
        scale = 4
        size = 18
        image = Image.new(
            "RGBA", (size * scale, size * scale), (0, 0, 0, 0)
        )
        draw = ImageDraw.Draw(image)
        color = (248, 250, 252, 255)

        if kind == "microphone":
            draw.rounded_rectangle(
                (6 * scale, 1 * scale, 12 * scale, 11 * scale),
                radius=3 * scale,
                outline=color,
                width=2 * scale,
            )
            draw.arc(
                (3 * scale, 5 * scale, 15 * scale, 16 * scale),
                start=0,
                end=180,
                fill=color,
                width=2 * scale,
            )
            draw.line(
                (9 * scale, 15 * scale, 9 * scale, 17 * scale),
                fill=color,
                width=2 * scale,
            )
            draw.line(
                (6 * scale, 17 * scale, 12 * scale, 17 * scale),
                fill=color,
                width=2 * scale,
            )
        elif kind == "dice":
            draw.rounded_rectangle(
                (2 * scale, 2 * scale, 16 * scale, 16 * scale),
                radius=3 * scale,
                outline=color,
                width=2 * scale,
            )
            pip_radius = 1.15 * scale
            for x, y in ((6, 6), (12, 6), (9, 9), (6, 12), (12, 12)):
                draw.ellipse(
                    (
                        x * scale - pip_radius,
                        y * scale - pip_radius,
                        x * scale + pip_radius,
                        y * scale + pip_radius,
                    ),
                    fill=color,
                )
        else:
            center = 9 * scale
            for step in range(8):
                angle = math.radians(step * 45)
                start_radius = 4.8 * scale
                end_radius = 7.6 * scale
                draw.line(
                    (
                        center + math.cos(angle) * start_radius,
                        center + math.sin(angle) * start_radius,
                        center + math.cos(angle) * end_radius,
                        center + math.sin(angle) * end_radius,
                    ),
                    fill=color,
                    width=3 * scale,
                )
            draw.ellipse(
                (4 * scale, 4 * scale, 14 * scale, 14 * scale),
                fill=color,
            )
            draw.ellipse(
                (7 * scale, 7 * scale, 11 * scale, 11 * scale),
                fill=(0, 0, 0, 0),
            )

        image = image.resize((size, size), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

    def _make_volume_icon(self) -> ImageTk.PhotoImage:
        scale = 4
        image = Image.new("RGBA", (20 * scale, 18 * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = (248, 250, 252, 255)
        draw.polygon(
            [(1 * scale, 7 * scale), (5 * scale, 7 * scale),
             (10 * scale, 3 * scale), (10 * scale, 15 * scale),
             (5 * scale, 11 * scale), (1 * scale, 11 * scale)],
            fill=color,
        )
        draw.arc(
            (8 * scale, 4 * scale, 17 * scale, 14 * scale),
            start=-55,
            end=55,
            fill=color,
            width=2 * scale,
        )
        draw.arc(
            (8 * scale, 1 * scale, 20 * scale, 17 * scale),
            start=-55,
            end=55,
            fill=color,
            width=2 * scale,
        )
        image = image.resize((20, 18), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

    def _start_tray_icon(self) -> None:
        icon_path = self._icon_image_path()
        if AudioLibTrayIcon is None or icon_path is None:
            return
        try:
            with Image.open(icon_path) as source:
                tray_image = source.convert("RGBA").copy()
            self.tray_icon = AudioLibTrayIcon(
                "AudioLib",
                tray_image,
                "AudioLib",
                menu=None,
                on_single_click=lambda: self.root.after(0, self._show_tray_window),
                on_double_click=lambda: self.root.after(0, self.show_window),
            )
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def _refresh_tray_favorites(self) -> None:
        cached: list[tuple[int, str]] = []
        for track in self.store.list_favorites():
            person = self.store.get_person(track.person_id)
            label = f"{track.title} — {person.name}" if person else track.title
            cached.append((track.id, label))
        self.tray_favorites = cached
        if self.tray_window is not None and self.tray_window.winfo_exists():
            self._populate_tray_window()

    def _build_tray_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg="#334155")
        window.protocol("WM_DELETE_WINDOW", self._hide_tray_window)
        self.tray_window = window

        panel = tk.Frame(window, bg=BG, padx=12, pady=10)
        panel.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(panel, bg=BG)
        header.pack(fill="x", pady=(0, 8))
        tk.Label(
            header,
            text="★ Favorites",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left")
        close_button = tk.Button(
            header,
            text="×",
            command=self._hide_tray_window,
            bg=BG,
            fg=MUTED,
            activebackground="#334155",
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=8,
            pady=0,
            cursor="hand2",
            font=("Segoe UI Semibold", 15),
        )
        close_button.pack(side="right")
        close_button.bind("<Enter>", lambda _event: close_button.configure(bg="#334155", fg=TEXT))
        close_button.bind("<Leave>", lambda _event: close_button.configure(bg=BG, fg=MUTED))

        list_area = tk.Frame(panel, bg=BG)
        list_area.pack(fill="both", expand=True)
        self.tray_window_canvas = tk.Canvas(
            list_area,
            bg=BG,
            highlightthickness=0,
            bd=0,
            width=390,
        )
        scrollbar = ttk.Scrollbar(
            list_area,
            orient="vertical",
            command=self.tray_window_canvas.yview,
            style="AudioLib.Vertical.TScrollbar",
        )
        self.tray_window_items = tk.Frame(self.tray_window_canvas, bg=BG)
        items_window = self.tray_window_canvas.create_window(
            (0, 0), window=self.tray_window_items, anchor="nw"
        )
        self.tray_window_canvas.configure(yscrollcommand=scrollbar.set)
        self.tray_window_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tray_window_items.bind(
            "<Configure>",
            lambda _event: self.tray_window_canvas.configure(
                scrollregion=self.tray_window_canvas.bbox("all")
            ),
        )
        self.tray_window_canvas.bind(
            "<Configure>",
            lambda event: self.tray_window_canvas.itemconfigure(
                items_window, width=event.width
            ),
        )
        self.tray_window_canvas.bind(
            "<MouseWheel>",
            lambda event: self.tray_window_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        stop_icon = Image.new("RGBA", (20, 18), (0, 0, 0, 0))
        stop_draw = ImageDraw.Draw(stop_icon)
        stop_draw.rectangle((7, 6, 13, 12), fill=(248, 250, 252, 255))
        self.tray_stop_icon = ImageTk.PhotoImage(stop_icon)

        footer = tk.Frame(panel, bg=BG)
        footer.pack(fill="x", pady=(10, 0))
        self._button(footer, "Вийти", self._close, DANGER).pack(side="right")

        window.bind("<FocusOut>", self._schedule_tray_window_focus_check, add="+")
        self._populate_tray_window()

    def _populate_tray_window(self) -> None:
        if self.tray_window_items is None or not self.tray_window_items.winfo_exists():
            return
        for child in self.tray_window_items.winfo_children():
            child.destroy()
        if not self.tray_favorites:
            tk.Label(
                self.tray_window_items,
                text="Немає вибраних записів",
                bg=BG,
                fg=MUTED,
                pady=24,
                font=("Segoe UI", 10),
            ).pack(fill="x")
        else:
            for track_id, label in self.tray_favorites:
                item = tk.Frame(
                    self.tray_window_items,
                    bg=CARD,
                    padx=12,
                    pady=5,
                )
                item.pack(fill="x", pady=2)
                title = tk.Label(
                    item,
                    text=label,
                    bg=CARD,
                    fg=TEXT,
                    anchor="w",
                    font=("Segoe UI", 10),
                )
                title.pack(side="left", fill="x", expand=True)

                def tray_control(
                    parent: tk.Misc,
                    *,
                    command,
                    image: ImageTk.PhotoImage | None = None,
                    text: str = "",
                ) -> tk.Button:
                    button = tk.Button(
                        parent,
                        text=text,
                        image=image,
                        command=command,
                        bg=CARD,
                        fg=TEXT,
                        activebackground="#475569",
                        activeforeground=TEXT,
                        relief="flat",
                        bd=0,
                        highlightthickness=0,
                        width=30,
                        height=26,
                        padx=0,
                        pady=0,
                        anchor="center",
                        cursor="hand2",
                        font=("Segoe UI Semibold", 10),
                    )
                    button.bind(
                        "<Enter>",
                        lambda _event, control=button: control.configure(bg="#475569"),
                    )
                    button.bind(
                        "<Leave>",
                        lambda _event, control=button: control.configure(bg=CARD),
                    )
                    return button

                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.stop_track_audio, tid
                    ),
                    image=self.tray_stop_icon,
                ).pack(side="right", padx=(3, 0))
                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.toggle_track_microphone, tid
                    ),
                    image=self.tray_control_icons["mic_play"],
                ).pack(side="right", padx=(3, 0))
                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.toggle_track_speaker, tid
                    ),
                    image=self.tray_control_icons["speaker_play"],
                ).pack(side="right", padx=(3, 0))

                for widget in (item, title, *item.winfo_children()):
                    widget.bind(
                        "<MouseWheel>",
                        lambda event: self.tray_window_canvas.yview_scroll(
                            -1 if event.delta > 0 else 1, "units"
                        ),
                    )
        if self.tray_window_canvas is not None:
            row_count = max(1, len(self.tray_favorites))
            self.tray_window_canvas.configure(height=min(430, 48 + row_count * 40))
            self.tray_window.update_idletasks()
            self.tray_window_canvas.configure(
                scrollregion=self.tray_window_canvas.bbox("all")
            )

    def _run_tray_track_action(self, action, track_id: int) -> None:
        action(track_id)
        if self.tray_window is not None and self.tray_window.winfo_viewable():
            self.tray_window.lift()
            self.tray_window.focus_force()

    def _show_tray_window(self) -> None:
        if self.tray_window is None or not self.tray_window.winfo_exists():
            self._build_tray_window()
        else:
            self._populate_tray_window()
        if self.tray_window is None:
            return
        self.tray_window.deiconify()
        self.tray_window.update_idletasks()
        width = max(420, self.tray_window.winfo_reqwidth())
        height = self.tray_window.winfo_reqheight()
        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        left, top, right, bottom = self._work_area()
        x = min(max(left + 8, pointer_x - width + 18), right - width - 8)
        y = min(max(top + 8, pointer_y - height - 12), bottom - height - 8)
        self.tray_window.geometry(f"{width}x{height}+{x}+{y}")
        self.tray_window.lift()
        self.tray_window.focus_force()

    def _hide_tray_window(self) -> None:
        if self.tray_window is not None and self.tray_window.winfo_exists():
            self.tray_window.withdraw()

    def _schedule_tray_window_focus_check(self, _event=None) -> None:
        self.root.after(120, self._hide_tray_window_if_unfocused)

    def _hide_tray_window_if_unfocused(self) -> None:
        if self.tray_window is None or not self.tray_window.winfo_viewable():
            return
        focused = self.root.focus_get()
        if focused is None or not str(focused).startswith(str(self.tray_window)):
            self.tray_window.withdraw()

    def show_window(self) -> None:
        self._hide_tray_window()
        self.root.deiconify()
        self.root.state("normal")
        self.root.overrideredirect(True)
        self.root.after(20, self._ensure_taskbar_presence)
        try:
            handle = self._native_window_handle()
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(handle), 9)
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(handle))
        except (AttributeError, OSError):
            pass
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self) -> None:
        if self.tray_icon is None:
            self._minimize_window()
            return
        self.root.withdraw()
        self.status_text.set("AudioLib працює у фоні")

    def handle_window_close(self) -> None:
        if self.app_settings.get("close_action") == "exit":
            self._close()
        else:
            self.hide_to_tray()

    def _native_window_handle(self) -> int:
        handle = int(self.root.winfo_id())
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [ctypes.c_void_p]
        get_parent.restype = ctypes.c_void_p
        parent = int(get_parent(ctypes.c_void_p(handle)) or 0)
        return parent or handle

    def _ensure_taskbar_presence(self) -> None:
        if not self.root.winfo_exists():
            return
        try:
            user32 = ctypes.windll.user32
            handle = self._native_window_handle()
            GWL_EXSTYLE = -20
            GWL_STYLE = -16
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            WS_MINIMIZEBOX = 0x00020000
            WS_SYSMENU = 0x00080000
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            SW_HIDE = 0
            SW_SHOW = 5
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            style = user32.GetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            user32.SetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE, style)
            # Tk removes the standard caption for our custom title bar. Keep
            # the native minimize capability, however, so Windows treats a
            # click on the active taskbar button like a normal app window and
            # sends SC_MINIMIZE instead of immediately reactivating it.
            window_style = user32.GetWindowLongW(
                ctypes.c_void_p(handle), GWL_STYLE
            )
            window_style |= WS_SYSMENU | WS_MINIMIZEBOX
            user32.SetWindowLongW(
                ctypes.c_void_p(handle), GWL_STYLE, window_style
            )
            user32.SetWindowPos(
                ctypes.c_void_p(handle),
                None,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            if not self._taskbar_style_initialized and self.root.winfo_viewable():
                user32.ShowWindow(ctypes.c_void_p(handle), SW_HIDE)
                user32.ShowWindow(ctypes.c_void_p(handle), SW_SHOW)
            self._taskbar_style_initialized = True
        except (AttributeError, OSError):
            pass

    def _on_root_map(self, _event=None) -> None:
        if self._restore_override_after_map:
            self._restore_override_after_map = False
            self.root.after_idle(self._restore_custom_frame)
        else:
            self.root.after_idle(self._ensure_taskbar_presence)

    def _restore_custom_frame(self) -> None:
        if not self.root.winfo_exists():
            return
        self.root.overrideredirect(True)
        self._ensure_taskbar_presence()
        self.root.lift()

    def _minimize_window(self) -> None:
        try:
            handle = self._native_window_handle()
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(handle), 6)
        except (AttributeError, OSError):
            self.root.iconify()

    def _work_area(self) -> tuple[int, int, int, int]:
        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", ctypes.c_ulong),
            ]

        try:
            user32 = ctypes.windll.user32
            user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.GetMonitorInfoW.restype = ctypes.c_int
            monitor = user32.MonitorFromWindow(
                ctypes.c_void_p(self._native_window_handle()), 2
            )
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right, work.bottom
        except (AttributeError, OSError):
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _set_window_mode(self, mode: str) -> None:
        if mode == "normal":
            self._restore_window()
            return
        if self._window_mode == "normal":
            self._restore_geometry = self.root.geometry()
        left, top, right, bottom = self._work_area()
        width = right - left
        height = bottom - top
        if mode == "maximized":
            geometry = f"{width}x{height}+{left}+{top}"
        elif mode == "left":
            geometry = f"{width // 2}x{height}+{left}+{top}"
        else:
            half = width // 2
            geometry = f"{width - half}x{height}+{left + half}+{top}"
        self.root.geometry(geometry)
        self._window_mode = mode
        self.maximize_button.configure(image=self.titlebar_control_icons["restore"])
        self._show_resize_grips(False)

    def _restore_window(self) -> None:
        if self._window_mode == "normal":
            return
        self.root.geometry(self._restore_geometry)
        self._window_mode = "normal"
        self.maximize_button.configure(image=self.titlebar_control_icons["maximize"])
        self._show_resize_grips(True)

    def _toggle_maximize(self, _event=None) -> None:
        if self._window_mode == "maximized":
            self._restore_window()
        else:
            self._set_window_mode("maximized")

    def _start_window_drag(self, event: tk.Event) -> None:
        if self._window_mode != "normal":
            old_width = max(1, self.root.winfo_width())
            relative_x = min(1.0, max(0.0, (event.x_root - self.root.winfo_x()) / old_width))
            self._restore_window()
            self.root.update_idletasks()
            new_x = int(event.x_root - self.root.winfo_width() * relative_x)
            new_y = event.y_root - 15
            self.root.geometry(f"+{new_x}+{new_y}")
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _drag_window(self, event: tk.Event) -> None:
        offset_x, offset_y = self._drag_offset
        self.root.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _finish_window_drag(self, event: tk.Event) -> None:
        left, top, right, _bottom = self._work_area()
        snap_distance = 10
        if event.y_root <= top + snap_distance:
            self._set_window_mode("maximized")
        elif event.x_root <= left + snap_distance:
            self._set_window_mode("left")
        elif event.x_root >= right - snap_distance:
            self._set_window_mode("right")

    def _create_resize_grips(self) -> None:
        specs = (
            # Keep resize handles away from the title-bar controls.  Tk frames
            # are opaque, so placing them over a hovered button visually cuts
            # its background at the top and right edges.
            ("n", "sb_v_double_arrow", {"x": 7, "y": 0, "relwidth": 1, "width": -152, "height": 5}),
            ("s", "sb_v_double_arrow", {"x": 7, "rely": 1, "y": -5, "relwidth": 1, "width": -14, "height": 5}),
            ("w", "sb_h_double_arrow", {"x": 0, "y": 35, "width": 5, "relheight": 1, "height": -42}),
            ("e", "sb_h_double_arrow", {"relx": 1, "x": -5, "y": 35, "width": 5, "relheight": 1, "height": -42}),
            ("sw", "size_ne_sw", {"x": 0, "rely": 1, "y": -8, "width": 8, "height": 8}),
            ("se", "size_nw_se", {"relx": 1, "x": -8, "rely": 1, "y": -8, "width": 8, "height": 8}),
        )
        for edge, cursor, placement in specs:
            grip_color = "#0b1220" if edge == "n" else BG
            grip = tk.Frame(self.root, bg=grip_color, cursor=cursor)
            grip.place(**placement)
            grip.bind("<ButtonPress-1>", lambda event, side=edge: self._start_resize(event, side))
            grip.bind("<B1-Motion>", self._resize_window)
            grip.bind("<ButtonRelease-1>", lambda _event: setattr(self, "_resize_data", None))
            grip.lift()
            self.resize_grips.append(grip)

    def _show_resize_grips(self, visible: bool) -> None:
        for grip in self.resize_grips:
            if visible:
                grip.lift()
            else:
                grip.lower()

    def _start_resize(self, event: tk.Event, edge: str) -> None:
        self._resize_data = (
            edge,
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )

    def _resize_window(self, event: tk.Event) -> None:
        if self._resize_data is None or self._window_mode != "normal":
            return
        edge, start_x, start_y, x, y, width, height = self._resize_data
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        min_width, min_height = 920, 620
        if "e" in edge:
            width = max(min_width, width + delta_x)
        if "s" in edge:
            height = max(min_height, height + delta_y)
        if "w" in edge:
            new_width = max(min_width, width - delta_x)
            x += width - new_width
            width = new_width
        if "n" in edge:
            new_height = max(min_height, height - delta_y)
            y += height - new_height
            height = new_height
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_title_bar(self) -> None:
        title_color = "#0b1220"
        hover_color = "#253247"
        self.title_bar = tk.Frame(self.root, bg=title_color, height=34)
        self.title_bar.pack(fill="x", side="top")
        self.title_bar.pack_propagate(False)

        icon_path = self._icon_image_path()
        self.titlebar_icon: ImageTk.PhotoImage | None = None
        if icon_path is not None:
            try:
                with Image.open(icon_path) as source:
                    icon = source.convert("RGBA").resize((16, 16), Image.Resampling.LANCZOS)
                self.titlebar_icon = ImageTk.PhotoImage(icon)
            except OSError:
                self.titlebar_icon = None
        if self.titlebar_icon is not None:
            icon_label = tk.Label(
                self.title_bar, image=self.titlebar_icon, bg=title_color, bd=0
            )
            icon_label.pack(side="left", padx=(10, 7))
        else:
            icon_label = None

        title_label = tk.Label(
            self.title_bar,
            text="AudioLib",
            bg=title_color,
            fg="#dbe4f0",
            font=("Segoe UI", 9),
        )
        title_label.pack(side="left")

        def control_icon(kind: str) -> ImageTk.PhotoImage:
            scale = 4
            image = Image.new("RGBA", (18 * scale, 18 * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            color = (219, 228, 240, 255)
            line_width = max(1, scale)
            if kind == "minimize":
                draw.line(
                    (4 * scale, 12 * scale, 14 * scale, 12 * scale),
                    fill=color,
                    width=line_width,
                )
            elif kind == "maximize":
                draw.rectangle(
                    (4 * scale, 4 * scale, 14 * scale, 14 * scale),
                    outline=color,
                    width=line_width,
                )
            elif kind == "restore":
                draw.rectangle(
                    (6 * scale, 3 * scale, 14 * scale, 11 * scale),
                    outline=color,
                    width=line_width,
                )
                draw.rectangle(
                    (3 * scale, 6 * scale, 11 * scale, 14 * scale),
                    outline=color,
                    width=line_width,
                )
            else:
                draw.line(
                    (4 * scale, 4 * scale, 14 * scale, 14 * scale),
                    fill=color,
                    width=line_width,
                )
                draw.line(
                    (14 * scale, 4 * scale, 4 * scale, 14 * scale),
                    fill=color,
                    width=line_width,
                )
            image = image.resize((18, 18), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image)

        self.titlebar_control_icons = {
            name: control_icon(name)
            for name in ("minimize", "maximize", "restore", "close")
        }

        def title_button(icon: str, command, close: bool = False) -> tk.Button:
            active = "#e81123" if close else hover_color
            button = tk.Button(
                self.title_bar,
                image=self.titlebar_control_icons[icon],
                command=command,
                bg=title_color,
                fg="#dbe4f0",
                activebackground=active,
                activeforeground="white",
                relief="flat",
                bd=0,
                width=46,
                height=32,
                padx=0,
                pady=0,
                takefocus=False,
                cursor="hand2",
            )
            button.bind("<Enter>", lambda _event, item=button: item.configure(bg=active))
            button.bind("<Leave>", lambda _event, item=button: item.configure(bg=title_color))
            return button

        close_button = title_button("close", self.handle_window_close, close=True)
        self.maximize_button = title_button("maximize", self._toggle_maximize)
        minimize_button = title_button("minimize", self._minimize_window)
        close_button.pack(side="right", fill="y")
        self.maximize_button.pack(side="right", fill="y")
        minimize_button.pack(side="right", fill="y")

        drag_area = tk.Frame(self.title_bar, bg=title_color)
        drag_area.pack(side="left", fill="both", expand=True)
        drag_widgets = [self.title_bar, title_label, drag_area]
        if icon_label is not None:
            drag_widgets.append(icon_label)
        for widget in drag_widgets:
            widget.bind("<ButtonPress-1>", self._start_window_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<ButtonRelease-1>", self._finish_window_drag)
            widget.bind("<Double-Button-1>", self._toggle_maximize)

        tk.Frame(self.root, bg="#334155", height=1).pack(fill="x", side="top")

    def _build_secondary_title_bar(self, window: tk.Toplevel, title: str) -> None:
        """Draw a compact custom frame matching the main AudioLib window."""
        title_color = "#0b1220"
        hover_color = "#253247"
        window.overrideredirect(True)
        window._audiolib_close_allowed = True

        bar = tk.Frame(window, bg=title_color, height=34)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        if self.titlebar_icon is not None:
            icon_label = tk.Label(bar, image=self.titlebar_icon, bg=title_color, bd=0)
            icon_label.pack(side="left", padx=(10, 7))
        else:
            icon_label = None
        title_label = tk.Label(
            bar,
            text=title,
            bg=title_color,
            fg="#dbe4f0",
            font=("Segoe UI", 9),
        )
        title_label.pack(side="left")

        def request_close() -> None:
            if getattr(window, "_audiolib_close_allowed", True):
                callback = getattr(window, "_audiolib_close_callback", window.destroy)
                callback()

        def title_button(icon: str, command, *, close: bool = False) -> tk.Button:
            active = "#e81123" if close else hover_color
            button = tk.Button(
                bar,
                image=self.titlebar_control_icons[icon],
                command=command,
                bg=title_color,
                activebackground=active,
                relief="flat",
                bd=0,
                width=46,
                height=32,
                padx=0,
                pady=0,
                takefocus=False,
                cursor="hand2",
            )
            button.bind("<Enter>", lambda _event: button.configure(bg=active))
            button.bind("<Leave>", lambda _event: button.configure(bg=title_color))
            return button

        title_button("close", request_close, close=True).pack(side="right", fill="y")
        drag_area = tk.Frame(bar, bg=title_color)
        drag_area.pack(side="left", fill="both", expand=True)
        drag_offset = [0, 0]

        def start_drag(event: tk.Event) -> None:
            drag_offset[0] = event.x_root - window.winfo_x()
            drag_offset[1] = event.y_root - window.winfo_y()

        def drag(event: tk.Event) -> None:
            window.geometry(
                f"+{event.x_root - drag_offset[0]}+{event.y_root - drag_offset[1]}"
            )

        widgets = [bar, title_label, drag_area]
        if icon_label is not None:
            widgets.append(icon_label)
        for widget in widgets:
            widget.bind("<ButtonPress-1>", start_drag)
            widget.bind("<B1-Motion>", drag)
        window.protocol("WM_DELETE_WINDOW", request_close)
        tk.Frame(window, bg="#334155", height=1).pack(fill="x", side="top")

    def _start_virtual_microphone_background(self) -> bool:
        return self._begin_virtual_microphone_start()

    def _begin_virtual_microphone_start(
        self,
        physical: CaptureEndpoint | None = None,
        *,
        reset_existing: bool = False,
        save_selection: bool = False,
        callback=None,
    ) -> bool:
        if self.virtual_microphone.is_running() and not reset_existing:
            if callback is not None:
                self.root.after_idle(lambda: callback(True, physical, None))
            return True
        if self._virtual_start_in_progress:
            if callback is not None:
                self._virtual_start_callback = callback
            return True
        physical = physical or self.microphone_routing.selected_microphone()
        if physical is None:
            physical = self._choose_physical_microphone(
                "Оберіть мікрофон, голос з якого AudioLib передаватиме у VB-CABLE."
            )
            if physical is None:
                self.status_text.set("Фізичний мікрофон не вибрано")
                return False
        self._virtual_start_in_progress = True
        self._virtual_start_callback = callback
        self.status_text.set("Запуск віртуального мікрофона…")

        def worker() -> None:
            selected = physical
            try:
                if save_selection:
                    self.microphone_routing.save_selected_microphone(
                        selected.endpoint_id
                    )
                if reset_existing:
                    restored = self.microphone_routing.restore_default_microphone()
                    self.virtual_microphone.close()
                    if restored:
                        self.microphone_routing.disable_cable_output()
                selected = self.microphone_routing.activate_for_app(
                    selected.endpoint_id
                )
                if self._closing_event.is_set():
                    self.microphone_routing.restore_after_failed_start()
                    return
                self.virtual_microphone.start(
                    selected.friendly_name,
                    selected.description,
                )
                if self._closing_event.is_set():
                    self.virtual_microphone.close()
                    self.microphone_routing.restore_after_failed_start()
                    return
                self._write_audio_routing_log(
                    f"START selected={microphone_label(selected)} cable_default=true "
                    f"screen_capture_protected={self.virtual_microphone.screen_capture_protected}"
                )
                self._virtual_start_results.put((selected, None))
            except Exception as error:
                self.virtual_microphone.close()
                self.microphone_routing.restore_after_failed_start()
                self._write_audio_routing_log(f"START_ERROR {error}")
                self._virtual_start_results.put((None, str(error)))

        threading.Thread(
            target=worker,
            name="AudioLibVirtualMicrophoneStart",
            daemon=True,
        ).start()
        return True

    def _poll_virtual_start_results(self) -> None:
        try:
            while True:
                physical, error_text = self._virtual_start_results.get_nowait()
                self._virtual_start_in_progress = False
                callback = self._virtual_start_callback
                self._virtual_start_callback = None
                if error_text:
                    self.status_text.set(str(error_text))
                elif self.virtual_microphone.screen_capture_protected:
                    self.status_text.set(
                        "Віртуальний мікрофон працює; звук виключено із захоплення демонстрації"
                    )
                else:
                    self._write_audio_routing_log(
                        "CABLE_OUTPUT_SHARED_MODE "
                        + self.virtual_microphone.screen_capture_protection_error
                    )
                    self.status_text.set(
                        "Віртуальний мікрофон працює у сумісному режимі VB-CABLE"
                    )
                if callback is not None:
                    callback(error_text is None, physical, error_text)
                pending_playback = self._pending_microphone_playback
                if error_text is None and pending_playback is not None:
                    payload = self._pending_microphone_playback
                    self._pending_microphone_playback = None
                    self._play_pending_microphone_audio(payload)
                elif error_text is not None:
                    self._pending_microphone_playback = None
                    if pending_playback is not None and messagebox.askyesno(
                        "Віртуальний мікрофон",
                        f"{error_text}\n\nВідкрити налаштування?",
                        parent=self.root,
                    ):
                        self.open_virtual_microphone_setup()
        except queue.Empty:
            pass
        try:
            self.root.after(10, self._poll_virtual_start_results)
        except tk.TclError:
            pass

    def _choose_physical_microphone(self, explanation: str) -> CaptureEndpoint | None:
        try:
            microphones = list_physical_microphones()
        except AudioEndpointError as error:
            messagebox.showerror("Вибір мікрофона", str(error), parent=self.root)
            return None
        if not microphones:
            messagebox.showerror(
                "Вибір мікрофона",
                "Windows не знайшла жодного доступного фізичного мікрофона.",
                parent=self.root,
            )
            return None

        was_hidden = self.root.state() == "withdrawn"
        if was_hidden:
            self.show_window()
        dialog = tk.Toplevel(self.root)
        dialog.title("Вибір фізичного мікрофона")
        dialog.geometry("560x390")
        dialog.minsize(480, 320)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        selected: list[CaptureEndpoint] = []

        tk.Label(
            dialog,
            text="Фізичний мікрофон",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w", padx=22, pady=(20, 6))
        tk.Label(
            dialog,
            text=explanation,
            bg=BG,
            fg=MUTED,
            justify="left",
            wraplength=510,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=22, pady=(0, 12))
        microphone_list = tk.Listbox(
            dialog,
            bg=PANEL,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 11),
            activestyle="none",
        )
        microphone_list.pack(fill="both", expand=True, padx=22, pady=4)
        for endpoint in microphones:
            microphone_list.insert("end", microphone_label(endpoint))
        preferred = current_default_microphone()
        preferred_index = next(
            (
                index
                for index, endpoint in enumerate(microphones)
                if preferred is not None and endpoint.endpoint_id == preferred.endpoint_id
            ),
            0,
        )
        microphone_list.selection_set(preferred_index)
        microphone_list.see(preferred_index)

        def accept() -> None:
            indices = microphone_list.curselection()
            if not indices:
                return
            endpoint = microphones[int(indices[0])]
            try:
                self.microphone_routing.save_selected_microphone(endpoint.endpoint_id)
            except (AudioEndpointError, OSError) as error:
                messagebox.showerror("Вибір мікрофона", str(error), parent=dialog)
                return
            selected.append(endpoint)
            dialog.destroy()

        controls = tk.Frame(dialog, bg=BG)
        controls.pack(fill="x", padx=22, pady=16)
        self._button(controls, "Зберегти", accept, "#0f766e").pack(side="left")
        self._button(controls, "Скасувати", dialog.destroy, "#475569").pack(
            side="left", padx=8
        )
        microphone_list.bind("<Double-Button-1>", lambda _event: accept())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()
        if was_hidden:
            self.root.withdraw()
        return selected[0] if selected else None

    def _apply_physical_microphone(
        self,
        physical: CaptureEndpoint,
        parent: tk.Misc | None = None,
        *,
        show_confirmation: bool = True,
        on_complete=None,
    ) -> None:
        def completed(
            success: bool,
            selected: CaptureEndpoint | None,
            error_text: str | None,
        ) -> None:
            if success and selected is not None:
                self.status_text.set(
                    f"Фізичний мікрофон: {microphone_label(selected)}"
                )
                if parent is not None and show_confirmation and parent.winfo_exists():
                    messagebox.showinfo(
                        "Вибір мікрофона",
                        f"Використовується: {microphone_label(selected)}",
                        parent=parent,
                    )
            elif error_text:
                messagebox.showerror(
                    "Вибір мікрофона",
                    error_text,
                    parent=parent or self.root,
                )
            if on_complete is not None:
                on_complete()

        self._begin_virtual_microphone_start(
            physical,
            reset_existing=True,
            save_selection=True,
            callback=completed,
        )

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        density = str(self.app_settings.get("list_density", "comfortable"))
        row_height = {"compact": 28, "comfortable": 34, "spacious": 40}.get(
            density, 34
        )
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=row_height, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#374151", foreground=TEXT,
                        relief="flat", font=("Segoe UI Semibold", 10))
        style.map("Treeview", background=[("selected", ACCENT)])
        style.configure("TScale", background=BG, troughcolor="#374151")
        style.configure(
            "Settings.Horizontal.TProgressbar",
            troughcolor="#334155",
            background=ACCENT,
            bordercolor="#334155",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=7,
        )

        def scrollbar_thumb(color: str) -> ImageTk.PhotoImage:
            scale = 4
            image = Image.new("RGBA", (12 * scale, 32 * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (2 * scale, 0, 10 * scale - 1, 32 * scale - 1),
                radius=4 * scale,
                fill=color,
            )
            image = image.resize((12, 32), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image)

        def horizontal_scrollbar_thumb(color: str) -> ImageTk.PhotoImage:
            scale = 4
            image = Image.new("RGBA", (32 * scale, 12 * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (0, 2 * scale, 32 * scale - 1, 10 * scale - 1),
                radius=4 * scale,
                fill=color,
            )
            image = image.resize((32, 12), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image)

        self.scrollbar_images = {
            "normal": scrollbar_thumb("#64748b"),
            "active": scrollbar_thumb(ACCENT),
            "pressed": scrollbar_thumb(ACCENT_HOVER),
            "horizontal_normal": horizontal_scrollbar_thumb("#64748b"),
            "horizontal_active": horizontal_scrollbar_thumb(ACCENT),
            "horizontal_pressed": horizontal_scrollbar_thumb(ACCENT_HOVER),
        }
        style.element_create(
            "AudioLib.Scrollbar.thumb",
            "image",
            self.scrollbar_images["normal"],
            ("pressed", self.scrollbar_images["pressed"]),
            ("active", self.scrollbar_images["active"]),
            border=(0, 8, 0, 8),
            sticky="ns",
        )
        style.layout(
            "AudioLib.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            ("AudioLib.Scrollbar.thumb", {"expand": "1", "sticky": "ns"})
                        ],
                    },
                )
            ],
        )
        style.configure(
            "AudioLib.Vertical.TScrollbar",
            width=12,
            troughcolor="#0b1220",
            background="#64748b",
            bordercolor="#0b1220",
            lightcolor="#0b1220",
            darkcolor="#0b1220",
            relief="flat",
            borderwidth=0,
        )
        style.element_create(
            "AudioLib.Horizontal.Scrollbar.thumb",
            "image",
            self.scrollbar_images["horizontal_normal"],
            ("pressed", self.scrollbar_images["horizontal_pressed"]),
            ("active", self.scrollbar_images["horizontal_active"]),
            border=(8, 0, 8, 0),
            sticky="we",
        )
        style.layout(
            "AudioLib.Horizontal.TScrollbar",
            [
                (
                    "Horizontal.Scrollbar.trough",
                    {
                        "sticky": "we",
                        "children": [
                            (
                                "AudioLib.Horizontal.Scrollbar.thumb",
                                {"expand": "1", "sticky": "we"},
                            )
                        ],
                    },
                )
            ],
        )
        style.configure(
            "AudioLib.Horizontal.TScrollbar",
            width=12,
            troughcolor="#0b1220",
            background="#64748b",
            bordercolor="#0b1220",
            lightcolor="#0b1220",
            darkcolor="#0b1220",
            relief="flat",
            borderwidth=0,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        color: str = ACCENT,
        *,
        rounded: bool = True,
        image: ImageTk.PhotoImage | None = None,
    ) -> tk.Widget:
        hover_colors = {
            ACCENT: ACCENT_HOVER,
            DANGER: "#b91c1c",
            "#0f766e": "#0d9488",
            "#475569": "#64748b",
        }
        hover_color = hover_colors.get(color, ACCENT_HOVER)
        if rounded:
            return RoundedButton(
                parent,
                text,
                command,
                color,
                hover_color,
                image=image,
            )
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="white",
            activebackground=hover_color,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover_color))
        button.bind("<Leave>", lambda _event: button.configure(bg=color))
        return button

    def _ask_text(
        self,
        title: str,
        prompt: str,
        *,
        initial_value: str = "",
    ) -> str | None:
        """Show a modal text dialog styled like the rest of AudioLib."""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)

        width = 470
        height = 235
        self.root.update_idletasks()
        x = self.root.winfo_x() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        self._build_secondary_title_bar(dialog, title)

        content = tk.Frame(dialog, bg=BG, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text=prompt,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        field_height = 42
        field_radius = 10
        field_color = "#273449"
        field = tk.Canvas(
            content,
            bg=BG,
            height=field_height,
            highlightthickness=0,
            bd=0,
        )
        field.pack(fill="x")
        value = tk.StringVar(value=initial_value)
        entry = tk.Entry(
            field,
            textvariable=value,
            bg=field_color,
            fg=TEXT,
            insertbackground=TEXT,
            insertwidth=2,
            insertontime=700,
            insertofftime=500,
            selectbackground=ACCENT,
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            font=("Segoe UI", 11),
        )

        def draw_field(_event=None, *, focused: bool | None = None) -> None:
            if focused is None:
                focused = dialog.focus_get() is entry
            field.update_idletasks()
            field_width = max(100, field.winfo_width())
            field.delete("field_surface")
            field._surface_image = render_rounded_surface(
                field_width,
                field_height,
                fill=field_color,
                radius=field_radius,
                outline=ACCENT if focused else "#64748b",
                outline_width=1,
            )
            field.create_image(
                0,
                0,
                image=field._surface_image,
                anchor="nw",
                tags="field_surface",
            )
            field.tag_lower("field_surface")
            entry.place(x=14, y=4, width=max(72, field_width - 28), height=34)

        field.bind("<Configure>", draw_field)
        entry.bind("<FocusIn>", lambda _event: draw_field(focused=True))
        entry.bind("<FocusOut>", lambda _event: draw_field(focused=False))

        error_text = tk.StringVar()
        tk.Label(
            content,
            textvariable=error_text,
            bg=BG,
            fg="#fca5a5",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

        result: list[str] = []

        def close_dialog() -> None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def submit() -> None:
            clean_value = value.get().strip()
            if not clean_value:
                error_text.set("Поле не може бути порожнім.")
                entry.focus_set()
                return
            result.append(clean_value)
            close_dialog()

        actions = tk.Frame(content, bg=BG)
        actions.pack(fill="x", side="bottom", pady=(14, 0))
        self._button(actions, "Скасувати", close_dialog, "#475569").pack(
            side="right"
        )
        self._button(actions, "Зберегти", submit, ACCENT).pack(
            side="right", padx=(0, 8)
        )

        # A subtle outline keeps the borderless dialog readable over panels
        # that use nearly the same dark background.
        outline_color = "#64748b"
        tk.Frame(dialog, bg=outline_color, height=1).place(
            x=0, y=0, relwidth=1
        )
        tk.Frame(dialog, bg=outline_color, height=1).place(
            x=0, rely=1, y=-1, relwidth=1
        )
        tk.Frame(dialog, bg=outline_color, width=1).place(
            x=0, y=0, relheight=1
        )
        tk.Frame(dialog, bg=outline_color, width=1).place(
            relx=1, x=-1, y=0, relheight=1
        )

        dialog._audiolib_close_callback = close_dialog
        dialog.bind("<Escape>", lambda _event: close_dialog())
        dialog.bind("<Return>", lambda _event: submit())
        dialog.deiconify()
        dialog.lift()
        dialog.grab_set()
        entry.focus_set()
        entry.selection_range(0, "end")
        dialog.wait_window()
        return result[0] if result else None

    def _build_ui(self) -> None:
        self._build_title_bar()
        header = tk.Frame(self.root, bg=BG, padx=18, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="AudioLib", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 24)).pack(side="left")
        tk.Label(header, text="Локальна бібліотека аудіозаписів", bg=BG, fg=MUTED,
                 font=("Segoe UI", 11)).pack(side="left", padx=14, pady=(8, 0))
        header_actions = tk.Frame(header, bg=BG)
        header_actions.pack(side="right")
        self._button(
            header_actions,
            "Налаштування",
            self.open_virtual_microphone_setup,
            "#475569",
            image=self.header_action_icons["settings"],
        ).pack(side="left")

        body = tk.PanedWindow(self.root, orient="horizontal", bg=BG, sashwidth=6,
                              sashrelief="flat", bd=0)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        left = tk.Frame(body, bg=PANEL, width=390)
        right = tk.Frame(body, bg=BG)
        body.add(left, minsize=300)
        body.add(right, minsize=500)

        left_top = tk.Frame(left, bg=PANEL, padx=16, pady=14)
        left_top.pack(fill="x")
        tk.Label(left_top, text="Категорії", bg=PANEL, fg=TEXT,
                 font=("Segoe UI Semibold", 16)).pack(side="left")
        self._button(left_top, "+ Додати", self.add_person).pack(side="right")
        people_search_color = "#374151"
        people_search_height = 38
        people_search_radius = 10
        people_search_container = tk.Canvas(
            left,
            bg=PANEL,
            height=people_search_height,
            highlightthickness=0,
            bd=0,
        )
        people_search_container.pack(fill="x", padx=16)

        def draw_people_search_background(event) -> None:
            width = max(people_search_radius * 2, event.width)
            people_search_container.delete("search_background")
            people_search_container._surface_image = render_rounded_surface(
                width,
                people_search_height,
                fill=people_search_color,
                radius=people_search_radius,
            )
            people_search_container.create_image(
                0,
                0,
                image=people_search_container._surface_image,
                anchor="nw",
                tags="search_background",
            )
            people_search_container.tag_lower("search_background")

        people_search_container.bind("<Configure>", draw_people_search_background)
        self.person_search_entry = tk.Entry(
            people_search_container,
            textvariable=self.person_query,
            bg=people_search_color,
            fg=TEXT,
            insertbackground=TEXT,
            insertwidth=2,
            insertontime=700,
            insertofftime=500,
            relief="flat",
            bd=0,
            font=("Segoe UI", 11),
        )
        self.person_search_entry.place(x=10, y=3, relwidth=1.0, width=-20, height=32)
        people_search_icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        people_search_draw = ImageDraw.Draw(people_search_icon)
        people_search_icon_color = (148, 163, 184, 255)
        people_search_draw.ellipse(
            (8, 8, 40, 40), outline=people_search_icon_color, width=6
        )
        people_search_draw.line(
            (36, 36, 56, 56), fill=people_search_icon_color, width=6
        )
        people_search_icon = people_search_icon.resize(
            (16, 16), Image.Resampling.LANCZOS
        )
        self.person_search_icon = ImageTk.PhotoImage(people_search_icon)
        self.person_search_placeholder = tk.Label(
            people_search_container,
            text=" Пошук",
            image=self.person_search_icon,
            compound="left",
            bg=people_search_color,
            fg="#94a3b8",
            font=("Segoe UI", 10),
            cursor="xterm",
        )
        self.person_search_placeholder.place(x=10, rely=0.5, anchor="w")
        self.person_search_placeholder.bind(
            "<Button-1>", lambda _event: self.person_search_entry.focus_set()
        )
        self.person_search_entry.bind(
            "<FocusIn>", lambda _event: self._update_person_search_placeholder()
        )
        self.person_search_entry.bind(
            "<FocusOut>", lambda _event: self._update_person_search_placeholder()
        )

        self.people_canvas = tk.Canvas(left, bg=PANEL, highlightthickness=0)
        people_scroll = ttk.Scrollbar(
            left,
            orient="vertical",
            command=self.people_canvas.yview,
            style="AudioLib.Vertical.TScrollbar",
        )
        self.people_frame = tk.Frame(self.people_canvas, bg=PANEL)
        self.people_window = self.people_canvas.create_window((0, 0), window=self.people_frame, anchor="nw")
        self.people_canvas.configure(yscrollcommand=people_scroll.set)
        self.people_canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=12)
        people_scroll.pack(side="right", fill="y", pady=12)
        self.people_frame.bind("<Configure>", lambda _: self.people_canvas.configure(
            scrollregion=self.people_canvas.bbox("all")))
        self.people_canvas.bind("<Configure>", lambda e: self.people_canvas.itemconfigure(
            self.people_window, width=e.width))
        self.root.bind_all("<MouseWheel>", self._on_people_mousewheel, add="+")

        person_header = tk.Frame(right, bg=BG, pady=8)
        person_header.pack(fill="x", padx=(18, 0))
        self.person_title = tk.Label(person_header, text="Оберіть людину", bg=BG, fg=TEXT,
                                     font=("Segoe UI Semibold", 22))
        self.person_title.pack(side="left")
        self.delete_person_button = self._button(person_header, "Видалити", self.delete_person, DANGER)
        self.edit_person_button = self._button(person_header, "Змінити", self.edit_person, "#475569")
        self.delete_person_button.pack(side="right", padx=(8, 0))
        self.edit_person_button.pack(side="right")

        tools = tk.Frame(right, bg=BG, pady=8)
        tools.pack(fill="x", padx=(18, 0))
        self.add_tracks_button = self._button(tools, "+ Додати аудіо", self.add_tracks)
        self.add_tracks_button.pack(side="left")
        self.source_column_button = self._button(
            tools, "Показати оригінальну назву", self.toggle_source_column, "#475569"
        )
        self.source_column_button.pack(side="left", padx=(8, 0))
        search_width = 270
        search_height = 38
        search_radius = 10
        search_color_hex = "#374151"
        search_container = tk.Canvas(
            tools,
            bg=BG,
            width=search_width,
            height=search_height,
            highlightthickness=0,
            bd=0,
        )
        search_container.pack(side="right")
        search_container._surface_image = render_rounded_surface(
            search_width,
            search_height,
            fill=search_color_hex,
            radius=search_radius,
        )
        search_container.create_image(
            0,
            0,
            image=search_container._surface_image,
            anchor="nw",
        )
        self.track_search_entry = tk.Entry(
            search_container,
            textvariable=self.track_query,
            bg=search_color_hex,
            fg=TEXT,
            insertbackground=TEXT,
            insertwidth=2,
            insertontime=700,
            insertofftime=500,
            relief="flat",
            bd=0,
            font=("Segoe UI", 11),
        )
        self.track_search_entry.place(x=10, y=3, width=250, height=32)
        search_icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        search_draw = ImageDraw.Draw(search_icon)
        search_color = (148, 163, 184, 255)
        search_draw.ellipse((8, 8, 40, 40), outline=search_color, width=6)
        search_draw.line((36, 36, 56, 56), fill=search_color, width=6)
        search_icon = search_icon.resize((16, 16), Image.Resampling.LANCZOS)
        self.track_search_icon = ImageTk.PhotoImage(search_icon)
        self.track_search_placeholder = tk.Label(
            search_container,
            text=" Пошук",
            image=self.track_search_icon,
            compound="left",
            bg=search_color_hex,
            fg="#94a3b8",
            font=("Segoe UI", 10),
            cursor="xterm",
        )
        self.track_search_placeholder.place(x=10, rely=0.5, anchor="w")
        self.track_search_placeholder.bind(
            "<Button-1>", lambda _event: self.track_search_entry.focus_set()
        )
        self.track_search_entry.bind(
            "<FocusIn>", lambda _event: self._update_track_search_placeholder()
        )
        self.track_search_entry.bind(
            "<FocusOut>", lambda _event: self._update_track_search_placeholder()
        )

        tk.Frame(right, bg="#334155", height=1, bd=0).pack(
            fill="x",
            padx=(18, 0),
            pady=(4, 0),
        )

        table_frame = tk.Frame(right, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=(18, 0), pady=(7, 8))
        self.track_table = CustomTrackTable(
            table_frame,
            columns=tuple(self.track_heading_labels),
            show="headings",
            selectmode="browse",
        )
        self.track_table.column("number", width=48, minwidth=42, anchor="center", stretch=False)
        self.track_table.column("title", width=240, minwidth=130)
        self.track_table.column("speaker_play", width=58, minwidth=54, anchor="center", stretch=False)
        self.track_table.column("favorite", width=48, minwidth=44, anchor="center", stretch=False)
        self.track_table.column("hotkey", width=145, minwidth=120, anchor="center", stretch=False)
        self.track_table.column("source", width=190, minwidth=100)
        self.track_table.column("duration", width=85, minwidth=75, anchor="center", stretch=False)
        self.track_table.column("mic_play", width=62, minwidth=58, anchor="center", stretch=False)
        self.track_table.column("mic_stop", width=58, minwidth=54, anchor="center", stretch=False)
        self._update_track_headings()
        self.track_table.configure(displaycolumns=self._visible_track_columns())
        track_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.track_table.yview,
            style="AudioLib.Vertical.TScrollbar",
        )
        track_horizontal_scroll = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.track_table.xview,
            style="AudioLib.Horizontal.TScrollbar",
        )

        def update_horizontal_scroll(first: str, last: str) -> None:
            track_horizontal_scroll.set(first, last)
            if float(first) <= 0.0 and float(last) >= 1.0:
                track_horizontal_scroll.grid_remove()
            else:
                track_horizontal_scroll.grid()

        self.track_table.configure(
            yscrollcommand=track_scroll.set,
            xscrollcommand=update_horizontal_scroll,
        )
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        self.track_table.grid(row=0, column=0, sticky="nsew")
        track_scroll.grid(row=0, column=1, sticky="ns")
        track_horizontal_scroll.grid(row=1, column=0, sticky="ew")
        self.track_table.bind("<<TreeviewSelect>>", lambda _: self._update_track_buttons())
        self.track_table.bind("<ButtonRelease-1>", self._on_track_table_click, add="+")
        self.track_table.bind("<Double-Button-1>", self._on_track_double_click)

        track_actions = tk.Frame(right, bg=BG, pady=4)
        track_actions.pack(fill="x", padx=(18, 0))
        self.random_track_button = self._button(
            track_actions,
            "Випадковий запис",
            self.play_random_track,
            "#0f766e",
            image=self.button_action_icons["random"],
        )
        self.random_track_button.pack(side="left", padx=(0, 8))
        self._button(track_actions, "Перейменувати", self.rename_track, "#475569").pack(
            side="left", padx=(0, 8)
        )
        self._button(track_actions, "Видалити запис", self.delete_track, DANGER).pack(side="left")

        player = tk.Frame(self.root, bg="#0b1220", padx=18, pady=12)
        player.pack(fill="x")
        tk.Label(player, textvariable=self.now_playing, bg="#0b1220", fg=TEXT,
                 font=("Segoe UI Semibold", 10), width=26, anchor="w").pack(side="left")
        self.play_pause_button = self._button(
            player, "▶", self.toggle_pause
        )
        self.play_pause_button.pack(side="left", padx=(4, 12))
        self._button(
            player, "■", self.stop_playback, "#475569"
        ).pack(side="left", padx=(0, 12))
        self.progress = FlatScale(
            player,
            from_=0,
            to=1,
            command=self._preview_seek,
            background="#0b1220",
            trough_color="#374151",
            fill_color="#64748b",
            active_color=ACCENT,
            thumb_radius=4,
        )
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress.bind(
            "<ButtonPress-1>", lambda _: setattr(self, "seeking", True), add="+"
        )
        self.progress.bind("<ButtonRelease-1>", self._commit_seek, add="+")
        tk.Label(player, textvariable=self.time_text, bg="#0b1220", fg=MUTED,
                 font=("Segoe UI", 9), width=12).pack(side="left", padx=10)
        self.volume_icon = self._make_volume_icon()
        tk.Label(player, image=self.volume_icon, bg="#0b1220", bd=0).pack(side="left")
        self.volume_scale = FlatScale(
            player,
            from_=0,
            to=1000,
            command=self._set_volume,
            width=100,
            background="#0b1220",
            trough_color="#4b5563",
            fill_color="#f8fafc",
            active_color="#ffffff",
            thumb_radius=0,
        )
        self.volume_scale.set(self.volume_value)
        self.volume_scale.pack(side="left", padx=(5, 0))

    def _on_people_mousewheel(self, event: tk.Event) -> None:
        pointer_x, pointer_y = self.root.winfo_pointerxy()
        canvas_x = self.people_canvas.winfo_rootx()
        canvas_y = self.people_canvas.winfo_rooty()
        inside_canvas = (
            canvas_x <= pointer_x < canvas_x + self.people_canvas.winfo_width()
            and canvas_y <= pointer_y < canvas_y + self.people_canvas.winfo_height()
        )
        if inside_canvas and event.delta:
            direction = -1 if event.delta > 0 else 1
            self.people_canvas.yview_scroll(direction, "units")

    def toggle_source_column(self) -> None:
        self.source_column_visible = not self.source_column_visible
        self.track_table.configure(displaycolumns=self._visible_track_columns())
        self.source_column_button.configure(
            text=(
                "Сховати оригінальну назву"
                if self.source_column_visible
                else "Показати оригінальну назву"
            )
        )

    def _visible_track_columns(self) -> tuple[str, ...]:
        columns = ["number", "title"]
        if self.source_column_visible:
            columns.append("source")
        columns.extend(("speaker_play", "favorite", "duration", "mic_play", "mic_stop"))
        if self.selected_person_id == FAVORITES_PERSON_ID:
            columns.append("hotkey")
        return tuple(columns)

    def _on_track_query_changed(self, *_args) -> None:
        self._update_track_search_placeholder()
        self.refresh_tracks()

    def _on_person_query_changed(self, *_args) -> None:
        self._update_person_search_placeholder()
        self.refresh_people()

    def _update_person_search_placeholder(self) -> None:
        if self.person_query.get() or self.root.focus_get() == self.person_search_entry:
            self.person_search_placeholder.place_forget()
        else:
            self.person_search_placeholder.place(x=10, rely=0.5, anchor="w")

    def _update_track_search_placeholder(self) -> None:
        if self.track_query.get() or self.root.focus_get() == self.track_search_entry:
            self.track_search_placeholder.place_forget()
        else:
            self.track_search_placeholder.place(x=10, rely=0.5, anchor="w")

    @staticmethod
    def _circular_photo(image: Image.Image, size: int) -> ImageTk.PhotoImage:
        image = image.convert("RGBA")
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize(
            (size, size), Image.Resampling.LANCZOS
        )
        mask_scale = 4
        mask_size = size * mask_scale
        mask = Image.new("L", (mask_size, mask_size), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, mask_size - 1, mask_size - 1), fill=255
        )
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)
        rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        rounded.paste(image, (0, 0), mask)
        return ImageTk.PhotoImage(rounded)

    def _placeholder(self, name: str, size: int = 92) -> ImageTk.PhotoImage:
        cache_key = ("placeholder", name, size)
        cached = self._photo_cache.get(cache_key)
        if cached is not None:
            return cached
        image = Image.new("RGB", (size, size), "#4c1d95")
        draw = ImageDraw.Draw(image)
        initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
        try:
            font = ImageFont.truetype("arial.ttf", size // 3)
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), initials, font=font)
        draw.text(((size - box[2]) / 2, (size - box[3]) / 2 - 2), initials, fill="white", font=font)
        result = self._circular_photo(image, size)
        self._remember_photo(cache_key, result)
        return result

    def _remember_photo(
        self, key: tuple[object, ...], image: ImageTk.PhotoImage
    ) -> None:
        if len(self._photo_cache) >= 256:
            self._photo_cache.pop(next(iter(self._photo_cache)))
        self._photo_cache[key] = image

    def _person_photo(self, person: Person, size: int = 92) -> ImageTk.PhotoImage:
        path = self.store.absolute(person.photo_path)
        if path and path.exists():
            try:
                stat = path.stat()
                cache_key = ("person", str(path), stat.st_mtime_ns, stat.st_size, size)
                cached = self._photo_cache.get(cache_key)
                if cached is not None:
                    return cached
                with Image.open(path) as source:
                    result = self._circular_photo(source, size)
                self._remember_photo(cache_key, result)
                return result
            except OSError:
                pass
        return self._placeholder(person.name, size)

    def _favorites_photo(self, size: int = 92) -> ImageTk.PhotoImage:
        asset_roots = [self.root_dir / "assets"]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            asset_roots.insert(0, Path(sys._MEIPASS) / "assets")
        for assets in asset_roots:
            path = assets / "favorites_icon.png"
            try:
                if path.exists():
                    stat = path.stat()
                    cache_key = (
                        "favorites",
                        str(path),
                        stat.st_mtime_ns,
                        stat.st_size,
                        size,
                    )
                    cached = self._photo_cache.get(cache_key)
                    if cached is not None:
                        return cached
                    with Image.open(path) as source:
                        result = self._circular_photo(source, size)
                    self._remember_photo(cache_key, result)
                    return result
            except OSError:
                continue
        return self._placeholder("★", size)

    def refresh_people(self) -> None:
        for widget in self.people_frame.winfo_children():
            widget.destroy()
        self.photo_refs.clear()
        self._person_card_widgets.clear()
        favorites_selected = self.selected_person_id == FAVORITES_PERSON_ID
        photo_size = int(self.app_settings.get("photo_size", 92))
        density = str(self.app_settings.get("list_density", "comfortable"))
        card_padding = {"compact": 5, "comfortable": 8, "spacious": 11}.get(
            density, 8
        )
        card_gap = {"compact": 2, "comfortable": 4, "spacious": 7}.get(
            density, 4
        )
        favorites_card = tk.Frame(
            self.people_frame,
            bg=ACCENT if favorites_selected else "#854d0e",
            padx=8,
            pady=card_padding,
            cursor="hand2",
        )
        favorites_card.pack(fill="x", padx=6, pady=(card_gap, card_gap * 2))
        favorite_photo = self._favorites_photo(photo_size)
        self.photo_refs.append(favorite_photo)
        favorite_photo_label = tk.Label(
            favorites_card,
            image=favorite_photo,
            bg=favorites_card["bg"],
            cursor="hand2",
        )
        favorite_photo_label.pack(side="left")
        favorite_name_label = tk.Label(
            favorites_card,
            text="★ Favorites",
            bg=favorites_card["bg"],
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
            wraplength=180,
            justify="left",
            cursor="hand2",
        )
        favorite_name_label.pack(side="left", padx=12)
        favorite_count_label = tk.Label(
            favorites_card,
            text=f"{self.store.favorite_count()}/{FAVORITES_LIMIT}",
            bg=favorites_card["bg"],
            fg=TEXT if favorites_selected else MUTED,
            font=("Segoe UI Semibold", 11),
            cursor="hand2",
        )
        favorite_count_label.pack(side="right", padx=(8, 4))
        for widget in (
            favorites_card,
            favorite_photo_label,
            favorite_name_label,
            favorite_count_label,
        ):
            widget.bind(
                "<Button-1>",
                lambda _event: self.select_person(FAVORITES_PERSON_ID),
            )
        self._person_card_widgets[FAVORITES_PERSON_ID] = (
            favorites_card,
            favorite_photo_label,
            favorite_name_label,
            favorite_count_label,
        )
        people = self.store.list_people(self.person_query.get())
        track_counts = self.store.track_counts_by_person()
        if not people:
            tk.Label(self.people_frame, text="Додайте першу людину", bg=PANEL, fg=MUTED,
                     pady=30, font=("Segoe UI", 11)).pack(fill="x")
        for person in people:
            selected = person.id == self.selected_person_id
            card = tk.Frame(self.people_frame, bg=ACCENT if selected else CARD, padx=8, pady=card_padding,
                            cursor="hand2")
            card.pack(fill="x", padx=6, pady=card_gap)
            photo = self._person_photo(person, photo_size)
            self.photo_refs.append(photo)
            photo_label = tk.Label(card, image=photo, bg=card["bg"], cursor="hand2")
            photo_label.pack(side="left")
            count_label = tk.Label(
                card,
                text=str(track_counts.get(person.id, 0)),
                bg=card["bg"],
                fg=TEXT if selected else MUTED,
                font=("Segoe UI Semibold", 11),
                cursor="hand2",
            )
            count_label.pack(side="right", padx=(8, 4))
            name_label = tk.Label(card, text=person.name, bg=card["bg"], fg=TEXT,
                                  font=("Segoe UI Semibold", 11), wraplength=180, justify="left",
                                  cursor="hand2")
            name_label.pack(side="left", padx=12)
            for widget in (card, photo_label, name_label, count_label):
                widget.bind("<Button-1>", lambda _, pid=person.id: self.select_person(pid))
            self._person_card_widgets[person.id] = (
                card,
                photo_label,
                name_label,
                count_label,
            )

    def _set_person_card_selected(self, person_id: int | None, selected: bool) -> None:
        if person_id is None:
            return
        widgets = self._person_card_widgets.get(person_id)
        if widgets is None:
            return
        background = (
            ACCENT
            if selected
            else ("#854d0e" if person_id == FAVORITES_PERSON_ID else CARD)
        )
        card, photo_label, name_label, count_label = widgets
        for widget in (card, photo_label, name_label, count_label):
            try:
                widget.configure(bg=background)
            except tk.TclError:
                return
        count_label.configure(fg=TEXT if selected else MUTED)

    def select_person(self, person_id: int) -> None:
        previous_person_id = self.selected_person_id
        is_favorites = person_id == FAVORITES_PERSON_ID
        person = None if is_favorites else self.store.get_person(person_id)
        if not is_favorites and person is None:
            person_id = FAVORITES_PERSON_ID
            is_favorites = True
        self.selected_person_id = person_id
        if not is_favorites:
            self.app_settings["last_person_id"] = person_id
            self._schedule_volume_save()
        self.person_title.configure(
            text="★ Favorites" if is_favorites else (person.name if person else "Оберіть людину")
        )
        normal_state = "disabled" if is_favorites else "normal"
        self.edit_person_button.configure(state=normal_state)
        self.delete_person_button.configure(state=normal_state)
        self.add_tracks_button.configure(state=normal_state)
        self.track_table.configure(displaycolumns=self._visible_track_columns())
        if person_id in self._person_card_widgets:
            self._set_person_card_selected(previous_person_id, False)
            self._set_person_card_selected(person_id, True)
        else:
            self.refresh_people()
        self.refresh_tracks()

    def add_person(self) -> None:
        name = self._ask_text("Нова людина", "Введіть ім'я:")
        if not name:
            return
        try:
            selected = choose_files(
                self.root,
                title="Оберіть фотографію (можна скасувати)",
                filetypes=[("Зображення", " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS)))],
            )
        except FileDialogError as error:
            messagebox.showerror("Помилка вибору файлу", str(error), parent=self.root)
            return
        photo = selected[0] if selected else ""
        try:
            person = self.store.add_person(name, Path(photo) if photo else None)
            self.status_text.set(f"Додано людину: {person.name}")
            self.select_person(person.id)
        except (OSError, ValueError) as error:
            messagebox.showerror("Не вдалося додати", str(error), parent=self.root)

    def edit_person(self) -> None:
        if self.selected_person_id is None or self.selected_person_id == FAVORITES_PERSON_ID:
            return
        person = self.store.get_person(self.selected_person_id)
        if not person:
            return
        name = self._ask_text(
            "Змінити ім'я",
            "Ім'я:",
            initial_value=person.name,
        )
        if name:
            try:
                self.store.rename_person(person.id, name)
            except ValueError as error:
                messagebox.showerror("Помилка", str(error), parent=self.root)
                return
        if messagebox.askyesno("Фотографія", "Вибрати нову фотографію?", parent=self.root):
            try:
                selected = choose_files(
                    self.root,
                    title="Оберіть фотографію",
                    filetypes=[("Зображення", " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS)))],
                )
            except FileDialogError as error:
                messagebox.showerror("Помилка вибору файлу", str(error), parent=self.root)
                return
            photo = selected[0] if selected else ""
            if photo:
                try:
                    self.store.set_person_photo(person.id, Path(photo))
                except (OSError, ValueError) as error:
                    messagebox.showerror("Помилка фотографії", str(error), parent=self.root)
        updated = self.store.get_person(person.id)
        self.person_title.configure(text=updated.name if updated else "")
        self.refresh_people()

    def delete_person(self) -> None:
        if self.selected_person_id is None or self.selected_person_id == FAVORITES_PERSON_ID:
            return
        person = self.store.get_person(self.selected_person_id)
        if not person:
            return
        if self._library_task_in_progress:
            self.status_text.set("Дочекайтеся завершення поточної операції з бібліотекою")
            return
        if not messagebox.askyesno(
            "Видалити людину",
            f"Видалити «{person.name}» та всі пов'язані аудіозаписи?\nЦю дію неможливо скасувати.",
            icon="warning", parent=self.root,
        ):
            return
        self._clear_player()
        self.stop_virtual_microphone_sound()
        self._library_task_in_progress = True
        self.status_text.set(f"Видалення категорії: {person.name}…")

        def worker() -> None:
            worker_store = None
            try:
                worker_store = LibraryStore(self.root_dir)
                worker_store.delete_person(person.id)
                self._library_task_results.put(
                    ("delete_person", person.id, person.name, None)
                )
            except Exception as error:
                self._library_task_results.put(
                    ("delete_person", person.id, person.name, str(error))
                )
            finally:
                if worker_store is not None:
                    worker_store.close()

        threading.Thread(
            target=worker,
            name="AudioLibCategoryDelete",
            daemon=True,
        ).start()

    def add_tracks(self) -> None:
        if self.selected_person_id is None or self.selected_person_id == FAVORITES_PERSON_ID:
            messagebox.showinfo("Спочатку оберіть людину", "Аудіозаписи додаються до вибраної людини.")
            return
        try:
            paths = choose_files(
                self.root,
                title="Оберіть аудіозаписи",
                filetypes=[("Аудіофайли", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS)))],
                multiple=True,
            )
        except FileDialogError as error:
            messagebox.showerror("Помилка вибору файлу", str(error), parent=self.root)
            return
        if not paths:
            return
        if self._library_task_in_progress:
            self.status_text.set("Імпорт аудіозаписів уже виконується")
            return
        person_id = int(self.selected_person_id)
        sources = tuple(Path(path) for path in paths)
        self._library_task_in_progress = True
        self.status_text.set(f"Додавання аудіозаписів: {len(sources)}…")

        def worker() -> None:
            worker_store = None
            try:
                worker_store = LibraryStore(self.root_dir)
                added = worker_store.add_tracks(person_id, sources)
                self._library_task_results.put(
                    ("add_tracks", person_id, len(added), None)
                )
            except Exception as error:
                self._library_task_results.put(
                    ("add_tracks", person_id, 0, str(error))
                )
            finally:
                if worker_store is not None:
                    worker_store.close()

        threading.Thread(
            target=worker,
            name="AudioLibTrackImport",
            daemon=True,
        ).start()

    def _poll_library_task_results(self) -> None:
        try:
            while True:
                operation, item_id, detail, error_text = (
                    self._library_task_results.get_nowait()
                )
                if operation != "metadata":
                    self._library_task_in_progress = False
                if error_text and operation == "metadata":
                    self._write_audio_routing_log(
                        f"METADATA_REFRESH_ERROR {error_text}"
                    )
                elif error_text:
                    messagebox.showerror(
                        "Операція з бібліотекою не виконана",
                        str(error_text),
                        parent=self.root,
                    )
                elif operation == "add_tracks":
                    self.refresh_people()
                    if self.selected_person_id == int(item_id):
                        self.refresh_tracks()
                    self.status_text.set(f"Додано записів: {int(detail)}")
                elif operation == "delete_person":
                    self._photo_cache.clear()
                    self._person_card_widgets.clear()
                    self._refresh_tray_favorites()
                    self._reload_global_hotkeys()
                    self.selected_person_id = FAVORITES_PERSON_ID
                    self.select_person(FAVORITES_PERSON_ID)
                    self.status_text.set(f"Видалено: {detail}")
                elif operation == "metadata":
                    if int(detail) > 0:
                        self.refresh_tracks()
        except queue.Empty:
            pass
        try:
            self.root.after(10, self._poll_library_task_results)
        except tk.TclError:
            pass

    def _refresh_legacy_metadata_async(self) -> None:
        def worker() -> None:
            worker_store = None
            try:
                worker_store = LibraryStore(self.root_dir)
                updated = worker_store.refresh_missing_metadata()
                self._library_task_results.put(("metadata", 0, updated, None))
            except Exception as error:
                self._library_task_results.put(("metadata", 0, 0, str(error)))
            finally:
                if worker_store is not None:
                    worker_store.close()

        threading.Thread(
            target=worker,
            name="AudioLibMetadataRefresh",
            daemon=True,
        ).start()

    def refresh_tracks(self) -> None:
        selected = self.track_table.selection()
        selected_id = selected[0] if selected else None
        view_anchor = self.track_table.view_anchor()
        if self.selected_person_id is None:
            self.track_table.replace_rows([])
            return
        self.track_table.set_title_autofit(
            self.selected_person_id == FAVORITES_PERSON_ID
        )
        if self.selected_person_id == FAVORITES_PERSON_ID:
            tracks = self.store.list_favorites(
                self.track_query.get(),
                self.track_sort_column,
                self.track_sort_descending,
            )
        else:
            tracks = self.store.list_tracks(
                self.selected_person_id,
                self.track_query.get(),
                self.track_sort_column,
                self.track_sort_descending,
            )
        favorite_ids = self.store.favorite_track_ids()
        favorite_hotkeys = self.store.favorite_hotkeys()
        self.track_table.fit_hotkey_column(
            display_hotkey(favorite_hotkeys.get(track.id)) for track in tracks
        )
        self.track_table.fit_source_column(track.source_name for track in tracks)
        rows: list[tuple[str, tuple[object, ...]]] = []
        for number, track in enumerate(tracks, start=1):
            speaker_state = (
                self.player.mode()
                if track.id == self.current_track_id and self.player.loaded_path
                else "stopped"
            )
            microphone_state = (
                self.virtual_microphone.playback_state()
                if track.id == self.virtual_track_id
                else "stopped"
            )
            rows.append(
                (
                    str(track.id),
                    (
                    number,
                    track.title,
                    "Ⅱ" if speaker_state == "playing" else "▶",
                    "★" if track.id in favorite_ids else "☆",
                    display_hotkey(favorite_hotkeys.get(track.id)),
                    track.source_name,
                    format_duration(track.duration_ms),
                    "Ⅱ" if microphone_state == "playing" else "▶",
                    "■",
                ),
                )
            )
        self.track_table.replace_rows(rows)
        self.track_table.restore_view_anchor(view_anchor)
        if selected_id and self.track_table.exists(selected_id):
            self.track_table.selection_set(selected_id)
            self.track_table.focus(selected_id)

    def _update_track_headings(self) -> None:
        for column, label in self.track_heading_labels.items():
            if column in self.track_heading_icons:
                self.track_table.heading(
                    column,
                    text="",
                    image=self.track_heading_icons[column],
                    anchor="center",
                    command="",
                )
                continue
            if column in {"number", "favorite", "hotkey", "mic_stop"}:
                self.track_table.heading(column, text=label, command="")
                continue
            arrow = ""
            if column == self.track_sort_column:
                arrow = " ▼" if self.track_sort_descending else " ▲"
            self.track_table.heading(
                column,
                text=f"{label}{arrow}",
                command=lambda key=column: self.sort_tracks(key),
            )

    def sort_tracks(self, column: str) -> None:
        if column == self.track_sort_column:
            self.track_sort_descending = not self.track_sort_descending
        else:
            self.track_sort_column = column
            self.track_sort_descending = False
        self._update_track_headings()
        self.refresh_tracks()

    def _selected_track(self) -> Track | None:
        selected = self.track_table.selection()
        return self.store.get_track(int(selected[0])) if selected else None

    def _update_track_buttons(self) -> None:
        # Selection is only a pending choice.  The currently loaded recording
        # must keep playing until the user explicitly presses "Відтворити".
        return

    def _on_track_table_click(self, event: tk.Event) -> None:
        if self.track_table.identify_region(event.x, event.y) != "cell":
            return
        row_id = self.track_table.identify_row(event.y)
        display_column = self.track_table.identify_column(event.x)
        if not row_id or not display_column.startswith("#"):
            return
        display_columns = tuple(self.track_table.cget("displaycolumns"))
        if display_columns == ("#all",):
            display_columns = tuple(self.track_table.cget("columns"))
        index = int(display_column[1:]) - 1
        if not 0 <= index < len(display_columns):
            return
        column = display_columns[index]
        if column == "speaker_play":
            self.track_table.selection_set(row_id)
            self.toggle_track_speaker(int(row_id))
        elif column == "favorite":
            self.track_table.selection_set(row_id)
            self.toggle_favorite(int(row_id))
        elif column == "hotkey":
            self.track_table.selection_set(row_id)
            self.assign_favorite_hotkey(int(row_id))
        elif column == "mic_play":
            self.track_table.selection_set(row_id)
            self.toggle_track_microphone(int(row_id))
        elif column == "mic_stop":
            self.track_table.selection_set(row_id)
            self.stop_track_audio(int(row_id))

    def _on_track_double_click(self, event: tk.Event) -> str | None:
        if self.track_table.identify_region(event.x, event.y) != "cell":
            return None
        row_id = self.track_table.identify_row(event.y)
        display_column = self.track_table.identify_column(event.x)
        if not row_id or not display_column.startswith("#"):
            return None
        display_columns = tuple(self.track_table.cget("displaycolumns"))
        if display_columns == ("#all",):
            display_columns = tuple(self.track_table.cget("columns"))
        index = int(display_column[1:]) - 1
        if not 0 <= index < len(display_columns):
            return None
        if display_columns[index] in {"speaker_play", "favorite", "hotkey", "mic_play", "mic_stop"}:
            return "break"
        self.track_table.selection_set(row_id)
        self.track_table.focus(row_id)
        self.play_selected()
        return "break"

    def toggle_favorite(self, track_id: int) -> None:
        favorite_ids = self.store.favorite_track_ids()
        adding = track_id not in favorite_ids
        try:
            self.store.set_favorite(track_id, adding, FAVORITES_LIMIT)
        except ValueError as error:
            messagebox.showwarning("Favorites", str(error), parent=self.root)
            return
        self._refresh_tray_favorites()
        self._reload_global_hotkeys()
        self.refresh_people()
        self.refresh_tracks()
        self.status_text.set(
            "Додано до Favorites" if adding else "Видалено з Favorites"
        )

    def _reload_global_hotkeys(self) -> dict[int, str]:
        return self.hotkey_manager.replace(self._collect_global_hotkeys())

    def _collect_global_hotkeys(self) -> dict[int, str]:
        bindings = self.store.favorite_hotkeys()
        stop_hotkey = parse_hotkey(str(self.app_settings["stop_hotkey"])).canonical
        valid_bindings: dict[int, str] = {STOP_HOTKEY_ACTION_ID: stop_hotkey}
        for track_id, value in bindings.items():
            try:
                canonical = parse_hotkey(value).canonical
            except ValueError:
                self.store.set_favorite_hotkey(track_id, None)
                continue
            if canonical.casefold() == stop_hotkey.casefold():
                self.store.set_favorite_hotkey(track_id, None)
                continue
            valid_bindings[track_id] = canonical
        return valid_bindings

    def _reload_global_hotkeys_async(self, callback) -> None:
        """Register hotkeys without ever waiting for Windows in Tk's UI thread."""
        self._replace_global_hotkeys_async(self._collect_global_hotkeys(), callback)

    def _replace_global_hotkeys_async(self, bindings: dict[int, str], callback) -> None:
        """Replace a prepared binding set without blocking Tk's event loop."""

        def worker() -> None:
            failures = self.hotkey_manager.replace(bindings)
            self._hotkey_reload_results.put((callback, failures))

        threading.Thread(
            target=worker,
            daemon=True,
            name="AudioLibHotkeyReload",
        ).start()

    def _hide_notice(self) -> None:
        if self._notice_hide_job is not None:
            try:
                self.root.after_cancel(self._notice_hide_job)
            except tk.TclError:
                pass
            self._notice_hide_job = None
        if self._notice_widget is not None:
            try:
                self._notice_widget.destroy()
            except tk.TclError:
                pass
            self._notice_widget = None

    def _show_notice(
        self,
        message: str,
        parent: tk.Misc | None = None,
        *,
        error: bool = False,
    ) -> None:
        """Show one auto-closing, non-modal notification."""
        self._hide_notice()
        host = parent.winfo_toplevel() if parent is not None else self.root
        width = min(720, max(420, host.winfo_width() - 44))
        height = 56
        notice = tk.Canvas(
            host,
            width=width,
            height=height,
            bg=str(host.cget("bg")),
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
        notice._surface_image = render_rounded_surface(
            width,
            height,
            fill="#450a0a" if error else "#422006",
            outline="#dc2626" if error else "#b45309",
            radius=11,
        )
        notice.create_image(0, 0, image=notice._surface_image, anchor="nw")
        notice.create_text(
            width / 2,
            height / 2 - 2,
            text=message,
            fill="#fecaca" if error else "#fde68a",
            font=("Segoe UI Semibold", 10),
            anchor="center",
            width=width - 34,
            justify="center",
        )
        notice.bind("<Button-1>", lambda _event: self._hide_notice())
        bottom_offset = -76 if host is self.root else -16
        notice.place(relx=0.5, rely=1.0, y=bottom_offset, anchor="s")
        self._notice_widget = notice
        self._notice_hide_job = self.root.after(5200, self._hide_notice)

    def _show_hotkey_notice(self, value: str, parent: tk.Misc | None = None) -> None:
        """Warn unobtrusively when a bare key is captured globally."""
        try:
            parsed = parse_hotkey(value)
        except ValueError:
            return
        self._hide_notice()
        if parsed.modifiers:
            return
        self._show_notice(
            (
                f"Клавіша {display_hotkey(value)} працює глобально й може "
                "перехоплюватися під час введення тексту."
            ),
            parent,
        )

    def _poll_global_hotkeys(self) -> None:
        try:
            while True:
                callback, failures = self._hotkey_reload_results.get_nowait()
                callback(failures)
        except queue.Empty:
            pass
        try:
            while True:
                track_id = self._hotkey_events.get_nowait()
                if self._hotkey_capture_active:
                    continue
                if track_id == STOP_HOTKEY_ACTION_ID:
                    self.stop_all_audio()
                else:
                    self.play_track_in_microphone(track_id)
        except queue.Empty:
            pass
        try:
            self.root.after(10, self._poll_global_hotkeys)
        except tk.TclError:
            pass

    def _reload_hotkeys_after_release(self, value: str | None, callback) -> None:
        """Wait for the captured keys to be released without blocking Tk."""
        virtual_keys: list[int] = []
        if value:
            try:
                parsed = parse_hotkey(value)
                virtual_keys.append(parsed.virtual_key)
                if parsed.modifiers & 0x0001:
                    virtual_keys.append(0x12)  # Alt
                if parsed.modifiers & 0x0002:
                    virtual_keys.append(0x11)  # Ctrl
                if parsed.modifiers & 0x0004:
                    virtual_keys.append(0x10)  # Shift
                if parsed.modifiers & 0x0008:
                    virtual_keys.extend((0x5B, 0x5C))  # Win
            except ValueError:
                virtual_keys.clear()
        deadline = time.monotonic() + 2.0

        def check_release() -> None:
            keys_still_pressed = any(
                ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000
                for key in virtual_keys
            )
            if keys_still_pressed and time.monotonic() < deadline:
                self.root.after(40, check_release)
                return
            try:
                callback()
            finally:
                self._hotkey_capture_active = False

        self.root.after(60, check_release)

    def assign_favorite_hotkey(self, track_id: int) -> None:
        if self.selected_person_id != FAVORITES_PERSON_ID:
            return
        track = self.store.get_track(track_id)
        if track is None:
            return
        current_hotkey = self.store.favorite_hotkeys().get(track_id)
        self._hotkey_capture_active = True

        dialog = tk.Toplevel(self.root)
        dialog.configure(bg=BG)
        dialog.geometry("520x255")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        self._build_secondary_title_bar(dialog, "Гаряча клавіша")

        content = tk.Frame(dialog, bg=BG, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text=track.title,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            content,
            text=(
                "Натисніть бажану комбінацію. Вона працюватиме глобально, "
                "навіть коли AudioLib згорнута в трей. Можна призначити "
                "як комбінацію, так і одну окрему клавішу."
            ),
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=465,
            anchor="w",
        ).pack(fill="x", pady=(8, 14))
        captured_text = tk.StringVar(value=display_hotkey(current_hotkey))
        hotkey_label = tk.Label(
            content,
            textvariable=captured_text,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
            height=2,
        )
        hotkey_label.pack(fill="x")
        error_text = tk.StringVar()
        tk.Label(
            content,
            textvariable=error_text,
            bg=BG,
            fg="#fca5a5",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))
        tk.Label(
            content,
            text="Backspace — очистити · Esc — скасувати",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        completed = False
        pending_hotkey: str | None = None
        pending_keycode: int | None = None

        def close_dialog() -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self._reload_hotkeys_after_release(
                None,
                lambda: self._reload_global_hotkeys_async(lambda _failures: None),
            )

        def save_hotkey(value: str | None) -> None:
            nonlocal completed
            try:
                self.store.set_favorite_hotkey(track_id, value)
            except ValueError as error:
                error_text.set(str(error))
                return
            completed = True
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self.refresh_tracks()
            self.status_text.set(
                "Гарячу клавішу очищено"
                if value is None
                else f"Призначено: {display_hotkey(value)}"
            )

            def finish_registration() -> None:
                def registration_finished(failures: dict[int, str]) -> None:
                    if track_id not in failures:
                        if value is not None:
                            self._show_hotkey_notice(value)
                        return
                    self.store.set_favorite_hotkey(track_id, current_hotkey)
                    self._reload_global_hotkeys_async(lambda _failures: None)
                    self.refresh_tracks()
                    self._show_notice(
                        failures[track_id],
                        self.root,
                        error=True,
                    )

                self._reload_global_hotkeys_async(registration_finished)

            self._reload_hotkeys_after_release(value, finish_registration)

        def capture_press(event: tk.Event) -> str:
            nonlocal pending_hotkey, pending_keycode
            if completed:
                return "break"
            if event.keysym == "Escape":
                close_dialog()
                return "break"
            if event.keysym == "BackSpace":
                save_hotkey(None)
                return "break"
            try:
                value = hotkey_from_tk_event(
                    event.keysym,
                    int(event.state),
                    int(event.keycode),
                )
            except ValueError as error:
                error_text.set(str(error))
                return "break"
            if value.casefold() == str(self.app_settings["stop_hotkey"]).casefold():
                error_text.set("Ця комбінація призначена глобальній команді Stop")
                return "break"
            captured_text.set(display_hotkey(value))
            error_text.set("")
            pending_hotkey = value
            pending_keycode = int(event.keycode)
            return "break"

        def capture_release(event: tk.Event) -> str:
            nonlocal pending_hotkey, pending_keycode
            if completed or pending_hotkey is None:
                return "break"
            if int(event.keycode) != pending_keycode:
                return "break"
            value = pending_hotkey
            pending_hotkey = None
            pending_keycode = None

            def finish_capture() -> None:
                if not completed:
                    save_hotkey(value)

            dialog.after(30, finish_capture)
            return "break"

        dialog._audiolib_close_callback = close_dialog
        dialog.bind("<KeyPress>", capture_press)
        dialog.bind("<KeyRelease>", capture_release)
        dialog.update_idletasks()
        x = self.root.winfo_x() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        def activate_capture() -> None:
            try:
                if dialog.winfo_exists():
                    dialog.grab_set()
                    dialog.focus_force()
            except tk.TclError:
                pass

        # Start capture only after Windows confirms that old global bindings
        # are gone; otherwise the key can be consumed before Tk sees it.
        self._replace_global_hotkeys_async({}, lambda _failures: activate_capture())

    def assign_stop_hotkey(self, parent: tk.Misc, on_saved=None) -> None:
        current_hotkey = str(self.app_settings["stop_hotkey"])
        self._hotkey_capture_active = True

        dialog = tk.Toplevel(parent)
        dialog.configure(bg=BG)
        dialog.geometry("520x245")
        dialog.resizable(False, False)
        dialog.transient(parent)
        self._build_secondary_title_bar(dialog, "Гаряча клавіша Stop")

        content = tk.Frame(dialog, bg=BG, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text="Зупинити все відтворення",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            content,
            text=(
                "Натисніть нову комбінацію. Вона зупинятиме звук у динаміках "
                "і віртуальному мікрофоні, навіть коли AudioLib згорнута в трей. "
                "Також можна призначити одну окрему клавішу."
            ),
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=465,
            anchor="w",
        ).pack(fill="x", pady=(8, 14))
        captured_text = tk.StringVar(value=display_hotkey(current_hotkey))
        tk.Label(
            content,
            textvariable=captured_text,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
            height=2,
        ).pack(fill="x")
        error_text = tk.StringVar()
        tk.Label(
            content,
            textvariable=error_text,
            bg=BG,
            fg="#fca5a5",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

        completed = False
        pending_hotkey: str | None = None
        pending_keycode: int | None = None

        def close_dialog() -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self._reload_hotkeys_after_release(
                None,
                lambda: self._reload_global_hotkeys_async(lambda _failures: None),
            )

        def capture_press(event: tk.Event) -> str:
            nonlocal completed, pending_hotkey, pending_keycode
            if completed:
                return "break"
            if event.keysym == "Escape":
                close_dialog()
                return "break"
            try:
                value = hotkey_from_tk_event(
                    event.keysym,
                    int(event.state),
                    int(event.keycode),
                )
            except ValueError as error:
                error_text.set(str(error))
                return "break"
            favorite_values = {
                parse_hotkey(item).canonical.casefold()
                for item in self.store.favorite_hotkeys().values()
            }
            if value.casefold() in favorite_values:
                error_text.set("Ця комбінація вже призначена запису з Favorites")
                return "break"
            captured_text.set(display_hotkey(value))
            error_text.set("")
            pending_hotkey = value
            pending_keycode = int(event.keycode)
            return "break"

        def capture_release(event: tk.Event) -> str:
            nonlocal completed, pending_hotkey, pending_keycode
            if completed or pending_hotkey is None:
                return "break"
            if int(event.keycode) != pending_keycode:
                return "break"
            value = pending_hotkey
            pending_hotkey = None
            pending_keycode = None

            def finish_capture() -> None:
                nonlocal completed
                if completed:
                    return
                self.app_settings["stop_hotkey"] = value
                self._save_volume_setting()
                completed = True
                try:
                    dialog.grab_release()
                    dialog.destroy()
                except tk.TclError:
                    pass
                if on_saved is not None:
                    on_saved(value)
                self.status_text.set(
                    f"Гаряча клавіша Stop: {display_hotkey(value)}"
                )

                def finish_registration() -> None:
                    def registration_finished(failures: dict[int, str]) -> None:
                        if STOP_HOTKEY_ACTION_ID not in failures:
                            self._show_hotkey_notice(value, parent)
                            return
                        self.app_settings["stop_hotkey"] = current_hotkey
                        self._save_volume_setting()
                        self._reload_global_hotkeys_async(lambda _failures: None)
                        if on_saved is not None:
                            on_saved(current_hotkey)
                        self._show_notice(
                            failures[STOP_HOTKEY_ACTION_ID],
                            parent,
                            error=True,
                        )

                    self._reload_global_hotkeys_async(registration_finished)

                self._reload_hotkeys_after_release(value, finish_registration)

            dialog.after(30, finish_capture)
            return "break"

        dialog._audiolib_close_callback = close_dialog
        dialog.bind("<KeyPress>", capture_press)
        dialog.bind("<KeyRelease>", capture_release)
        dialog.update_idletasks()
        owner = parent.winfo_toplevel()
        x = owner.winfo_x() + max(0, (owner.winfo_width() - dialog.winfo_width()) // 2)
        y = owner.winfo_y() + max(0, (owner.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        def activate_capture() -> None:
            try:
                if dialog.winfo_exists():
                    dialog.grab_set()
                    dialog.focus_force()
            except tk.TclError:
                pass

        self._replace_global_hotkeys_async({}, lambda _failures: activate_capture())

    def play_random_track(self) -> None:
        if self.selected_person_id is None:
            messagebox.showinfo(
                "Спочатку оберіть людину",
                "Випадковий запис обирається для поточної людини.",
                parent=self.root,
            )
            return
        tracks = (
            self.store.list_favorites()
            if self.selected_person_id == FAVORITES_PERSON_ID
            else self.store.list_tracks(self.selected_person_id)
        )
        if not tracks:
            messagebox.showinfo(
                "Немає записів",
                (
                    "У Favorites ще немає аудіозаписів."
                    if self.selected_person_id == FAVORITES_PERSON_ID
                    else "Для вибраної людини ще немає аудіозаписів."
                ),
                parent=self.root,
            )
            return
        track = random.choice(tracks)
        if self.track_query.get():
            self.track_query.set("")
        if self.track_table.exists(str(track.id)):
            self.track_table.selection_set(str(track.id))
            self.track_table.focus(str(track.id))
            self.track_table.see(str(track.id))
        self.play_selected()

    def play_selected(self) -> None:
        track = self._selected_track()
        if not track:
            return
        self.play_track_in_speaker(track.id)

    def _save_track_normalization(
        self, track_id: int, analysis: tuple[float, float] | None
    ) -> None:
        if analysis is None:
            return
        loudness_db, normalization_gain = analysis
        self.store.set_track_normalization(
            track_id, loudness_db, normalization_gain
        )

    def _prepare_track_audio(self, target: str, track: Track, path: Path) -> None:
        """Decode and analyse a track without blocking Tk's event loop."""
        if target == "speaker":
            self._speaker_load_serial += 1
            serial = self._speaker_load_serial
            self._speaker_load_track_id = track.id
        else:
            self._microphone_load_serial += 1
            serial = self._microphone_load_serial
            self._microphone_load_track_id = track.id

        cached_gain = track.normalization_gain
        self.status_text.set(
            f"Підготовка запису: {track.title}"
            if cached_gain is not None and cached_gain > 0
            else f"Аналіз гучності: {track.title}"
        )

        def worker() -> None:
            try:
                samples = decode_pcm16_file(path, channels=2, sample_rate=48_000)
                current_serial = (
                    self._speaker_load_serial
                    if target == "speaker"
                    else self._microphone_load_serial
                )
                if serial != current_serial:
                    return
                analysis = None
                normalization_gain = cached_gain
                if normalization_gain is None or normalization_gain <= 0:
                    analysis = analyze_pcm16_loudness(
                        samples, channels=2, sample_rate=48_000
                    )
                    normalization_gain = analysis[1]
                current_serial = (
                    self._speaker_load_serial
                    if target == "speaker"
                    else self._microphone_load_serial
                )
                if serial != current_serial:
                    return
                self._audio_load_results.put(
                    (
                        target,
                        serial,
                        track.id,
                        path,
                        samples,
                        float(normalization_gain),
                        analysis,
                        None,
                    )
                )
            except Exception as error:
                self._audio_load_results.put(
                    (target, serial, track.id, path, None, 1.0, None, str(error))
                )

        threading.Thread(
            target=worker,
            name=f"AudioLib-{target}-loader",
            daemon=True,
        ).start()

    def _poll_audio_load_results(self) -> None:
        try:
            while True:
                (
                    target,
                    serial,
                    track_id,
                    path,
                    samples,
                    normalization_gain,
                    analysis,
                    error_text,
                ) = self._audio_load_results.get_nowait()
                expected_serial = (
                    self._speaker_load_serial
                    if target == "speaker"
                    else self._microphone_load_serial
                )
                if serial != expected_serial:
                    continue
                if target == "speaker":
                    self._speaker_load_track_id = None
                else:
                    self._microphone_load_track_id = None

                track = self.store.get_track(int(track_id))
                if track is None:
                    self.status_text.set("Запис більше не існує")
                    continue
                if error_text:
                    messagebox.showerror(
                        "Помилка відтворення",
                        f"Не вдалося підготувати аудіофайл: {error_text}",
                        parent=self.root,
                    )
                    self.status_text.set("Не вдалося відкрити аудіозапис")
                    continue

                self._save_track_normalization(track.id, analysis)
                normalize = bool(self.app_settings["normalize_loudness"])
                if target == "speaker":
                    try:
                        self.player.load_decoded(
                            path,
                            samples,
                            normalization_gain=normalization_gain,
                            normalize=normalize,
                        )
                        self.player.set_volume(self.volume_value)
                        self.current_track_id = track.id
                        self.now_playing.set(track.title)
                        self.current_track_length_ms = (
                            self.player.length() or max(0, track.duration_ms or 0)
                        )
                        self.progress.set_range(max(1, self.current_track_length_ms))
                        self.progress.set(0)
                        self.time_text.set(
                            f"0:00 / {format_time(self.current_track_length_ms)}"
                        )
                        self.player.play()
                        self.play_pause_button.configure(text="Ⅱ")
                        self.status_text.set(f"Відтворюється: {track.title}")
                        self.refresh_tracks()
                    except PlayerError as error:
                        messagebox.showerror(
                            "Помилка відтворення", str(error), parent=self.root
                        )
                else:
                    payload = (
                        serial,
                        track.id,
                        samples,
                        normalization_gain,
                        normalize,
                    )
                    if self.virtual_microphone.is_running():
                        self._play_pending_microphone_audio(payload)
                    else:
                        self._pending_microphone_playback = payload
                        if not self._begin_virtual_microphone_start():
                            self._pending_microphone_playback = None
        except queue.Empty:
            pass
        try:
            self.root.after(10, self._poll_audio_load_results)
        except tk.TclError:
            pass

    def _play_pending_microphone_audio(
        self, payload: tuple[object, ...]
    ) -> None:
        serial, track_id, samples, normalization_gain, normalize = payload
        if int(serial) != self._microphone_load_serial:
            return
        track = self.store.get_track(int(track_id))
        if track is None:
            return
        try:
            self.virtual_microphone.play_decoded(
                samples,
                self.volume_value / 1000,
                normalization_gain=float(normalization_gain),
                normalize=bool(normalize),
            )
            self.virtual_track_id = track.id
            self.status_text.set(f"У мікрофон: {track.title}")
            self.refresh_tracks()
        except VirtualAudioError as error:
            if messagebox.askyesno(
                "Віртуальний мікрофон",
                f"{error}\n\nВідкрити налаштування?",
                parent=self.root,
            ):
                self.open_virtual_microphone_setup()

    def play_track_in_speaker(self, track_id: int) -> None:
        track = self.store.get_track(track_id)
        if not track:
            self.status_text.set("Запис більше не існує")
            self._refresh_tray_favorites()
            return
        path = self.store.absolute(track.file_path)
        if not path or not path.exists():
            messagebox.showerror("Файл не знайдено", "Аудіофайл відсутній у бібліотеці.", parent=self.root)
            return
        try:
            self.player.stop()
        except PlayerError:
            pass
        self._prepare_track_audio("speaker", track, path)

    def toggle_track_speaker(self, track_id: int) -> None:
        track = self.store.get_track(track_id)
        if not track:
            return
        if self.current_track_id == track_id and self.player.loaded_path:
            try:
                mode = self.player.mode()
                if mode == "playing":
                    self.player.pause()
                    self.play_pause_button.configure(text="▶")
                    self.status_text.set(f"Пауза: {track.title}")
                elif mode == "paused":
                    self.player.resume()
                    self.play_pause_button.configure(text="Ⅱ")
                    self.status_text.set(f"Відтворюється: {track.title}")
                else:
                    self.player.play()
                    self.play_pause_button.configure(text="Ⅱ")
                    self.status_text.set(f"Відтворюється: {track.title}")
                self.refresh_tracks()
                return
            except PlayerError as error:
                messagebox.showerror("Помилка відтворення", str(error), parent=self.root)
                return
        if self.track_table.exists(str(track_id)):
            self.track_table.selection_set(str(track_id))
            self.track_table.focus(str(track_id))
        self.play_track_in_speaker(track_id)

    def toggle_track_microphone(self, track_id: int) -> None:
        track = self.store.get_track(track_id)
        if not track:
            return
        if self.virtual_track_id == track.id:
            state = self.virtual_microphone.playback_state()
            if state == "playing":
                self.virtual_microphone.pause_sound()
                self.status_text.set(f"Пауза у мікрофоні: {track.title}")
                self.refresh_tracks()
                return
            if state == "paused":
                self.virtual_microphone.resume_sound()
                self.status_text.set(f"У мікрофон: {track.title}")
                self.refresh_tracks()
                return
        self.play_track_in_microphone(track_id)

    def play_track_in_microphone(self, track_id: int) -> None:
        track = self.store.get_track(track_id)
        if not track:
            self.status_text.set("Запис із Favorites більше не існує")
            self._refresh_tray_favorites()
            return
        path = self.store.absolute(track.file_path)
        if not path or not path.exists():
            messagebox.showerror("Файл не знайдено", "Аудіофайл відсутній у бібліотеці.", parent=self.root)
            return
        self.virtual_microphone.stop_sound()
        self.virtual_track_id = None
        self._prepare_track_audio("microphone", track, path)

    def stop_track_audio(self, track_id: int) -> None:
        cancelled_pending = False
        if self._speaker_load_track_id == track_id:
            self._speaker_load_serial += 1
            self._speaker_load_track_id = None
            cancelled_pending = True
        if self._microphone_load_track_id == track_id:
            self._microphone_load_serial += 1
            self._microphone_load_track_id = None
            cancelled_pending = True
        stopped_speaker = self.current_track_id == track_id and bool(self.player.loaded_path)
        stopped_microphone = self.virtual_track_id == track_id
        if stopped_speaker:
            try:
                self.player.stop()
            except PlayerError:
                pass
            self.progress.set(0)
            self.play_pause_button.configure(text="▶")
            self.time_text.set(f"0:00 / {format_time(self.current_track_length_ms)}")
        if stopped_microphone:
            self.virtual_microphone.stop_sound()
            self.virtual_track_id = None
        if stopped_speaker or stopped_microphone:
            self.status_text.set("Відтворення запису повністю зупинено")
        elif cancelled_pending:
            self.status_text.set("Підготовку запису скасовано")
        self.refresh_tracks()

    def stop_virtual_microphone_sound(self) -> None:
        self._microphone_load_serial += 1
        self._microphone_load_track_id = None
        self.virtual_microphone.stop_sound()
        self.virtual_track_id = None
        self.status_text.set("Звук у віртуальному мікрофоні зупинено")
        self.refresh_tracks()

    def open_virtual_microphone_setup(self) -> None:
        existing = getattr(self, "settings_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("Налаштування")
        window_width = 620
        window_height = 650
        self.root.update_idletasks()
        window_x = self.root.winfo_x() + max(0, (self.root.winfo_width() - window_width) // 2)
        window_y = self.root.winfo_y() + max(0, (self.root.winfo_height() - window_height) // 2)
        window.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
        window.resizable(False, False)
        window.configure(bg=BG)
        window.transient(self.root)
        self._build_secondary_title_bar(window, "Налаштування")

        def close_settings() -> None:
            if getattr(self, "settings_window", None) is window:
                self.settings_window = None
            window.destroy()

        window._audiolib_close_callback = close_settings
        window.bind(
            "<Destroy>",
            lambda event: setattr(self, "settings_window", None)
            if event.widget is window and getattr(self, "settings_window", None) is window
            else None,
            add="+",
        )
        status = tk.StringVar()

        shell = tk.Frame(window, bg=BG)
        shell.pack(fill="both", expand=True)
        settings_canvas = tk.Canvas(
            shell, bg=BG, highlightthickness=0, bd=0
        )
        settings_scrollbar = ttk.Scrollbar(
            shell,
            orient="vertical",
            command=settings_canvas.yview,
            style="AudioLib.Vertical.TScrollbar",
        )
        content = tk.Frame(settings_canvas, bg=BG)
        content_window = settings_canvas.create_window(
            (0, 0), window=content, anchor="nw"
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_scrollbar.pack(side="right", fill="y", pady=8)
        content.bind(
            "<Configure>",
            lambda _event: settings_canvas.configure(
                scrollregion=settings_canvas.bbox("all")
            ),
        )
        settings_canvas.bind(
            "<Configure>",
            lambda event: settings_canvas.itemconfigure(
                content_window, width=event.width
            ),
        )
        window.bind(
            "<MouseWheel>",
            lambda event: settings_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )

        def add_section_heading(text: str, top_padding: int = 18) -> None:
            section_header = tk.Frame(content, bg=BG)
            section_header.pack(
                fill="x", padx=22, pady=(top_padding, 8)
            )
            tk.Frame(section_header, bg="#334155", height=1).pack(
                side="left", fill="x", expand=True, padx=(0, 10)
            )
            tk.Label(
                section_header,
                text=text,
                bg=BG,
                fg=MUTED,
                font=("Segoe UI Semibold", 10),
            ).pack(side="left")
            tk.Frame(section_header, bg="#334155", height=1).pack(
                side="left", fill="x", expand=True, padx=(10, 0)
            )

        autostart_value = tk.BooleanVar(
            value=bool(self.app_settings["autostart_enabled"])
        )
        minimized_value = tk.BooleanVar(
            value=bool(self.app_settings["start_minimized"])
        )
        automatic_microphone_value = tk.BooleanVar(
            value=bool(self.app_settings["auto_virtual_microphone"])
        )
        close_action_value = tk.StringVar(
            value=str(self.app_settings["close_action"])
        )
        startup_view_value = tk.StringVar(
            value=str(self.app_settings["startup_view"])
        )
        language_value = tk.StringVar(value="Українська")
        output_device_value = tk.StringVar()
        local_monitoring_value = tk.BooleanVar(
            value=bool(self.app_settings["local_monitoring"])
        )
        normalize_loudness_value = tk.BooleanVar(
            value=bool(self.app_settings["normalize_loudness"])
        )
        latency_value = tk.StringVar()
        accent_value = tk.StringVar(value=str(self.app_settings["accent_color"]))
        photo_size_value = tk.StringVar()
        density_value = tk.StringVar()

        def save_setting(name: str, value: object) -> None:
            self.app_settings[name] = value
            self._save_volume_setting()

        def apply_autostart() -> None:
            enabled = autostart_value.get()
            try:
                if enabled:
                    register_autostart(self.root_dir, minimized_value.get())
                else:
                    unregister_autostart()
            except OSError as error:
                autostart_value.set(not enabled)
                messagebox.showerror("Автозапуск", str(error), parent=window)
                return
            save_setting("autostart_enabled", enabled)
            minimized_switch.set_enabled(enabled)

        def apply_start_minimized() -> None:
            value = minimized_value.get()
            if autostart_value.get():
                try:
                    register_autostart(self.root_dir, value)
                except OSError as error:
                    minimized_value.set(not value)
                    messagebox.showerror("Автозапуск", str(error), parent=window)
                    return
            save_setting("start_minimized", value)

        def switch_row(
            parent: tk.Misc,
            title: str,
            description: str,
            variable: tk.BooleanVar,
            command,
        ) -> ToggleSwitch:
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", pady=5)
            labels = tk.Frame(row, bg=PANEL)
            labels.pack(side="left", fill="x", expand=True)
            tk.Label(
                labels,
                text=title,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 10),
            ).pack(fill="x")
            tk.Label(
                labels,
                text=description,
                bg=PANEL,
                fg=MUTED,
                anchor="w",
                justify="left",
                wraplength=445,
                font=("Segoe UI", 9),
            ).pack(fill="x")
            toggle = ToggleSwitch(row, variable, command)
            toggle.pack(side="right", padx=(12, 0))
            return toggle

        add_section_heading("Загальні")
        general_panel = tk.Frame(content, bg=PANEL, padx=14, pady=8)
        general_panel.pack(fill="x", padx=22)
        switch_row(
            general_panel,
            "Запускати AudioLib разом із Windows",
            "Додає поточну програму до автозапуску цього користувача.",
            autostart_value,
            apply_autostart,
        )
        minimized_switch = switch_row(
            general_panel,
            "Запускати згорнутою в трей",
            "Працює лише для автоматичного запуску разом із Windows.",
            minimized_value,
            apply_start_minimized,
        )
        minimized_switch.set_enabled(autostart_value.get())
        switch_row(
            general_panel,
            "Автоматично вмикати віртуальний мікрофон",
            "Активує VB-CABLE одразу після запуску AudioLib.",
            automatic_microphone_value,
            lambda: save_setting(
                "auto_virtual_microphone", automatic_microphone_value.get()
            ),
        )

        def radio_group(
            title: str,
            variable: tk.StringVar,
            options: tuple[tuple[str, str], ...],
            setting_name: str,
        ) -> None:
            row = tk.Frame(general_panel, bg=PANEL)
            row.pack(fill="x", pady=(7, 3))
            tk.Label(
                row,
                text=title,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 10),
            ).pack(fill="x")
            choices = tk.Frame(row, bg=PANEL)
            choices.pack(fill="x", pady=(3, 0))
            for label, value in options:
                SmoothRadioButton(
                    choices,
                    text=label,
                    value=value,
                    variable=variable,
                    command=lambda name=setting_name, var=variable: save_setting(
                        name, var.get()
                    ),
                ).pack(side="left", padx=(0, 18))

        radio_group(
            "При закритті вікна",
            close_action_value,
            (("Згортати в трей", "tray"), ("Повністю завершувати", "exit")),
            "close_action",
        )
        radio_group(
            "Відкривати при запуску",
            startup_view_value,
            (("Favorites", "favorites"), ("Останнього користувача", "last")),
            "startup_view",
        )

        language_row = tk.Frame(general_panel, bg=PANEL)
        language_row.pack(fill="x", pady=(8, 3))
        tk.Label(
            language_row,
            text="Мова",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x", pady=(0, 5))
        language_dropdown = RoundedDropdown(
            language_row,
            textvariable=language_value,
            command=lambda _index: save_setting("language", "uk"),
            width=520,
        )
        language_dropdown.configure(values=["Українська"])
        language_dropdown.current(0)
        language_dropdown.pack(fill="x")

        window_size_row = tk.Frame(general_panel, bg=PANEL)
        window_size_row.pack(fill="x", pady=(10, 5))
        window_size_labels = tk.Frame(window_size_row, bg=PANEL)
        window_size_labels.pack(side="left", fill="x", expand=True)
        tk.Label(
            window_size_labels,
            text="Розмір вікна",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")
        tk.Label(
            window_size_labels,
            text="Повертає стандартний розмір і розташування.",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 9),
        ).pack(fill="x")

        def reset_main_window_geometry() -> None:
            geometry = self._default_window_geometry()
            self.root.geometry(geometry)
            self._window_mode = "normal"
            self._restore_geometry = geometry
            self.maximize_button.configure(
                image=self.titlebar_control_icons["maximize"]
            )
            self._show_resize_grips(True)
            self.app_settings["window_geometry"] = geometry
            self._save_volume_setting()
            status.set("Розмір вікна повернуто за замовчуванням")

        reset_window_button = self._button(
            window_size_row,
            "Повернути за замовчуванням",
            reset_main_window_geometry,
            "#475569",
        )
        reset_window_button.pack(side="right", padx=(12, 0))

        add_section_heading("Відтворення")
        playback_panel = tk.Frame(content, bg=PANEL, padx=14, pady=10)
        playback_panel.pack(fill="x", padx=22)

        output_row = tk.Frame(playback_panel, bg=PANEL)
        output_row.pack(fill="x", pady=(0, 9))
        tk.Label(
            output_row,
            text="Пристрій виведення:",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 9))
        output_names = [
            name for name in list_output_devices()
            if not ("cable" in name.casefold() and "vb-audio" in name.casefold())
        ]
        output_labels = ["Автоматично (фізичний пристрій)", *output_names]
        saved_output = str(self.app_settings["output_device"])
        output_device_value.set(
            saved_output if saved_output in output_names else output_labels[0]
        )

        def apply_output_device() -> None:
            selected = output_device_value.get()
            device_name = "" if selected == output_labels[0] else selected
            try:
                self.player.set_output_device(device_name)
            except PlayerError as error:
                messagebox.showerror("Пристрій виведення", str(error), parent=window)
                return
            save_setting("output_device", device_name)
            bridge_was_running = self.virtual_microphone.is_running()
            self.virtual_microphone.configure(
                latency_profile=str(self.app_settings["latency_profile"]),
                local_monitoring=bool(self.app_settings["local_monitoring"]),
                monitor_output_name=device_name,
            )
            if bridge_was_running:
                self._start_virtual_microphone_background()
            self.status_text.set("Пристрій виведення змінено")

        output_box = RoundedDropdown(
            output_row,
            textvariable=output_device_value,
            command=lambda _index: apply_output_device(),
            width=365,
            placeholder="Оберіть пристрій виведення",
        )
        output_box.configure(values=output_labels)
        output_box.current(output_labels.index(output_device_value.get()))
        output_box.pack(side="left", fill="x", expand=True)

        def apply_loudness_normalization() -> None:
            enabled = normalize_loudness_value.get()
            save_setting("normalize_loudness", enabled)
            self.player.set_normalization_enabled(enabled)
            self.virtual_microphone.set_normalization_enabled(enabled)
            self.status_text.set(
                "Вирівнювання гучності ввімкнено"
                if enabled else "Вирівнювання гучності вимкнено"
            )

        switch_row(
            playback_panel,
            "Вирівнювати гучність записів",
            "Автоматично підсилювати тихі й приглушувати гучні записи без зміни файлів.",
            normalize_loudness_value,
            apply_loudness_normalization,
        )

        def apply_local_monitoring() -> None:
            enabled = local_monitoring_value.get()
            save_setting("local_monitoring", enabled)
            bridge_was_running = self.virtual_microphone.is_running()
            self.virtual_microphone.configure(
                latency_profile=str(self.app_settings["latency_profile"]),
                local_monitoring=enabled,
                monitor_output_name=str(self.app_settings["output_device"]),
            )
            if bridge_was_running:
                self._start_virtual_microphone_background()
            self.status_text.set(
                "Локальне прослуховування ввімкнено"
                if enabled else "Локальне прослуховування вимкнено"
            )

        switch_row(
            playback_panel,
            "Локальне прослуховування",
            "Відтворювати у вибрані навушники записи з віртуального мікрофона, "
            "не спрямовуючи копію назад у VB-CABLE.",
            local_monitoring_value,
            apply_local_monitoring,
        )

        stop_hotkey_row = tk.Frame(playback_panel, bg=PANEL)
        stop_hotkey_row.pack(fill="x", pady=(9, 2))
        stop_hotkey_labels = tk.Frame(stop_hotkey_row, bg=PANEL)
        stop_hotkey_labels.pack(side="left", fill="x", expand=True)
        tk.Label(
            stop_hotkey_labels,
            text="Гаряча клавіша Stop",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")
        tk.Label(
            stop_hotkey_labels,
            text="Зупиняє будь-який активний запис у динаміках і мікрофоні.",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=330,
            font=("Segoe UI", 9),
        ).pack(fill="x")

        stop_hotkey_button: tk.Widget

        def update_stop_hotkey_button(value: str) -> None:
            stop_hotkey_button.configure(text=display_hotkey(value))

        stop_hotkey_button = self._button(
            stop_hotkey_row,
            display_hotkey(str(self.app_settings["stop_hotkey"])),
            lambda: self.assign_stop_hotkey(window, update_stop_hotkey_button),
            "#475569",
        )
        stop_hotkey_button.pack(side="right", padx=(12, 0))

        add_section_heading("Віртуальний мікрофон")

        header = tk.Frame(content, bg=BG)
        header.pack(fill="x", padx=22, pady=(0, 4))
        tk.Label(
            header, text="VB-CABLE", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 20),
        ).pack(side="left")
        self._button(
            header,
            "Офіційна сторінка VB-Audio",
            lambda: webbrowser.open(DRIVER_PAGE),
            "#475569",
        ).pack(side="right")
        tk.Label(
            content,
            text=(
                "AudioLib використовує офіційний підписаний драйвер VB-Audio. "
                "Встановлення не виконується автоматично: Windows покаже запит адміністратора, "
                "а потім вікно інсталятора. Після встановлення або видалення перезавантажте ПК."
            ),
            bg=BG, fg=MUTED, justify="left", wraplength=570,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=22, pady=(0, 12))
        status_label = tk.Label(
            content, textvariable=status, bg=PANEL, fg=TEXT, anchor="w",
            padx=12, pady=10, font=("Segoe UI Semibold", 10),
        )
        status_label.pack(fill="x", padx=22, pady=6)

        microphone_choices: list[CaptureEndpoint] = []
        microphone_value = tk.StringVar()
        microphone_row = tk.Frame(content, bg=BG)
        microphone_row.pack(fill="x", padx=22, pady=(2, 6))
        tk.Label(
            microphone_row,
            text="Фізичний мікрофон:",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 9))
        microphone_box = RoundedDropdown(
            microphone_row,
            textvariable=microphone_value,
            command=lambda _index: apply_microphone(),
            placeholder="Оберіть фізичний мікрофон",
        )

        def load_microphones() -> None:
            nonlocal microphone_choices
            try:
                microphone_choices = list_physical_microphones()
            except AudioEndpointError:
                microphone_choices = []
            labels = [microphone_label(item) for item in microphone_choices]
            microphone_box.configure(values=labels)
            selected = self.microphone_routing.selected_microphone()
            selected_index = next(
                (
                    index for index, item in enumerate(microphone_choices)
                    if selected is not None and item.endpoint_id == selected.endpoint_id
                ),
                0 if microphone_choices else -1,
            )
            if selected_index >= 0:
                microphone_box.current(selected_index)
            else:
                microphone_box.current(-1)

        def apply_microphone() -> None:
            index = microphone_box.current()
            if index < 0 or index >= len(microphone_choices):
                messagebox.showwarning(
                    "Вибір мікрофона",
                    "Оберіть фізичний мікрофон зі списку.",
                    parent=window,
                )
                return
            self._apply_physical_microphone(
                microphone_choices[index],
                window,
                show_confirmation=False,
                on_complete=refresh_status,
            )

        microphone_box.pack(side="left", fill="x", expand=True)

        latency_row = tk.Frame(content, bg=BG)
        latency_row.pack(fill="x", padx=22, pady=(4, 6))
        tk.Label(
            latency_row,
            text="Затримка / стабільність:",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 9))
        latency_labels = {
            "low": "Мінімальна затримка",
            "balanced": "Збалансовано",
            "stable": "Максимальна стабільність",
        }
        latency_keys = list(latency_labels)
        latency_options = [latency_labels[key] for key in latency_keys]
        current_latency = str(self.app_settings["latency_profile"])
        latency_value.set(latency_labels[current_latency])

        def apply_latency_profile() -> None:
            try:
                profile = latency_keys[latency_options.index(latency_value.get())]
            except (ValueError, IndexError):
                return
            save_setting("latency_profile", profile)
            bridge_was_running = self.virtual_microphone.is_running()
            self.virtual_microphone.configure(
                latency_profile=profile,
                local_monitoring=bool(self.app_settings["local_monitoring"]),
                monitor_output_name=str(self.app_settings["output_device"]),
            )
            if bridge_was_running:
                self._start_virtual_microphone_background()
            self.status_text.set("Профіль віртуального мікрофона змінено")

        latency_box = RoundedDropdown(
            latency_row,
            textvariable=latency_value,
            command=lambda _index: apply_latency_profile(),
            width=350,
            placeholder="Оберіть профіль",
        )
        latency_box.configure(values=latency_options)
        latency_box.current(latency_options.index(latency_value.get()))
        latency_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            content,
            text=(
                "Увага: після встановлення Windows може автоматично вибрати CABLE Input "
                "як основний динамік. Якщо звук у колонках або навушниках зникне, відкрийте "
                "Параметри → Система → Звук і поверніть свій основний пристрій, наприклад "
                "Динаміки (Realtek Audio)."
            ),
            bg="#422006", fg="#fde68a", justify="left", wraplength=548,
            padx=11, pady=9, font=("Segoe UI Semibold", 9),
        ).pack(fill="x", padx=22, pady=(6, 4))

        def refresh_status() -> None:
            installed, text = self.virtual_microphone.device_status()
            status.set(("✓ " if installed else "• ") + text)
            status_label.configure(fg="#34d399" if installed else "#fbbf24")
            load_microphones()

        def prepare_and_launch(removing: bool) -> None:
            status.set("Завантаження та перевірка офіційного пакета…")

            def worker() -> None:
                try:
                    setup = prepare_official_installer()
                    self.root.after(0, lambda: finish(setup, removing))
                except Exception as error:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Помилка VB-CABLE", str(error), parent=window
                    ))
                    self.root.after(0, refresh_status)

            threading.Thread(target=worker, daemon=True).start()

        def finish(setup: Path, removing: bool) -> None:
            action = "Remove Driver" if removing else "Install Driver"
            messagebox.showinfo(
                "VB-CABLE",
                f"У вікні VB-CABLE натисніть «{action}».\n"
                "Після завершення перезавантажте Windows.\n\n"
                "Якщо Windows перемкне основний звук на CABLE Input, поверніть у "
                "налаштуваннях звуку свої динаміки або навушники.",
                parent=window,
            )
            try:
                launch_driver_setup(setup)
                status.set("Інсталятор запущено. Потім перезавантажте Windows.")
            except VirtualAudioError as error:
                messagebox.showerror("Помилка VB-CABLE", str(error), parent=window)
                refresh_status()

        buttons = tk.Frame(content, bg=BG)
        buttons.pack(fill="x", padx=22, pady=10)
        for column in range(3):
            buttons.columnconfigure(column, weight=1, uniform="virtual-mic-actions")
        self._button(
            buttons, "Встановити", lambda: prepare_and_launch(False), "#0f766e"
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._button(
            buttons, "Видалити / відкотити", lambda: prepare_and_launch(True), DANGER
        ).grid(row=0, column=1, sticky="ew", padx=4)
        self._button(
            buttons, "Перевірити", refresh_status, "#475569"
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))
        tk.Label(
            content,
            text=(
                "Після встановлення оберіть CABLE Output як мікрофон у Discord/грі. "
                "AudioLib змішуватиме звук з вашим фізичним мікрофоном."
            ),
            bg=BG, fg=MUTED, justify="left", wraplength=570, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=22)

        add_section_heading("Бібліотека")
        library_panel = tk.Frame(content, bg=PANEL, padx=14, pady=12)
        library_panel.pack(fill="x", padx=22)
        tk.Label(
            library_panel,
            text="Резервна копія містить базу, аудіозаписи, зображення та налаштування.",
            bg=PANEL,
            fg=MUTED,
            justify="left",
            wraplength=530,
            font=("Segoe UI", 9),
        ).pack(fill="x", pady=(0, 9))
        library_actions = tk.Frame(library_panel, bg=PANEL)
        library_actions.pack(fill="x")
        for column in range(2):
            library_actions.columnconfigure(column, weight=1, uniform="library-actions")
        library_status_value = tk.StringVar()
        library_progress_row = tk.Frame(library_panel, bg=PANEL)
        tk.Label(
            library_progress_row,
            textvariable=library_status_value,
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 9),
        ).pack(fill="x", pady=(0, 5))
        library_progress = ttk.Progressbar(
            library_progress_row,
            mode="indeterminate",
            style="Settings.Horizontal.TProgressbar",
        )
        library_progress.pack(fill="x")

        def set_library_busy(active: bool, text: str = "") -> None:
            library_status_value.set(text)
            state = "disabled" if active else "normal"
            backup_button.configure(state=state)
            import_button.configure(state=state)
            if active:
                library_progress_row.pack(fill="x", pady=(10, 0))
                library_progress.start(12)
                window._audiolib_close_allowed = False
                try:
                    window.grab_set()
                except tk.TclError:
                    pass
            else:
                library_progress.stop()
                library_progress_row.pack_forget()
                window._audiolib_close_allowed = True
                try:
                    window.grab_release()
                except tk.TclError:
                    pass

        def make_library_backup() -> None:
            try:
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                selected = choose_save_file(
                    window,
                    title="Зберегти резервну копію AudioLib",
                    filetypes=[("Резервна копія AudioLib", "*.zip")],
                    default_name=f"AudioLib_backup_{stamp}.zip",
                    default_extension="zip",
                )
                if not selected:
                    return
                self.store.connection.commit()
            except (FileDialogError, OSError) as error:
                messagebox.showerror("Резервна копія", str(error), parent=window)
                return
            destination = Path(selected)
            set_library_busy(True, "Створення резервної копії…")
            self.status_text.set("Створення резервної копії…")

            def worker() -> None:
                try:
                    result = create_backup(self.root_dir, destination)
                    self.root.after(0, lambda: backup_finished(result, None))
                except Exception as error:
                    self.root.after(0, lambda error=error: backup_finished(None, error))

            def backup_finished(result: Path | None, error: Exception | None) -> None:
                set_library_busy(False)
                if error is not None:
                    self.status_text.set("Не вдалося створити резервну копію")
                    messagebox.showerror("Резервна копія", str(error), parent=window)
                    return
                assert result is not None
                self.status_text.set(f"Створено резервну копію: {result.name}")
                messagebox.showinfo(
                    "Резервна копія створена",
                    f"Файл збережено тут:\n{result}",
                    parent=window,
                )

            threading.Thread(target=worker, daemon=True).start()

        def import_library_backup() -> None:
            try:
                selected = choose_files(
                    window,
                    title="Оберіть резервну копію AudioLib",
                    filetypes=[("Резервна копія AudioLib", "*.zip")],
                )
            except FileDialogError as error:
                messagebox.showerror("Імпорт", str(error), parent=window)
                return
            if not selected:
                return
            if not messagebox.askyesno(
                "Відновити бібліотеку",
                "Поточну бібліотеку буде замінено даними з резервної копії. Продовжити?",
                icon="warning",
                parent=window,
            ):
                return
            self._clear_player()
            self.stop_virtual_microphone_sound()
            self.hotkey_manager.close()
            self.store.connection.commit()
            self.store.close()
            set_library_busy(True, "Імпорт і перевірка резервної копії…")
            self.status_text.set("Імпорт резервної копії…")

            def worker() -> None:
                try:
                    restore_backup(self.root_dir, Path(selected[0]))
                    self.root.after(0, lambda: import_finished(None))
                except Exception as error:
                    self.root.after(0, lambda error=error: import_finished(error))

            def import_finished(error: Exception | None) -> None:
                self.store = LibraryStore(self.root_dir)
                set_library_busy(False)
                if error is not None:
                    self._reload_global_hotkeys()
                    self.status_text.set("Не вдалося імпортувати резервну копію")
                    messagebox.showerror("Імпорт", str(error), parent=window)
                    return
                self.app_settings = self._load_app_settings()
                self._reload_global_hotkeys()
                self.volume_value = int(self.app_settings["volume"])
                self.volume_scale.set(self.volume_value)
                self.player.set_volume(self.volume_value)
                normalization_enabled = bool(
                    self.app_settings["normalize_loudness"]
                )
                self.player.set_normalization_enabled(normalization_enabled)
                self.virtual_microphone.set_normalization_enabled(
                    normalization_enabled
                )
                self.player.set_output_device(str(self.app_settings["output_device"]))
                self.virtual_microphone.configure(
                    latency_profile=str(self.app_settings["latency_profile"]),
                    local_monitoring=bool(self.app_settings["local_monitoring"]),
                    monitor_output_name=str(self.app_settings["output_device"]),
                )
                self.selected_person_id = None
                self.select_person(FAVORITES_PERSON_ID)
                self._refresh_tray_favorites()
                self.status_text.set("Бібліотеку відновлено з резервної копії")
                window.destroy()
                messagebox.showinfo(
                    "Імпорт завершено",
                    "Бібліотеку відновлено. Перезапустіть AudioLib, щоб застосувати налаштування з копії.",
                    parent=self.root,
                )

            threading.Thread(target=worker, daemon=True).start()

        backup_button = self._button(
            library_actions, "Створити копію", make_library_backup, "#0f766e"
        )
        backup_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        import_button = self._button(
            library_actions, "Імпортувати", import_library_backup, "#475569"
        )
        import_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        add_section_heading("Вигляд")
        appearance_panel = tk.Frame(content, bg=PANEL, padx=14, pady=12)
        appearance_panel.pack(fill="x", padx=22)
        for column in range(3):
            appearance_panel.columnconfigure(column, weight=1, uniform="appearance")

        def appearance_choice(
            column: int,
            title: str,
            variable: tk.StringVar,
            values: list[str],
            callback,
        ) -> RoundedDropdown:
            frame = tk.Frame(appearance_panel, bg=PANEL)
            frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
            tk.Label(
                frame,
                text=title,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 9),
            ).pack(fill="x")
            dropdown = RoundedDropdown(
                frame,
                textvariable=variable,
                command=lambda _index: callback(),
                width=165,
            )
            dropdown.configure(values=values)
            dropdown.current(values.index(variable.get()))
            dropdown.pack(fill="x", pady=(5, 0))
            return dropdown

        def apply_accent() -> None:
            save_setting("accent_color", accent_value.get())
            self._apply_saved_accent()
            ttk.Style(self.root).map("Treeview", background=[("selected", ACCENT)])
            self.refresh_people()
            self.track_table.refresh_style()
            self.status_text.set("Колір акценту збережено; усі елементи оновляться після перезапуску")

        photo_labels = {"Малі": 72, "Середні": 92, "Великі": 108}
        photo_size_value.set(
            next(
                label for label, size in photo_labels.items()
                if size == int(self.app_settings["photo_size"])
            )
        )

        def apply_photo_size() -> None:
            save_setting("photo_size", photo_labels[photo_size_value.get()])
            self.refresh_people()

        density_labels = {
            "Компактно": "compact",
            "Звичайно": "comfortable",
            "Просторо": "spacious",
        }
        density_value.set(
            next(
                label for label, value in density_labels.items()
                if value == str(self.app_settings["list_density"])
            )
        )

        def apply_density() -> None:
            density = density_labels[density_value.get()]
            save_setting("list_density", density)
            row_height = {"compact": 28, "comfortable": 34, "spacious": 40}[density]
            ttk.Style(self.root).configure("Treeview", rowheight=row_height)
            self.refresh_people()

        appearance_choice(0, "Колір акценту", accent_value, list(ACCENT_PRESETS), apply_accent)
        appearance_choice(1, "Розмір зображень", photo_size_value, list(photo_labels), apply_photo_size)
        appearance_choice(2, "Щільність списків", density_value, list(density_labels), apply_density)

        about_header = tk.Frame(content, bg=BG)
        about_header.pack(fill="x", padx=22, pady=(18, 8))
        tk.Frame(about_header, bg="#334155", height=1).pack(
            side="left", fill="x", expand=True, padx=(0, 10)
        )
        tk.Label(
            about_header,
            text="Про програму",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")
        tk.Frame(about_header, bg="#334155", height=1).pack(
            side="left", fill="x", expand=True, padx=(10, 0)
        )

        about_panel = tk.Frame(content, bg=PANEL, padx=14, pady=10)
        about_panel.pack(fill="x", padx=22, pady=(0, 22))
        about_panel.columnconfigure(1, weight=1)
        about_rows = (
            ("Назва", "AudioLib"),
            ("Версія", __version__),
            ("Розробник", "MULTIGID + ChatGPT"),
            ("Опис", "Локальна бібліотека аудіозаписів"),
        )
        for row, (label, value) in enumerate(about_rows):
            tk.Label(
                about_panel,
                text=f"{label}:",
                bg=PANEL,
                fg=MUTED,
                anchor="w",
                font=("Segoe UI", 10),
            ).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=2)
            tk.Label(
                about_panel,
                text=value,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 10),
            ).grid(row=row, column=1, sticky="w", pady=2)
        refresh_status()
        window.update_idletasks()
        window.deiconify()
        window.lift()
        window.attributes("-topmost", True)
        window.after(150, lambda: window.attributes("-topmost", False))
        window.focus_force()

    def toggle_pause(self) -> None:
        if not self.player.loaded_path or self.current_track_id is None:
            self.status_text.set("Спочатку натисніть «Відтворити» для вибраного запису")
            return
        try:
            mode = self.player.mode()
            if mode == "stopped":
                self.player.stop()
                self.player.play()
                self.play_pause_button.configure(text="Ⅱ")
            elif self.player.paused:
                self.player.resume()
                self.play_pause_button.configure(text="Ⅱ")
            else:
                self.player.pause()
                self.play_pause_button.configure(text="▶")
            self.refresh_tracks()
        except PlayerError as error:
            messagebox.showerror("Помилка", str(error), parent=self.root)

    def _set_volume(self, value: str) -> None:
        self.volume_value = int(float(value))
        self.player.set_volume(self.volume_value)
        self.virtual_microphone.set_volume(self.volume_value / 1000)
        self._schedule_volume_save()

    def stop_playback(self) -> None:
        self._speaker_load_serial += 1
        self._speaker_load_track_id = None
        try:
            self.player.stop()
        except PlayerError:
            pass
        self.progress.set(0)
        self.play_pause_button.configure(text="▶")
        self.time_text.set(f"0:00 / {format_time(self.current_track_length_ms)}")
        self.refresh_tracks()

    def stop_all_audio(self) -> None:
        self._speaker_load_serial += 1
        self._microphone_load_serial += 1
        self._speaker_load_track_id = None
        self._microphone_load_track_id = None
        try:
            self.player.stop()
        except PlayerError:
            pass
        try:
            self.virtual_microphone.stop_sound()
        except VirtualAudioError:
            pass
        self.virtual_track_id = None
        self.progress.set(0)
        self.play_pause_button.configure(text="▶")
        self.time_text.set(f"0:00 / {format_time(self.current_track_length_ms)}")
        self.status_text.set("Усе відтворення зупинено")
        self.refresh_tracks()

    def _clear_player(self) -> None:
        self._speaker_load_serial += 1
        self._speaker_load_track_id = None
        self.player.close()
        self.current_track_id = None
        self.current_track_length_ms = 0
        self.now_playing.set("Оберіть запис")
        self.progress.set_range(1)
        self.progress.set(0)
        self.time_text.set("0:00 / 0:00")
        self.play_pause_button.configure(text="▶")

    def rename_track(self) -> None:
        track = self._selected_track()
        if not track:
            return
        title = self._ask_text(
            "Перейменувати запис",
            "Назва:",
            initial_value=track.title,
        )
        if title:
            try:
                self.store.rename_track(track.id, title)
                self._refresh_tray_favorites()
                if self.current_track_id == track.id:
                    self.now_playing.set(title.strip())
                self.refresh_tracks()
            except ValueError as error:
                messagebox.showerror("Помилка", str(error), parent=self.root)

    def delete_track(self) -> None:
        track = self._selected_track()
        if not track:
            return
        if not messagebox.askyesno("Видалити запис", f"Видалити «{track.title}»?", parent=self.root):
            return
        if self.current_track_id == track.id:
            self._clear_player()
        if self.virtual_track_id == track.id:
            self.stop_virtual_microphone_sound()
        self.store.delete_track(track.id)
        self._refresh_tray_favorites()
        self._reload_global_hotkeys()
        self.refresh_people()
        self.refresh_tracks()
        self.status_text.set(f"Видалено запис: {track.title}")

    def _preview_seek(self, value: str) -> None:
        if self.seeking:
            self.time_text.set(
                f"{format_time(int(float(value)))} / {format_time(self.current_track_length_ms)}"
            )

    def _commit_seek(self, _event=None) -> None:
        if self.player.loaded_path:
            try:
                self.player.seek(int(self.progress.get()))
            except PlayerError:
                pass
        self.seeking = False

    def _update_progress(self) -> None:
        if self.player.loaded_path and not self.seeking:
            position = self.player.position()
            reported_length = self.player.length()
            if reported_length > 0:
                self.current_track_length_ms = reported_length
            length = self.current_track_length_ms
            self.progress.set_range(max(1, length))
            self.progress.set(position)
            self.time_text.set(f"{format_time(position)} / {format_time(length)}")
            if self.player.mode() == "stopped" and position >= max(0, length - 500):
                self.play_pause_button.configure(text="▶")
        speaker_table_state = (
            self.current_track_id,
            self.player.mode() if self.player.loaded_path else "stopped",
        )
        if speaker_table_state != self._last_speaker_table_state:
            self._last_speaker_table_state = speaker_table_state
            self.refresh_tracks()
        if self.virtual_track_id is not None and self.virtual_microphone.playback_state() == "stopped":
            self.virtual_track_id = None
            self.refresh_tracks()
        self.root.after(400, self._update_progress)

    def _shutdown_audio_routing(self) -> None:
        if self._audio_shutdown_done:
            return
        self._audio_shutdown_done = True
        self._closing_event.set()
        self._save_window_geometry()
        self._save_volume_setting()
        restored = False
        hidden = False
        error_text = ""
        try:
            restored = self.microphone_routing.restore_default_microphone()
        except Exception as error:
            error_text = f" restore_error={error}"
        try:
            self.virtual_microphone.close()
        except Exception as error:
            error_text += f" bridge_error={error}"
        if restored:
            try:
                hidden = self.microphone_routing.disable_cable_output()
            except Exception as error:
                error_text += f" hide_error={error}"
        if restored and hidden:
            try:
                self.microphone_routing.clear_recovery_state()
            except OSError as error:
                error_text += f" marker_error={error}"
        self._write_audio_routing_log(
            f"STOP restored={restored} cable_hidden={hidden}{error_text}"
        )

    def _close(self) -> None:
        # Restore Windows audio before stopping the tray helper.  This also
        # keeps the operation deterministic when Exit originates in pystray's
        # worker thread.
        self.hotkey_manager.close()
        self._shutdown_audio_routing()
        if self.tray_window is not None:
            try:
                self.tray_window.destroy()
            except tk.TclError:
                pass
            self.tray_window = None
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.player.close()
        self.store.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
