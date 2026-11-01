from __future__ import annotations

import tkinter as tk
from pathlib import Path


BG = "#111827"
PANEL = "#1f2937"
TEXT = "#f8fafc"
MUTED = "#9ca3af"
ACCENT = "#8b5cf6"


class StartupSplash:
    """Small, dependency-free splash shown while the main window is built."""

    def __init__(
        self,
        root_dir: Path,
        language: str = "uk",
        master: tk.Misc | None = None,
    ) -> None:
        self._language = "en" if language == "en" else "uk"
        self._closed = False
        self.window = tk.Toplevel(master) if master is not None else tk.Tk()
        self.window.overrideredirect(True)
        self.window.configure(bg="#334155")
        self.window.attributes("-topmost", True)

        width, height = 430, 250
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

        body = tk.Frame(self.window, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        self._logo = None
        logo_path = root_dir / "assets" / "audiolib_icon.png"
        try:
            source = tk.PhotoImage(file=str(logo_path))
            factor = max(1, round(max(source.width(), source.height()) / 88))
            self._logo = source.subsample(factor, factor)
            tk.Label(body, image=self._logo, bg=BG, borderwidth=0).pack(pady=(24, 8))
        except tk.TclError:
            tk.Label(
                body,
                text="♪",
                font=("Segoe UI Symbol", 42),
                fg=ACCENT,
                bg=BG,
            ).pack(pady=(20, 4))

        tk.Label(
            body,
            text="AudioLib",
            font=("Segoe UI Semibold", 20),
            fg=TEXT,
            bg=BG,
        ).pack()
        self.status = tk.Label(
            body,
            text=self._text("Запуск програми…", "Starting application…"),
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG,
        )
        self.status.pack(pady=(4, 14))

        self.progress = tk.Canvas(
            body,
            width=350,
            height=8,
            bg=PANEL,
            highlightthickness=0,
            borderwidth=0,
        )
        self.progress.pack()
        self._progress_fill = self.progress.create_rectangle(
            0, 0, 0, 8, fill=ACCENT, outline=""
        )
        self.percent = tk.Label(
            body,
            text="0%",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg=BG,
        )
        self.percent.pack(pady=(5, 0))
        self.window.update_idletasks()
        self.window.update()

    def _text(self, uk: str, en: str) -> str:
        return en if self._language == "en" else uk

    def update(self, value: int, uk_status: str, en_status: str) -> None:
        if self._closed:
            return
        value = max(0, min(100, int(value)))
        try:
            self.progress.coords(self._progress_fill, 0, 0, 3.5 * value, 8)
            self.status.configure(text=self._text(uk_status, en_status))
            self.percent.configure(text=f"{value}%")
            self.window.update_idletasks()
            self.window.update()
        except tk.TclError:
            self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.destroy()
        except tk.TclError:
            pass
