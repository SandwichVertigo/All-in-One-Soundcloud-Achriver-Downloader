import json
import os
import shutil
from pathlib import Path

CONFIG_DIR = Path.home() / ".aio_soundcloud_archiver"
CONFIG_FILE = CONFIG_DIR / "settings.json"

OLD_CONFIG_DIR = Path.home() / ".soundcloud_grabber"
OLD_CONFIG_FILE = OLD_CONFIG_DIR / "settings.json"

LIGHT_THEMES = ["flatly", "litera", "cosmo", "yeti", "journal", "minty", "sandstone", "pulse"]

DEFAULTS = {
    "default_folder": str(Path.home() / "Downloads"),
    "filename_pattern": "{artist} - {title}",
    "confirm_before_download": True,
    "disclaimer_accepted": False,
    "theme": "flatly",
    "font_size": 10,
    "auto_open_folder": True,
    "manual_client_id": "",
    "cached_client_id": "",
    "max_workers": 2,
    "skip_existing": True,
    "subfolder_per_artist": False,
    "save_artwork_separately": False,
    "write_log": False,
    "recent_links": [],
    "window_geometry": "",
    "clipboard_watch": True,
    "proxy_url": "",
    "request_delay_enabled": False,
    "randomize_user_agent": False,
    "clear_recent_on_exit": False,
}


def _migrate_if_needed():
    if CONFIG_FILE.exists():
        return
    if OLD_CONFIG_FILE.exists():
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(OLD_CONFIG_FILE, CONFIG_FILE)
        except Exception:
            pass


def _lock_down_permissions():
    if os.name == "nt":
        return
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def load_settings() -> dict:
    _migrate_if_needed()
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return dict(DEFAULTS)

    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
    _lock_down_permissions()


def clear_all() -> dict:
    try:
        if CONFIG_FILE.exists():
            os.remove(CONFIG_FILE)
    except OSError:
        pass
    return dict(DEFAULTS)


def push_recent_link(settings: dict, url: str, max_items: int = 8) -> None:
    url = url.strip()
    if not url:
        return
    links = [link for link in settings.get("recent_links", []) if link != url]
    links.insert(0, url)
    settings["recent_links"] = links[:max_items]
