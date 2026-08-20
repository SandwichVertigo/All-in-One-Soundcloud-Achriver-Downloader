import json
import os
import shutil
import time
import traceback
from pathlib import Path

CONFIG_DIR = Path.home() / ".aio_soundcloud_archiver"
CONFIG_FILE = CONFIG_DIR / "settings.json"
DEBUG_LOG_FILE = CONFIG_DIR / "debug.log"
MAX_LOG_BYTES = 2 * 1024 * 1024

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
    "debug_log_enabled": False,
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
    try:
        if DEBUG_LOG_FILE.exists():
            os.remove(DEBUG_LOG_FILE)
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


def validate_proxy_url(proxy_url: str) -> str:
    """Returns an error message string if invalid, or '' if the value is OK to save."""
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""
    valid_schemes = ("http://", "https://", "socks4://", "socks5://")
    if not proxy_url.startswith(valid_schemes):
        return "Proxy URL should start with http://, https://, socks4://, or socks5://"
    host_part = proxy_url.split("://", 1)[1]
    if not host_part or host_part.startswith(("/", ":")):
        return "Proxy URL is missing a host (e.g. http://127.0.0.1:8080)"
    return ""


_debug_logging_enabled = False


def set_debug_logging(enabled: bool) -> None:
    global _debug_logging_enabled
    _debug_logging_enabled = bool(enabled)


def log_error(context: str, exc: Exception) -> None:
    if not _debug_logging_enabled:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if DEBUG_LOG_FILE.exists() and DEBUG_LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            os.remove(DEBUG_LOG_FILE)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{timestamp}] {context}\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if os.name != "nt":
            os.chmod(DEBUG_LOG_FILE, 0o600)
    except Exception:
        pass
