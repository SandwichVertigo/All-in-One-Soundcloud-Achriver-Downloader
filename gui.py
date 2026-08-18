import io
import os
import queue
import threading
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import *
from PIL import Image, ImageTk

from backend import SoundCloudBackend, SoundCloudError, make_secure_temp_dir
import settings as settings_module

REPO_URL = "https://github.com/SandwichVertigo/"

DISCLAIMER_TEXT = (
    "This tool lets you download audio from links you provide.\n\n"
    "This is an independent, third-party project. It is not affiliated "
    "with, endorsed by, sponsored by, or officially connected to SoundCloud "
    "Limited or any of its affiliates in any way.\n\n"
    "Use it only for content you own, content that is explicitly marked as "
    "downloadable by the creator, or content licensed for personal use "
    "(such as Creative Commons tracks).\n\n"
    "Downloading copyrighted material without permission may violate "
    "SoundCloud's Terms of Service and copyright law in your country.\n\n"
    "You are solely responsible for how you use this application. "
    "The developer provides this software as-is, with no warranty, and "
    "accepts no liability for misuse or any consequences arising from it."
)

STATUS_COLORS = {
    "downloading": "info",
    "downloaded": "success",
    "failed": "danger",
    "skipped": "secondary",
}


def _make_wheel_handler(canvas):
    def _on_wheel(event):
        if getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")
        else:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    return _on_wheel


def bind_mousewheel_tree(widget, canvas):
    handler = _make_wheel_handler(canvas)

    def _apply(w):
        w.bind("<MouseWheel>", handler, add="+")
        w.bind("<Button-4>", handler, add="+")
        w.bind("<Button-5>", handler, add="+")
        for child in w.winfo_children():
            _apply(child)

    _apply(widget)


class CheckRow(tb.Frame):
    THUMB_SIZE = 40

    def __init__(self, master, item, **kwargs):
        super().__init__(master, **kwargs)
        self.item = item
        self.var = tb.BooleanVar(value=True)
        self._photo = None

        check = tb.Checkbutton(self, variable=self.var)
        check.grid(row=0, column=0, rowspan=2, padx=(6, 8), pady=6)

        self.thumb_lbl = tb.Label(self, text="\u266a", font=("Segoe UI", 14), width=4, anchor="center")
        self.thumb_lbl.grid(row=0, column=1, rowspan=2, padx=(0, 10), pady=6)

        title_lbl = tb.Label(self, text=item.title, font=("Segoe UI", 10, "bold"))
        title_lbl.grid(row=0, column=2, sticky="w")

        artist_lbl = tb.Label(self, text=item.artist, font=("Segoe UI", 9), bootstyle="secondary")
        artist_lbl.grid(row=1, column=2, sticky="w")

        dur_lbl = tb.Label(self, text=item.duration_str, font=("Segoe UI", 9), bootstyle="secondary")
        dur_lbl.grid(row=0, column=3, padx=10, sticky="e")

        self.status_lbl = tb.Label(self, text="", font=("Segoe UI", 8, "bold"))
        self.status_lbl.grid(row=1, column=3, padx=10, sticky="e")

        self.columnconfigure(2, weight=1)

    def is_selected(self):
        return self.var.get()

    def set_status(self, text, style=None):
        self.status_lbl.configure(text=text)
        if style:
            self.status_lbl.configure(bootstyle=style)

    def set_thumbnail(self, photo):
        self._photo = photo
        self.thumb_lbl.configure(image=photo, text="")


class ResultsPanel(tb.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.rows = []
        self.rows_by_item = {}

        self.canvas = tb.Canvas(self, highlightthickness=0)
        scrollbar = tb.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.list_container = tb.Frame(self.canvas)

        self.list_container.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.list_container, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        scrollbar.pack(side=RIGHT, fill=Y)
        bind_mousewheel_tree(self.canvas, self.canvas)
        bind_mousewheel_tree(self.list_container, self.canvas)

        self.empty_label = tb.Label(
            self.list_container, text="Results will show up here.", font=("Segoe UI", 9), bootstyle="secondary"
        )
        self.empty_label.pack(padx=10, pady=20)

    def clear(self):
        for widget in self.list_container.winfo_children():
            widget.destroy()
        self.rows = []
        self.rows_by_item = {}

    def populate(self, items):
        self.clear()
        for item in items:
            row = CheckRow(self.list_container, item, padding=(4, 2))
            row.pack(fill=X, pady=2)
            self.rows.append(row)
            self.rows_by_item[id(item)] = row
        bind_mousewheel_tree(self.list_container, self.canvas)
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)

    def selected_items(self):
        return [row.item for row in self.rows if row.is_selected()]

    def set_all(self, value):
        for row in self.rows:
            row.var.set(value)

    def row_for(self, item):
        return self.rows_by_item.get(id(item))


class SoundCloudGrabberApp(tb.Window):
    def __init__(self):
        self.settings = settings_module.load_settings()
        super().__init__(themename=self.settings.get("theme", "flatly"))

        self.title("All in One SoundCloud Archiver")
        geometry = self.settings.get("window_geometry") or "780x640"
        self.geometry(geometry)
        self.minsize(680, 500)

        self.backend = SoundCloudBackend(
            manual_client_id=self.settings.get("manual_client_id"),
            cached_client_id=self.settings.get("cached_client_id"),
            on_client_id_learned=self._on_client_id_learned,
            proxy_url=self.settings.get("proxy_url"),
            randomize_user_agent=self.settings.get("randomize_user_agent", False),
            request_delay_enabled=self.settings.get("request_delay_enabled", False),
        )
        self.result_queue = queue.Queue()
        self.cancel_event = None
        self.last_failed_items = []
        self.last_batch_folder = None
        self.last_batch_zip = None
        self._last_clipboard = ""
        self._progress_running = False
        self._minimized = False

        self._build_layout()
        self.after(200, self._poll_queue)
        self.after(500, self._check_clipboard)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)

        if not self.settings.get("disclaimer_accepted"):
            self.after(300, self._show_disclaimer)

    def _on_client_id_learned(self, client_id):
        self.settings["cached_client_id"] = client_id
        settings_module.save_settings(self.settings)

    def _on_close(self):
        self.settings["window_geometry"] = self.geometry()
        if self.settings.get("clear_recent_on_exit"):
            self.settings["recent_links"] = []
        settings_module.save_settings(self.settings)
        self.destroy()

    def _build_layout(self):
        self.notebook = tb.Notebook(self)
        self.notebook.pack(fill=BOTH, expand=YES, padx=12, pady=12)

        self.download_tab = tb.Frame(self.notebook)
        self.search_tab = tb.Frame(self.notebook)
        self.settings_tab = tb.Frame(self.notebook)
        self.about_tab = tb.Frame(self.notebook)

        self.notebook.add(self.download_tab, text="  Download  ")
        self.notebook.add(self.search_tab, text="  Search  ")
        self.notebook.add(self.settings_tab, text="  Settings  ")
        self.notebook.add(self.about_tab, text="  About  ")

        self._build_download_tab()
        self._build_search_tab()
        self._build_settings_tab()
        self._build_about_tab()

    def _build_download_tab(self):
        top = tb.Frame(self.download_tab)
        top.pack(fill=X, pady=(4, 10))

        tb.Label(top, text="SoundCloud link", font=("Segoe UI", 10)).pack(anchor="w")

        entry_row = tb.Frame(top)
        entry_row.pack(fill=X, pady=(4, 0))

        self.url_entry = tb.Entry(entry_row, font=("Segoe UI", 10))
        self.url_entry.pack(side=LEFT, fill=X, expand=YES)
        self.url_entry.bind("<Return>", lambda e: self._on_fetch())

        fetch_btn = tb.Button(entry_row, text="Fetch", bootstyle="primary", command=self._on_fetch)
        fetch_btn.pack(side=LEFT, padx=(8, 0))

        recent_row = tb.Frame(top)
        recent_row.pack(fill=X, pady=(6, 0))

        tb.Label(recent_row, text="Recent:", font=("Segoe UI", 8), bootstyle="secondary").pack(side=LEFT)

        self.recent_var = tb.StringVar()
        self.recent_combo = tb.Combobox(
            recent_row, textvariable=self.recent_var, values=self.settings.get("recent_links", []),
            font=("Segoe UI", 8), state="readonly"
        )
        self.recent_combo.pack(side=LEFT, padx=(6, 0), fill=X, expand=YES)
        self.recent_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)

        self.clipboard_hint = tb.Label(
            top, text="", font=("Segoe UI", 8), bootstyle="info", cursor="hand2"
        )
        self.clipboard_hint.pack(anchor="w", pady=(4, 0))
        self.clipboard_hint.bind("<Button-1>", self._use_clipboard_link)

        hint = tb.Label(
            top,
            text="Paste a track, playlist, or profile link. A profile link fetches all of that account's tracks.",
            font=("Segoe UI", 8),
            bootstyle="secondary",
        )
        hint.pack(anchor="w", pady=(4, 0))

        list_frame = tb.Labelframe(self.download_tab, text="Results")
        list_frame.pack(fill=BOTH, expand=YES, pady=(0, 10))
        self.download_panel = ResultsPanel(list_frame)
        self.download_panel.pack(fill=BOTH, expand=YES)

        select_row = tb.Frame(self.download_tab)
        select_row.pack(fill=X, pady=(0, 8))

        tb.Button(
            select_row, text="Select all", bootstyle="link", command=lambda: self.download_panel.set_all(True)
        ).pack(side=LEFT)
        tb.Button(
            select_row, text="Select none", bootstyle="link", command=lambda: self.download_panel.set_all(False)
        ).pack(side=LEFT, padx=(10, 0))

        action_row = tb.Frame(self.download_tab)
        action_row.pack(fill=X)

        self.download_folder_btn = tb.Button(
            action_row, text="Download selected", bootstyle="success",
            command=lambda: self._start_download(self.download_panel)
        )
        self.download_folder_btn.pack(side=LEFT)

        self.download_zip_btn = tb.Button(
            action_row, text="Download selected as ZIP", bootstyle="info",
            command=lambda: self._start_download(self.download_panel, as_zip=True)
        )
        self.download_zip_btn.pack(side=LEFT, padx=(8, 0))

        self.cancel_btn = tb.Button(action_row, text="Cancel", bootstyle="danger-outline", command=self._on_cancel)
        self.retry_btn = tb.Button(action_row, text="Retry failed", bootstyle="warning", command=self._on_retry_failed)

        self.status_label = tb.Label(self.download_tab, text="", font=("Segoe UI", 9), bootstyle="secondary")
        self.status_label.pack(anchor="w", pady=(8, 0))

        self.progress = tb.Progressbar(self.download_tab, mode="determinate")
        self.progress.pack(fill=X, pady=(4, 0))

    def _build_search_tab(self):
        top = tb.Frame(self.search_tab)
        top.pack(fill=X, pady=(4, 10))

        tb.Label(top, text="Search SoundCloud", font=("Segoe UI", 10)).pack(anchor="w")

        entry_row = tb.Frame(top)
        entry_row.pack(fill=X, pady=(4, 0))

        self.search_entry = tb.Entry(entry_row, font=("Segoe UI", 10))
        self.search_entry.pack(side=LEFT, fill=X, expand=YES)
        self.search_entry.bind("<Return>", lambda e: self._on_search())

        tb.Button(entry_row, text="Search", bootstyle="primary", command=self._on_search).pack(
            side=LEFT, padx=(8, 0)
        )

        list_frame = tb.Labelframe(self.search_tab, text="Results")
        list_frame.pack(fill=BOTH, expand=YES, pady=(0, 10))
        self.search_panel = ResultsPanel(list_frame)
        self.search_panel.pack(fill=BOTH, expand=YES)

        select_row = tb.Frame(self.search_tab)
        select_row.pack(fill=X, pady=(0, 8))

        tb.Button(
            select_row, text="Select all", bootstyle="link", command=lambda: self.search_panel.set_all(True)
        ).pack(side=LEFT)
        tb.Button(
            select_row, text="Select none", bootstyle="link", command=lambda: self.search_panel.set_all(False)
        ).pack(side=LEFT, padx=(10, 0))

        action_row = tb.Frame(self.search_tab)
        action_row.pack(fill=X)

        tb.Button(
            action_row, text="Download selected", bootstyle="success",
            command=lambda: self._start_download(self.search_panel)
        ).pack(side=LEFT)

        tb.Button(
            action_row, text="Download selected as ZIP", bootstyle="info",
            command=lambda: self._start_download(self.search_panel, as_zip=True)
        ).pack(side=LEFT, padx=(8, 0))

        self.search_status_label = tb.Label(self.search_tab, text="", font=("Segoe UI", 9), bootstyle="secondary")
        self.search_status_label.pack(anchor="w", pady=(8, 0))

    def _build_settings_tab(self):
        outer = tb.Frame(self.settings_tab)
        outer.pack(fill=BOTH, expand=YES)

        canvas = tb.Canvas(outer, highlightthickness=0)
        scrollbar = tb.Scrollbar(outer, orient="vertical", command=canvas.yview)
        frame = tb.Frame(canvas, padding=10)

        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        scrollbar.pack(side=RIGHT, fill=Y)

        row = 0
        frame.columnconfigure(0, weight=1)

        tb.Label(frame, text="Default download folder", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1

        folder_row = tb.Frame(frame)
        folder_row.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        row += 1

        self.folder_var = tb.StringVar(value=self.settings.get("default_folder"))
        tb.Entry(folder_row, textvariable=self.folder_var, font=("Segoe UI", 10)).pack(
            side=LEFT, fill=X, expand=YES
        )
        tb.Button(folder_row, text="Browse", command=self._pick_default_folder).pack(side=LEFT, padx=(8, 0))

        tb.Label(frame, text="Filename pattern", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1
        tb.Label(
            frame,
            text="Available: {artist} {title} {album} {track_no}",
            font=("Segoe UI", 8), bootstyle="secondary",
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1

        self.pattern_var = tb.StringVar(value=self.settings.get("filename_pattern"))
        tb.Entry(frame, textvariable=self.pattern_var, font=("Segoe UI", 10)).grid(
            row=row, column=0, sticky="ew", pady=(0, 16)
        )
        row += 1

        self.confirm_var = tb.BooleanVar(value=self.settings.get("confirm_before_download"))
        tb.Checkbutton(
            frame, text="Ask for confirmation before starting a download", variable=self.confirm_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.open_folder_var = tb.BooleanVar(value=self.settings.get("auto_open_folder"))
        tb.Checkbutton(
            frame, text="Open the destination folder when a download finishes", variable=self.open_folder_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.skip_existing_var = tb.BooleanVar(value=self.settings.get("skip_existing"))
        tb.Checkbutton(
            frame, text="Skip tracks that already exist in the destination folder", variable=self.skip_existing_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.subfolder_var = tb.BooleanVar(value=self.settings.get("subfolder_per_artist"))
        tb.Checkbutton(
            frame, text="Put each artist's tracks in their own subfolder", variable=self.subfolder_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.artwork_var = tb.BooleanVar(value=self.settings.get("save_artwork_separately"))
        tb.Checkbutton(
            frame, text="Also save cover art as a separate .jpg file", variable=self.artwork_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.log_var = tb.BooleanVar(value=self.settings.get("write_log"))
        tb.Checkbutton(
            frame, text="Write a download_log.csv in the destination folder", variable=self.log_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.clipboard_var = tb.BooleanVar(value=self.settings.get("clipboard_watch"))
        tb.Checkbutton(
            frame, text="Detect SoundCloud links copied to the clipboard", variable=self.clipboard_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1

        tb.Label(frame, text="Simultaneous downloads", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1

        self.workers_var = tb.IntVar(value=self.settings.get("max_workers", 2))
        workers_row = tb.Frame(frame)
        workers_row.grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1
        tb.Spinbox(workers_row, from_=1, to=6, textvariable=self.workers_var, width=5).pack(side=LEFT)
        tb.Label(
            workers_row, text="Higher can be faster but may get rate-limited.", font=("Segoe UI", 8),
            bootstyle="secondary"
        ).pack(side=LEFT, padx=(8, 0))

        tb.Label(frame, text="Theme", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1

        self.theme_var = tb.StringVar(value=self.settings.get("theme", "flatly"))
        theme_row = tb.Frame(frame)
        theme_row.grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1
        tb.Combobox(
            theme_row, textvariable=self.theme_var, values=settings_module.LIGHT_THEMES,
            state="readonly", width=15
        ).pack(side=LEFT)
        tb.Label(
            theme_row, text="Applies next time you open the app.", font=("Segoe UI", 8), bootstyle="secondary"
        ).pack(side=LEFT, padx=(8, 0))

        tb.Label(frame, text="Privacy and security", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1
        tb.Label(
            frame,
            text="This app never sends anything anywhere except SoundCloud's own API. "
            "No analytics, no crash reporting.",
            font=("Segoe UI", 8), bootstyle="secondary", wraplength=480, justify="left",
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        tb.Label(frame, text="Proxy URL (optional)", font=("Segoe UI", 9)).grid(
            row=row, column=0, sticky="w", pady=(0, 2)
        )
        row += 1
        tb.Label(
            frame,
            text="Routes all requests through a proxy you run (e.g. a local Tor or VPN proxy bridge). "
            "This app doesn't provide anonymity by itself — pair it with a proxy or VPN you trust. "
            "Example: http://127.0.0.1:8080",
            font=("Segoe UI", 8), bootstyle="secondary", wraplength=480, justify="left",
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1

        self.proxy_var = tb.StringVar(value=self.settings.get("proxy_url", ""))
        tb.Entry(frame, textvariable=self.proxy_var, font=("Segoe UI", 10)).grid(
            row=row, column=0, sticky="ew", pady=(0, 12)
        )
        row += 1

        self.randomize_ua_var = tb.BooleanVar(value=self.settings.get("randomize_user_agent"))
        tb.Checkbutton(
            frame, text="Use a randomized browser identity each launch", variable=self.randomize_ua_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.request_delay_var = tb.BooleanVar(value=self.settings.get("request_delay_enabled"))
        tb.Checkbutton(
            frame, text="Add a small random delay between download requests", variable=self.request_delay_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        self.clear_on_exit_var = tb.BooleanVar(value=self.settings.get("clear_recent_on_exit"))
        tb.Checkbutton(
            frame, text="Clear recent links list when the app closes", variable=self.clear_on_exit_var
        ).grid(row=row, column=0, sticky="w", pady=(0, 8))
        row += 1

        tb.Button(
            frame, text="Clear all saved data now", bootstyle="danger-outline", command=self._on_clear_data
        ).grid(row=row, column=0, sticky="w", pady=(0, 16))
        row += 1

        tb.Label(frame, text="Client ID override (advanced)", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1
        tb.Label(
            frame,
            text="Leave blank to auto-detect. Only fill this in if fetching keeps failing with a 401 error "
            "and you have a known-good client id from elsewhere.",
            font=("Segoe UI", 8), bootstyle="secondary", wraplength=480, justify="left",
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1

        client_id_row = tb.Frame(frame)
        client_id_row.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        row += 1

        self.client_id_var = tb.StringVar(value=self.settings.get("manual_client_id", ""))
        tb.Entry(client_id_row, textvariable=self.client_id_var, font=("Segoe UI", 10)).pack(
            side=LEFT, fill=X, expand=YES
        )
        tb.Button(client_id_row, text="Test connection", command=self._on_test_connection).pack(
            side=LEFT, padx=(8, 0)
        )

        self.settings_status_label = tb.Label(frame, text="", font=("Segoe UI", 9), bootstyle="secondary")
        self.settings_status_label.grid(row=row, column=0, sticky="w", pady=(0, 12))
        row += 1

        tb.Button(frame, text="Save settings", bootstyle="primary", command=self._save_settings_clicked).grid(
            row=row, column=0, sticky="w"
        )

        bind_mousewheel_tree(canvas, canvas)
        bind_mousewheel_tree(frame, canvas)

    def _build_about_tab(self):
        frame = tb.Frame(self.about_tab, padding=16)
        frame.pack(fill=BOTH, expand=YES)

        tb.Label(frame, text="All in One SoundCloud Archiver", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tb.Label(
            frame,
            text="A simple downloader for tracks, playlists, and profiles you have the right to download.",
            font=("Segoe UI", 9), bootstyle="secondary", wraplength=500, justify="left",
        ).pack(anchor="w", pady=(4, 16))

        tb.Label(frame, text=DISCLAIMER_TEXT, font=("Segoe UI", 9), wraplength=500, justify="left").pack(
            anchor="w", pady=(0, 16)
        )

        link_row = tb.Frame(frame)
        link_row.pack(anchor="w")
        tb.Label(link_row, text="Project by SandwichVertigo:", font=("Segoe UI", 9)).pack(side=LEFT)
        link = tb.Label(
            link_row, text=REPO_URL, font=("Segoe UI", 9, "underline"), bootstyle="info", cursor="hand2"
        )
        link.pack(side=LEFT, padx=(6, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(REPO_URL))

    def _show_disclaimer(self):
        answer = messagebox.askokcancel(
            "Before you continue", DISCLAIMER_TEXT + "\n\nClick OK to confirm you understand and agree."
        )
        if answer:
            self.settings["disclaimer_accepted"] = True
            settings_module.save_settings(self.settings)
        else:
            self.destroy()

    def _pick_default_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if chosen:
            self.folder_var.set(chosen)

    def _save_settings_clicked(self):
        self.settings["default_folder"] = self.folder_var.get()
        self.settings["filename_pattern"] = self.pattern_var.get() or "{artist} - {title}"
        self.settings["confirm_before_download"] = self.confirm_var.get()
        self.settings["auto_open_folder"] = self.open_folder_var.get()
        self.settings["skip_existing"] = self.skip_existing_var.get()
        self.settings["subfolder_per_artist"] = self.subfolder_var.get()
        self.settings["save_artwork_separately"] = self.artwork_var.get()
        self.settings["write_log"] = self.log_var.get()
        self.settings["clipboard_watch"] = self.clipboard_var.get()
        self.settings["max_workers"] = max(1, min(6, self.workers_var.get()))
        self.settings["theme"] = self.theme_var.get()
        self.settings["manual_client_id"] = self.client_id_var.get().strip()
        self.settings["proxy_url"] = self.proxy_var.get().strip()
        self.settings["randomize_user_agent"] = self.randomize_ua_var.get()
        self.settings["request_delay_enabled"] = self.request_delay_var.get()
        self.settings["clear_recent_on_exit"] = self.clear_on_exit_var.get()
        settings_module.save_settings(self.settings)
        self.backend.set_manual_client_id(self.settings["manual_client_id"])
        self.backend.set_proxy(self.settings["proxy_url"])
        self.backend.request_delay_enabled = self.settings["request_delay_enabled"]
        messagebox.showinfo(
            "Settings",
            "Settings saved. The randomized browser identity option takes effect next launch.",
        )

    def _on_clear_data(self):
        confirmed = messagebox.askyesno(
            "Clear all saved data",
            "This deletes your saved settings file, recent links, cached client id, and download "
            "folder preference. This cannot be undone. Continue?",
        )
        if not confirmed:
            return

        self.settings = settings_module.clear_all()
        self.backend.set_manual_client_id("")
        self.backend.api.client_id = None
        self.backend.set_proxy("")

        self.folder_var.set(self.settings.get("default_folder"))
        self.pattern_var.set(self.settings.get("filename_pattern"))
        self.confirm_var.set(self.settings.get("confirm_before_download"))
        self.open_folder_var.set(self.settings.get("auto_open_folder"))
        self.skip_existing_var.set(self.settings.get("skip_existing"))
        self.subfolder_var.set(self.settings.get("subfolder_per_artist"))
        self.artwork_var.set(self.settings.get("save_artwork_separately"))
        self.log_var.set(self.settings.get("write_log"))
        self.clipboard_var.set(self.settings.get("clipboard_watch"))
        self.workers_var.set(self.settings.get("max_workers", 2))
        self.theme_var.set(self.settings.get("theme", "flatly"))
        self.client_id_var.set("")
        self.proxy_var.set("")
        self.randomize_ua_var.set(False)
        self.request_delay_var.set(False)
        self.clear_on_exit_var.set(False)
        self.recent_combo.configure(values=[])

        messagebox.showinfo("Cleared", "All saved data has been cleared.")

    def _on_test_connection(self):
        self.backend.set_manual_client_id(self.client_id_var.get().strip())
        self.settings_status_label.configure(text="Testing connection...")
        threading.Thread(target=self._test_connection_worker, daemon=True).start()

    def _test_connection_worker(self):
        try:
            self.backend.test_connection()
            self.result_queue.put(("test_ok", None))
        except SoundCloudError as exc:
            self.result_queue.put(("test_error", str(exc)))
        except Exception as exc:
            self.result_queue.put(("test_error", f"Unexpected error: {exc}"))

    def _check_clipboard(self):
        try:
            if self.settings.get("clipboard_watch", True):
                content = self.clipboard_get().strip()
                if content != self._last_clipboard and "soundcloud.com/" in content and " " not in content:
                    self._last_clipboard = content
                    self.clipboard_hint.configure(text=f"Clipboard link detected — click to use: {content}")
                elif "soundcloud.com/" not in content:
                    self._last_clipboard = content
        except Exception:
            pass
        self.after(1500, self._check_clipboard)

    def _use_clipboard_link(self, event=None):
        text = self.clipboard_hint.cget("text")
        if "click to use: " in text:
            link = text.split("click to use: ", 1)[1]
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, link)
            self.clipboard_hint.configure(text="")

    def _on_recent_selected(self, event=None):
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, self.recent_var.get())

    def _set_busy(self, busy: bool, message: str = "", cancellable: bool = False):
        state = "disabled" if busy else "normal"
        self.download_folder_btn.configure(state=state)
        self.download_zip_btn.configure(state=state)
        self.status_label.configure(text=message)

        if busy and cancellable:
            self.cancel_btn.pack(side=LEFT, padx=(8, 0))
        else:
            self.cancel_btn.pack_forget()

        if not busy and self.last_failed_items:
            self.retry_btn.pack(side=LEFT, padx=(8, 0))
        else:
            self.retry_btn.pack_forget()

        self._progress_running = busy
        if busy:
            self.progress.configure(mode="indeterminate")
            if not self._minimized:
                self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    def _on_unmap(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._minimized = True
        self.progress.stop()

    def _on_map(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._minimized = False
        if self._progress_running:
            self.progress.start(12)

    def _on_fetch(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing link", "Paste a SoundCloud link first.")
            return

        settings_module.push_recent_link(self.settings, url)
        settings_module.save_settings(self.settings)
        self.recent_combo.configure(values=self.settings.get("recent_links", []))

        self._set_busy(True, "Fetching tracks...")
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url):
        try:
            items = self.backend.resolve(url)
            self.result_queue.put(("fetch_ok", items))
        except SoundCloudError as exc:
            self.result_queue.put(("fetch_error", str(exc)))
        except Exception as exc:
            self.result_queue.put(("fetch_error", f"Unexpected error: {exc}"))

    def _on_search(self):
        query = self.search_entry.get().strip()
        if not query:
            messagebox.showwarning("Missing search", "Type something to search for.")
            return
        self.search_status_label.configure(text="Searching...")
        threading.Thread(target=self._search_worker, args=(query,), daemon=True).start()

    def _search_worker(self, query):
        try:
            items = self.backend.search(query)
            self.result_queue.put(("search_ok", items))
        except SoundCloudError as exc:
            self.result_queue.put(("search_error", str(exc)))
        except Exception as exc:
            self.result_queue.put(("search_error", f"Unexpected error: {exc}"))

    def _start_download(self, panel: ResultsPanel, as_zip: bool = False, items=None):
        items = items if items is not None else panel.selected_items()
        if not items:
            messagebox.showwarning("Nothing selected", "Select at least one track first.")
            return

        if self.settings.get("confirm_before_download"):
            label = "as a ZIP" if as_zip else ""
            if not messagebox.askyesno("Confirm", f"Download {len(items)} track(s) {label}?".strip()):
                return

        if as_zip:
            zip_path = filedialog.asksaveasfilename(
                title="Choose where to save the ZIP", defaultextension=".zip",
                filetypes=[("ZIP archive", "*.zip")],
                initialdir=self.settings.get("default_folder") or os.path.expanduser("~"),
                initialfile="soundcloud_download.zip",
            )
            if not zip_path:
                return
            folder = None
        else:
            folder = filedialog.askdirectory(
                title="Choose a folder to save tracks",
                initialdir=self.settings.get("default_folder") or os.path.expanduser("~"),
            )
            if not folder:
                return
            zip_path = None

        self.active_panel = panel
        self.last_failed_items = []
        self.cancel_event = threading.Event()
        self._set_busy(True, "Starting download...", cancellable=True)

        threading.Thread(target=self._download_worker, args=(items, folder, zip_path), daemon=True).start()

    def _on_cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.status_label.configure(text="Cancelling...")

    def _on_retry_failed(self):
        if not self.last_failed_items:
            return
        panel = getattr(self, "active_panel", self.download_panel)
        if self.last_batch_zip:
            self._start_download(panel, as_zip=True, items=self.last_failed_items)
        else:
            self._retry_into_folder(panel, self.last_failed_items, self.last_batch_folder)

    def _retry_into_folder(self, panel, items, folder):
        if not folder:
            self._start_download(panel, items=items)
            return
        self.active_panel = panel
        self.last_failed_items = []
        self.cancel_event = threading.Event()
        self._set_busy(True, "Retrying failed tracks...", cancellable=True)
        threading.Thread(target=self._download_worker, args=(items, folder, None), daemon=True).start()

    def _progress_from_thread(self, item, status, message):
        self.result_queue.put(("row_status", (item, status, message)))

    def _download_worker(self, items, folder, zip_path):
        temp_folder = folder
        cleanup_temp = False
        if zip_path:
            temp_folder = make_secure_temp_dir()
            cleanup_temp = True

        try:
            batch = self.backend.download_batch(
                items,
                temp_folder,
                filename_pattern=self.settings.get("filename_pattern", "{artist} - {title}"),
                skip_existing=self.settings.get("skip_existing", True),
                subfolder_per_artist=self.settings.get("subfolder_per_artist", False),
                save_artwork_separately=self.settings.get("save_artwork_separately", False),
                write_log=self.settings.get("write_log", False),
                max_workers=self.settings.get("max_workers", 2),
                cancel_event=self.cancel_event,
                progress_cb=self._progress_from_thread,
            )
        except Exception as exc:
            self.result_queue.put(("download_done", (None, str(exc), None)))
            return

        final_path = zip_path if zip_path else folder

        if zip_path and batch.downloaded:
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for result in batch.downloaded:
                        arcname = os.path.relpath(result.path, temp_folder)
                        archive.write(result.path, arcname=arcname)
            except Exception as exc:
                self.result_queue.put(("download_done", (None, f"Could not build ZIP: {exc}", None)))
                return

        if cleanup_temp:
            for result in batch.downloaded:
                try:
                    os.remove(result.path)
                except OSError:
                    pass
            for root, dirs, files in os.walk(temp_folder, topdown=False):
                try:
                    os.rmdir(root)
                except OSError:
                    pass

        self.last_batch_folder = folder
        self.last_batch_zip = zip_path
        self.result_queue.put(("download_done", (batch, None, final_path)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "fetch_ok":
                    self._on_fetch_ok(payload)
                elif kind == "fetch_error":
                    self._set_busy(False, "")
                    messagebox.showerror("Could not fetch", payload)
                elif kind == "search_ok":
                    self.search_panel.populate(payload)
                    self.search_status_label.configure(text=f"Found {len(payload)} track(s).")
                    self._start_thumbnail_fetch(self.search_panel, payload)
                elif kind == "search_error":
                    self.search_status_label.configure(text="")
                    messagebox.showerror("Could not search", payload)
                elif kind == "row_status":
                    self._on_row_status(*payload)
                elif kind == "download_done":
                    self._on_download_done(payload)
                elif kind == "test_ok":
                    self.settings_status_label.configure(text="Connection works — the client id is valid.")
                elif kind == "test_error":
                    self.settings_status_label.configure(text=f"Test failed: {payload}")
                elif kind == "thumbnail":
                    self._apply_thumbnail(*payload)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _on_row_status(self, item, status, message):
        panel = getattr(self, "active_panel", None)
        if not panel:
            return
        row = panel.row_for(item)
        if not row:
            return
        text_map = {
            "downloading": "downloading...",
            "downloaded": "done",
            "failed": "failed",
            "skipped": "skipped",
        }
        row.set_status(text_map.get(status, status), STATUS_COLORS.get(status))

    def _on_fetch_ok(self, items):
        self._set_busy(False, f"Found {len(items)} track(s).")
        self.download_panel.populate(items)
        self._start_thumbnail_fetch(self.download_panel, items)

    def _start_thumbnail_fetch(self, panel, items):
        def worker():
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(self.backend.get_thumbnail_bytes, item.artwork_url): item
                    for item in items
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        data = future.result()
                    except Exception:
                        data = None
                    if data:
                        self.result_queue.put(("thumbnail", (panel, item, data)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_thumbnail(self, panel, item, data):
        if self._minimized or not self.winfo_viewable():
            return
        row = panel.row_for(item)
        if not row:
            return
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB")
            image = image.resize((CheckRow.THUMB_SIZE, CheckRow.THUMB_SIZE), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            return
        row.set_thumbnail(photo)

    def _on_download_done(self, payload):
        batch, error, final_path = payload

        if error:
            self._set_busy(False, "")
            messagebox.showerror("Download failed", error)
            return

        downloaded = len(batch.downloaded)
        skipped = len(batch.skipped)
        failed = batch.failed
        self.last_failed_items = [r.item for r in failed]

        summary = f"Downloaded {downloaded}"
        if skipped:
            summary += f", skipped {skipped}"
        if failed:
            summary += f", {len(failed)} failed"
        summary += "."

        self._set_busy(False, summary)

        if failed:
            messages = "\n".join(f"{r.item.artist} - {r.item.title}: {r.message}" for r in failed[:10])
            messagebox.showwarning("Finished with some errors", f"{summary}\n\n{messages}")
        else:
            messagebox.showinfo("Done", f"{summary}\nSaved to:\n{final_path}")

        if self.settings.get("auto_open_folder") and downloaded > 0:
            target = final_path if os.path.isdir(final_path) else os.path.dirname(final_path)
            self._open_in_explorer(target)

    def _open_in_explorer(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif os.uname().sysname == "Darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception:
            pass
