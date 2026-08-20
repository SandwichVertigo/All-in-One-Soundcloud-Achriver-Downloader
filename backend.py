import csv
import json
import os
import random
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.error import HTTPError, URLError

import mutagen
import mutagen.id3

from sclib import SoundcloudAPI, Track, Playlist
from sclib.sync import UnsupportedFormatError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

USER_AGENT_POOL = [
    DEFAULT_USER_AGENT,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

CLIENT_ID_PATTERN = re.compile(r'client_id["\']?\s*[:=]\s*["\']?([a-zA-Z0-9]{16,})')
SCRIPT_SRC_PATTERN = re.compile(r'<script[^>]+src="([^"]+)"')

CLIENT_ID_SCRAPE_PAGES = [
    "https://soundcloud.com/",
    "https://soundcloud.com/discover",
    "https://soundcloud.com/mt-marcy/cold-nights",
]

TEST_TRACK_URL = "https://soundcloud.com/mt-marcy/cold-nights"
TRACKS_BATCH_URL = "https://api-v2.soundcloud.com/tracks?ids={ids}&client_id={client_id}"
TRACKS_BATCH_SIZE = 50
MIN_VALID_MP3_BYTES = 2000


class SoundCloudError(Exception):
    pass


class _AuthExpired(Exception):
    pass


@dataclass
class TrackItem:
    title: str
    artist: str
    duration_ms: int
    track_obj: object
    album: Optional[str] = None
    track_no: Optional[int] = None
    artwork_url: Optional[str] = None

    @property
    def duration_str(self) -> str:
        seconds = int(self.duration_ms / 1000) if self.duration_ms else 0
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"

    def _clean(self, text: str) -> str:
        invalid = '<>:"/\\|?*'
        for ch in invalid:
            text = text.replace(ch, "")
        return re.sub(r"\s+", " ", text).strip()

    def render_filename(self, pattern: str) -> str:
        pattern = pattern or "{artist} - {title}"
        try:
            name = pattern.format(
                artist=self.artist or "Unknown artist",
                title=self.title or "Unknown title",
                album=self.album or "",
                track_no=self.track_no or "",
            )
        except (KeyError, IndexError):
            name = f"{self.artist} - {self.title}"
        name = self._clean(name)
        return name[:150] if name else "Untitled Track"


@dataclass
class DownloadResult:
    item: TrackItem
    status: str
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class BatchResult:
    results: List[DownloadResult] = field(default_factory=list)

    @property
    def downloaded(self):
        return [r for r in self.results if r.status == "downloaded"]

    @property
    def skipped(self):
        return [r for r in self.results if r.status == "skipped"]

    @property
    def failed(self):
        return [r for r in self.results if r.status == "failed"]


def _track_item_from(t: Track) -> TrackItem:
    title = getattr(t, "title", None) or "Unknown title"
    artist = getattr(t, "artist", None)
    user = getattr(t, "user", None)
    if not artist:
        artist = user.get("username") if isinstance(user, dict) else "Unknown artist"
    duration = getattr(t, "duration", 0) or 0
    album = getattr(t, "album", None)
    track_no = getattr(t, "track_no", None)
    artwork_url = getattr(t, "artwork_url", None)
    if not artwork_url and isinstance(user, dict):
        artwork_url = user.get("avatar_url")
    return TrackItem(
        title=title, artist=artist, duration_ms=duration, track_obj=t,
        album=album, track_no=track_no, artwork_url=artwork_url,
    )


class SoundCloudBackend:
    def __init__(
        self,
        manual_client_id: Optional[str] = None,
        cached_client_id: Optional[str] = None,
        on_client_id_learned: Optional[Callable[[str], None]] = None,
        proxy_url: Optional[str] = None,
        randomize_user_agent: bool = False,
        request_delay_enabled: bool = False,
    ):
        self.api = SoundcloudAPI()
        self.manual_client_id = manual_client_id.strip() if manual_client_id else None
        self.on_client_id_learned = on_client_id_learned
        self.request_delay_enabled = request_delay_enabled

        self.user_agent = random.choice(USER_AGENT_POOL) if randomize_user_agent else DEFAULT_USER_AGENT
        self.opener = None
        self.set_proxy(proxy_url)

        if self.manual_client_id:
            self.api.client_id = self.manual_client_id
        elif cached_client_id:
            self.api.client_id = cached_client_id.strip()

    def set_proxy(self, proxy_url: Optional[str]):
        proxy_url = (proxy_url or "").strip()
        if proxy_url:
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        else:
            handler = urllib.request.ProxyHandler({})
        self.opener = urllib.request.build_opener(handler)

    def set_manual_client_id(self, client_id: Optional[str]):
        client_id = (client_id or "").strip()
        self.manual_client_id = client_id or None
        if self.manual_client_id:
            self.api.client_id = self.manual_client_id

    def _http_get_text(self, url: str, timeout: int = 20) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with self.opener.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _http_get_bytes(self, url: str, timeout: int = 30) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with self.opener.open(request, timeout=timeout) as response:
            return response.read()

    def get_thumbnail_bytes(self, artwork_url: Optional[str], size: str = "t67x67") -> Optional[bytes]:
        if not artwork_url:
            return None
        for token in ("large", "original", "t500x500"):
            if token in artwork_url:
                artwork_url = artwork_url.replace(token, size)
                break
        try:
            return self._http_get_bytes(artwork_url, timeout=10)
        except Exception:
            return None

    def _fetch_json(self, url: str):
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 401:
                raise _AuthExpired()
            if exc.code == 404:
                raise SoundCloudError("SoundCloud says that link doesn't exist (404). Double check the URL.")
            raise SoundCloudError(f"SoundCloud returned an error (HTTP {exc.code}).")
        except URLError as exc:
            raise SoundCloudError(f"Could not reach SoundCloud: {exc.reason}")

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise SoundCloudError("SoundCloud sent back something unexpected. Try again in a moment.")

    def _scrape_client_id(self) -> Optional[str]:
        for page_url in CLIENT_ID_SCRAPE_PAGES:
            try:
                html = self._http_get_text(page_url)
            except Exception:
                continue

            script_urls = SCRIPT_SRC_PATTERN.findall(html)
            for script_url in script_urls:
                if script_url.startswith("//"):
                    script_url = "https:" + script_url
                elif not script_url.startswith("http"):
                    continue
                try:
                    script_text = self._http_get_text(script_url)
                except Exception:
                    continue
                match = CLIENT_ID_PATTERN.search(script_text)
                if match:
                    return match.group(1)
        return None

    def _refresh_client_id(self, force_scrape: bool = False):
        if self.manual_client_id and not force_scrape:
            self.api.client_id = self.manual_client_id
            return
        self.api.client_id = None
        client_id = self._scrape_client_id()
        if not client_id:
            raise SoundCloudError(
                "Could not find a usable SoundCloud client id automatically. "
                "You can paste one manually in Settings."
            )
        self.api.client_id = client_id
        if self.on_client_id_learned and not self.manual_client_id:
            self.on_client_id_learned(client_id)

    def _ensure_client_id(self):
        if not self.api.client_id:
            self._refresh_client_id()

    def _get_with_retry(self, url_builder: Callable[[str], str]):
        self._ensure_client_id()
        try:
            return self._fetch_json(url_builder(self.api.client_id))
        except _AuthExpired:
            self._refresh_client_id(force_scrape=True)
            try:
                return self._fetch_json(url_builder(self.api.client_id))
            except _AuthExpired:
                raise SoundCloudError(
                    "SoundCloud keeps rejecting the request (401) even after refreshing the client id. "
                    "Try pasting a known-good client id manually in Settings."
                )

    def test_connection(self):
        self._get_with_retry(
            lambda cid: SoundcloudAPI.RESOLVE_URL.format(url=TEST_TRACK_URL, client_id=cid)
        )

    def resolve(self, url: str) -> List[TrackItem]:
        url = url.strip()
        if not url:
            raise SoundCloudError("Paste a SoundCloud link first.")
        if not url.startswith(("http://", "https://")):
            raise SoundCloudError("That doesn't look like a link. Paste a full soundcloud.com URL.")

        encoded_url = urllib.parse.quote(url, safe="")
        obj = self._get_with_retry(
            lambda cid: SoundcloudAPI.RESOLVE_URL.format(url=encoded_url, client_id=cid)
        )

        if not obj:
            raise SoundCloudError("SoundCloud didn't return anything for that link. Double check the URL.")

        kind = obj.get("kind")

        if kind == "track":
            tracks_raw = [Track(obj=obj, client=self.api)]
        elif kind in ("playlist", "system-playlist"):
            tracks_raw = self._resolve_playlist_tracks(obj)
        elif kind == "user":
            tracks_raw = self._resolve_user_tracks(obj.get("id"))
        else:
            raise SoundCloudError(f"That link type ('{kind or 'unknown'}') isn't supported yet.")

        items = [_track_item_from(t) for t in tracks_raw if t is not None]
        if not items:
            raise SoundCloudError("No downloadable tracks were found at that link.")
        return items

    def search(self, query: str, limit: int = 25) -> List[TrackItem]:
        query = query.strip()
        if not query:
            raise SoundCloudError("Type something to search for.")

        encoded = urllib.parse.quote(query)
        obj = self._get_with_retry(
            lambda cid: SoundcloudAPI.SEARCH_URL.format(query=encoded, client_id=cid, limit=limit, offset=0)
        )
        collection = (obj or {}).get("collection", [])
        tracks_raw = [Track(obj=raw, client=self.api) for raw in collection if raw.get("kind") == "track"]
        items = [_track_item_from(t) for t in tracks_raw]
        if not items:
            raise SoundCloudError("No tracks matched that search.")
        return items

    def _resolve_playlist_tracks(self, obj) -> List[Track]:
        raw_tracks = obj.get("tracks", [])
        complete = []
        incomplete_ids = []
        for raw in raw_tracks:
            if "title" in raw:
                complete.append(Track(obj=raw, client=self.api))
            else:
                incomplete_ids.append(raw.get("id"))

        if incomplete_ids:
            complete.extend(self._fetch_tracks_by_ids(incomplete_ids))

        return complete

    def _fetch_tracks_by_ids(self, track_ids) -> List[Track]:
        track_ids = [i for i in track_ids if i]
        chunks = [track_ids[i:i + TRACKS_BATCH_SIZE] for i in range(0, len(track_ids), TRACKS_BATCH_SIZE)]
        results = []

        def fetch_chunk(chunk):
            ids_str = ",".join(str(i) for i in chunk)
            return self._get_with_retry(
                lambda cid: TRACKS_BATCH_URL.format(ids=ids_str, client_id=cid)
            )

        if len(chunks) <= 1:
            for chunk in chunks:
                data = fetch_chunk(chunk)
                results.extend(data or [])
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
                futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]
                for future in as_completed(futures):
                    data = future.result()
                    results.extend(data or [])

        return [Track(obj=raw, client=self.api) for raw in results]

    def _resolve_user_tracks(self, user_id) -> List[Track]:
        if not user_id:
            raise SoundCloudError("Could not read that profile's id from SoundCloud's response.")

        all_tracks = []
        next_href = (
            f"https://api-v2.soundcloud.com/users/{user_id}/tracks"
            f"?limit=50&linked_partitioning=1"
        )
        guard = 0
        while next_href and guard < 40:
            guard += 1
            href = next_href

            def build(cid, href=href):
                sep = "&" if "?" in href else "?"
                return href if "client_id=" in href else f"{href}{sep}client_id={cid}"

            page = self._get_with_retry(build)
            collection = page.get("collection", [])
            for raw in collection:
                if raw.get("kind") == "track":
                    all_tracks.append(Track(obj=raw, client=self.api))
            next_href = page.get("next_href")

        return all_tracks

    def download_batch(
        self,
        items: List[TrackItem],
        base_folder: str,
        filename_pattern: str = "{artist} - {title}",
        skip_existing: bool = True,
        subfolder_per_artist: bool = False,
        save_artwork_separately: bool = False,
        write_log: bool = False,
        max_workers: int = 1,
        cancel_event: Optional[threading.Event] = None,
        progress_cb: Optional[Callable[[TrackItem, str, Optional[str]], None]] = None,
    ) -> BatchResult:
        os.makedirs(base_folder, exist_ok=True)
        cancel_event = cancel_event or threading.Event()
        batch = BatchResult()
        lock = threading.Lock()

        def process(item: TrackItem) -> DownloadResult:
            if cancel_event.is_set():
                return DownloadResult(item=item, status="skipped", message="Cancelled")

            if self.request_delay_enabled:
                time.sleep(random.uniform(0.2, 1.2))
                if cancel_event.is_set():
                    return DownloadResult(item=item, status="skipped", message="Cancelled")

            folder = base_folder
            if subfolder_per_artist:
                folder = os.path.join(base_folder, item._clean(item.artist or "Unknown artist"))

            filename = item.render_filename(filename_pattern) + ".mp3"
            path = os.path.join(folder, filename)

            if skip_existing and os.path.exists(path):
                if progress_cb:
                    progress_cb(item, "skipped", None)
                return DownloadResult(item=item, status="skipped", path=path, message="Already exists")

            try:
                os.makedirs(folder, exist_ok=True)
                if progress_cb:
                    progress_cb(item, "downloading", None)

                artwork_bytes = self._download_and_tag_mp3(item, path)

                if os.path.getsize(path) < MIN_VALID_MP3_BYTES:
                    raise SoundCloudError(
                        "Downloaded file looks incomplete or corrupt (too small). "
                        "This can happen if the connection dropped mid-download."
                    )

                if save_artwork_separately:
                    self._save_artwork(item, folder, artwork_bytes)

                if progress_cb:
                    progress_cb(item, "downloaded", None)
                return DownloadResult(item=item, status="downloaded", path=path)
            except UnsupportedFormatError:
                message = "This track isn't available for direct download (streaming only)."
                if progress_cb:
                    progress_cb(item, "failed", message)
                return DownloadResult(item=item, status="failed", message=message)
            except Exception as exc:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
                message = str(exc) or type(exc).__name__
                if progress_cb:
                    progress_cb(item, "failed", message)
                return DownloadResult(item=item, status="failed", message=message)

        if max_workers <= 1:
            for item in items:
                if cancel_event.is_set():
                    batch.results.append(DownloadResult(item=item, status="skipped", message="Cancelled"))
                    continue
                batch.results.append(process(item))
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process, item): item for item in items}
                for future in as_completed(futures):
                    with lock:
                        batch.results.append(future.result())

        if write_log:
            self._write_log(base_folder, batch)

        return batch

    def _download_and_tag_mp3(self, item: TrackItem, path: str) -> Optional[bytes]:
        track = item.track_obj
        stream_url = track.get_stream_url()
        audio_bytes = self._http_get_bytes(stream_url)

        with open(path, "wb") as handle:
            handle.write(audio_bytes)

        artwork_bytes = None
        artwork_url = getattr(track, "artwork_url", None)
        if artwork_url:
            try:
                large_url = artwork_url.replace("large", "t500x500")
                artwork_bytes = self._http_get_bytes(large_url)
            except Exception:
                artwork_bytes = None

        self._tag_mp3(path, item, artwork_bytes)
        return artwork_bytes

    def _tag_mp3(self, path: str, item: TrackItem, artwork_bytes: Optional[bytes]):
        try:
            audio = mutagen.File(path, easy=False)
        except Exception:
            return
        if audio is None:
            return

        if audio.tags is None:
            audio.add_tags()
        else:
            audio.tags.clear()

        title_frame = mutagen.id3.TIT2(encoding=3)
        title_frame.append(item.title or "Unknown title")
        audio.tags.add(title_frame)

        artist_frame = mutagen.id3.TPE1(encoding=3)
        artist_frame.append(item.artist or "Unknown artist")
        audio.tags.add(artist_frame)

        if item.album:
            album_frame = mutagen.id3.TALB(encoding=3)
            album_frame.append(item.album)
            audio.tags.add(album_frame)

        if item.track_no:
            track_frame = mutagen.id3.TRCK(encoding=3)
            track_frame.append(str(item.track_no))
            audio.tags.add(track_frame)

        if artwork_bytes:
            audio.tags.add(
                mutagen.id3.APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=artwork_bytes
                )
            )

        audio.save(path, v1=2)

    def _save_artwork(self, item: TrackItem, folder: str, artwork_bytes: Optional[bytes] = None):
        if not artwork_bytes:
            artwork_url = getattr(item.track_obj, "artwork_url", None)
            if not artwork_url:
                return
            try:
                large_url = artwork_url.replace("large", "t500x500")
                artwork_bytes = self._http_get_bytes(large_url)
            except Exception:
                return
        filename = item.render_filename("{artist} - {title}") + ".jpg"
        with open(os.path.join(folder, filename), "wb") as handle:
            handle.write(artwork_bytes)

    def _write_log(self, base_folder: str, batch: BatchResult):
        log_path = os.path.join(base_folder, "download_log.csv")
        file_exists = os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not file_exists:
                writer.writerow(["timestamp", "status", "artist", "title", "path", "message"])
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            for result in batch.results:
                writer.writerow(
                    [
                        timestamp,
                        result.status,
                        result.item.artist,
                        result.item.title,
                        result.path or "",
                        result.message or "",
                    ]
                )
        if os.name != "nt":
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass


def make_secure_temp_dir(prefix: str = "aiosca_") -> str:
    return tempfile.mkdtemp(prefix=prefix)
