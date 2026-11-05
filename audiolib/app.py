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
from tkinter import colorchooser, messagebox, ttk

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
from .i18n import get_language, set_language, tr
from .player import PlayerError, WindowsMediaPlayer, list_output_devices
from .startup import register_autostart, unregister_autostart
from .storage import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, LibraryStore, Person, Track
from .updates import GitHubUpdateManager, ReleaseInfo, UpdateError
from .virtual_audio import (
    DRIVER_PAGE,
    MicrophoneRelayProcess,
    VirtualAudioError,
    VirtualMicrophoneBridge,
    launch_driver_setup,
    prepare_official_installer,
)
from .windows_file_dialog import FileDialogError, choose_files, choose_folder


BG = "#141415"
PANEL = "#0f0f10"
CARD = "#19191b"
FIELD = "#232325"
HOVER = "#2b2b2e"
BORDER = "#343438"
SECONDARY = "#2b2b2e"
PLAYER_BG = "#0f0f10"
TITLE_BG = "#0f0f10"
TITLE_HOVER = "#232325"
PLACEHOLDER = "#85858b"
TITLE_TEXT = "#ededed"
FAVORITES_BG = "#19191b"
FAVORITES_SELECTED_BG = "#2b2b2e"
FAVORITES_TEXT = "#ededed"
FAVORITES_MUTED = "#85858b"
WARNING_BG = "#422006"
WARNING_TEXT = "#fde68a"
ERROR_BG = "#450a0a"
ERROR_TEXT = "#fecaca"
ERROR_BORDER = "#dc2626"
CAUTION_BORDER = "#b45309"
SUCCESS_TEXT = "#34d399"
CAUTION_TEXT = "#fbbf24"
SWITCH_OFF = "#343438"
SWITCH_DISABLED = "#232325"
SWITCH_THUMB = "#ededed"
TRAY_STOP_COLOR = (237, 237, 237, 255)
ACCENT = "#a34f0d"
ACCENT_HOVER = "#873a0a"
ACCENT_PRESETS = {
    "Янтарний": ("#a34f0d", "#873a0a"),
    "Синій": ("#1d4ed8", "#1e40af"),
    "Бірюзовий": ("#0f766e", "#115e59"),
    "Червоний": ("#b42318", "#912018"),
    "Фіолетовий": ("#7c3aed", "#6d28d9"),
    "Зелений": ("#15803d", "#166534"),
    "Рожевий": ("#be185d", "#9d174d"),
    "Помаранчевий": ("#c2410c", "#9a3412"),
}
TEXT = "#ededed"
MUTED = "#a1a1a6"
DANGER = "#dc2626"
FAVORITES_PERSON_ID = -1
FAVORITES_LIMIT = 15
STOP_HOTKEY_ACTION_ID = -2
DEFAULT_STOP_HOTKEY = "Ctrl+Alt+Backspace"
DEFAULT_WINDOW_WIDTH = 1440
DEFAULT_WINDOW_HEIGHT = 820
MIN_WINDOW_WIDTH = 1040
MIN_WINDOW_HEIGHT = 680


def _valid_hex_color(value: object, fallback: str = "#a34f0d") -> str:
    color = str(value).strip()
    return color.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else fallback


def _darken_hex_color(value: str, factor: float = 0.82) -> str:
    color = _valid_hex_color(value)
    channels = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    return "#" + "".join(f"{max(0, min(255, int(channel * factor))):02x}" for channel in channels)


def _contrast_text_color(
    background: str,
    *,
    light: str = "#ffffff",
    dark: str = "#171717",
) -> str:
    """Return readable foreground text for an arbitrary solid background."""
    color = _valid_hex_color(background, "#171717")
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return dark if luminance > 0.42 else light


class LocalizedStringVar(tk.StringVar):
    """StringVar that translates application-owned interface messages."""

    def __init__(self, *args, value=None, **kwargs) -> None:
        super().__init__(*args, value=tr(value) if value is not None else value, **kwargs)

    def set(self, value: object) -> None:
        super().set(tr(value))


class LocalizedLabel(tk.Label):
    """Label that translates static application text at display time."""

    def __init__(self, master=None, cnf=None, **kwargs) -> None:
        options = dict(cnf or {})
        options.update(kwargs)
        if "text" in options:
            options["text"] = tr(options["text"])
        super().__init__(master, **options)

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if "text" in options:
            options["text"] = tr(options["text"])
        return super().configure(**options)

    config = configure


def _showerror(title: object, message: object, **kwargs):
    return messagebox.showerror(tr(title), tr(message), **kwargs)


def _showwarning(title: object, message: object, **kwargs):
    return messagebox.showwarning(tr(title), tr(message), **kwargs)


def _showinfo(title: object, message: object, **kwargs):
    return messagebox.showinfo(tr(title), tr(message), **kwargs)


def _askyesno(title: object, message: object, **kwargs):
    return messagebox.askyesno(tr(title), tr(message), **kwargs)


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


def render_bookmark_image(
    size: int,
    foreground: str,
    *,
    filled: bool = False,
    background: str | None = None,
) -> ImageTk.PhotoImage:
    """Render a visually centered, anti-aliased bookmark control."""
    scale = 4
    pixels = max(1, int(size))
    image = Image.new("RGBA", (pixels * scale, pixels * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if background is not None:
        inset = scale
        draw.ellipse(
            (inset, inset, image.width - inset - 1, image.height - inset - 1),
            fill=background,
        )
    center_x = image.width / 2
    # The V-shaped lower edge makes the bookmark look visually higher even
    # when its geometric bounds are centered.  A one-pixel optical correction
    # keeps it aligned with the neighbouring media controls.
    center_y = image.height / 2 + scale
    half_width = pixels * 0.19 * scale
    half_height = pixels * 0.28 * scale
    top = center_y - half_height
    bottom = center_y + half_height
    points = (
        (center_x - half_width, top),
        (center_x + half_width, top),
        (center_x + half_width, bottom),
        (center_x, bottom - pixels * 0.16 * scale),
        (center_x - half_width, bottom),
    )
    draw.polygon(
        points,
        fill=foreground if filled else None,
        outline=foreground,
        width=max(scale, int(pixels * 0.055 * scale)),
    )
    image = image.resize((pixels, pixels), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(image)


def render_scrollbar_thumb_image(
    color: str,
    *,
    horizontal: bool = False,
) -> Image.Image:
    """Render an antialiased scrollbar thumb that can be recolored live."""
    scale = 4
    logical_width, logical_height = ((32, 12) if horizontal else (12, 32))
    image = Image.new(
        "RGBA",
        (logical_width * scale, logical_height * scale),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(image)
    if horizontal:
        bounds = (0, 2 * scale, 32 * scale - 1, 10 * scale - 1)
    else:
        bounds = (2 * scale, 0, 10 * scale - 1, 32 * scale - 1)
    draw.rounded_rectangle(bounds, radius=4 * scale, fill=color)
    return image.resize(
        (logical_width, logical_height),
        Image.Resampling.LANCZOS,
    )


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
        background: str = PLAYER_BG,
        trough_color: str = FIELD,
        fill_color: str = BORDER,
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
        media_control: bool = True,
    ) -> None:
        self.text = tr(text)
        self.button_image = image
        self.media_control = media_control
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
        if (
            self.media_control
            and self.button_image is None
            and self.text in {"▶", "■", "Ⅱ", "‖"}
        ):
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
            return FIELD
        if self.hovered or self.pressed:
            return self.hover_color
        return self.normal_color

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width(), int(self.cget("width")))
        height = max(1, self.winfo_height(), int(self.cget("height")))
        color = self._background_color()
        symbol_color = (
            PLACEHOLDER if self.state == "disabled" else _contrast_text_color(color)
        )
        if (
            self.media_control
            and self.button_image is None
            and self.text in {"▶", "■", "Ⅱ", "‖"}
        ):
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
        if "color" in options:
            self.normal_color = str(options.pop("color"))
            redraw = True
        if "hover_color" in options:
            self.hover_color = str(options.pop("hover_color"))
            redraw = True
        if "text" in options:
            self.text = tr(options.pop("text"))
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
        if key == "color":
            return self.normal_color
        if key == "hover_color":
            return self.hover_color
        return super().cget(key)


class WorkspaceTab(tk.Canvas):
    """A single-piece editor tab with an integrated close target."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        select_command,
        close_command,
        *,
        active: bool = False,
    ) -> None:
        self.text = text
        self.select_command = select_command
        self.close_command = close_command
        self.active = active
        self.hovered = False
        self.close_hovered = False
        self.button_font = tkfont.Font(root=parent, font=("Segoe UI Semibold", 9))
        self.close_width = 28
        width = self.button_font.measure(text) + 20 + self.close_width
        super().__init__(
            parent,
            width=width,
            height=30,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self._surface_image: ImageTk.PhotoImage | None = None
        self._close_surface_image: ImageTk.PhotoImage | None = None
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda _event: self.select_command())
        self.bind("<space>", lambda _event: self.select_command())
        self.bind("<Delete>", lambda _event: self.close_command())
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self._draw()

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.close_hovered = False
        self._draw()

    def _motion(self, event: tk.Event) -> None:
        close_hovered = event.x >= self.winfo_width() - self.close_width
        if close_hovered != self.close_hovered:
            self.close_hovered = close_hovered
            self._draw()

    def _release(self, event: tk.Event) -> None:
        if event.x >= self.winfo_width() - self.close_width:
            self.close_command()
        else:
            self.select_command()

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width(), int(self.cget("width")))
        height = max(1, self.winfo_height(), int(self.cget("height")))
        color = ACCENT if self.active else HOVER if self.hovered else SECONDARY
        text_color = _contrast_text_color(color) if self.active else TEXT
        self._surface_image = render_rounded_surface(
            width,
            height,
            fill=color,
            radius=7,
        )
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
        divider_x = width - self.close_width
        self.create_line(divider_x, 6, divider_x, height - 6, fill=BORDER)
        if self.close_hovered:
            self._close_surface_image = render_rounded_surface(
                self.close_width - 6,
                height - 6,
                fill=ACCENT_HOVER if self.active else BORDER,
                radius=5,
            )
            self.create_image(
                divider_x + 3,
                3,
                image=self._close_surface_image,
                anchor="nw",
            )
        self.create_text(
            10,
            height / 2 - 1,
            text=self.text,
            fill=text_color,
            font=self.button_font,
            anchor="w",
        )
        self.create_text(
            width - self.close_width / 2,
            height / 2 - 1,
            text="×",
            fill=text_color,
            font=("Segoe UI", 11),
            anchor="center",
        )


class RemoteFavoriteButton(tk.Canvas):
    """Resizable remote tile with protected text padding and hotkey footer."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        hotkey: str,
        command,
        color: str,
        hover_color: str,
    ) -> None:
        super().__init__(
            parent,
            width=104,
            height=104,
            bg=parent.cget("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
        )
        self.title = title.strip()
        self.hotkey = "" if hotkey == "—" else hotkey
        self.command = command
        self.color = color
        self.hover_color = hover_color
        self.hovered = False
        self.pressed = False
        self.triggered = False
        self._trigger_job: str | None = None
        self.title_font = tkfont.Font(root=parent, font=("Segoe UI Semibold", 9))
        self.hotkey_font = tkfont.Font(root=parent, font=("Segoe UI", 8))
        self._surface_image: ImageTk.PhotoImage | None = None
        self.bind("<Configure>", lambda _event: self._draw(), add="+")
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Return>", lambda _event: self.command())
        self.bind("<space>", lambda _event: self.command())
        self._draw()

    @staticmethod
    def _fit_line(text: str, width: int, font: tkfont.Font, *, ellipsis: bool) -> str:
        if font.measure(text) <= width:
            return text
        suffix = "…" if ellipsis else ""
        available = max(1, width - font.measure(suffix))
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if font.measure(text[:middle]) <= available:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + suffix

    def _display_title(self, width: int) -> str:
        if self.title_font.measure(self.title) <= width:
            return self.title
        first = self._fit_line(self.title, width, self.title_font, ellipsis=False)
        remainder = self.title[len(first):].lstrip()
        second = self._fit_line(remainder, width, self.title_font, ellipsis=True)
        return f"{first}\n{second}" if second else first

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width(), int(self.cget("width")))
        height = max(1, self.winfo_height(), int(self.cget("height")))
        active = self.hovered or self.pressed or self.triggered
        fill = self.hover_color if active else self.color
        content_color = _contrast_text_color(fill)
        self._surface_image = render_rounded_surface(
            width,
            height,
            fill=fill,
            radius=min(9, width // 2, height // 2),
            outline=ACCENT if active else BORDER,
            outline_width=2 if self.triggered else 1,
        )
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
        horizontal_padding = 12
        title_width = max(1, width - horizontal_padding * 2)
        title_y = (height - 26) / 2 if self.hotkey else height / 2
        if self.title:
            self.create_text(
                width / 2,
                title_y,
                text=self._display_title(title_width),
                width=title_width,
                fill=content_color,
                font=self.title_font,
                justify="center",
                anchor="center",
            )
        if self.hotkey:
            self.create_text(
                width / 2,
                height - 15,
                text=self.hotkey,
                fill=content_color,
                font=self.hotkey_font,
                anchor="center",
            )

    def _enter(self, _event=None) -> None:
        self.hovered = True
        self._draw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.pressed = False
        self._draw()

    def _press(self, _event=None) -> None:
        self.pressed = True
        self._draw()

    def _release(self, event: tk.Event) -> None:
        invoke = (
            self.pressed
            and 0 <= event.x <= self.winfo_width()
            and 0 <= event.y <= self.winfo_height()
        )
        self.pressed = False
        self._draw()
        if invoke:
            self.command()

    def flash(self, duration_ms: int = 450) -> None:
        """Show a short pressed-state pulse for external/hotkey activation."""
        if self._trigger_job is not None:
            try:
                self.after_cancel(self._trigger_job)
            except tk.TclError:
                pass
        self.triggered = True
        self._draw()

        def clear() -> None:
            self._trigger_job = None
            try:
                self.triggered = False
                self._draw()
            except tk.TclError:
                pass

        self._trigger_job = self.after(duration_ms, clear)


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
        self.text = tr(text)
        self.value = value
        self.variable = variable
        self.command = command
        self.hovered = False
        self.button_font = tkfont.Font(root=parent, font=("Segoe UI", 9))
        self._indicator_image: ImageTk.PhotoImage | None = None
        width = 22 + self.button_font.measure(self.text)
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
        ring = ACCENT if selected else TEXT if self.hovered else BORDER
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
            else SWITCH_OFF if self.enabled
            else SWITCH_DISABLED
        )
        thumb_color = (
            "#ffffff" if active and self.enabled
            else SWITCH_THUMB if self.enabled
            else BORDER
        )
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
        translate_values: bool = True,
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
        self.translate_values = translate_values
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
        outline = ACCENT if self.hovered or self.popup is not None else BORDER
        self._surface_image = self._rounded_surface(
            width,
            height,
            fill=FIELD,
            outline=outline,
        )
        self.create_image(0, 0, image=self._surface_image, anchor="nw")
        value = self.textvariable.get()
        label = self._display_value(value) if value else tr(self.placeholder)
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

    def _display_value(self, value: str) -> str:
        return tr(value) if self.translate_values else value

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
        row_height = max(34, self.winfo_height())
        row_gap = 4
        popup_padding = 6
        height = (
            popup_padding * 2
            + visible_rows * row_height
            + max(0, visible_rows - 1) * row_gap
        )
        popup = tk.Toplevel(self)
        owner = self.winfo_toplevel()
        popup.transient(owner)
        popup.overrideredirect(True)
        popup.configure(bg=BG)
        self.popup = popup
        self._popup_size = (width, height)
        RoundedDropdown._active_dropdown = self
        self._sync_popup_position()
        surface = tk.Canvas(
            popup,
            width=width,
            height=height,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        surface.pack(fill="both", expand=True)
        popup._audiolib_surface_image = self._rounded_surface(
            width,
            height,
            fill=PANEL,
            outline=BORDER,
        )
        surface.create_image(
            0,
            0,
            image=popup._audiolib_surface_image,
            anchor="nw",
        )

        content_width = max(1, width - popup_padding * 2)
        viewport_height = max(1, height - popup_padding * 2)
        viewport = tk.Canvas(
            popup,
            bg=PANEL,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=True,
            yscrollincrement=row_height + row_gap,
        )
        viewport.place(
            x=popup_padding,
            y=popup_padding,
            width=content_width,
            height=viewport_height,
        )
        rows_frame = tk.Frame(viewport, bg=PANEL, bd=0, highlightthickness=0)
        rows_window = viewport.create_window(
            0,
            0,
            window=rows_frame,
            anchor="nw",
            width=content_width,
        )
        rows: list[tk.Canvas] = []
        keyboard_index = self.selected_index if self.selected_index >= 0 else 0

        def draw_row(index: int, *, hovered: bool = False) -> None:
            row = rows[index]
            row.delete("all")
            selected = index == self.selected_index
            fill = ACCENT if selected else (HOVER if hovered else CARD)
            outline = ACCENT_HOVER if selected else (BORDER if hovered else None)
            row._audiolib_surface_image = self._rounded_surface(
                content_width,
                row_height,
                fill=fill,
                outline=outline,
                radius=8,
            )
            row.create_image(
                0,
                0,
                image=row._audiolib_surface_image,
                anchor="nw",
            )
            row.create_text(
                12,
                row_height / 2,
                text=self._display_value(self.values[index]),
                fill=TEXT,
                font=("Segoe UI", 10),
                anchor="w",
            )

        def choose(index: int) -> str:
            if 0 <= index < len(self.values):
                self._select(index)
            return "break"

        def scroll_rows(event) -> str:
            if len(self.values) > visible_rows:
                viewport.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        for index in range(len(self.values)):
            row = tk.Canvas(
                rows_frame,
                width=content_width,
                height=row_height,
                bg=PANEL,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            row.pack(
                fill="x",
                pady=(0, row_gap if index < len(self.values) - 1 else 0),
            )
            rows.append(row)
            draw_row(index)
            row.bind("<Enter>", lambda _event, i=index: draw_row(i, hovered=True))
            row.bind("<Leave>", lambda _event, i=index: draw_row(i))
            row.bind("<ButtonRelease-1>", lambda _event, i=index: choose(i))
            row.bind("<MouseWheel>", scroll_rows)

        content_height = len(self.values) * row_height + max(
            0, len(self.values) - 1
        ) * row_gap
        viewport.configure(scrollregion=(0, 0, content_width, content_height))
        viewport.itemconfigure(rows_window, width=content_width)
        if self.selected_index >= visible_rows:
            viewport.yview_moveto(
                min(
                    1.0,
                    (self.selected_index * (row_height + row_gap))
                    / max(1, content_height),
                )
            )

        def move_keyboard(step: int) -> str:
            nonlocal keyboard_index
            previous = keyboard_index
            keyboard_index = max(0, min(len(self.values) - 1, keyboard_index + step))
            draw_row(previous)
            draw_row(keyboard_index, hovered=True)
            row_top = keyboard_index * (row_height + row_gap)
            row_bottom = row_top + row_height
            visible_top = viewport.canvasy(0)
            visible_bottom = visible_top + viewport_height
            if row_top < visible_top:
                viewport.yview_moveto(row_top / max(1, content_height))
            elif row_bottom > visible_bottom:
                viewport.yview_moveto(
                    max(0.0, (row_bottom - viewport_height) / max(1, content_height))
                )
            return "break"

        viewport.bind("<Up>", lambda _event: move_keyboard(-1))
        viewport.bind("<Down>", lambda _event: move_keyboard(1))
        viewport.bind("<Home>", lambda _event: move_keyboard(-len(self.values)))
        viewport.bind("<End>", lambda _event: move_keyboard(len(self.values)))
        viewport.bind("<Return>", lambda _event: choose(keyboard_index))
        viewport.bind("<MouseWheel>", scroll_rows)
        viewport.bind("<Escape>", lambda _event: self._close_popup())
        popup.bind("<Escape>", lambda _event: self._close_popup())
        popup.bind("<FocusOut>", lambda _event: popup.after(80, self._close_if_unfocused))
        popup.lift()
        viewport.focus_force()
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
        self._view_mode = "table"
        self._row_font = tkfont.Font(root=self, font=("Segoe UI", 10))
        self._header_font = tkfont.Font(root=self, font=("Segoe UI Semibold", 9))
        self._hotkey_font = tkfont.Font(root=self, font=("Segoe UI Semibold", 8))
        self._action_image_cache: dict[
            tuple[str, str, str | None, int], ImageTk.PhotoImage
        ] = {}
        self._bookmark_image_cache: dict[
            tuple[str, bool, str | None, int], ImageTk.PhotoImage
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

    def _bookmark_image(
        self,
        foreground: str,
        filled: bool,
        background: str | None,
        size: int = 27,
    ) -> ImageTk.PhotoImage:
        key = (foreground, filled, background, size)
        image = self._bookmark_image_cache.get(key)
        if image is None:
            image = render_bookmark_image(
                size,
                foreground,
                filled=filled,
                background=background,
            )
            self._bookmark_image_cache[key] = image
        return image

    def set_view_mode(self, mode: str) -> None:
        mode = mode if mode in {"table", "tiles"} else "table"
        if mode == self._view_mode:
            return
        anchor = self.view_anchor()
        self._view_mode = mode
        if mode == "tiles":
            self.header.pack_forget()
            self.body.configure(yscrollincrement=182)
            self.body.xview_moveto(0.0)
        else:
            if not self.header.winfo_manager():
                self.header.pack(fill="x", before=self.body)
            self.body.configure(yscrollincrement=self._row_height())
        self._update_geometry()
        self.restore_view_anchor(anchor)

    def _row_height(self) -> int:
        return self.ROW_HEIGHT

    def _tile_metrics(self) -> tuple[int, int, int, int]:
        viewport = max(280, self.body.winfo_width())
        gap = 8
        columns = max(1, min(6, viewport // 170))
        width = max(135, (viewport - gap * (columns + 1)) // columns)
        return columns, width, 174, gap

    def _item_geometry(self, index: int) -> tuple[int, int, int, int]:
        if self._view_mode != "tiles":
            height = self._row_height()
            return 0, index * height, max(1, self.body.winfo_width()), height
        columns, width, height, gap = self._tile_metrics()
        row, column = divmod(index, columns)
        return gap + column * (width + gap), gap + row * (height + gap), width, height

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
        if self._view_mode == "tiles":
            columns, _width, height, gap = self._tile_metrics()
            index = int(top // (height + gap)) * columns
        else:
            index = int(top // self._row_height())
        index = max(0, min(len(self._rows) - 1, index))
        iid = self._rows[index][0]
        _x, item_top, _width, _height = self._item_geometry(index)
        offset = top - item_top
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
            _x, item_top, _width, _height = self._item_geometry(index)
            total_height = self._content_height()
            target = max(0.0, item_top + offset)
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
        _x, top, _width, height = self._item_geometry(index)
        bottom = top + height
        visible_top = self.body.canvasy(0)
        visible_bottom = visible_top + self.body.winfo_height()
        total_height = self._content_height()
        if top < visible_top:
            self.body.yview_moveto(top / total_height)
        elif bottom > visible_bottom:
            target = max(0, bottom - self.body.winfo_height())
            self.body.yview_moveto(target / total_height)
        self._schedule_redraw()

    def identify_region(self, x: int, y: int) -> str:
        return "cell" if self.identify_row(y, x) and self.identify_column(x, y) else "nothing"

    def identify_row(self, y: int, x: int | None = None) -> str:
        canvas_y = self.body.canvasy(y)
        if self._view_mode == "tiles":
            canvas_x = self.body.canvasx(0 if x is None else x)
            columns, width, height, gap = self._tile_metrics()
            column = int((canvas_x - gap) // (width + gap))
            row = int((canvas_y - gap) // (height + gap))
            if row < 0 or column < 0 or column >= columns:
                return ""
            tile_x = gap + column * (width + gap)
            tile_y = gap + row * (height + gap)
            if not (tile_x <= canvas_x < tile_x + width and tile_y <= canvas_y < tile_y + height):
                return ""
            index = row * columns + column
        else:
            index = int(canvas_y // self._row_height())
        if 0 <= index < len(self._rows):
            return self._rows[index][0]
        return ""

    def identify_column(self, x: int, y: int | None = None) -> str:
        if self._view_mode == "tiles":
            iid = self.identify_row(0 if y is None else y, x)
            index = self._row_lookup.get(iid, -1)
            if index < 0:
                return ""
            tile_x, tile_y, tile_width, tile_height = self._item_geometry(index)
            canvas_x = self.body.canvasx(x)
            canvas_y = self.body.canvasy(0 if y is None else y)
            has_hotkey = "hotkey" in self._display_columns
            if has_hotkey and canvas_y >= tile_y + tile_height - 50:
                column = "hotkey"
            else:
                actions = ["speaker_play", "favorite", "mic_play", "mic_stop"]
                group_width = min(192, tile_width - 36)
                group_left = tile_x + (tile_width - group_width) / 2
                button_y = tile_y + tile_height - (79 if has_hotkey else 38)
                if (
                    group_left <= canvas_x < group_left + group_width
                    and button_y - 22 <= canvas_y <= button_y + 22
                ):
                    action_width = group_width / len(actions)
                    action_index = min(
                        len(actions) - 1,
                        max(0, int((canvas_x - group_left) // action_width)),
                    )
                    column = actions[action_index]
                else:
                    column = "title"
            try:
                return f"#{self._display_columns.index(column) + 1}"
            except ValueError:
                return "#1"
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
        iid = self.identify_row(event.y, event.x)
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
        iid = self.identify_row(event.y, event.x)
        column_id = self.identify_column(event.x, event.y)
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
        total_width = (
            max(1, self.body.winfo_width())
            if self._view_mode == "tiles"
            else max(1, sum(right - left for _name, left, right in self._column_layout()))
        )
        total_height = max(1, self.body.winfo_height(), self._content_height())
        self.body.configure(scrollregion=(0, 0, total_width, total_height))
        if not self._rows or self.body.canvasy(0) < 0:
            self.body.yview_moveto(0.0)
        self.header.configure(scrollregion=(0, 0, total_width, self.HEADER_HEIGHT))
        self._draw_header()
        self._schedule_redraw()

    def _content_height(self) -> int:
        if self._view_mode == "tiles":
            columns, _width, height, gap = self._tile_metrics()
            rows = math.ceil(len(self._rows) / columns) if self._rows else 0
            return max(1, gap + rows * (height + gap))
        return max(1, len(self._rows) * self._row_height())

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
        if self._view_mode == "tiles":
            self._draw_tiles()
            return
        row_height = self._row_height()
        top = max(0.0, self.body.canvasy(0))
        bottom = top + max(1, self.body.winfo_height())
        first = max(0, int(top // row_height) - 1)
        last = min(len(self._rows), int(bottom // row_height) + 2)
        layout = self._column_layout()
        total_width = max(1, layout[-1][2] if layout else 1)
        value_indexes = {name: index for index, name in enumerate(self._all_columns)}
        for row_index in range(first, last):
            iid, values = self._rows[row_index]
            y1 = row_index * row_height
            y2 = y1 + row_height
            selected = iid == self._selected_iid
            hovered_row = self._hovered is not None and self._hovered[0] == iid
            if selected:
                fill = HOVER
            elif hovered_row:
                fill = HOVER
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
            if selected:
                self.body.create_rectangle(
                    0,
                    y1,
                    3,
                    y2,
                    fill=ACCENT,
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
                    symbol_color = TEXT if selected else ACCENT if symbol == "★" else MUTED
                    hover_color = (
                        (ACCENT_HOVER if selected else SECONDARY)
                        if is_action_hover
                        else None
                    )
                    if name == "favorite":
                        bookmark = self._bookmark_image(
                            symbol_color,
                            symbol == "★",
                            hover_color,
                        )
                        self.body.create_image(
                            int(center_x + 0.5),
                            int(center_y + 0.5),
                            image=bookmark,
                            anchor="center",
                            tags="row",
                        )
                    elif symbol in {"▶", "■", "Ⅱ", "‖"}:
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
                        fill=ACCENT_HOVER if is_action_hover else PLAYER_BG,
                        outline=BORDER,
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
            if row_index < len(self._rows) - 1:
                self.body.create_line(
                    10,
                    y2 - 1,
                    max(10, total_width - 10),
                    y2 - 1,
                    fill=BORDER,
                    width=1,
                    tags="row",
                )

    def _draw_tiles(self) -> None:
        top = max(0.0, self.body.canvasy(0))
        bottom = top + max(1, self.body.winfo_height())
        columns, _tile_width, tile_height, gap = self._tile_metrics()
        first_grid_row = max(0, int(top // (tile_height + gap)) - 1)
        last_grid_row = int(bottom // (tile_height + gap)) + 2
        first = first_grid_row * columns
        last = min(len(self._rows), last_grid_row * columns)
        indexes = {name: index for index, name in enumerate(self._all_columns)}
        for index in range(first, last):
            iid, values = self._rows[index]
            x, y, width, height = self._item_geometry(index)
            selected = iid == self._selected_iid
            hovered = self._hovered is not None and self._hovered[0] == iid
            fill = HOVER if selected or hovered else CARD
            self.body.create_rectangle(
                x,
                y,
                x + width,
                y + height,
                fill=fill,
                outline=BORDER if not selected else ACCENT_HOVER,
                width=1,
                tags="row",
            )
            title = values[indexes["title"]]
            source = values[indexes["source"]]
            duration = values[indexes["duration"]]
            self.body.create_text(
                x + 14,
                y + 24,
                text=self._fit_text(title, width - 28, self._row_font),
                fill=TEXT,
                font=("Segoe UI Semibold", 10),
                anchor="w",
                tags="row",
            )
            secondary = str(source) if "source" in self._display_columns else tr("Тривалість") + f": {duration}"
            self.body.create_text(
                x + 14,
                y + 54,
                text=self._fit_text(secondary, width - 28, self._hotkey_font),
                fill=MUTED,
                font=("Segoe UI", 8),
                anchor="w",
                tags="row",
            )
            actions = ["speaker_play", "favorite", "mic_play", "mic_stop"]
            has_hotkey = "hotkey" in self._display_columns
            group_width = min(192, width - 36)
            action_width = group_width / len(actions)
            group_left = x + (width - group_width) / 2
            button_y = y + height - (79 if has_hotkey else 38)
            for action_index, name in enumerate(actions):
                center_x = group_left + action_width * (action_index + 0.5)
                center_y = button_y
                value = values[indexes[name]]
                is_action_hover = self._hovered == (iid, name)
                symbol = str(value)
                foreground = TEXT if selected else ACCENT if symbol == "★" else MUTED
                background = HOVER if is_action_hover and not selected else ACCENT_HOVER if is_action_hover else None
                if name == "favorite":
                    image = self._bookmark_image(
                        foreground,
                        symbol == "★",
                        background,
                    )
                    self.body.create_image(
                        int(center_x + 0.5),
                        int(center_y + 0.5),
                        image=image,
                        anchor="center",
                        tags="row",
                    )
                    continue
                image = self._action_image(symbol, foreground, background)
                self.body.create_image(center_x, center_y, image=image, tags="row")
            if has_hotkey:
                hotkey_value = str(values[indexes["hotkey"]])
                hotkey_hover = self._hovered == (iid, "hotkey")
                self.body.create_rectangle(
                    x + 14,
                    y + height - 48,
                    x + width - 14,
                    y + height - 14,
                    fill=ACCENT_HOVER if hotkey_hover else PLAYER_BG,
                    outline=ACCENT if hotkey_hover else BORDER,
                    width=1,
                    tags="row",
                )
                self.body.create_text(
                    x + width / 2,
                    y + height - 31,
                    text=hotkey_value,
                    fill=TEXT if hotkey_value != "—" else MUTED,
                    font=self._hotkey_font,
                    anchor="center",
                    tags="row",
                )


class OperationProgressDialog:
    """Small modal progress window shared by long-running operations."""

    def __init__(
        self,
        app: "AudioLibraryApp",
        parent: tk.Misc,
        title: str,
        initial_stage: str,
    ) -> None:
        self.app = app
        self.parent = parent
        self.window = tk.Toplevel(app.root)
        self.window.title(tr(title))
        self.window.configure(bg=BG)
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window._audiolib_close_allowed = False

        width, height = 500, 210
        parent.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        app._build_secondary_title_bar(self.window, title)
        self.window._audiolib_close_allowed = False

        content = tk.Frame(self.window, bg=BG, padx=22, pady=20)
        content.pack(fill="both", expand=True)
        self.stage = LocalizedStringVar(value=initial_stage)
        LocalizedLabel(
            content,
            textvariable=self.stage,
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 11),
        ).pack(fill="x")

        progress_row = tk.Frame(content, bg=BG)
        progress_row.pack(fill="x", pady=(15, 8))
        self.progress = ttk.Progressbar(
            progress_row,
            mode="determinate",
            maximum=100,
            style="Settings.Horizontal.TProgressbar",
        )
        self.progress.pack(side="left", fill="x", expand=True)
        self.percent = tk.StringVar(value="0%")
        tk.Label(
            progress_row,
            textvariable=self.percent,
            bg=BG,
            fg=TEXT,
            width=6,
            anchor="e",
            font=("Segoe UI Semibold", 9),
        ).pack(side="right", padx=(12, 0))

        self.detail = LocalizedStringVar(value="Підготовка операції…")
        LocalizedLabel(
            content,
            textvariable=self.detail,
            bg=BG,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 9),
        ).pack(fill="x")
        self._indeterminate = False
        self.window.update_idletasks()
        try:
            self.window.grab_set()
        except tk.TclError:
            pass

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KB", "MB", "GB") if get_language() == "en" else (
            "Б",
            "КБ",
            "МБ",
            "ГБ",
        )
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                if unit == "Б":
                    return f"{int(amount)} {unit}"
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{int(value)} Б"

    def set_indeterminate(self, stage: str, detail: str = "") -> None:
        if not self.exists():
            return
        self.stage.set(stage)
        if detail:
            self.detail.set(detail)
        self.percent.set("")
        if not self._indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self._indeterminate = True

    def set_progress(
        self,
        stage: str,
        completed: int,
        total: int,
        detail: str = "",
    ) -> None:
        if not self.exists():
            return
        if self._indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._indeterminate = False
        percent = max(0, min(100, int(completed * 100 / max(1, total))))
        self.stage.set(stage)
        self.progress.configure(value=percent)
        self.percent.set(f"{percent}%")
        if detail:
            self.detail.set(detail)
        elif total > 0:
            self.detail.set(
                tr("{completed} із {total}").format(
                    completed=self._format_bytes(completed),
                    total=self._format_bytes(total),
                )
            )

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        if not self.exists():
            return
        if self._indeterminate:
            self.progress.stop()
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class AudioLibraryApp:
    def __init__(
        self,
        root_dir: Path,
        start_hidden: bool = False,
        startup_progress=None,
        root: tk.Tk | None = None,
    ) -> None:
        def report_startup(value: int, uk_status: str, en_status: str) -> None:
            if startup_progress is None:
                return
            try:
                startup_progress(value, uk_status, en_status)
            except Exception:
                pass

        self.root_dir = root_dir
        self.app_settings_path = root_dir / "data" / "app_settings.json"
        self.app_settings = self._load_app_settings()
        set_language(str(self.app_settings["language"]))
        self.restart_requested = False
        self.update_command: list[str] | None = None
        self.update_manager = GitHubUpdateManager(__version__)
        self.available_update: ReleaseInfo | None = None
        self._update_check_in_progress = False
        self._update_download_in_progress = False
        self._update_progress_percent = 0
        self._update_state = "idle"
        self._update_error = ""
        self._update_settings_refresh = None
        self.update_manager.cleanup_stale_downloads()
        self._apply_saved_theme()
        self._apply_saved_accent()
        self.start_hidden = start_hidden
        report_startup(36, "Відкриття бази даних…", "Opening database…")
        self.store = LibraryStore(root_dir)
        report_startup(45, "Підготовка аудіосистеми…", "Preparing audio system…")
        self.player = WindowsMediaPlayer()
        kernel_streaming_cable = bool(
            self.app_settings["kernel_streaming_cable"]
        )
        # Shared mode keeps the physical voice in an isolated helper process.
        # Kernel Streaming owns the cable pin directly, so the main bridge
        # mixes the microphone and recordings into its single KS stream.
        self.virtual_microphone = VirtualMicrophoneBridge(
            mix_microphone=kernel_streaming_cable
        )
        self.microphone_relay = MicrophoneRelayProcess(root_dir)
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
            kernel_streaming_cable=kernel_streaming_cable,
            mix_microphone=kernel_streaming_cable,
        )
        self.microphone_routing = WindowsMicrophoneRouting(root_dir)
        self._audio_shutdown_done = False
        self._volume_save_job = None
        self._geometry_save_job = None
        self._session_wndproc = None
        self._previous_wndproc = None
        self.root = root if root is not None else tk.Tk()
        report_startup(58, "Створення головного вікна…", "Creating main window…")
        self.root.title(tr("AudioLib — локальна бібліотека аудіозаписів"))
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
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self.handle_window_close)
        self._window_mode = "normal"
        self._restore_geometry = initial_geometry
        self._drag_offset = (0, 0)
        self._resize_data: tuple[str, int, int, int, int, int, int] | None = None
        self._restore_override_after_map = False
        self._taskbar_style_initialized = False
        self.resize_grips: list[tk.Frame] = []
        self.window_outline_frames: list[tk.Frame] = []
        self._accent_buttons: list[tk.Widget] = []
        self.app_icon: ImageTk.PhotoImage | None = None
        self.tray_icon = None
        self.tray_window: tk.Toplevel | None = None
        self.remote_window: tk.Toplevel | None = None
        self.remote_settings_window: tk.Toplevel | None = None
        self.remote_output_target = "speaker"
        self.remote_output_buttons: dict[str, tk.Widget] = {}
        self.remote_track_buttons: dict[int, list[RemoteFavoriteButton]] = {}
        self.remote_favorites_frame: tk.Frame | None = None
        self.remote_count_label: tk.Label | None = None
        self.tray_count_label: tk.Label | None = None
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
        self._audio_recovery_attempts = 0
        self._audio_recovery_last_attempt = 0.0
        self._audio_health_last_reason = ""
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
        self.workspace_open_tabs: list[int] = []
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
        self.now_playing = LocalizedStringVar(value="Оберіть запис")
        self.time_text = tk.StringVar(value="0:00 / 0:00")
        self.status_text = LocalizedStringVar(value="Готово")
        self.categories_collapsed = bool(
            self.app_settings.get("categories_collapsed", False)
        )
        self._categories_expanded_width = 280
        self.track_sort_column = "title"
        self.track_sort_descending = False
        self.source_column_visible = False
        self.track_heading_labels = {
            "number": "",
            "title": tr("Назва"),
            "speaker_play": "",
            "favorite": "",
            "hotkey": tr("Гаряча клавіша"),
            "source": tr("Початковий файл"),
            "duration": tr("Тривалість"),
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
        report_startup(70, "Побудова інтерфейсу…", "Building interface…")
        self._build_ui()
        self._create_resize_grips()
        self.root.bind("<Map>", self._on_root_map, add="+")
        self.root.bind("<Configure>", self._schedule_window_geometry_save, add="+")
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
        report_startup(84, "Завантаження списку записів…", "Loading recordings…")
        self._refresh_tray_favorites()
        self._reload_global_hotkeys()
        self.refresh_tracks()
        self.root.after(10, self._poll_global_hotkeys)
        self.root.after(10, self._poll_audio_load_results)
        self.root.after(10, self._poll_virtual_start_results)
        self.root.after(10, self._poll_library_task_results)
        self.root.after(1000, self._poll_audio_stream_health)
        self._refresh_legacy_metadata_async()
        self._start_tray_icon()
        report_startup(94, "Завершення запуску…", "Finishing startup…")
        if self.start_hidden:
            self.root.withdraw()
        if self.app_settings["auto_virtual_microphone"]:
            self.root.after_idle(self._start_virtual_microphone_background)
        if self.app_settings["check_updates_on_startup"]:
            self.root.after_idle(self._check_for_updates)
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
            "kernel_streaming_cable": True,
            "latency_profile": "balanced",
            "accent_color": "Янтарний",
            "custom_accent_color": "#a34f0d",
            "photo_size": 92,
            "list_density": "comfortable",
            "library_view": "table",
            "theme": "dark",
            "window_geometry": None,
            "check_updates_on_startup": True,
            "categories_collapsed": False,
            "remote_button_customizations": {},
            "remote_slots": [None] * FAVORITES_LIMIT,
            "show_workspace_tabs": False,
        }
        try:
            payload = json.loads(self.app_settings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                defaults.update(
                    {key: payload[key] for key in defaults.keys() if key in payload}
                )
                # The first experimental build used WASAPI Exclusive. Preserve
                # the user's enabled protection choice while migrating it to
                # the working direct Kernel Streaming implementation.
                if (
                    "kernel_streaming_cable" not in payload
                    and payload.get("exclusive_cable") is True
                ):
                    defaults["kernel_streaming_cable"] = True
                    defaults["local_monitoring"] = True
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
            ("kernel_streaming_cable", True),
            ("check_updates_on_startup", True),
            ("categories_collapsed", False),
            ("show_workspace_tabs", False),
        ):
            if not isinstance(defaults[key], bool):
                defaults[key] = fallback
        if defaults["close_action"] not in {"tray", "exit"}:
            defaults["close_action"] = "tray"
        if defaults["startup_view"] not in {"favorites", "last"}:
            defaults["startup_view"] = "favorites"
        if defaults["language"] not in {"uk", "en"}:
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
        if defaults["accent_color"] not in {*ACCENT_PRESETS, "Власний"}:
            defaults["accent_color"] = "Янтарний"
        defaults["custom_accent_color"] = _valid_hex_color(
            defaults.get("custom_accent_color")
        )
        if defaults["photo_size"] not in {72, 92, 108}:
            defaults["photo_size"] = 92
        if defaults["list_density"] not in {"compact", "comfortable", "spacious"}:
            defaults["list_density"] = "comfortable"
        if defaults["library_view"] not in {"table", "tiles"}:
            defaults["library_view"] = "table"
        if defaults["theme"] not in {"dark", "light"}:
            defaults["theme"] = "dark"
        if not isinstance(defaults["window_geometry"], str):
            defaults["window_geometry"] = None
        customizations = defaults.get("remote_button_customizations")
        clean_customizations: dict[str, dict[str, str]] = {}
        allowed_colors = {"Графітний", *ACCENT_PRESETS.keys(), "Власний"}
        if isinstance(customizations, dict):
            for track_id, value in customizations.items():
                if not isinstance(value, dict):
                    continue
                label = str(value.get("label", "")).strip()[:64]
                color = str(value.get("color", "Графітний"))
                if color not in allowed_colors:
                    color = "Графітний"
                clean_customizations[str(track_id)] = {
                    "label": label,
                    "color": color,
                }
        defaults["remote_button_customizations"] = clean_customizations
        slots = defaults.get("remote_slots")
        clean_slots: list[dict[str, object] | None] = []
        if isinstance(slots, list):
            for value in slots[:FAVORITES_LIMIT]:
                if not isinstance(value, dict):
                    clean_slots.append(None)
                    continue
                try:
                    track_id = int(value.get("track_id"))
                except (TypeError, ValueError):
                    clean_slots.append(None)
                    continue
                label = str(value.get("label", "")).strip()[:64]
                color = str(value.get("color", "Графітний"))
                if color not in allowed_colors:
                    color = "Графітний"
                clean_slots.append(
                    {
                        "track_id": track_id,
                        "label": label,
                        "color": color,
                        "custom_color": _valid_hex_color(
                            value.get("custom_color", "#a34f0d")
                        ),
                    }
                )
        clean_slots.extend([None] * (FAVORITES_LIMIT - len(clean_slots)))
        defaults["remote_slots"] = clean_slots
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
        width = max(MIN_WINDOW_WIDTH, min(width, screen_width))
        height = max(MIN_WINDOW_HEIGHT, min(height, screen_height))
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

    def _schedule_window_geometry_save(self, event: tk.Event | None = None) -> None:
        """Persist the last stable size and position even after an abrupt exit."""
        if event is not None and event.widget is not self.root:
            return
        if self._window_mode != "normal":
            return
        if self._geometry_save_job is not None:
            try:
                self.root.after_cancel(self._geometry_save_job)
            except tk.TclError:
                pass
        self._geometry_save_job = self.root.after(600, self._persist_window_geometry)

    def _persist_window_geometry(self) -> None:
        self._geometry_save_job = None
        if self._window_mode != "normal":
            return
        self._save_window_geometry()
        self._save_volume_setting()

    def _apply_saved_accent(self) -> None:
        global ACCENT, ACCENT_HOVER
        name = str(self.app_settings.get("accent_color", "Янтарний"))
        if name == "Власний":
            ACCENT = _valid_hex_color(self.app_settings.get("custom_accent_color"))
            ACCENT_HOVER = _darken_hex_color(ACCENT)
        else:
            ACCENT, ACCENT_HOVER = ACCENT_PRESETS.get(
                name, ACCENT_PRESETS["Янтарний"]
            )

    def _apply_saved_theme(self) -> None:
        global BG, PANEL, CARD, FIELD, HOVER, BORDER, SECONDARY
        global PLAYER_BG, TITLE_BG, TITLE_HOVER, PLACEHOLDER, TITLE_TEXT, TEXT, MUTED
        global FAVORITES_BG, FAVORITES_SELECTED_BG, FAVORITES_TEXT, FAVORITES_MUTED
        global WARNING_BG, WARNING_TEXT
        global ERROR_BG, ERROR_TEXT, ERROR_BORDER, CAUTION_BORDER
        global SUCCESS_TEXT, CAUTION_TEXT
        global SWITCH_OFF, SWITCH_DISABLED, SWITCH_THUMB, TRAY_STOP_COLOR
        if self.app_settings.get("theme") == "light":
            BG = "#eceff3"
            PANEL = "#f8f9fb"
            CARD = "#ffffff"
            FIELD = "#e3e7ed"
            HOVER = "#d9dee6"
            BORDER = "#c5cbd4"
            SECONDARY = "#e1e5eb"
            PLAYER_BG = "#f8f9fb"
            TITLE_BG = "#ffffff"
            TITLE_HOVER = "#e8ebf0"
            PLACEHOLDER = "#6b7280"
            TITLE_TEXT = "#171717"
            TEXT = "#171717"
            MUTED = "#596273"
            FAVORITES_BG = "#fff1c2"
            FAVORITES_SELECTED_BG = "#f6d98d"
            FAVORITES_TEXT = "#5f3b00"
            FAVORITES_MUTED = "#8a5700"
            WARNING_BG = "#fff3d6"
            WARNING_TEXT = "#7a3a00"
            ERROR_BG = "#fff1f2"
            ERROR_TEXT = "#991b1b"
            ERROR_BORDER = "#dc2626"
            CAUTION_BORDER = "#d97706"
            SUCCESS_TEXT = "#047857"
            CAUTION_TEXT = "#a16207"
            SWITCH_OFF = "#c7ced8"
            SWITCH_DISABLED = "#e3e7ed"
            SWITCH_THUMB = "#ffffff"
            TRAY_STOP_COLOR = (23, 23, 23, 255)
        else:
            BG = "#141415"
            PANEL = "#0f0f10"
            CARD = "#19191b"
            FIELD = "#232325"
            HOVER = "#2b2b2e"
            BORDER = "#343438"
            SECONDARY = "#2b2b2e"
            PLAYER_BG = "#0f0f10"
            TITLE_BG = "#0f0f10"
            TITLE_HOVER = "#232325"
            PLACEHOLDER = "#85858b"
            TITLE_TEXT = "#ededed"
            TEXT = "#ededed"
            MUTED = "#a1a1a6"
            FAVORITES_BG = "#19191b"
            FAVORITES_SELECTED_BG = "#2b2b2e"
            FAVORITES_TEXT = "#ededed"
            FAVORITES_MUTED = "#85858b"
            WARNING_BG = "#422006"
            WARNING_TEXT = "#fde68a"
            ERROR_BG = "#450a0a"
            ERROR_TEXT = "#fecaca"
            ERROR_BORDER = "#dc2626"
            CAUTION_BORDER = "#b45309"
            SUCCESS_TEXT = "#34d399"
            CAUTION_TEXT = "#fbbf24"
            SWITCH_OFF = "#343438"
            SWITCH_DISABLED = "#232325"
            SWITCH_THUMB = "#ededed"
            TRAY_STOP_COLOR = (237, 237, 237, 255)

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
        color = tuple(bytes.fromhex(TEXT.lstrip("#"))) + (255,)

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
        icon_surface = SECONDARY if kind == "settings" else ACCENT
        color = tuple(
            bytes.fromhex(_contrast_text_color(icon_surface).lstrip("#"))
        ) + (255,)

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
        color = tuple(bytes.fromhex(TEXT.lstrip("#"))) + (255,)
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
        window.configure(bg=BORDER)
        window.protocol("WM_DELETE_WINDOW", self._hide_tray_window)
        self.tray_window = window

        panel = tk.Frame(window, bg=BG, padx=16, pady=14)
        panel.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(panel, bg=BG)
        header.pack(fill="x", pady=(0, 12))
        LocalizedLabel(
            header,
            text="Favorites",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
        ).pack(side="left")
        self.tray_count_label = LocalizedLabel(
            header,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.tray_count_label.pack(side="left", padx=(8, 0), pady=(2, 0))
        close_button = tk.Button(
            header,
            text="×",
            command=self._hide_tray_window,
            bg=BG,
            fg=MUTED,
            activebackground=HOVER,
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=7,
            pady=0,
            cursor="hand2",
            font=("Segoe UI", 14),
        )
        close_button.pack(side="right")
        close_button.bind("<Enter>", lambda _event: close_button.configure(bg=HOVER, fg=TEXT))
        close_button.bind("<Leave>", lambda _event: close_button.configure(bg=BG, fg=MUTED))

        list_area = tk.Frame(panel, bg=BG)
        list_area.pack(fill="both", expand=True)
        self.tray_window_canvas = tk.Canvas(
            list_area,
            bg=BG,
            highlightthickness=0,
            bd=0,
            width=410,
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
        scrollbar.pack(side="right", fill="y")
        self.tray_window_canvas.pack(side="left", fill="both", expand=True)
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
        stop_draw.rectangle((7, 6, 13, 12), fill=TRAY_STOP_COLOR)
        self.tray_stop_icon = ImageTk.PhotoImage(stop_icon)

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", pady=(12, 10))
        footer = tk.Frame(panel, bg=BG)
        footer.pack(fill="x")
        LocalizedLabel(
            footer,
            text="Один клік — керування, подвійний — AudioLib",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(side="left")
        self._button(footer, "Вийти", self._close, DANGER).pack(side="right")

        window.bind("<FocusOut>", self._schedule_tray_window_focus_check, add="+")
        self._populate_tray_window()

    def _populate_tray_window(self) -> None:
        if self.tray_window_items is None or not self.tray_window_items.winfo_exists():
            return
        for child in self.tray_window_items.winfo_children():
            child.destroy()
        if self.tray_count_label is not None:
            self.tray_count_label.configure(text=str(len(self.tray_favorites)))
        if not self.tray_favorites:
            LocalizedLabel(
                self.tray_window_items,
                text="Немає вибраних записів",
                bg=BG,
                fg=MUTED,
                pady=24,
                font=("Segoe UI", 10),
            ).pack(fill="x")
        else:
            for index, (track_id, label) in enumerate(self.tray_favorites):
                item = tk.Frame(
                    self.tray_window_items,
                    bg=BG,
                    padx=4,
                    pady=7,
                )
                item.pack(fill="x")
                label_parts = label.rsplit(" — ", 1)
                text_area = tk.Frame(item, bg=BG)
                text_area.pack(side="left", fill="x", expand=True)
                title = LocalizedLabel(
                    text_area,
                    text=label_parts[0],
                    bg=BG,
                    fg=TEXT,
                    anchor="w",
                    font=("Segoe UI Semibold", 9),
                )
                title.pack(fill="x")
                category = LocalizedLabel(
                    text_area,
                    text=label_parts[1] if len(label_parts) > 1 else "",
                    bg=BG,
                    fg=MUTED,
                    anchor="w",
                    font=("Segoe UI", 8),
                )
                category.pack(fill="x", pady=(1, 0))

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
                        bg=BG,
                        fg=TEXT,
                        activebackground=SECONDARY,
                        activeforeground=TEXT,
                        relief="flat",
                        bd=0,
                        highlightthickness=0,
                        width=30,
                        height=30,
                        padx=0,
                        pady=0,
                        anchor="center",
                        cursor="hand2",
                        font=("Segoe UI Semibold", 10),
                    )
                    button.bind(
                        "<Enter>",
                        lambda _event, control=button: control.configure(bg=HOVER),
                    )
                    button.bind(
                        "<Leave>",
                        lambda _event, control=button: control.configure(bg=BG),
                    )
                    return button

                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.stop_track_audio, tid
                    ),
                    image=self.tray_stop_icon,
                ).pack(side="right", padx=(4, 0))
                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.toggle_track_microphone, tid
                    ),
                    image=self.tray_control_icons["mic_play"],
                ).pack(side="right", padx=(4, 0))
                tray_control(
                    item,
                    command=lambda tid=track_id: self._run_tray_track_action(
                        self.toggle_track_speaker, tid
                    ),
                    image=self.tray_control_icons["speaker_play"],
                ).pack(side="right", padx=(8, 0))

                for widget in (item, text_area, title, category, *item.winfo_children()):
                    widget.bind(
                        "<MouseWheel>",
                        lambda event: self.tray_window_canvas.yview_scroll(
                            -1 if event.delta > 0 else 1, "units"
                        ),
                    )
                if index < len(self.tray_favorites) - 1:
                    tk.Frame(
                        self.tray_window_items,
                        bg=BORDER,
                        height=1,
                    ).pack(fill="x", padx=4)
        if self.tray_window_canvas is not None:
            row_count = max(1, len(self.tray_favorites))
            self.tray_window_canvas.configure(height=min(430, 12 + row_count * 52))
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

    def _ensure_auxiliary_taskbar_presence(self, window: tk.Toplevel) -> None:
        """Expose an overrideredirect utility window as a real taskbar window."""
        try:
            if not window.winfo_exists():
                return
            user32 = ctypes.windll.user32
            handle = int(window.winfo_id())
            get_parent = user32.GetParent
            get_parent.argtypes = [ctypes.c_void_p]
            get_parent.restype = ctypes.c_void_p
            wrapper = int(get_parent(ctypes.c_void_p(handle)) or 0)
            handle = wrapper or handle
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
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
            user32.SetWindowLongW(
                ctypes.c_void_p(handle),
                GWL_EXSTYLE,
                (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW,
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
            if not getattr(window, "_audiolib_taskbar_initialized", False):
                user32.ShowWindow(ctypes.c_void_p(handle), SW_HIDE)
                user32.ShowWindow(ctypes.c_void_p(handle), SW_SHOW)
                window._audiolib_taskbar_initialized = True
            self._set_toplevel_topmost(window, True)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _set_toplevel_topmost(self, window: tk.Toplevel, enabled: bool) -> None:
        """Set the real Windows z-order in addition to Tk's topmost flag."""
        try:
            if not window.winfo_exists():
                return
            window.attributes("-topmost", bool(enabled))
            user32 = ctypes.windll.user32
            handle = int(window.winfo_id())
            get_parent = user32.GetParent
            get_parent.argtypes = [ctypes.c_void_p]
            get_parent.restype = ctypes.c_void_p
            wrapper = int(get_parent(ctypes.c_void_p(handle)) or 0)
            handle = wrapper or handle
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(
                ctypes.c_void_p(handle),
                ctypes.c_void_p(HWND_TOPMOST if enabled else HWND_NOTOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
            )
        except (AttributeError, OSError, tk.TclError):
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
            self._restore_geometry = self._validated_window_geometry(
                self.root.geometry(),
                self._default_window_geometry(),
            )
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
        self._restore_geometry = self._validated_window_geometry(
            self._restore_geometry,
            self._default_window_geometry(),
        )
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
            # geometry() is applied asynchronously.  Reading winfo_x/y here can
            # still return the old maximized position and makes the restored
            # window move at a large offset from the pointer.  The new position
            # is already known, so keep the drag anchor based on it directly.
            self._drag_offset = (event.x_root - new_x, event.y_root - new_y)
            return
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
            ("n", "sb_v_double_arrow", {"x": 10, "y": 0, "relwidth": 1, "width": -158, "height": 8}),
            ("s", "sb_v_double_arrow", {"x": 10, "rely": 1, "y": -10, "relwidth": 1, "width": -20, "height": 10}),
            ("w", "sb_h_double_arrow", {"x": 0, "y": 35, "width": 10, "relheight": 1, "height": -45}),
            ("e", "sb_h_double_arrow", {"relx": 1, "x": -10, "y": 35, "width": 10, "relheight": 1, "height": -45}),
            ("sw", "size_ne_sw", {"x": 0, "rely": 1, "y": -14, "width": 14, "height": 14}),
            ("se", "size_nw_se", {"relx": 1, "x": -14, "rely": 1, "y": -14, "width": 14, "height": 14}),
        )
        for edge, cursor, placement in specs:
            grip_color = TITLE_BG if edge == "n" else BG
            grip = tk.Frame(self.root, bg=grip_color, cursor=cursor)
            grip.place(**placement)
            grip.bind("<ButtonPress-1>", lambda event, side=edge: self._start_resize(event, side))
            grip.bind("<B1-Motion>", self._resize_window)
            grip.bind("<ButtonRelease-1>", lambda _event: setattr(self, "_resize_data", None))
            grip.lift()
            self.resize_grips.append(grip)
        self.window_outline_frames = [
            tk.Frame(self.root, bg=BORDER, height=1),
            tk.Frame(self.root, bg=BORDER, width=1),
            tk.Frame(self.root, bg=BORDER, width=1),
            tk.Frame(self.root, bg=BORDER, height=1),
        ]
        self.window_outline_frames[0].place(x=0, y=0, relwidth=1)
        self.window_outline_frames[1].place(x=0, y=0, relheight=1)
        self.window_outline_frames[2].place(relx=1, x=-1, y=0, relheight=1)
        self.window_outline_frames[3].place(x=0, rely=1, y=-1, relwidth=1)
        for frame in self.window_outline_frames:
            frame.lift()

    def _show_resize_grips(self, visible: bool) -> None:
        for grip in self.resize_grips:
            if visible:
                grip.lift()
            else:
                grip.lower()
        for frame in self.window_outline_frames:
            frame.lift()

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
        min_width, min_height = MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT
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
        title_color = TITLE_BG
        hover_color = TITLE_HOVER
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
            icon_label = LocalizedLabel(
                self.title_bar, image=self.titlebar_icon, bg=title_color, bd=0
            )
            icon_label.pack(side="left", padx=(10, 7))
        else:
            icon_label = None

        title_label = LocalizedLabel(
            self.title_bar,
            text="AudioLib",
            bg=title_color,
            fg=TITLE_TEXT,
            font=("Segoe UI", 9),
        )
        title_label.pack(side="left")

        def control_icon(kind: str) -> ImageTk.PhotoImage:
            scale = 4
            image = Image.new("RGBA", (18 * scale, 18 * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            color = tuple(bytes.fromhex(TITLE_TEXT.lstrip("#"))) + (255,)
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
                fg=TITLE_TEXT,
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

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="top")

    def _build_secondary_title_bar(self, window: tk.Toplevel, title: str) -> None:
        """Draw a compact custom frame matching the main AudioLib window."""
        title_color = TITLE_BG
        hover_color = TITLE_HOVER
        window.overrideredirect(True)
        window._audiolib_close_allowed = True

        bar = tk.Frame(window, bg=title_color, height=34)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        if self.titlebar_icon is not None:
            icon_label = LocalizedLabel(bar, image=self.titlebar_icon, bg=title_color, bd=0)
            icon_label.pack(side="left", padx=(10, 7))
        else:
            icon_label = None
        title_label = LocalizedLabel(
            bar,
            text=title,
            bg=title_color,
            fg=TITLE_TEXT,
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
        tk.Frame(window, bg=BORDER, height=1).pack(fill="x", side="top")
        outline_frames = (
            tk.Frame(window, bg=BORDER, height=1),
            tk.Frame(window, bg=BORDER, width=1),
            tk.Frame(window, bg=BORDER, width=1),
            tk.Frame(window, bg=BORDER, height=1),
        )
        outline_frames[0].place(x=0, y=0, relwidth=1)
        outline_frames[1].place(x=0, y=0, relheight=1)
        outline_frames[2].place(relx=1, x=-1, y=0, relheight=1)
        outline_frames[3].place(x=0, rely=1, y=-1, relwidth=1)

        def lift_outline(_event=None) -> None:
            for frame in outline_frames:
                frame.lift()

        window.bind("<Configure>", lift_outline, add="+")
        window.after_idle(lift_outline)

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
                    self.microphone_relay.stop()
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
                if self.virtual_microphone.uses_kernel_streaming:
                    # The direct KS pin is owned by the main bridge, which
                    # already mixes the physical microphone into that stream.
                    self.microphone_relay.stop()
                else:
                    self.microphone_relay.start(
                        selected.friendly_name,
                        selected.description,
                    )
                if self._closing_event.is_set():
                    self.microphone_relay.stop()
                    self.virtual_microphone.close()
                    self.microphone_routing.restore_after_failed_start()
                    return
                self._write_audio_routing_log(
                    f"START selected={microphone_label(selected)} cable_default=true "
                    f"screen_capture_protected={self.virtual_microphone.screen_capture_protected} "
                    f"mode={'kernel-streaming' if self.virtual_microphone.uses_kernel_streaming else 'shared'}"
                )
                self._virtual_start_results.put((selected, None))
            except Exception as error:
                self.microphone_relay.stop()
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
                elif self.microphone_relay.is_running():
                    self._write_audio_routing_log(
                        "MICROPHONE_RELAY_ISOLATED process=true"
                    )
                    self.status_text.set(
                        "Віртуальний мікрофон працює; голос ізольовано від демонстрації"
                    )
                elif self.virtual_microphone.screen_capture_protected:
                    self.status_text.set(
                        "Віртуальний мікрофон працює через Kernel Streaming"
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
                    if pending_playback is not None and _askyesno(
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

    def _poll_audio_stream_health(self) -> None:
        """Recover a stalled microphone callback without treating silence as failure."""
        try:
            if self._closing_event.is_set():
                return
            if self.virtual_microphone.is_running() and not self._virtual_start_in_progress:
                healthy, reason = self.virtual_microphone.health_status()
                if (
                    healthy
                    and not self.virtual_microphone.uses_kernel_streaming
                    and not self.microphone_relay.is_running()
                ):
                    healthy = False
                    reason = "Процес фізичного мікрофона зупинився"

                now = time.monotonic()
                if healthy:
                    self._audio_health_last_reason = ""
                    if now - self._audio_recovery_last_attempt > 8.0:
                        self._audio_recovery_attempts = 0
                elif (
                    now - self._audio_recovery_last_attempt >= 5.0
                    and self._audio_recovery_attempts < 3
                ):
                    self._audio_recovery_attempts += 1
                    self._audio_recovery_last_attempt = now
                    self._audio_health_last_reason = reason
                    self._write_audio_routing_log(
                        "WATCHDOG_RESTART "
                        f"attempt={self._audio_recovery_attempts} reason={reason}"
                    )
                    self.status_text.set(
                        "Відновлення потоку фізичного мікрофона…"
                    )

                    def recovery_finished(
                        success: bool,
                        _physical: CaptureEndpoint | None,
                        error_text: str | None,
                    ) -> None:
                        if success:
                            self._write_audio_routing_log("WATCHDOG_RECOVERED")
                            self.status_text.set("Потік фізичного мікрофона відновлено")
                        else:
                            self._write_audio_routing_log(
                                f"WATCHDOG_RECOVERY_ERROR {error_text or 'unknown'}"
                            )

                    self._begin_virtual_microphone_start(
                        reset_existing=True,
                        callback=recovery_finished,
                    )
        except Exception as error:
            if str(error) != self._audio_health_last_reason:
                self._audio_health_last_reason = str(error)
                self._write_audio_routing_log(f"WATCHDOG_ERROR {error}")
        finally:
            try:
                if not self._closing_event.is_set():
                    self.root.after(1000, self._poll_audio_stream_health)
            except tk.TclError:
                pass

    def _choose_physical_microphone(self, explanation: str) -> CaptureEndpoint | None:
        try:
            microphones = list_physical_microphones()
        except AudioEndpointError as error:
            _showerror("Вибір мікрофона", str(error), parent=self.root)
            return None
        if not microphones:
            _showerror(
                "Вибір мікрофона",
                "Windows не знайшла жодного доступного фізичного мікрофона.",
                parent=self.root,
            )
            return None

        was_hidden = self.root.state() == "withdrawn"
        if was_hidden:
            self.show_window()
        dialog = tk.Toplevel(self.root)
        dialog.title(tr("Вибір фізичного мікрофона"))
        dialog.geometry("560x390")
        dialog.minsize(480, 320)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()
        self._build_secondary_title_bar(dialog, "Вибір фізичного мікрофона")
        selected: list[CaptureEndpoint] = []

        LocalizedLabel(
            dialog,
            text="Фізичний мікрофон",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w", padx=22, pady=(20, 6))
        LocalizedLabel(
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
            selectforeground=_contrast_text_color(ACCENT),
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
                _showerror("Вибір мікрофона", str(error), parent=dialog)
                return
            selected.append(endpoint)
            dialog.destroy()

        controls = tk.Frame(dialog, bg=BG)
        controls.pack(fill="x", padx=22, pady=16)
        self._button(controls, "Зберегти", accept, ACCENT).pack(side="left")
        self._button(controls, "Скасувати", dialog.destroy, SECONDARY).pack(
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
                    _showinfo(
                        "Вибір мікрофона",
                        f"Використовується: {microphone_label(selected)}",
                        parent=parent,
                    )
            elif error_text:
                _showerror(
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
        style.configure("Treeview.Heading", background=FIELD, foreground=TEXT,
                        relief="flat", font=("Segoe UI Semibold", 10))
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", _contrast_text_color(ACCENT))],
        )
        style.configure("TScale", background=BG, troughcolor=FIELD)
        style.configure(
            "Settings.Horizontal.TProgressbar",
            troughcolor=HOVER,
            background=ACCENT,
            bordercolor=HOVER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=7,
        )

        self.scrollbar_images = {
            "normal": ImageTk.PhotoImage(render_scrollbar_thumb_image(BORDER)),
            "active": ImageTk.PhotoImage(render_scrollbar_thumb_image(ACCENT)),
            "pressed": ImageTk.PhotoImage(render_scrollbar_thumb_image(ACCENT_HOVER)),
            "horizontal_normal": ImageTk.PhotoImage(
                render_scrollbar_thumb_image(BORDER, horizontal=True)
            ),
            "horizontal_active": ImageTk.PhotoImage(
                render_scrollbar_thumb_image(ACCENT, horizontal=True)
            ),
            "horizontal_pressed": ImageTk.PhotoImage(
                render_scrollbar_thumb_image(ACCENT_HOVER, horizontal=True)
            ),
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
            troughcolor=PLAYER_BG,
            background=BORDER,
            bordercolor=PLAYER_BG,
            lightcolor=PLAYER_BG,
            darkcolor=PLAYER_BG,
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
            troughcolor=PLAYER_BG,
            background=BORDER,
            bordercolor=PLAYER_BG,
            lightcolor=PLAYER_BG,
            darkcolor=PLAYER_BG,
            relief="flat",
            borderwidth=0,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        color: str | None = None,
        *,
        rounded: bool = True,
        image: ImageTk.PhotoImage | None = None,
        media_control: bool = True,
    ) -> tk.Widget:
        uses_accent = color is None or color == ACCENT
        color = color or ACCENT
        hover_colors = {
            ACCENT: ACCENT_HOVER,
            DANGER: "#b91c1c",
            SECONDARY: BORDER,
        }
        hover_color = hover_colors.get(color, ACCENT_HOVER)
        if rounded:
            button = RoundedButton(
                parent,
                text,
                command,
                color,
                hover_color,
                image=image,
                media_control=media_control,
            )
            if uses_accent:
                self._accent_buttons.append(button)
            return button
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg=_contrast_text_color(color),
            activebackground=hover_color,
            activeforeground=_contrast_text_color(hover_color),
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=hover_color))
        button.bind("<Leave>", lambda _event: button.configure(bg=color))
        if uses_accent:
            self._accent_buttons.append(button)
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
        LocalizedLabel(
            content,
            text=prompt,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        field_height = 42
        field_radius = 10
        field_color = CARD
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
                outline=ACCENT if focused else BORDER,
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

        error_text = LocalizedStringVar()
        LocalizedLabel(
            content,
            textvariable=error_text,
            bg=BG,
            fg=ERROR_TEXT,
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
        self._button(actions, "Скасувати", close_dialog, SECONDARY).pack(
            side="right"
        )
        self._button(actions, "Зберегти", submit, ACCENT).pack(
            side="right", padx=(0, 8)
        )

        # A subtle outline keeps the borderless dialog readable over panels
        # that use nearly the same dark background.
        outline_color = BORDER
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
        header = tk.Frame(self.root, bg=TITLE_BG, padx=20, pady=10)
        header.pack(fill="x")
        LocalizedLabel(header, text="AudioLib", bg=TITLE_BG, fg=TEXT,
                 font=("Segoe UI", 18)).pack(side="left")
        LocalizedLabel(header, text="Локальна бібліотека аудіозаписів", bg=TITLE_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=12, pady=(4, 0))
        header_actions = tk.Frame(header, bg=TITLE_BG)
        header_actions.pack(side="right")
        self._button(
            header_actions,
            "Пульт",
            self._open_remote_control,
            ACCENT,
        ).pack(side="left", padx=(0, 8))
        self._button(
            header_actions,
            "Налаштування",
            self.open_virtual_microphone_setup,
            SECONDARY,
            image=self.header_action_icons["settings"],
        ).pack(side="left")

        body = tk.PanedWindow(
            self.root,
            orient="horizontal",
            bg=BG,
            sashwidth=10,
            sashrelief="flat",
            sashcursor="sb_h_double_arrow",
            bd=0,
        )
        body.pack(fill="both", expand=True, padx=20, pady=(16, 12))
        left = tk.Frame(
            body,
            bg=PANEL,
            width=280,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
        )
        right = tk.Frame(
            body,
            bg=BG,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
        )
        body.add(left, minsize=240)
        body.add(right, minsize=500)
        self.body_paned = body
        self.categories_panel = left

        left_top = tk.Frame(left, bg=PANEL, padx=12, pady=12)
        left_top.pack(fill="x")
        self.categories_header = left_top
        self.categories_title = LocalizedLabel(
            left_top,
            text="Категорії",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI", 13),
        )
        self.categories_title.pack(side="left")
        self.add_person_button = self._button(
            left_top, "Додати", self.add_person
        )
        self.add_person_button.pack(side="right")
        self.categories_toggle_button = self._button(
            left_top,
            "▶" if self.categories_collapsed else "◀",
            self._toggle_categories_panel,
            SECONDARY,
            media_control=False,
        )
        self.categories_toggle_button.configure(width=40, height=34)
        self.categories_toggle_button.pack(side="right", padx=(0, 8))
        people_search_color = FIELD
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
        self.people_search_container = people_search_container

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
        self.person_search_placeholder = LocalizedLabel(
            people_search_container,
            text=" Пошук",
            image=self.person_search_icon,
            compound="left",
            bg=people_search_color,
            fg=PLACEHOLDER,
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
        people_scroll.pack(side="right", fill="y", pady=12)
        self.people_canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=12)
        self.people_scroll = people_scroll
        self.people_frame.bind("<Configure>", lambda _: self.people_canvas.configure(
            scrollregion=self.people_canvas.bbox("all")))
        self.people_canvas.bind("<Configure>", lambda e: self.people_canvas.itemconfigure(
            self.people_window, width=e.width))
        self.root.bind_all("<MouseWheel>", self._on_people_mousewheel, add="+")
        self.root.after_idle(self._refresh_workspace_tabs)

        tabs_bar = tk.Frame(right, bg=PANEL, padx=8, pady=4)
        self.workspace_tabs_bar = tabs_bar
        if bool(self.app_settings.get("show_workspace_tabs", False)):
            tabs_bar.pack(fill="x", padx=20, pady=(0, 4))
        tabs_view = tk.Frame(tabs_bar, bg=PANEL)
        tabs_view.pack(fill="both", expand=True)
        self.workspace_tabs_canvas = tk.Canvas(
            tabs_view,
            bg=PANEL,
            height=32,
            highlightthickness=0,
            bd=0,
            xscrollincrement=40,
        )
        self.workspace_tabs_frame = tk.Frame(self.workspace_tabs_canvas, bg=PANEL)
        self.workspace_tabs_window = self.workspace_tabs_canvas.create_window(
            (0, 0), window=self.workspace_tabs_frame, anchor="nw"
        )
        self.workspace_tabs_frame.bind(
            "<Configure>",
            lambda _event: self.workspace_tabs_canvas.configure(
                scrollregion=self.workspace_tabs_canvas.bbox("all")
            ),
        )
        self.workspace_tabs_canvas.bind(
            "<Configure>",
            lambda event: self.workspace_tabs_canvas.itemconfigure(
                self.workspace_tabs_window, height=event.height
            ),
        )
        self.workspace_tabs_canvas.bind(
            "<MouseWheel>",
            lambda event: self.workspace_tabs_canvas.xview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )
        self.workspace_tabs_canvas.pack(fill="x", expand=True)
        self.workspace_tab_buttons: dict[int, tk.Widget] = {}
        self._refresh_workspace_tabs()

        person_header = tk.Frame(right, bg=BG, pady=8)
        person_header.pack(fill="x", padx=20)
        self.person_title = LocalizedLabel(person_header, text="Оберіть людину", bg=BG, fg=TEXT,
                                     font=("Segoe UI", 20))
        self.person_title.pack(side="left")
        self.delete_person_button = self._button(person_header, "Видалити", self.delete_person, DANGER)
        self.edit_person_button = self._button(person_header, "Змінити", self.edit_person, SECONDARY)
        self.delete_person_button.pack(side="right", padx=(8, 0))
        self.edit_person_button.pack(side="right")

        tools = tk.Frame(right, bg=BG, pady=4)
        tools.pack(fill="x", padx=20)
        self.add_tracks_button = self._button(tools, "Додати аудіо", self.add_tracks)
        self.add_tracks_button.grid(row=0, column=0, sticky="w")
        self.source_column_button = self._button(
            tools, "Показати оригінальну назву", self.toggle_source_column, SECONDARY
        )
        self.source_column_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        tools.grid_columnconfigure(2, weight=1)
        search_width = 270
        search_height = 38
        search_radius = 10
        search_color_hex = FIELD
        search_container = tk.Canvas(
            tools,
            bg=BG,
            width=search_width,
            height=search_height,
            highlightthickness=0,
            bd=0,
        )
        search_container.grid(row=0, column=2, sticky="e", padx=(8, 0))

        def draw_track_search_background(event: tk.Event) -> None:
            width = max(search_radius * 2, event.width)
            search_container.delete("search_background")
            search_container._surface_image = render_rounded_surface(
                width,
                search_height,
                fill=search_color_hex,
                radius=search_radius,
            )
            search_container.create_image(
                0,
                0,
                image=search_container._surface_image,
                anchor="nw",
                tags="search_background",
            )
            search_container.tag_lower("search_background")

        search_container.bind("<Configure>", draw_track_search_background)
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
        self.track_search_entry.place(
            x=10,
            y=3,
            relwidth=1.0,
            width=-20,
            height=32,
        )
        search_icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        search_draw = ImageDraw.Draw(search_icon)
        search_color = (148, 163, 184, 255)
        search_draw.ellipse((8, 8, 40, 40), outline=search_color, width=6)
        search_draw.line((36, 36, 56, 56), fill=search_color, width=6)
        search_icon = search_icon.resize((16, 16), Image.Resampling.LANCZOS)
        self.track_search_icon = ImageTk.PhotoImage(search_icon)
        self.track_search_placeholder = LocalizedLabel(
            search_container,
            text=" Пошук",
            image=self.track_search_icon,
            compound="left",
            bg=search_color_hex,
            fg=PLACEHOLDER,
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

        tools_layout = {"compact": None}

        def update_tools_layout(event: tk.Event) -> None:
            compact = event.width < 660
            if tools_layout["compact"] == compact:
                return
            tools_layout["compact"] = compact
            search_container.grid_forget()
            if compact:
                search_container.grid(
                    row=1,
                    column=0,
                    columnspan=3,
                    sticky="ew",
                    pady=(8, 0),
                )
            else:
                search_container.configure(width=search_width)
                search_container.grid(
                    row=0,
                    column=2,
                    sticky="e",
                    padx=(8, 0),
                )

        tools.bind("<Configure>", update_tools_layout, add="+")

        tk.Frame(right, bg=BORDER, height=1, bd=0).pack(
            fill="x",
            padx=20,
            pady=(8, 0),
        )

        table_frame = tk.Frame(right, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=20, pady=(8, 12))
        self.track_table = CustomTrackTable(
            table_frame,
            columns=tuple(self.track_heading_labels),
            show="headings",
            selectmode="browse",
        )
        self.track_table.set_view_mode(str(self.app_settings["library_view"]))
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
        track_actions.pack(fill="x", padx=20, pady=(8, 4), before=table_frame)
        self.random_track_button = self._button(
            track_actions,
            "Випадковий запис",
            self.play_random_track,
            ACCENT,
            image=self.button_action_icons["random"],
        )
        self.random_track_button.pack(side="left", padx=(0, 8))
        self.rename_track_button = self._button(
            track_actions,
            "Перейменувати",
            self.rename_track,
            SECONDARY,
        )
        self.rename_track_button.pack(side="right", padx=(8, 0))
        self.delete_track_button = self._button(
            track_actions,
            "Видалити запис",
            self.delete_track,
            DANGER,
        )
        self.delete_track_button.pack(side="right")
        self.rename_track_button.configure(state="disabled")
        self.delete_track_button.configure(state="disabled")

        player = tk.Frame(self.root, bg=PLAYER_BG, padx=24, pady=16)
        player.pack(fill="x")
        player_meta = tk.Frame(player, bg=PLAYER_BG)
        player_meta.pack(fill="x", pady=(0, 8))
        LocalizedLabel(player, textvariable=self.now_playing, bg=PLAYER_BG, fg=TEXT,
                 font=("Segoe UI", 11), anchor="w").pack(in_=player_meta, side="left")
        LocalizedLabel(player, textvariable=self.time_text, bg=PLAYER_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(in_=player_meta, side="right")
        transport = tk.Frame(player, bg=PLAYER_BG)
        transport.pack(fill="x")
        self.play_pause_button = self._button(
            transport, "▶", self.toggle_pause
        )
        self.play_pause_button.pack(side="left", padx=(4, 12))
        self._button(
            transport, "■", self.stop_playback, SECONDARY
        ).pack(side="left", padx=(0, 12))
        self.progress = FlatScale(
            transport,
            from_=0,
            to=1,
            command=self._preview_seek,
            background=PLAYER_BG,
            trough_color=BORDER,
            fill_color=ACCENT,
            active_color=ACCENT_HOVER,
            thumb_radius=5,
        )
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress.bind(
            "<ButtonPress-1>", lambda _: setattr(self, "seeking", True), add="+"
        )
        self.progress.bind("<ButtonRelease-1>", self._commit_seek, add="+")
        self.volume_icon = self._make_volume_icon()
        LocalizedLabel(transport, image=self.volume_icon, bg=PLAYER_BG, bd=0).pack(side="left", padx=(16, 0))
        self.volume_scale = FlatScale(
            transport,
            from_=0,
            to=1000,
            command=self._set_volume,
            width=108,
            background=PLAYER_BG,
            trough_color=BORDER,
            fill_color=TEXT,
            active_color=ACCENT,
            thumb_radius=3,
        )
        self.volume_scale.set(self.volume_value)
        self.volume_scale.pack(side="left", padx=(5, 0))

    def _refresh_workspace_tabs(self) -> None:
        if not hasattr(self, "workspace_tabs_frame"):
            return
        show_tabs = bool(self.app_settings.get("show_workspace_tabs", False))
        if hasattr(self, "workspace_tabs_bar"):
            if show_tabs and not self.workspace_tabs_bar.winfo_manager():
                self.workspace_tabs_bar.pack(fill="x", padx=20, pady=(0, 4))
            elif not show_tabs and self.workspace_tabs_bar.winfo_manager():
                self.workspace_tabs_bar.pack_forget()
        if not show_tabs:
            return
        for widget in self.workspace_tabs_frame.winfo_children():
            widget.destroy()
        self.workspace_tab_buttons.clear()
        counts = self.store.track_counts_by_person()
        valid_tabs: list[int] = []
        for person_id in self.workspace_open_tabs:
            if person_id == FAVORITES_PERSON_ID or self.store.get_person(person_id):
                if person_id not in valid_tabs:
                    valid_tabs.append(person_id)
        self.workspace_open_tabs = valid_tabs
        for person_id in self.workspace_open_tabs:
            if person_id == FAVORITES_PERSON_ID:
                name = "Favorites"
                count = self.store.favorite_count()
            else:
                person = self.store.get_person(person_id)
                if person is None:
                    continue
                name = person.name
                count = counts.get(person_id, 0)
            selected = person_id == self.selected_person_id
            tab = WorkspaceTab(
                self.workspace_tabs_frame,
                f"{name}  {count}",
                lambda pid=person_id: self.select_person(pid),
                lambda pid=person_id: self._close_workspace_tab(pid),
                active=selected,
            )
            tab.pack(side="left", padx=(0, 6), pady=2)
            self.workspace_tab_buttons[person_id] = tab

    def _close_workspace_tab(self, person_id: int) -> None:
        if person_id not in self.workspace_open_tabs:
            return
        tab_index = self.workspace_open_tabs.index(person_id)
        self.workspace_open_tabs.remove(person_id)
        if person_id != self.selected_person_id:
            self._refresh_workspace_tabs()
            return
        if self.workspace_open_tabs:
            next_index = min(tab_index, len(self.workspace_open_tabs) - 1)
            self.select_person(self.workspace_open_tabs[next_index])
            return
        self.select_person(FAVORITES_PERSON_ID)

    def _remote_color_values(self) -> dict[str, tuple[str, str]]:
        return {"Графітний": (SECONDARY, BORDER), **ACCENT_PRESETS}

    def _remote_customizations(self) -> dict[str, dict[str, str]]:
        value = self.app_settings.get("remote_button_customizations")
        if not isinstance(value, dict):
            value = {}
            self.app_settings["remote_button_customizations"] = value
        return value

    def _remote_slots(self) -> list[dict[str, object] | None]:
        value = self.app_settings.get("remote_slots")
        if not isinstance(value, list):
            value = []
        slots: list[dict[str, object] | None] = []
        for item in value[:FAVORITES_LIMIT]:
            slots.append(item if isinstance(item, dict) else None)
        slots.extend([None] * (FAVORITES_LIMIT - len(slots)))
        self.app_settings["remote_slots"] = slots
        return slots

    def _all_remote_tracks(self) -> list[tuple[object, str]]:
        result: list[tuple[object, str]] = []
        for person in self.store.list_people():
            for track in self.store.list_tracks(person.id):
                result.append((track, f"{person.name} — {track.title}"))
        return result

    def _open_remote_control(self) -> None:
        """Show Favorites as a compact grid of configurable launch buttons."""
        if self.remote_window is not None:
            try:
                if self.remote_window.winfo_exists():
                    self._render_remote_buttons()
                    self.remote_window.deiconify()
                    self._set_toplevel_topmost(self.remote_window, True)
                    self.remote_window.lift()
                    return
            except tk.TclError:
                self.remote_window = None

        window = tk.Toplevel(self.root)
        window.title("AudioLib — Пульт")
        window.geometry("500x780+40+40")
        window.minsize(460, 760)
        window.configure(bg=BG)
        window._audiolib_close_callback = window.withdraw
        self._build_secondary_title_bar(window, "Пульт")
        self._set_toplevel_topmost(window, True)

        def keep_remote_visible(_event=None) -> None:
            try:
                if window.winfo_exists() and window.state() != "withdrawn":
                    settings_visible = (
                        self.remote_settings_window is not None
                        and self.remote_settings_window.winfo_exists()
                        and self.remote_settings_window.winfo_viewable()
                    )
                    if settings_visible:
                        self._set_toplevel_topmost(window, False)
                        return
                    self._set_toplevel_topmost(window, True)
                    window.lift()
                    self._ensure_auxiliary_taskbar_presence(window)
            except tk.TclError:
                pass

        window.bind(
            "<Map>",
            lambda _event: window.after_idle(keep_remote_visible),
            add="+",
        )
        window.bind(
            "<FocusOut>",
            lambda _event: window.after(50, keep_remote_visible),
            add="+",
        )
        window.after(50, keep_remote_visible)
        window.after(250, keep_remote_visible)
        self.remote_window = window

        header = tk.Frame(window, bg=TITLE_BG, padx=24, pady=16)
        header.pack(fill="x")
        header_text = tk.Frame(header, bg=TITLE_BG)
        header_text.pack(side="left", fill="x", expand=True)
        LocalizedLabel(
            header_text,
            text="Пульт",
            bg=TITLE_BG,
            fg=TEXT,
            font=("Segoe UI", 17),
        ).pack(anchor="w")
        self._button(
            header,
            "Налаштувати кнопки",
            self._open_remote_button_settings,
            SECONDARY,
        ).pack(side="right", padx=(16, 0))

        controls = tk.Frame(window, bg=BG, padx=24, pady=16)
        controls.pack(fill="x")
        LocalizedLabel(
            controls,
            text="Канал запуску",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        mode_buttons = tk.Frame(controls, bg=BG)
        mode_buttons.pack(side="left", padx=(16, 0))
        self.remote_output_buttons.clear()
        for target, label in (("speaker", "Динаміки"), ("microphone", "Мікрофон")):
            button = self._button(
                mode_buttons,
                label,
                lambda value=target: self._set_remote_output_target(value),
                ACCENT if target == self.remote_output_target else SECONDARY,
            )
            button.pack(side="left", padx=(0, 8))
            self.remote_output_buttons[target] = button
        self._button(
            controls,
            "Стоп",
            self._stop_remote_audio,
            SECONDARY,
        ).pack(side="right")
        self.remote_count_label = LocalizedLabel(
            controls,
            text="",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.remote_count_label.pack(side="right", padx=(0, 16))

        self.remote_favorites_frame = tk.Frame(window, bg=BG, padx=20, pady=8)
        self.remote_favorites_frame.pack(fill="both", expand=True)
        for column in range(3):
            self.remote_favorites_frame.grid_columnconfigure(
                column, weight=0, minsize=116, uniform="favorite"
            )
        for row in range(5):
            self.remote_favorites_frame.grid_rowconfigure(
                row, weight=0, minsize=116, uniform="favorite"
            )
        self.remote_favorites_frame.grid_anchor("center")
        self._render_remote_buttons()

    def _render_remote_buttons(self) -> None:
        frame = self.remote_favorites_frame
        if frame is None:
            return
        try:
            if not frame.winfo_exists():
                return
        except tk.TclError:
            return
        for widget in frame.winfo_children():
            widget.destroy()
        self.remote_track_buttons.clear()
        slots = self._remote_slots()
        configured_count = sum(1 for slot in slots if slot is not None)
        if self.remote_count_label is not None:
            self.remote_count_label.configure(text=f"{configured_count}/{FAVORITES_LIMIT}")
        hotkeys = self.store.favorite_hotkeys()
        colors = self._remote_color_values()
        for index in range(FAVORITES_LIMIT):
            slot = slots[index]
            track = None
            if slot is not None:
                try:
                    track = self.store.get_track(int(slot.get("track_id")))
                except (TypeError, ValueError):
                    track = None
            if track is None:
                title = ""
                hotkey = ""
                color, hover_color = FIELD, HOVER
                command = lambda slot_index=index: self._open_remote_button_settings(
                    slot_index
                )
            else:
                custom_label = str(slot.get("label", "")).strip()
                title = custom_label or track.title
                hotkey = display_hotkey(hotkeys.get(track.id))
                color_name = str(slot.get("color", "Графітний"))
                if color_name == "Власний":
                    color = _valid_hex_color(slot.get("custom_color", "#a34f0d"))
                    hover_color = _darken_hex_color(color)
                else:
                    color, hover_color = colors.get(
                        color_name, colors["Графітний"]
                    )
                command = lambda track_id=track.id: self._play_remote_favorite(track_id)
            button = RemoteFavoriteButton(
                frame,
                title=title,
                hotkey=hotkey,
                command=command,
                color=color,
                hover_color=hover_color,
            )
            button.grid(
                row=index // 3,
                column=index % 3,
                padx=6,
                pady=6,
            )
            if track is not None:
                self.remote_track_buttons.setdefault(track.id, []).append(button)

    def _flash_remote_track(self, track_id: int) -> None:
        window = self.remote_window
        if window is None:
            return
        try:
            if not window.winfo_exists() or not window.winfo_viewable():
                return
        except tk.TclError:
            return
        for button in self.remote_track_buttons.get(track_id, []):
            try:
                if button.winfo_exists():
                    button.flash()
            except tk.TclError:
                continue

    def _open_remote_button_settings(self, slot_index: int | None = None) -> None:
        if self.remote_settings_window is not None:
            try:
                if self.remote_settings_window.winfo_exists():
                    self.remote_settings_window.lift()
                    selector = getattr(self, "_remote_settings_select_slot", None)
                    if slot_index is not None and callable(selector):
                        selector(slot_index)
                    return
            except tk.TclError:
                self.remote_settings_window = None

        track_options = self._all_remote_tracks()
        favorite_tracks = self.store.list_favorites()
        favorite_options = []
        for track in favorite_tracks:
            person = self.store.get_person(track.person_id)
            prefix = person.name if person is not None else "Favorites"
            favorite_options.append((track, f"{prefix} — {track.title}"))

        window = tk.Toplevel(self.remote_window or self.root)
        window.title("AudioLib — Налаштування кнопок")
        window.geometry("980x640+80+80")
        window.minsize(900, 600)
        window.configure(bg=BG)
        self.remote_settings_window = window

        def close_window() -> None:
            self.remote_settings_window = None
            self._remote_settings_select_slot = None
            window.destroy()

            def restore_remote_topmost() -> None:
                remote = self.remote_window
                if remote is None:
                    return
                try:
                    if remote.winfo_exists() and remote.state() != "withdrawn":
                        self._set_toplevel_topmost(remote, True)
                        remote.lift()
                        self._ensure_auxiliary_taskbar_presence(remote)
                except tk.TclError:
                    pass

            restore_remote_topmost()
            self.root.after(50, restore_remote_topmost)
            self.root.after(250, restore_remote_topmost)

        window._audiolib_close_callback = close_window
        self._build_secondary_title_bar(window, "Налаштування кнопок")
        LocalizedLabel(
            window,
            text="Налаштування пульта",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI", 18),
        ).pack(anchor="w", padx=24, pady=(20, 4))
        LocalizedLabel(
            window,
            text="Оберіть слот, запис і його оформлення",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=24, pady=(0, 16))

        actions = tk.Frame(window, bg=BG, padx=24, pady=16)
        actions.pack(side="bottom", fill="x")

        content = tk.Frame(window, bg=BG)
        content.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        slots_panel = tk.Frame(
            content,
            bg=PANEL,
            padx=16,
            pady=16,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        slots_panel.pack(side="left", fill="y")
        LocalizedLabel(
            slots_panel,
            text="Кнопки",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        slot_buttons: dict[int, tk.Button] = {}
        selected_slot = max(0, min(FAVORITES_LIMIT - 1, slot_index or 0))
        selected_track_id: int | None = None

        editor_shell = tk.Frame(
            content,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        editor_shell.pack(side="left", fill="both", expand=True, padx=(16, 0))
        editor_canvas = tk.Canvas(
            editor_shell,
            bg=PANEL,
            highlightthickness=0,
            bd=0,
        )
        editor_scroll = ttk.Scrollbar(
            editor_shell,
            orient="vertical",
            command=editor_canvas.yview,
            style="AudioLib.Vertical.TScrollbar",
        )
        editor = tk.Frame(editor_canvas, bg=PANEL, padx=16, pady=16)
        editor_window = editor_canvas.create_window(
            (0, 0), window=editor, anchor="nw"
        )
        editor_canvas.configure(yscrollcommand=editor_scroll.set)
        editor_scroll.pack(side="right", fill="y", pady=8)
        editor_canvas.pack(side="left", fill="both", expand=True)
        editor.bind(
            "<Configure>",
            lambda _event: editor_canvas.configure(
                scrollregion=editor_canvas.bbox("all")
            ),
        )
        editor_canvas.bind(
            "<Configure>",
            lambda event: editor_canvas.itemconfigure(
                editor_window, width=event.width
            ),
        )
        editor_canvas.bind(
            "<MouseWheel>",
            lambda event: editor_canvas.yview_scroll(
                -1 if event.delta > 0 else 1, "units"
            ),
        )

        def scroll_remote_editor(event: tk.Event):
            if isinstance(event.widget, tk.Listbox):
                return None
            editor_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        window.bind("<MouseWheel>", scroll_remote_editor, add="+")
        list_area = tk.Frame(editor, bg=PANEL)
        list_area.pack(fill="both", expand=True)
        all_search_var = tk.StringVar()
        favorite_search_var = tk.StringVar()

        def build_track_column(
            parent: tk.Misc,
            title: str,
            search_variable: tk.StringVar,
        ) -> tuple[tk.Frame, tk.Listbox]:
            column = tk.Frame(parent, bg=PANEL)
            LocalizedLabel(
                column,
                text=title,
                bg=PANEL,
                fg=TEXT,
                font=("Segoe UI Semibold", 10),
            ).pack(anchor="w", pady=(0, 6))
            search = tk.Entry(
                column,
                textvariable=search_variable,
                bg=FIELD,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                bd=0,
                font=("Segoe UI", 9),
            )
            search.pack(fill="x", ipady=6, pady=(0, 8))
            placeholder = tr("Пошук")
            search_variable.set(placeholder)
            search.configure(fg=PLACEHOLDER)

            def focus_search(_event=None) -> None:
                if search_variable.get() == placeholder:
                    search_variable.set("")
                search.configure(fg=TEXT)

            def blur_search(_event=None) -> None:
                if not search_variable.get().strip():
                    search_variable.set(placeholder)
                    search.configure(fg=PLACEHOLDER)

            search.bind("<FocusIn>", focus_search)
            search.bind("<FocusOut>", blur_search)
            body = tk.Frame(column, bg=PANEL)
            body.pack(fill="both", expand=True)
            listbox = tk.Listbox(
                body,
                bg=FIELD,
                fg=TEXT,
                selectbackground=ACCENT,
                selectforeground=_contrast_text_color(ACCENT),
                activestyle="none",
                relief="flat",
                bd=0,
                highlightthickness=1,
                highlightbackground=BORDER,
                font=("Segoe UI", 9),
                exportselection=False,
            )
            scrollbar = ttk.Scrollbar(
                body,
                orient="vertical",
                command=listbox.yview,
                style="AudioLib.Vertical.TScrollbar",
            )
            listbox.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            listbox.pack(side="left", fill="both", expand=True)
            return column, listbox

        all_column, track_list = build_track_column(
            list_area, "Усі записи", all_search_var
        )
        all_column.pack(side="left", fill="both", expand=True)
        tk.Frame(list_area, bg=BORDER, width=1).pack(
            side="left", fill="y", padx=10
        )
        favorite_column, favorite_list = build_track_column(
            list_area, "Favorites", favorite_search_var
        )
        favorite_column.pack(side="left", fill="both", expand=True)

        visible_tracks: list[object] = []
        visible_favorites: list[object] = []

        def refill_list(
            listbox: tk.Listbox,
            source: list[tuple[object, str]],
            query: str,
            target: list[object],
        ) -> None:
            target.clear()
            listbox.delete(0, "end")
            normalized = query.strip().casefold()
            for track, display_name in source:
                if normalized and normalized not in display_name.casefold():
                    continue
                target.append(track)
                listbox.insert("end", display_name)

        def refresh_all_tracks(*_args) -> None:
            query = all_search_var.get()
            refill_list(
                track_list,
                track_options,
                "" if query == tr("Пошук") else query,
                visible_tracks,
            )

        def refresh_favorite_tracks(*_args) -> None:
            query = favorite_search_var.get()
            refill_list(
                favorite_list,
                favorite_options,
                "" if query == tr("Пошук") else query,
                visible_favorites,
            )

        all_search_var.trace_add("write", refresh_all_tracks)
        favorite_search_var.trace_add("write", refresh_favorite_tracks)
        refresh_all_tracks()
        refresh_favorite_tracks()

        label_var = tk.StringVar()
        color_var = tk.StringVar(value="Графітний")
        custom_color_var = tk.StringVar(value="#a34f0d")
        fields = tk.Frame(editor, bg=PANEL)
        fields.pack(fill="x", pady=(14, 0))
        LocalizedLabel(
            fields,
            text="Назва кнопки",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        label_entry = tk.Entry(
            fields,
            textvariable=label_var,
            bg=FIELD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            font=("Segoe UI", 11),
        )
        label_entry.pack(fill="x", ipady=7, pady=(5, 12))
        LocalizedLabel(
            fields,
            text="Колір кнопки",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 8))
        colors_frame = tk.Frame(fields, bg=PANEL)
        colors_frame.pack(fill="x")
        color_buttons: dict[str, tk.Button] = {}

        def refresh_color_buttons() -> None:
            selected = color_var.get()
            for name, button in color_buttons.items():
                button.configure(
                    relief="solid" if name == selected else "flat",
                    bd=2 if name == selected else 0,
                )
            custom_button = color_buttons.get("Власний")
            if custom_button is not None:
                custom = _valid_hex_color(custom_color_var.get())
                custom_button.configure(
                    bg=custom,
                    fg=_contrast_text_color(custom),
                    activebackground=_darken_hex_color(custom),
                    activeforeground=_contrast_text_color(
                        _darken_hex_color(custom)
                    ),
                )

        def choose_custom_button_color() -> None:
            _rgb, selected = colorchooser.askcolor(
                color=_valid_hex_color(custom_color_var.get()),
                title=tr("Власний колір кнопки"),
                parent=window,
            )
            if selected:
                custom_color_var.set(_valid_hex_color(selected))
                color_var.set("Власний")
                refresh_color_buttons()

        for index, (name, (color, hover)) in enumerate(
            self._remote_color_values().items()
        ):
            button = tk.Button(
                colors_frame,
                text=name,
                command=lambda value=name: (
                    color_var.set(value),
                    refresh_color_buttons(),
                ),
                bg=color,
                fg=_contrast_text_color(color),
                activebackground=hover,
                activeforeground=_contrast_text_color(hover),
                relief="flat",
                bd=0,
                font=("Segoe UI", 9),
                cursor="hand2",
            )
            button.grid(
                row=index // 3,
                column=index % 3,
                sticky="ew",
                padx=7,
                pady=7,
                ipady=5,
            )
            colors_frame.grid_columnconfigure(index % 3, weight=1)
            color_buttons[name] = button
        custom_index = len(color_buttons)
        custom_button = tk.Button(
            colors_frame,
            text="Власний…",
            command=choose_custom_button_color,
            bg=_valid_hex_color(custom_color_var.get()),
            fg=_contrast_text_color(_valid_hex_color(custom_color_var.get())),
            activebackground=_darken_hex_color(custom_color_var.get()),
            activeforeground=_contrast_text_color(
                _darken_hex_color(custom_color_var.get())
            ),
            relief="flat",
            bd=0,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        custom_button.grid(
            row=custom_index // 3,
            column=custom_index % 3,
            sticky="ew",
            padx=7,
            pady=7,
            ipady=5,
        )
        colors_frame.grid_columnconfigure(custom_index % 3, weight=1)
        color_buttons["Власний"] = custom_button

        def refresh_slot_buttons() -> None:
            slots = self._remote_slots()
            for index, button in slot_buttons.items():
                assigned = slots[index] is not None
                button.configure(
                    bg=ACCENT if index == selected_slot else SECONDARY if assigned else FIELD,
                    activebackground=ACCENT_HOVER if index == selected_slot else HOVER,
                    relief="solid",
                    bd=1,
                    highlightthickness=0,
                )

        def load_slot(index: int) -> None:
            nonlocal selected_slot, selected_track_id
            selected_slot = max(0, min(FAVORITES_LIMIT - 1, index))
            slot = self._remote_slots()[selected_slot]
            track_list.selection_clear(0, "end")
            favorite_list.selection_clear(0, "end")
            selected_track_id = None
            if slot is None:
                label_var.set("")
                color_var.set("Графітний")
                custom_color_var.set("#a34f0d")
            else:
                try:
                    selected_track_id = int(slot.get("track_id"))
                except (TypeError, ValueError):
                    selected_track_id = None
                track = self.store.get_track(selected_track_id) if selected_track_id else None
                label_var.set(str(slot.get("label", "")) or (track.title if track else ""))
                color_var.set(str(slot.get("color", "Графітний")))
                custom_color_var.set(
                    _valid_hex_color(slot.get("custom_color", "#a34f0d"))
                )
                all_index = next(
                    (
                        position
                        for position, item in enumerate(visible_tracks)
                        if item.id == selected_track_id
                    ),
                    None,
                )
                favorite_index = next(
                    (
                        position
                        for position, item in enumerate(visible_favorites)
                        if item.id == selected_track_id
                    ),
                    None,
                )
                if all_index is not None:
                    track_list.selection_set(all_index)
                    track_list.see(all_index)
                if favorite_index is not None:
                    favorite_list.selection_set(favorite_index)
                    favorite_list.see(favorite_index)
            refresh_color_buttons()
            refresh_slot_buttons()

        self._remote_settings_select_slot = load_slot

        for index in range(FAVORITES_LIMIT):
            button = tk.Button(
                slots_panel,
                text=str(index + 1),
                command=lambda value=index: load_slot(value),
                bg=FIELD,
                fg=TEXT,
                activeforeground=_contrast_text_color(ACCENT),
                relief="solid",
                bd=1,
                width=4,
                height=2,
                cursor="hand2",
                font=("Segoe UI Semibold", 9),
            )
            button.grid(
                row=1 + index // 3,
                column=index % 3,
                padx=4,
                pady=4,
            )
            slot_buttons[index] = button

        def select_track(
            listbox: tk.Listbox,
            visible: list[object],
            other_listbox: tk.Listbox,
        ) -> None:
            nonlocal selected_track_id
            selection = listbox.curselection()
            if not selection:
                return
            index = int(selection[0])
            if index >= len(visible):
                return
            track = visible[index]
            other_listbox.selection_clear(0, "end")
            previous = self.store.get_track(selected_track_id) if selected_track_id else None
            if not label_var.get().strip() or (
                previous is not None and label_var.get().strip() == previous.title
            ):
                label_var.set(track.title)
            selected_track_id = track.id

        def save_selected() -> None:
            if selected_track_id is None:
                _showwarning(
                    "Налаштування пульта",
                    "Оберіть аудіозапис для цієї кнопки.",
                    parent=window,
                )
                return
            track = self.store.get_track(selected_track_id)
            if track is None:
                return
            label = label_var.get().strip()[:64]
            if not label:
                label = track.title
            stored_label = "" if label == track.title else label
            self._remote_slots()[selected_slot] = {
                "track_id": track.id,
                "label": stored_label,
                "color": color_var.get(),
                "custom_color": _valid_hex_color(custom_color_var.get()),
            }
            self._schedule_volume_save()
            self._render_remote_buttons()
            refresh_slot_buttons()

        def reset_selected() -> None:
            nonlocal selected_track_id
            self._remote_slots()[selected_slot] = None
            selected_track_id = None
            self._schedule_volume_save()
            self._render_remote_buttons()
            track_list.selection_clear(0, "end")
            favorite_list.selection_clear(0, "end")
            label_var.set("")
            color_var.set("Графітний")
            custom_color_var.set("#a34f0d")
            refresh_color_buttons()
            refresh_slot_buttons()

        track_list.bind(
            "<<ListboxSelect>>",
            lambda _event: select_track(track_list, visible_tracks, favorite_list),
        )
        favorite_list.bind(
            "<<ListboxSelect>>",
            lambda _event: select_track(
                favorite_list, visible_favorites, track_list
            ),
        )
        load_slot(selected_slot)

        self._button(actions, "Зберегти", save_selected, ACCENT).pack(side="right")
        self._button(actions, "Закрити", close_window, SECONDARY).pack(
            side="right", padx=(0, 8)
        )
        self._button(actions, "Очистити кнопку", reset_selected, SECONDARY).pack(side="left")
        window.bind("<Escape>", lambda _event: close_window())
        remote = self.remote_window
        if remote is not None:
            try:
                if remote.winfo_exists():
                    self._set_toplevel_topmost(remote, False)
            except tk.TclError:
                pass
        window.transient(self.root)
        window.lift()

    def _set_remote_output_target(self, target: str) -> None:
        if target not in {"speaker", "microphone"}:
            return
        self.remote_output_target = target
        for value, button in self.remote_output_buttons.items():
            active = value == target
            button.configure(
                color=ACCENT if active else SECONDARY,
                hover_color=ACCENT_HOVER if active else BORDER,
            )

    def _play_remote_favorite(self, track_id: int) -> None:
        if self.remote_output_target == "microphone":
            self.play_track_in_microphone(track_id)
            return
        self.play_track_in_speaker(track_id)

    def _stop_remote_audio(self) -> None:
        self.stop_playback()
        self.stop_virtual_microphone_sound()

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

    def _apply_categories_panel_state(self) -> None:
        """Apply the compact or expanded category-sidebar layout."""
        if self.categories_collapsed:
            self.categories_title.pack_forget()
            self.add_person_button.pack_forget()
            self.categories_toggle_button.pack_forget()
            self.categories_toggle_button.configure(text="▶", width=40, height=34)
            self.categories_toggle_button.pack(anchor="center")
            self.people_search_container.pack_forget()
            self.people_canvas.pack_configure(padx=(4, 0))
            target_width = max(
                116,
                int(self.app_settings.get("photo_size", 92)) + 38,
            )
            self.body_paned.paneconfigure(
                self.categories_panel,
                minsize=target_width,
            )
        else:
            self.categories_toggle_button.pack_forget()
            self.categories_title.pack(side="left")
            self.add_person_button.pack(side="right")
            self.categories_toggle_button.configure(text="◀", width=40, height=34)
            self.categories_toggle_button.pack(side="right", padx=(0, 8))
            self.people_search_container.pack(
                fill="x",
                padx=16,
                before=self.people_canvas,
            )
            self.people_canvas.pack_configure(padx=(10, 0))
            target_width = max(280, self._categories_expanded_width)
            self.body_paned.paneconfigure(self.categories_panel, minsize=280)

        try:
            self.body_paned.sash_place(0, target_width, 0)
        except tk.TclError:
            pass

    def _toggle_categories_panel(self) -> None:
        if not self.categories_collapsed:
            try:
                current_width = int(self.body_paned.sash_coord(0)[0])
                if current_width >= 300:
                    self._categories_expanded_width = current_width
            except (tk.TclError, ValueError, TypeError):
                pass
        self.categories_collapsed = not self.categories_collapsed
        self.app_settings["categories_collapsed"] = self.categories_collapsed
        self._schedule_volume_save()
        self._apply_categories_panel_state()
        self.refresh_people()

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
        image = Image.new("RGB", (size, size), ACCENT)
        draw = ImageDraw.Draw(image)
        initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
        try:
            font = ImageFont.truetype("arial.ttf", size // 3)
        except OSError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), initials, font=font)
        draw.text(
            ((size - box[2]) / 2, (size - box[3]) / 2 - 2),
            initials,
            fill=_contrast_text_color(ACCENT),
            font=font,
        )
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
        cache_key = ("favorites-bookmark", size, CARD, FIELD, ACCENT)
        cached = self._photo_cache.get(cache_key)
        if cached is not None:
            return cached
        scale = 4
        canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        margin = 2 * scale
        draw.ellipse(
            (margin, margin, size * scale - margin, size * scale - margin),
            fill=FIELD,
            outline=BORDER,
            width=scale,
        )
        center_x = size * scale / 2
        # Match the optical centering used by table/tile bookmark controls.
        center_y = size * scale / 2 + scale
        half_width = size * 0.16 * scale
        half_height = size * 0.24 * scale
        left = int(center_x - half_width)
        right = int(center_x + half_width)
        top = int(center_y - half_height)
        bottom = int(center_y + half_height)
        notch = int(center_y + size * 0.08 * scale)
        draw.line(
            [(left, bottom), (left, top), (right, top), (right, bottom),
             (int(center_x), notch), (left, bottom)],
            fill=ACCENT,
            width=max(scale, int(size * 0.035 * scale)),
            joint="curve",
        )
        canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
        result = ImageTk.PhotoImage(canvas)
        self._remember_photo(cache_key, result)
        return result

    def refresh_people(self) -> None:
        for widget in self.people_frame.winfo_children():
            widget.destroy()
        self.photo_refs.clear()
        self._person_card_widgets.clear()
        favorites_selected = self.selected_person_id == FAVORITES_PERSON_ID
        photo_size = int(self.app_settings.get("photo_size", 92))
        density = str(self.app_settings.get("list_density", "comfortable"))
        card_padding = {"compact": 4, "comfortable": 8, "spacious": 12}.get(
            density, 8
        )
        card_gap = {"compact": 4, "comfortable": 8, "spacious": 12}.get(
            density, 4
        )
        favorites_card = tk.Frame(
            self.people_frame,
            bg=FAVORITES_SELECTED_BG if favorites_selected else FAVORITES_BG,
            padx=8,
            pady=card_padding,
            cursor="hand2",
        )
        favorites_card.pack(fill="x", padx=6, pady=(card_gap, card_gap * 2))
        favorite_photo = self._favorites_photo(photo_size)
        self.photo_refs.append(favorite_photo)
        favorite_photo_label = LocalizedLabel(
            favorites_card,
            image=favorite_photo,
            bg=favorites_card["bg"],
            cursor="hand2",
        )
        favorite_photo_label.pack(
            side="left" if not self.categories_collapsed else "top",
            anchor="center" if self.categories_collapsed else "w",
        )
        favorite_name_label = LocalizedLabel(
            favorites_card,
            text="Favorites",
            bg=favorites_card["bg"],
            fg=FAVORITES_TEXT,
            font=("Segoe UI", 11),
            wraplength=180,
            justify="left",
            cursor="hand2",
        )
        if not self.categories_collapsed:
            favorite_name_label.pack(side="left", padx=12)
        favorite_count_label = LocalizedLabel(
            favorites_card,
            text=f"{self.store.favorite_count()}/{FAVORITES_LIMIT}",
            bg=favorites_card["bg"],
            fg=ACCENT if favorites_selected else FAVORITES_MUTED,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        if not self.categories_collapsed:
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
            LocalizedLabel(self.people_frame, text="Додайте першу людину", bg=PANEL, fg=MUTED,
                     pady=30, font=("Segoe UI", 11)).pack(fill="x")
        for person in people:
            selected = person.id == self.selected_person_id
            card = tk.Frame(self.people_frame, bg=HOVER if selected else CARD, padx=8, pady=card_padding,
                            cursor="hand2")
            card.pack(fill="x", padx=6, pady=card_gap)
            photo = self._person_photo(person, photo_size)
            self.photo_refs.append(photo)
            photo_label = LocalizedLabel(card, image=photo, bg=card["bg"], cursor="hand2")
            photo_label.pack(
                side="left" if not self.categories_collapsed else "top",
                anchor="center" if self.categories_collapsed else "w",
            )
            count_label = LocalizedLabel(
                card,
                text=str(track_counts.get(person.id, 0)),
                bg=card["bg"],
                fg=ACCENT if selected else MUTED,
                font=("Segoe UI", 10),
                cursor="hand2",
            )
            if not self.categories_collapsed:
                count_label.pack(side="right", padx=(8, 4))
            name_label = LocalizedLabel(card, text=person.name, bg=card["bg"], fg=TEXT,
                                  font=("Segoe UI", 11), wraplength=180, justify="left",
                                  cursor="hand2")
            if not self.categories_collapsed:
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
            FAVORITES_SELECTED_BG
            if selected
            else (FAVORITES_BG if person_id == FAVORITES_PERSON_ID else CARD)
        )
        card, photo_label, name_label, count_label = widgets
        for widget in (card, photo_label, name_label, count_label):
            try:
                widget.configure(bg=background)
            except tk.TclError:
                return
        is_favorites = person_id == FAVORITES_PERSON_ID
        name_label.configure(
            fg=(
                FAVORITES_TEXT if is_favorites else TEXT
            )
        )
        count_label.configure(
            fg=(
                ACCENT
                if selected
                else FAVORITES_MUTED if is_favorites else MUTED
            )
        )

    def select_person(self, person_id: int) -> None:
        previous_person_id = self.selected_person_id
        is_favorites = person_id == FAVORITES_PERSON_ID
        person = None if is_favorites else self.store.get_person(person_id)
        if not is_favorites and person is None:
            person_id = FAVORITES_PERSON_ID
            is_favorites = True
        if person_id not in self.workspace_open_tabs:
            self.workspace_open_tabs.append(person_id)
        self.selected_person_id = person_id
        if not is_favorites:
            self.app_settings["last_person_id"] = person_id
            self._schedule_volume_save()
        self.person_title.configure(
            text="Favorites" if is_favorites else (person.name if person else "Оберіть людину")
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
        self._refresh_workspace_tabs()

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
            _showerror("Помилка вибору файлу", str(error), parent=self.root)
            return
        photo = selected[0] if selected else ""
        try:
            person = self.store.add_person(name, Path(photo) if photo else None)
            self.status_text.set(f"Додано людину: {person.name}")
            self.select_person(person.id)
        except (OSError, ValueError) as error:
            _showerror("Не вдалося додати", str(error), parent=self.root)

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
                _showerror("Помилка", str(error), parent=self.root)
                return
        if _askyesno("Фотографія", "Вибрати нову фотографію?", parent=self.root):
            try:
                selected = choose_files(
                    self.root,
                    title="Оберіть фотографію",
                    filetypes=[("Зображення", " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS)))],
                )
            except FileDialogError as error:
                _showerror("Помилка вибору файлу", str(error), parent=self.root)
                return
            photo = selected[0] if selected else ""
            if photo:
                try:
                    self.store.set_person_photo(person.id, Path(photo))
                except (OSError, ValueError) as error:
                    _showerror("Помилка фотографії", str(error), parent=self.root)
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
        if not _askyesno(
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
            _showinfo("Спочатку оберіть людину", "Аудіозаписи додаються до вибраної людини.")
            return
        try:
            paths = choose_files(
                self.root,
                title="Оберіть аудіозаписи",
                filetypes=[("Аудіофайли", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS)))],
                multiple=True,
            )
        except FileDialogError as error:
            _showerror("Помилка вибору файлу", str(error), parent=self.root)
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
                    _showerror(
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
            self._update_track_buttons()
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
        self._update_track_buttons()

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
        state = "normal" if self._selected_track() is not None else "disabled"
        self.rename_track_button.configure(state=state)
        self.delete_track_button.configure(state=state)

    def _on_track_table_click(self, event: tk.Event) -> None:
        if self.track_table.identify_region(event.x, event.y) != "cell":
            return
        row_id = self.track_table.identify_row(event.y, event.x)
        display_column = self.track_table.identify_column(event.x, event.y)
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
        row_id = self.track_table.identify_row(event.y, event.x)
        display_column = self.track_table.identify_column(event.x, event.y)
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
            _showwarning("Favorites", str(error), parent=self.root)
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
            fill=ERROR_BG if error else WARNING_BG,
            outline=ERROR_BORDER if error else CAUTION_BORDER,
            radius=11,
        )
        notice.create_image(0, 0, image=notice._surface_image, anchor="nw")
        notice.create_text(
            width / 2,
            height / 2 - 2,
            text=tr(message),
            fill=ERROR_TEXT if error else WARNING_TEXT,
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
        """Warn unobtrusively when a bare key is observed globally."""
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
                "запускати запис під час введення тексту, але саме натискання "
                "залишається доступним грі або іншій програмі."
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
                    self._flash_remote_track(track_id)
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
                virtual_keys.extend(parsed.virtual_keys)
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
        LocalizedLabel(
            content,
            text=track.title,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).pack(fill="x")
        LocalizedLabel(
            content,
            text=(
                "Натисніть бажану комбінацію. Вона працюватиме глобально, "
                "навіть коли AudioLib згорнута в трей. Можна призначити "
                "одну клавішу або послідовність, наприклад Alt + 1 + 0 + 2."
            ),
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=465,
            anchor="w",
        ).pack(fill="x", pady=(8, 14))
        captured_text = tk.StringVar(value=display_hotkey(current_hotkey))
        hotkey_label = LocalizedLabel(
            content,
            textvariable=captured_text,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
            height=2,
        )
        hotkey_label.pack(fill="x")
        error_text = LocalizedStringVar()
        LocalizedLabel(
            content,
            textvariable=error_text,
            bg=BG,
            fg=ERROR_TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))
        LocalizedLabel(
            content,
            text="Backspace — очистити · Esc — скасувати",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        completed = False
        pending_hotkey: str | None = None
        captured_key_names: list[str] = []
        captured_modifiers = 0
        capture_finalize_job: str | None = None

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
            nonlocal pending_hotkey, captured_modifiers
            if completed:
                return "break"
            if event.keysym == "Escape":
                close_dialog()
                return "break"
            modifier_keysyms = {
                "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                "Meta_L", "Meta_R", "Super_L", "Super_R",
            }
            if event.keysym in modifier_keysyms:
                return "break"
            modifiers_down = any(
                ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000
                for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)
            )
            if event.keysym == "BackSpace" and not modifiers_down:
                save_hotkey(None)
                return "break"
            try:
                pressed = parse_hotkey(
                    hotkey_from_tk_event(
                        event.keysym,
                        int(event.state),
                        int(event.keycode),
                    )
                )
                key_name = pressed.key_names[0]
                if key_name not in captured_key_names:
                    captured_key_names.append(key_name)
                captured_modifiers |= pressed.modifiers
                modifier_names = [
                    name
                    for name, flag in (
                        ("Ctrl", 0x0002),
                        ("Alt", 0x0001),
                        ("Shift", 0x0004),
                        ("Win", 0x0008),
                    )
                    if captured_modifiers & flag
                ]
                value = parse_hotkey(
                    "+".join((*modifier_names, *captured_key_names))
                ).canonical
            except ValueError as error:
                error_text.set(str(error))
                return "break"
            if value.casefold() == str(self.app_settings["stop_hotkey"]).casefold():
                error_text.set("Ця комбінація призначена глобальній команді Stop")
                return "break"
            captured_text.set(display_hotkey(value))
            error_text.set("")
            pending_hotkey = value
            return "break"

        def capture_release(event: tk.Event) -> str:
            nonlocal pending_hotkey, capture_finalize_job
            del event
            if completed or pending_hotkey is None:
                return "break"

            if capture_finalize_job is not None:
                try:
                    dialog.after_cancel(capture_finalize_job)
                except tk.TclError:
                    pass

            def finish_when_released() -> None:
                nonlocal pending_hotkey, capture_finalize_job
                capture_finalize_job = None
                if completed or pending_hotkey is None:
                    return
                try:
                    parsed = parse_hotkey(pending_hotkey)
                except ValueError as error:
                    error_text.set(str(error))
                    return
                keys = list(parsed.virtual_keys)
                if parsed.modifiers & 0x0001:
                    keys.append(0x12)
                if parsed.modifiers & 0x0002:
                    keys.append(0x11)
                if parsed.modifiers & 0x0004:
                    keys.append(0x10)
                if parsed.modifiers & 0x0008:
                    keys.extend((0x5B, 0x5C))
                if any(
                    ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000
                    for key in keys
                ):
                    capture_finalize_job = dialog.after(40, finish_when_released)
                    return
                value = pending_hotkey
                pending_hotkey = None
                save_hotkey(value)

            capture_finalize_job = dialog.after(80, finish_when_released)
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
        LocalizedLabel(
            content,
            text="Зупинити все відтворення",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).pack(fill="x")
        LocalizedLabel(
            content,
            text=(
                "Натисніть нову комбінацію. Вона зупинятиме звук у динаміках "
                "і віртуальному мікрофоні, навіть коли AudioLib згорнута в трей. "
                "Доступні окремі клавіші та послідовності на кшталт Alt + 1 + 0 + 2."
            ),
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=465,
            anchor="w",
        ).pack(fill="x", pady=(8, 14))
        captured_text = tk.StringVar(value=display_hotkey(current_hotkey))
        LocalizedLabel(
            content,
            textvariable=captured_text,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
            height=2,
        ).pack(fill="x")
        error_text = LocalizedStringVar()
        LocalizedLabel(
            content,
            textvariable=error_text,
            bg=BG,
            fg=ERROR_TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(5, 0))

        completed = False
        pending_hotkey: str | None = None
        captured_key_names: list[str] = []
        captured_modifiers = 0
        capture_finalize_job: str | None = None

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
            nonlocal completed, pending_hotkey, captured_modifiers
            if completed:
                return "break"
            if event.keysym == "Escape":
                close_dialog()
                return "break"
            if event.keysym in {
                "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                "Meta_L", "Meta_R", "Super_L", "Super_R",
            }:
                return "break"
            try:
                pressed = parse_hotkey(
                    hotkey_from_tk_event(
                        event.keysym,
                        int(event.state),
                        int(event.keycode),
                    )
                )
                key_name = pressed.key_names[0]
                if key_name not in captured_key_names:
                    captured_key_names.append(key_name)
                captured_modifiers |= pressed.modifiers
                modifier_names = [
                    name
                    for name, flag in (
                        ("Ctrl", 0x0002),
                        ("Alt", 0x0001),
                        ("Shift", 0x0004),
                        ("Win", 0x0008),
                    )
                    if captured_modifiers & flag
                ]
                value = parse_hotkey(
                    "+".join((*modifier_names, *captured_key_names))
                ).canonical
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
            return "break"

        def capture_release(event: tk.Event) -> str:
            nonlocal completed, pending_hotkey, capture_finalize_job
            del event
            if completed or pending_hotkey is None:
                return "break"

            if capture_finalize_job is not None:
                try:
                    dialog.after_cancel(capture_finalize_job)
                except tk.TclError:
                    pass

            def finish_when_released() -> None:
                nonlocal completed, pending_hotkey, capture_finalize_job
                capture_finalize_job = None
                if completed or pending_hotkey is None:
                    return
                try:
                    parsed = parse_hotkey(pending_hotkey)
                except ValueError as error:
                    error_text.set(str(error))
                    return
                keys = list(parsed.virtual_keys)
                if parsed.modifiers & 0x0001:
                    keys.append(0x12)
                if parsed.modifiers & 0x0002:
                    keys.append(0x11)
                if parsed.modifiers & 0x0004:
                    keys.append(0x10)
                if parsed.modifiers & 0x0008:
                    keys.extend((0x5B, 0x5C))
                if any(
                    ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000
                    for key in keys
                ):
                    capture_finalize_job = dialog.after(40, finish_when_released)
                    return

                value = pending_hotkey
                pending_hotkey = None
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

            capture_finalize_job = dialog.after(80, finish_when_released)
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
            _showinfo(
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
            _showinfo(
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
                    _showerror(
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
                        _showerror(
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
            if _askyesno(
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
            _showerror("Файл не знайдено", "Аудіофайл відсутній у бібліотеці.", parent=self.root)
            return
        # Do not render the same recording through the direct player and the
        # virtual-microphone monitor at once.  Besides sounding doubled
        # locally, Discord can capture the direct copy during screen sharing.
        if (
            self.virtual_track_id == track_id
            or self._microphone_load_track_id == track_id
        ):
            self._microphone_load_serial += 1
            self._microphone_load_track_id = None
            self.virtual_microphone.stop_sound()
            self.virtual_track_id = None
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
                _showerror("Помилка відтворення", str(error), parent=self.root)
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
            _showerror("Файл не знайдено", "Аудіофайл відсутній у бібліотеці.", parent=self.root)
            return
        # The bridge already creates the optional physical-headphone monitor.
        # Stop a direct copy of this same track so it cannot be captured a
        # second time by Discord's screen-share audio path.
        if (
            self.current_track_id == track_id
            or self._speaker_load_track_id == track_id
        ):
            self._speaker_load_serial += 1
            self._speaker_load_track_id = None
            try:
                self.player.stop()
            except PlayerError:
                pass
            self.progress.set(0)
            self.play_pause_button.configure(text="▶")
            self.time_text.set(f"0:00 / {format_time(self.current_track_length_ms)}")
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

    def _refresh_update_settings(self) -> None:
        callback = self._update_settings_refresh
        if callback is None:
            return
        try:
            callback()
        except tk.TclError:
            self._update_settings_refresh = None

    def _check_for_updates(self, manual: bool = False) -> None:
        if self._update_check_in_progress or self._update_download_in_progress:
            self._refresh_update_settings()
            return
        self._update_check_in_progress = True
        self._update_state = "checking"
        self._update_error = ""
        self._refresh_update_settings()

        def worker() -> None:
            release = None
            error = None
            try:
                release = self.update_manager.check_latest()
            except Exception as caught:
                error = caught
            if self._closing_event.is_set():
                return
            try:
                self.root.after(
                    0,
                    lambda: self._finish_update_check(release, error, manual),
                )
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update_check(
        self,
        release: ReleaseInfo | None,
        error: Exception | None,
        manual: bool,
    ) -> None:
        self._update_check_in_progress = False
        if error is not None:
            self._update_state = "error"
            self._update_error = str(error)
            if manual:
                self.status_text.set("Не вдалося перевірити оновлення")
        elif release is None:
            self.available_update = None
            self._update_state = "current"
            if manual:
                self.status_text.set("Установлено найновішу версію AudioLib")
        else:
            self.available_update = release
            self._update_state = "available"
            self.status_text.set(
                tr("Доступне оновлення AudioLib {version}").format(
                    version=release.version
                )
            )
        self._refresh_update_settings()

    def _install_available_update(self, parent: tk.Misc) -> None:
        release = self.available_update
        if release is None or self._update_download_in_progress:
            return
        if not getattr(sys, "frozen", False):
            if release.page_url:
                webbrowser.open(release.page_url)
            _showinfo(
                "Оновлення AudioLib",
                "Автоматичне встановлення доступне у зібраній версії AudioLib. Сторінку релізу відкрито у браузері.",
                parent=parent,
            )
            return
        question = tr(
            "Завантажити й установити AudioLib {version}? Програма автоматично перезапуститься, а бібліотека та налаштування залишаться без змін."
        ).format(version=release.version)
        if not _askyesno(
            "Оновлення AudioLib",
            question,
            parent=parent,
        ):
            return

        self._update_download_in_progress = True
        self._update_progress_percent = 0
        self._update_state = "downloading"
        self._update_error = ""
        self._refresh_update_settings()
        progress_dialog = OperationProgressDialog(
            self,
            parent,
            "Оновлення AudioLib",
            "Завантаження оновлення…",
        )
        progress_dialog.set_progress(
            "Завантаження оновлення…",
            0,
            release.size,
        )

        def progress(downloaded: int, total: int) -> None:
            if self._closing_event.is_set():
                return
            try:
                self.root.after(
                    0,
                    lambda current=downloaded, size=total: update_progress(
                        current,
                        size,
                    ),
                )
            except tk.TclError:
                pass

        def update_progress(downloaded: int, total: int) -> None:
            percent = max(0, min(100, int(downloaded * 100 / max(1, total))))
            self._update_progress_percent = percent
            progress_dialog.set_progress(
                "Завантаження оновлення…",
                downloaded,
                total,
            )
            self._refresh_update_settings()

        def worker() -> None:
            update_command = None
            error = None

            def preparation_progress(completed: int, total: int) -> None:
                if self._closing_event.is_set():
                    return
                try:
                    self.root.after(
                        0,
                        lambda current=completed, size=total: progress_dialog.set_progress(
                            "Підготовка файлів оновлення…",
                            current,
                            size,
                        ),
                    )
                except tk.TclError:
                    pass

            try:
                package = self.update_manager.download(release, progress)
                try:
                    self.root.after(
                        0,
                        lambda: progress_dialog.set_indeterminate(
                            "Перевірка пакета оновлення…",
                            "Перевірка цілісності та структури файлів",
                        ),
                    )
                except tk.TclError:
                    pass
                update_command = self.update_manager.prepare_installer(
                    package,
                    self.root_dir,
                    os.getpid(),
                    progress=preparation_progress,
                    language=str(self.app_settings["language"]),
                )
            except Exception as caught:
                error = caught
            if self._closing_event.is_set():
                return
            try:
                self.root.after(
                    0,
                    lambda: download_finished(update_command, error),
                )
            except tk.TclError:
                pass

        def download_finished(
            update_command: list[str] | None,
            error: Exception | None,
        ) -> None:
            self._update_download_in_progress = False
            if error is not None or update_command is None:
                progress_dialog.close()
                self._update_state = "error"
                self._update_error = str(error or UpdateError("Невідома помилка"))
                self._refresh_update_settings()
                _showerror("Оновлення AudioLib", self._update_error, parent=parent)
                return
            self.update_command = update_command
            progress_dialog.set_indeterminate(
                "Підготовка до перезапуску…",
                "AudioLib автоматично продовжить установлення після закриття",
            )
            self._update_state = "installing"
            self.status_text.set("Встановлення оновлення…")
            self._refresh_update_settings()
            self._close()

        threading.Thread(target=worker, daemon=True).start()

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
        window.title(tr("Налаштування"))
        window_width = 900
        window_height = 680
        self.root.update_idletasks()
        window_x = self.root.winfo_x() + max(0, (self.root.winfo_width() - window_width) // 2)
        window_y = self.root.winfo_y() + max(0, (self.root.winfo_height() - window_height) // 2)
        window.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
        window.minsize(820, 620)
        window.resizable(True, True)
        window.configure(bg=BG)
        window.transient(self.root)
        self._build_secondary_title_bar(window, "Налаштування")

        def close_settings() -> None:
            if getattr(self, "settings_window", None) is window:
                self.settings_window = None
            self._update_settings_refresh = None
            window.destroy()

        window._audiolib_close_callback = close_settings
        window.bind(
            "<Destroy>",
            lambda event: setattr(self, "settings_window", None)
            if event.widget is window and getattr(self, "settings_window", None) is window
            else None,
            add="+",
        )
        status = LocalizedStringVar()

        shell = tk.Frame(window, bg=BG)
        shell.pack(fill="both", expand=True)

        sidebar_bg = PANEL
        active_bg = SECONDARY
        navigation = tk.Frame(shell, bg=sidebar_bg, width=220)
        navigation.pack(side="left", fill="y")
        navigation.pack_propagate(False)
        tk.Frame(shell, bg=BORDER, width=1).pack(side="left", fill="y")
        page_host = tk.Frame(shell, bg=BG)
        page_host.pack(side="left", fill="both", expand=True)

        LocalizedLabel(
            navigation,
            text="Налаштування",
            bg=sidebar_bg,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 16),
        ).pack(fill="x", padx=20, pady=(24, 4))
        LocalizedLabel(
            navigation,
            text="AudioLib",
            bg=sidebar_bg,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 9),
        ).pack(fill="x", padx=20, pady=(0, 20))

        pages: dict[str, tuple[tk.Frame, tk.Canvas, tk.Frame]] = {}
        nav_buttons: dict[str, tk.Button] = {}
        active_page_key = ""
        content = tk.Frame(page_host, bg=BG)
        page_descriptions = {
            "Загальні": "Запуск, поведінка вікна та мова",
            "Відтворення": "Виведення звуку, гучність і гарячі клавіші",
            "Віртуальний мікрофон": "Маршрутизація, затримка та драйвер VB-CABLE",
            "Вигляд": "Тема, акцент і щільність бібліотеки",
            "Бібліотека": "Резервні копії аудіо та налаштувань",
            "Оновлення": "Версія програми та перевірка оновлень",
        }
        page_titles = {
            "Відтворення": "Звук",
            "Вигляд": "Інтерфейс",
        }

        def paint_navigation_button(page_key: str, *, hovered: bool = False) -> None:
            button = nav_buttons.get(page_key)
            if button is None:
                return
            active = page_key == active_page_key
            background = active_bg if active else HOVER if hovered else sidebar_bg
            button.configure(
                bg=background,
                fg=TEXT if active or hovered else MUTED,
                activebackground=HOVER,
                activeforeground=TEXT,
                font=("Segoe UI Semibold" if active else "Segoe UI", 10),
            )

        def activate_page(key: str) -> None:
            nonlocal active_page_key
            if key not in pages:
                return
            active_page_key = key
            for page_key, (page, _canvas, _inner) in pages.items():
                if page_key == key:
                    page.pack(fill="both", expand=True)
                else:
                    page.pack_forget()
            for page_key in nav_buttons:
                paint_navigation_button(page_key)

        def add_section_heading(text: str, top_padding: int = 18) -> None:
            del top_padding
            nonlocal content
            page = tk.Frame(page_host, bg=BG)
            canvas = tk.Canvas(page, bg=BG, highlightthickness=0, bd=0)
            scrollbar = ttk.Scrollbar(
                page,
                orient="vertical",
                command=canvas.yview,
                style="AudioLib.Vertical.TScrollbar",
            )
            content = tk.Frame(canvas, bg=BG)
            content_window = canvas.create_window((0, 0), window=content, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y", pady=8)
            canvas.pack(side="left", fill="both", expand=True)
            content.bind(
                "<Configure>",
                lambda _event, target=canvas: target.configure(
                    scrollregion=target.bbox("all")
                ),
            )
            canvas.bind(
                "<Configure>",
                lambda event, target=canvas, item=content_window: target.itemconfigure(
                    item, width=event.width
                ),
            )
            pages[text] = (page, canvas, content)

            page_header = tk.Frame(content, bg=BG)
            page_header.pack(fill="x", padx=24, pady=(24, 20))
            LocalizedLabel(
                page_header,
                text=page_titles.get(text, text),
                bg=BG,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 20),
            ).pack(fill="x")
            LocalizedLabel(
                page_header,
                text=page_descriptions.get(text, ""),
                bg=BG,
                fg=MUTED,
                anchor="w",
                font=("Segoe UI", 10),
            ).pack(fill="x", pady=(4, 0))

        def scroll_active_page(event: tk.Event) -> str:
            if active_page_key in pages:
                pages[active_page_key][1].yview_scroll(
                    -1 if event.delta > 0 else 1, "units"
                )
            return "break"

        window.bind("<MouseWheel>", scroll_active_page)

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
        language_keys = ["uk", "en"]
        language_labels = ["Українська", "English"]
        active_language = get_language()
        current_language = str(self.app_settings.get("language", active_language))
        if current_language not in language_keys:
            current_language = active_language
        language_value = tk.StringVar(
            value=language_labels[language_keys.index(current_language)]
        )
        output_device_value = tk.StringVar()
        local_monitoring_value = tk.BooleanVar(
            value=bool(self.app_settings["local_monitoring"])
        )
        kernel_streaming_value = tk.BooleanVar(
            value=bool(self.app_settings["kernel_streaming_cable"])
        )
        normalize_loudness_value = tk.BooleanVar(
            value=bool(self.app_settings["normalize_loudness"])
        )
        latency_value = tk.StringVar()
        accent_value = tk.StringVar(value=str(self.app_settings["accent_color"]))
        photo_size_value = tk.StringVar()
        density_value = tk.StringVar()
        library_view_value = tk.StringVar()
        theme_value = tk.StringVar()
        show_workspace_tabs_value = tk.BooleanVar(
            value=bool(self.app_settings.get("show_workspace_tabs", False))
        )
        check_updates_value = tk.BooleanVar(
            value=bool(self.app_settings["check_updates_on_startup"])
        )

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
                _showerror("Автозапуск", str(error), parent=window)
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
                    _showerror("Автозапуск", str(error), parent=window)
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
            LocalizedLabel(
                labels,
                text=title,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 10),
            ).pack(fill="x")
            LocalizedLabel(
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
        general_panel.pack(fill="x", padx=24)
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
            LocalizedLabel(
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
        LocalizedLabel(
            language_row,
            text="Мова",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x", pady=(0, 5))
        language_controls = tk.Frame(language_row, bg=PANEL)
        language_controls.pack(fill="x")

        restart_note = LocalizedLabel(
            language_row,
            text="Перезапустіть AudioLib, щоб застосувати зміни.",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 9),
        )

        def request_restart() -> None:
            self.restart_requested = True
            close_settings()
            self._close()

        restart_button = self._button(
            language_controls,
            "Перезапустити",
            request_restart,
            SECONDARY,
        )

        def update_language_restart_state(selected_language: str) -> None:
            restart_needed = selected_language != active_language
            restart_button.configure(
                state="normal" if restart_needed else "disabled"
            )
            if restart_needed:
                if not restart_note.winfo_manager():
                    restart_note.pack(fill="x", pady=(5, 0))
            elif restart_note.winfo_manager():
                restart_note.pack_forget()

        def apply_language(index: int) -> None:
            if not 0 <= index < len(language_keys):
                return
            selected_language = language_keys[index]
            save_setting("language", selected_language)
            update_language_restart_state(selected_language)

        language_dropdown = RoundedDropdown(
            language_controls,
            textvariable=language_value,
            command=apply_language,
            width=360,
            translate_values=False,
        )
        language_dropdown.configure(values=language_labels)
        language_dropdown.current(language_keys.index(current_language))
        language_dropdown.pack(side="left", fill="x", expand=True)
        restart_button.pack(side="right", padx=(8, 0))
        update_language_restart_state(current_language)

        window_size_row = tk.Frame(general_panel, bg=PANEL)
        window_size_row.pack(fill="x", pady=(10, 5))
        window_size_labels = tk.Frame(window_size_row, bg=PANEL)
        window_size_labels.pack(side="left", fill="x", expand=True)
        LocalizedLabel(
            window_size_labels,
            text="Розмір вікна",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")
        LocalizedLabel(
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
            SECONDARY,
        )
        reset_window_button.pack(side="right", padx=(12, 0))

        add_section_heading("Відтворення")
        playback_panel = tk.Frame(content, bg=PANEL, padx=14, pady=10)
        playback_panel.pack(fill="x", padx=24)

        output_row = tk.Frame(playback_panel, bg=PANEL)
        output_row.pack(fill="x", pady=(0, 9))
        LocalizedLabel(
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
                _showerror("Пристрій виведення", str(error), parent=window)
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
            "не спрямовуючи копію назад у VB-CABLE. Вимкніть під час демонстрації "
            "екрана, якщо Discord захоплює локальний звук удруге.",
            local_monitoring_value,
            apply_local_monitoring,
        )

        stop_hotkey_row = tk.Frame(playback_panel, bg=PANEL)
        stop_hotkey_row.pack(fill="x", pady=(9, 2))
        stop_hotkey_labels = tk.Frame(stop_hotkey_row, bg=PANEL)
        stop_hotkey_labels.pack(side="left", fill="x", expand=True)
        LocalizedLabel(
            stop_hotkey_labels,
            text="Гаряча клавіша Stop",
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")
        LocalizedLabel(
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
            SECONDARY,
        )
        stop_hotkey_button.pack(side="right", padx=(12, 0))

        add_section_heading("Віртуальний мікрофон")

        header = tk.Frame(content, bg=BG)
        header.pack(fill="x", padx=24, pady=(0, 4))
        LocalizedLabel(
            header, text="VB-CABLE", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 20),
        ).pack(side="left")
        self._button(
            header,
            "Офіційна сторінка VB-Audio",
            lambda: webbrowser.open(DRIVER_PAGE),
            SECONDARY,
        ).pack(side="right")
        LocalizedLabel(
            content,
            text=(
                "AudioLib використовує офіційний підписаний драйвер VB-Audio. "
                "Встановлення не виконується автоматично: Windows покаже запит адміністратора, "
                "а потім вікно інсталятора. Після встановлення або видалення перезавантажте ПК."
            ),
            bg=BG, fg=MUTED, justify="left", wraplength=570,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=24, pady=(0, 12))
        status_label = LocalizedLabel(
            content, textvariable=status, bg=PANEL, fg=TEXT, anchor="w",
            padx=12, pady=10, font=("Segoe UI Semibold", 10),
        )
        status_label.pack(fill="x", padx=24, pady=6)

        microphone_choices: list[CaptureEndpoint] = []
        microphone_value = tk.StringVar()
        microphone_row = tk.Frame(content, bg=BG)
        microphone_row.pack(fill="x", padx=24, pady=(2, 6))
        LocalizedLabel(
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
                _showwarning(
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
        latency_row.pack(fill="x", padx=24, pady=(4, 6))
        LocalizedLabel(
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

        kernel_streaming_panel = tk.Frame(content, bg=PANEL, padx=14, pady=5)
        kernel_streaming_panel.pack(fill="x", padx=24, pady=(5, 2))

        def apply_kernel_streaming() -> None:
            enabled = kernel_streaming_value.get()
            save_setting("kernel_streaming_cable", enabled)
            bridge_was_running = self.virtual_microphone.is_running()
            self.virtual_microphone.configure(
                latency_profile=str(self.app_settings["latency_profile"]),
                local_monitoring=bool(self.app_settings["local_monitoring"]),
                monitor_output_name=str(self.app_settings["output_device"]),
                kernel_streaming_cable=enabled,
                mix_microphone=enabled,
            )
            if bridge_was_running:
                self._begin_virtual_microphone_start(reset_existing=True)
            self.status_text.set(
                "Експериментальний Kernel Streaming увімкнено"
                if enabled
                else "Сумісний режим VB-CABLE увімкнено"
            )

        switch_row(
            kernel_streaming_panel,
            "Захист демонстрації (експериментально)",
            "Передає звук безпосередньо у WDM/KS-пін VB-CABLE, минаючи "
            "системний аудіомікшер Windows. Це має виключити мікрофонний мікс "
            "із захоплення системного звуку. Локальне прослуховування можна "
            "ввімкнути окремо в розділі відтворення.",
            kernel_streaming_value,
            apply_kernel_streaming,
        )
        LocalizedLabel(
            content,
            text=(
                "Увага: після встановлення Windows може автоматично вибрати CABLE Input "
                "як основний динамік. Якщо звук у колонках або навушниках зникне, відкрийте "
                "Параметри → Система → Звук і поверніть свій основний пристрій, наприклад "
                "Динаміки (Realtek Audio)."
            ),
            bg=WARNING_BG, fg=WARNING_TEXT, justify="left", wraplength=548,
            padx=11, pady=9, font=("Segoe UI Semibold", 9),
        ).pack(fill="x", padx=24, pady=(6, 4))

        def refresh_status() -> None:
            installed, text = self.virtual_microphone.device_status()
            status.set(("✓ " if installed else "• ") + text)
            status_label.configure(fg=SUCCESS_TEXT if installed else CAUTION_TEXT)
            load_microphones()

        def prepare_and_launch(removing: bool) -> None:
            status.set("Завантаження та перевірка офіційного пакета…")

            def worker() -> None:
                try:
                    setup = prepare_official_installer()
                    self.root.after(0, lambda: finish(setup, removing))
                except Exception as error:
                    self.root.after(0, lambda: _showerror(
                        "Помилка VB-CABLE", str(error), parent=window
                    ))
                    self.root.after(0, refresh_status)

            threading.Thread(target=worker, daemon=True).start()

        def finish(setup: Path, removing: bool) -> None:
            action = "Remove Driver" if removing else "Install Driver"
            _showinfo(
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
                _showerror("Помилка VB-CABLE", str(error), parent=window)
                refresh_status()

        buttons = tk.Frame(content, bg=BG)
        buttons.pack(fill="x", padx=24, pady=12)
        for column in range(3):
            buttons.columnconfigure(column, weight=1, uniform="virtual-mic-actions")
        self._button(
            buttons, "Встановити", lambda: prepare_and_launch(False), ACCENT
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._button(
            buttons, "Видалити / відкотити", lambda: prepare_and_launch(True), DANGER
        ).grid(row=0, column=1, sticky="ew", padx=4)
        self._button(
            buttons, "Перевірити", refresh_status, SECONDARY
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))
        LocalizedLabel(
            content,
            text=(
                "Після встановлення оберіть CABLE Output як мікрофон у Discord/грі. "
                "AudioLib змішуватиме звук з вашим фізичним мікрофоном."
            ),
            bg=BG, fg=MUTED, justify="left", wraplength=570, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=24)

        add_section_heading("Бібліотека")
        library_panel = tk.Frame(content, bg=PANEL, padx=14, pady=12)
        library_panel.pack(fill="x", padx=24)
        LocalizedLabel(
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
        library_status_value = LocalizedStringVar()
        library_progress_row = tk.Frame(library_panel, bg=PANEL)
        LocalizedLabel(
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
                selected = choose_folder(
                    window,
                    title="Оберіть папку для резервної копії AudioLib",
                )
                if not selected:
                    return
                self.store.connection.commit()
            except (FileDialogError, OSError, tk.TclError) as error:
                _showerror("Резервна копія", str(error), parent=window)
                return
            destination = Path(selected) / f"AudioLib_backup_{stamp}.zip"
            set_library_busy(True, "Створення резервної копії…")
            self.status_text.set("Створення резервної копії…")
            progress_dialog = OperationProgressDialog(
                self,
                window,
                "Резервна копія AudioLib",
                "Підготовка резервної копії…",
            )
            progress_dialog.set_indeterminate("Підготовка резервної копії…")
            backup_results: queue.Queue[
                tuple[Path | None, Exception | None]
            ] = queue.Queue(maxsize=1)
            backup_progress: queue.Queue[tuple[str, int, int]] = queue.Queue()

            def report_progress(stage: str, completed: int, total: int) -> None:
                backup_progress.put((stage, completed, total))

            def refresh_backup_progress() -> None:
                latest = None
                while True:
                    try:
                        latest = backup_progress.get_nowait()
                    except queue.Empty:
                        break
                if latest is None:
                    return
                stage, completed, total = latest
                if stage == "preparing":
                    progress_dialog.set_indeterminate("Підготовка резервної копії…")
                elif stage == "copying":
                    progress_dialog.set_progress(
                        "Копіювання даних бібліотеки…",
                        completed,
                        total,
                    )
                else:
                    progress_dialog.set_indeterminate(
                        "Завершення резервної копії…",
                        "Перевірка та збереження архіву",
                    )

            def worker() -> None:
                try:
                    result = create_backup(
                        self.root_dir,
                        destination,
                        report_progress,
                    )
                    backup_results.put((result, None))
                except Exception as error:
                    backup_results.put((None, error))

            def poll_backup_result() -> None:
                refresh_backup_progress()
                try:
                    result, error = backup_results.get_nowait()
                except queue.Empty:
                    if window.winfo_exists():
                        self.root.after(25, poll_backup_result)
                    return
                backup_finished(result, error)

            def backup_finished(result: Path | None, error: Exception | None) -> None:
                refresh_backup_progress()
                progress_dialog.close()
                set_library_busy(False)
                if error is not None:
                    self.status_text.set("Не вдалося створити резервну копію")
                    _showerror("Резервна копія", str(error), parent=window)
                    return
                assert result is not None
                self.status_text.set(f"Створено резервну копію: {result.name}")
                _showinfo(
                    "Резервна копія створена",
                    f"Файл збережено тут:\n{result}",
                    parent=window,
                )

            threading.Thread(target=worker, daemon=True).start()
            self.root.after(25, poll_backup_result)

        def import_library_backup() -> None:
            try:
                selected = choose_files(
                    window,
                    title="Оберіть резервну копію AudioLib",
                    filetypes=[("Резервна копія AudioLib", "*.zip")],
                )
            except FileDialogError as error:
                _showerror("Імпорт", str(error), parent=window)
                return
            if not selected:
                return
            if not _askyesno(
                "Відновити бібліотеку",
                "Поточну бібліотеку буде замінено даними з резервної копії. Продовжити?",
                icon="warning",
                parent=window,
            ):
                return
            self._clear_player()
            self.stop_virtual_microphone_sound()
            self.hotkey_manager.close()
            if self._volume_save_job is not None:
                try:
                    self.root.after_cancel(self._volume_save_job)
                except tk.TclError:
                    pass
                self._volume_save_job = None
            self.store.connection.commit()
            self.store.close()
            set_library_busy(True, "Імпорт і перевірка резервної копії…")
            self.status_text.set("Імпорт резервної копії…")
            progress_dialog = OperationProgressDialog(
                self,
                window,
                "Імпорт резервної копії",
                "Підготовка резервної копії…",
            )
            progress_dialog.set_indeterminate("Підготовка резервної копії…")
            import_results: queue.Queue[Exception | None] = queue.Queue(maxsize=1)
            import_progress: queue.Queue[tuple[str, int, int]] = queue.Queue()

            def report_progress(stage: str, completed: int, total: int) -> None:
                import_progress.put((stage, completed, total))

            def refresh_import_progress() -> None:
                latest = None
                while True:
                    try:
                        latest = import_progress.get_nowait()
                    except queue.Empty:
                        break
                if latest is None:
                    return
                stage, completed, total = latest
                if stage == "preparing":
                    progress_dialog.set_indeterminate("Підготовка резервної копії…")
                elif stage == "copying":
                    progress_dialog.set_progress(
                        "Копіювання даних бібліотеки…",
                        completed,
                        total,
                    )
                elif stage == "verifying":
                    progress_dialog.set_indeterminate(
                        "Перевірка бази даних…",
                        "Перевірка цілісності резервної копії",
                    )
                elif stage == "applying":
                    progress_dialog.set_indeterminate(
                        "Застосування резервної копії…",
                        "Оновлення бібліотеки AudioLib",
                    )
                else:
                    progress_dialog.set_progress(
                        "Завершення імпорту…",
                        max(completed, total),
                        max(1, total),
                    )

            def worker() -> None:
                try:
                    restore_backup(
                        self.root_dir,
                        Path(selected[0]),
                        report_progress,
                    )
                    import_results.put(None)
                except Exception as error:
                    import_results.put(error)

            def poll_import_result() -> None:
                refresh_import_progress()
                try:
                    error = import_results.get_nowait()
                except queue.Empty:
                    if window.winfo_exists():
                        self.root.after(25, poll_import_result)
                    return
                import_finished(error)

            def import_finished(error: Exception | None) -> None:
                refresh_import_progress()
                progress_dialog.close()
                self.store = LibraryStore(self.root_dir)
                set_library_busy(False)
                if error is not None:
                    self._reload_global_hotkeys()
                    self.status_text.set("Не вдалося імпортувати резервну копію")
                    _showerror("Імпорт", str(error), parent=window)
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
                    kernel_streaming_cable=bool(
                        self.app_settings["kernel_streaming_cable"]
                    ),
                    mix_microphone=bool(
                        self.app_settings["kernel_streaming_cable"]
                    ),
                )
                # The database and image files were replaced underneath the
                # current widgets.  Drop every view/cache tied to the old
                # library before rebuilding the visible lists.
                self._photo_cache.clear()
                self.selected_person_id = None
                if self.person_query.get():
                    self.person_query.set("")
                if self.track_query.get():
                    self.track_query.set("")
                self.refresh_people()
                self.select_person(FAVORITES_PERSON_ID)
                self.people_canvas.yview_moveto(0.0)
                self.track_table.yview("moveto", 0.0)
                self._refresh_tray_favorites()
                self.status_text.set("Бібліотеку відновлено з резервної копії")
                close_settings()
                _showinfo(
                    "Імпорт завершено",
                    "Бібліотеку відновлено з резервної копії",
                    parent=self.root,
                )

            threading.Thread(target=worker, daemon=True).start()
            self.root.after(25, poll_import_result)

        backup_button = self._button(
            library_actions, "Створити копію", make_library_backup, ACCENT
        )
        backup_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        import_button = self._button(
            library_actions, "Імпортувати", import_library_backup, SECONDARY
        )
        import_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        add_section_heading("Вигляд")
        appearance_panel = tk.Frame(content, bg=PANEL, padx=14, pady=12)
        appearance_panel.pack(fill="x", padx=24)
        for column in range(3):
            appearance_panel.columnconfigure(column, weight=1, uniform="appearance")

        def appearance_choice(
            row: int,
            column: int,
            title: str,
            variable: tk.StringVar,
            values: list[str],
            callback,
        ) -> RoundedDropdown:
            frame = tk.Frame(appearance_panel, bg=PANEL)
            frame.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0 if column == 0 else 5, 0),
                pady=(0 if row == 0 else 10, 0),
            )
            LocalizedLabel(
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
            selected_accent = accent_value.get()
            previous_accent = str(self.app_settings.get("accent_color", "Янтарний"))
            if selected_accent == "Власний":
                _rgb, selected_color = colorchooser.askcolor(
                    color=_valid_hex_color(
                        self.app_settings.get("custom_accent_color", "#a34f0d")
                    ),
                    title=tr("Власний колір акценту"),
                    parent=window,
                )
                if not selected_color:
                    accent_value.set(previous_accent)
                    return
                self.app_settings["custom_accent_color"] = _valid_hex_color(
                    selected_color
                )
            save_setting("accent_color", selected_accent)
            self._apply_saved_accent()
            style = ttk.Style(self.root)
            style.map(
                "Treeview",
                background=[("selected", ACCENT)],
                foreground=[("selected", _contrast_text_color(ACCENT))],
            )
            style.configure(
                "Settings.Horizontal.TProgressbar",
                background=ACCENT,
                lightcolor=ACCENT,
                darkcolor=ACCENT,
            )
            active_buttons: list[tk.Widget] = []
            for button in self._accent_buttons:
                try:
                    if not button.winfo_exists():
                        continue
                    if isinstance(button, RoundedButton):
                        button.configure(color=ACCENT, hover_color=ACCENT_HOVER)
                    else:
                        button.configure(
                            bg=ACCENT,
                            activebackground=ACCENT_HOVER,
                        )
                    active_buttons.append(button)
                except tk.TclError:
                    continue
            self._accent_buttons = active_buttons
            self.scrollbar_images["normal"].paste(
                render_scrollbar_thumb_image(BORDER)
            )
            self.scrollbar_images["active"].paste(
                render_scrollbar_thumb_image(ACCENT)
            )
            self.scrollbar_images["pressed"].paste(
                render_scrollbar_thumb_image(ACCENT_HOVER)
            )
            self.scrollbar_images["horizontal_normal"].paste(
                render_scrollbar_thumb_image(BORDER, horizontal=True)
            )
            self.scrollbar_images["horizontal_active"].paste(
                render_scrollbar_thumb_image(ACCENT, horizontal=True)
            )
            self.scrollbar_images["horizontal_pressed"].paste(
                render_scrollbar_thumb_image(ACCENT_HOVER, horizontal=True)
            )
            self.progress.fill_color = ACCENT
            self.progress.active_color = ACCENT_HOVER
            self.progress._draw()
            self.volume_scale.active_color = ACCENT
            self.volume_scale._draw()
            self.refresh_people()
            self.track_table.refresh_style()
            self.status_text.set("Колір акценту застосовано")

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

        view_labels = {
            "Таблиця": "table",
            "Плитки": "tiles",
        }
        library_view_value.set(
            next(
                label for label, value in view_labels.items()
                if value == str(self.app_settings["library_view"])
            )
        )

        def apply_library_view() -> None:
            mode = view_labels[library_view_value.get()]
            save_setting("library_view", mode)
            self.track_table.set_view_mode(mode)
            self.track_table.configure(displaycolumns=self._visible_track_columns())
            self.refresh_tracks()

        theme_labels = {"Темна": "dark", "Світла": "light"}
        active_theme = str(self.app_settings["theme"])
        theme_value.set(
            next(label for label, value in theme_labels.items() if value == active_theme)
        )

        def apply_theme() -> None:
            selected = theme_labels[theme_value.get()]
            save_setting("theme", selected)
            theme_restart_button.configure(
                state="normal" if selected != active_theme else "disabled"
            )
            theme_restart_note.configure(
                text=(
                    "Перезапустіть AudioLib, щоб застосувати зміни."
                    if selected != active_theme
                    else ""
                )
            )

        appearance_choice(
            0,
            0,
            "Колір акценту",
            accent_value,
            [*ACCENT_PRESETS, "Власний"],
            apply_accent,
        )
        appearance_choice(0, 1, "Розмір зображень", photo_size_value, list(photo_labels), apply_photo_size)
        appearance_choice(0, 2, "Щільність списків", density_value, list(density_labels), apply_density)
        appearance_choice(1, 0, "Відображення бібліотеки", library_view_value, list(view_labels), apply_library_view)
        appearance_choice(1, 1, "Тема", theme_value, list(theme_labels), apply_theme)
        theme_restart_frame = tk.Frame(appearance_panel, bg=PANEL)
        theme_restart_frame.grid(row=1, column=2, sticky="sew", padx=(5, 0), pady=(10, 0))
        theme_restart_note = LocalizedLabel(
            theme_restart_frame,
            text="",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            font=("Segoe UI", 8),
        )
        theme_restart_note.pack(fill="x", pady=(0, 4))
        theme_restart_button = self._button(
            theme_restart_frame,
            "Перезапустити",
            request_restart,
            SECONDARY,
        )
        theme_restart_button.pack(fill="x")
        theme_restart_button.configure(state="disabled")

        tabs_visibility_panel = tk.Frame(content, bg=PANEL, padx=14, pady=8)
        tabs_visibility_panel.pack(fill="x", padx=24, pady=(12, 0))

        def apply_workspace_tabs_visibility() -> None:
            save_setting("show_workspace_tabs", show_workspace_tabs_value.get())
            self._refresh_workspace_tabs()

        switch_row(
            tabs_visibility_panel,
            "Показувати вкладки категорій",
            "Відображає відкриті категорії над бібліотекою.",
            show_workspace_tabs_value,
            apply_workspace_tabs_visibility,
        )

        add_section_heading("Оновлення")
        update_panel = tk.Frame(content, bg=PANEL, padx=14, pady=10)
        update_panel.pack(fill="x", padx=24)
        switch_row(
            update_panel,
            "Перевіряти оновлення під час запуску",
            "Перевірка виконується у фоні й не затримує запуск програми.",
            check_updates_value,
            lambda: save_setting(
                "check_updates_on_startup", check_updates_value.get()
            ),
        )
        update_action_row = tk.Frame(update_panel, bg=PANEL)
        update_action_row.pack(fill="x", pady=(8, 0))
        update_labels = tk.Frame(update_action_row, bg=PANEL)
        update_labels.pack(side="left", fill="x", expand=True)
        LocalizedLabel(
            update_labels,
            text=tr("Поточна версія: {version}").format(version=__version__),
            bg=PANEL,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 10),
        ).pack(fill="x")
        update_status = LocalizedStringVar(value="Оновлення ще не перевірялися.")
        LocalizedLabel(
            update_labels,
            textvariable=update_status,
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=355,
            font=("Segoe UI", 9),
        ).pack(fill="x", pady=(2, 0))

        def update_button_action() -> None:
            if self._update_state == "available" and self.available_update is not None:
                self._install_available_update(window)
            else:
                self._check_for_updates(manual=True)

        update_button = self._button(
            update_action_row,
            "Перевірити оновлення",
            update_button_action,
            SECONDARY,
        )
        update_button.pack(side="right", padx=(12, 0))

        def refresh_update_controls() -> None:
            state = self._update_state
            release = self.available_update
            if state == "checking":
                update_status.set("Перевірка оновлень…")
                update_button.configure(text="Перевірка…", state="disabled")
            elif state == "downloading":
                update_status.set(
                    tr("Завантаження оновлення: {percent}%").format(
                        percent=self._update_progress_percent
                    )
                )
                update_button.configure(text="Завантаження…", state="disabled")
            elif state == "installing":
                update_status.set("Підготовка до перезапуску…")
                update_button.configure(text="Встановлення…", state="disabled")
            elif state == "available" and release is not None:
                update_status.set(
                    tr("Доступна нова версія: {version}").format(
                        version=release.version
                    )
                )
                update_button.configure(
                    text=tr("Оновити до {version}").format(version=release.version),
                    state="normal",
                )
            elif state == "current":
                update_status.set("Установлено найновішу версію AudioLib.")
                update_button.configure(text="Перевірити оновлення", state="normal")
            elif state == "error":
                update_status.set(self._update_error or "Не вдалося перевірити оновлення.")
                update_button.configure(text="Повторити", state="normal")
            else:
                update_status.set("Оновлення ще не перевірялися.")
                update_button.configure(text="Перевірити оновлення", state="normal")

        self._update_settings_refresh = refresh_update_controls
        refresh_update_controls()

        about_header = tk.Frame(content, bg=BG)
        about_header.pack(fill="x", padx=24, pady=(24, 8))
        LocalizedLabel(
            about_header,
            text="Про програму",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI Semibold", 11),
        ).pack(fill="x")

        about_panel = tk.Frame(content, bg=PANEL, padx=14, pady=10)
        about_panel.pack(fill="x", padx=24, pady=(0, 24))
        about_panel.columnconfigure(1, weight=1)
        about_rows = (
            ("Назва", "AudioLib"),
            ("Версія", __version__),
            ("Розробник", "MULTIGID + ChatGPT"),
            ("Опис", "Локальна бібліотека аудіозаписів"),
        )
        for row, (label, value) in enumerate(about_rows):
            LocalizedLabel(
                about_panel,
                text=f"{label}:",
                bg=PANEL,
                fg=MUTED,
                anchor="w",
                font=("Segoe UI", 10),
            ).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=2)
            LocalizedLabel(
                about_panel,
                text=value,
                bg=PANEL,
                fg=TEXT,
                anchor="w",
                font=("Segoe UI Semibold", 10),
            ).grid(row=row, column=1, sticky="w", pady=2)
        navigation_items = (
            ("Загальні", "Загальні"),
            ("Відтворення", "Звук"),
            ("Віртуальний мікрофон", "Віртуальний мікрофон"),
            ("Вигляд", "Інтерфейс"),
            ("Бібліотека", "Бібліотека"),
            ("Оновлення", "Оновлення"),
        )
        for page_key, label in navigation_items:
            button = tk.Button(
                navigation,
                text=tr(label),
                command=lambda key=page_key: activate_page(key),
                bg=sidebar_bg,
                fg=MUTED,
                activebackground=BORDER,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                highlightthickness=0,
                anchor="w",
                padx=12,
                pady=8,
                cursor="hand2",
                font=("Segoe UI", 10),
            )
            button.pack(fill="x", padx=12, pady=2)
            nav_buttons[page_key] = button
            button.bind(
                "<Enter>",
                lambda _event, key=page_key: paint_navigation_button(
                    key, hovered=True
                ),
            )
            button.bind(
                "<Leave>",
                lambda _event, key=page_key: paint_navigation_button(key),
            )

        LocalizedLabel(
            navigation,
            text="Зміни зберігаються автоматично",
            bg=sidebar_bg,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=176,
            font=("Segoe UI", 8),
        ).pack(side="bottom", fill="x", padx=20, pady=20)

        activate_page("Загальні")
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
            _showerror("Помилка", str(error), parent=self.root)

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
                _showerror("Помилка", str(error), parent=self.root)

    def delete_track(self) -> None:
        track = self._selected_track()
        if not track:
            return
        if not _askyesno("Видалити запис", f"Видалити «{track.title}»?", parent=self.root):
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
            self.microphone_relay.stop()
        except Exception as error:
            error_text += f" relay_error={error}"
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
