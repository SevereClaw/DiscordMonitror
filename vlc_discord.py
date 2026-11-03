# -*- coding: utf-8 -*-
"""
Music → Discord: показывает в статусе Discord, что сейчас играет —
в VLC и в любом приложении, которое видит Windows (Spotify, браузер, AIMP…).
Системные медиасессии требуют пакетов winrt (см. requirements.txt);
без них работает только VLC.
"""
import asyncio
import base64
import datetime as dt
import ctypes
import ctypes.wintypes
import csv
import hashlib
import json
import os
import queue
import sqlite3
import re
import struct
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from tkinter import messagebox

import updater

try:
    from winrt.windows.media.control import \
        GlobalSystemMediaTransportControlsSessionManager as MediaManager
    HAS_SMTC = True
except Exception:
    HAS_SMTC = False

try:
    import pystray
    from PIL import Image
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

APP_NAME = "Music → Discord"
UPDATE_REPO = "SevereClaw/DiscordMonitror"
UPDATE_ASSET = "Music-Discord.exe"
CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "VLCDiscord")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
VLCRC = os.path.join(os.environ.get("APPDATA", ""), "vlc", "vlcrc")
AUDIO_EXT = r"\.(mp3|flac|wav|m4a|ogg|opus|aac|wma|mp4|mkv|webm)$"

DEFAULT_CFG = {
    "client_id": "",
    "vlc_port": 8088,
    "vlc_password": "",
    "covers": True,
    "file_covers": True,        # обложка, встроенная в сам файл (MP3 Плеер, VLC) → временный хостинг
    "show_paused": True,
    "show_delay": 10,           # показывать новый трек, только если он играет столько секунд
    "lyrics": "off",            # текст песни: off | details | state
    "autostart": False,
    "auto_update": True,        # сама ставит новые сборки с GitHub
    "update_checked": 0,
    "buttons": ["youtube", "yandex"],
    "hotkey": "Ctrl+Alt+H",
    "blacklist": [],
    "source_mode": "auto",      # auto | vlc | system
    "multi": "auto",            # музыка и видео сразу: auto | music | video | both
    "client_id_video": "",      # второе приложение Discord — для карточки «Смотрит» (режим both)
    "video_image": "",          # картинка видео, если нет превью: ключ Art Assets второго приложения или https
    "ignore_apps": [],
    "fallback_image": "logo",   # ключ картинки из Art Assets в Discord или https-ссылка
    "badge": "none",            # значок в углу обложки: none | logo | app
    "browser_icon": "youtube",  # какой значок ставить для музыки из браузера
    "fmt_details": "{title}",   # строка 1
    "fmt_state": "{artist}",    # строка 2
    "fmt_large": "{album}",     # подпись при наведении на обложку
    "list_shows": "state",      # что видно в списке участников: state | details | name
    "lastfm_on": False,
    "lastfm_key": "",
    "lastfm_secret": "",
    "lastfm_session": "",
    "lastfm_user": "",
    "tray_hint_shown": False,
}

# Кнопки под статусом: id → (название в списке, подпись кнопки, шаблон ссылки)
BUTTONS = {
    "none":    ("— нет —", "", ""),
    "youtube": ("YouTube", "▶ Найти на YouTube", "https://www.youtube.com/results?search_query={q}"),
    "yandex":  ("Яндекс Музыка", "Слушать в Яндекс Музыке", "https://music.yandex.ru/search?text={q}"),
    "spotify": ("Spotify", "Открыть в Spotify", "https://open.spotify.com/search/{q}"),
    "vk":      ("VK Музыка", "Найти в VK Музыке", "https://vk.com/audio?q={q}"),
    "genius":  ("Genius (текст)", "Текст песни", "https://genius.com/search?q={q}"),
}


# ───────────────────────── конфиг ─────────────────────────
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg["vlc_password"]:
        cfg["vlc_password"] = uuid.uuid4().hex[:12]
    if cfg["badge"] is True:            # старые настройки: галочка → «мой логотип»
        cfg["badge"] = "logo"
    elif cfg["badge"] is False:
        cfg["badge"] = "none"
    return cfg


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ───────────────────────── настройка VLC ─────────────────────────
def vlc_is_running():
    try:
        out = os.popen('tasklist /FI "IMAGENAME eq vlc.exe" /NH').read().lower()
        return "vlc.exe" in out
    except Exception:
        return False


def configure_vlc(port, password):
    """Включает веб-интерфейс VLC (только локально) прямо в vlcrc."""
    os.makedirs(os.path.dirname(VLCRC), exist_ok=True)
    lines = []
    if os.path.exists(VLCRC):
        with open(VLCRC, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

    wanted = {"http-password": password, "http-port": str(port), "http-host": "127.0.0.1"}
    seen = set()
    extraintf_done = False
    out = []
    for line in lines:
        m = re.match(r"^#?\s*([\w-]+)=(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "extraintf":
                mods = [x for x in val.split(":") if x] if not line.startswith("#") else []
                if "http" not in mods:
                    mods.append("http")
                out.append("extraintf=" + ":".join(mods))
                extraintf_done = True
                continue
            if key in wanted:
                if key not in seen:
                    out.append(f"{key}={wanted[key]}")
                    seen.add(key)
                continue
        out.append(line)

    if not extraintf_done:
        out.append("extraintf=http")
    for k, v in wanted.items():
        if k not in seen:
            out.append(f"{k}={v}")

    with open(VLCRC, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


# ───────────────────────── чтение VLC ─────────────────────────
def vlc_http(port, password, what):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/requests/{what}")
    token = base64.b64encode(f":{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def vlc_current_path(port, password):
    """Полный путь к текущему файлу (для чёрного списка по папкам)."""
    def walk(node):
        if node.get("current") and node.get("uri"):
            return node["uri"]
        for c in node.get("children") or []:
            r = walk(c)
            if r:
                return r
        return None

    uri = walk(vlc_http(port, password, "playlist.json"))
    if not uri:
        return ""
    if uri.startswith("file:///"):
        return urllib.parse.unquote(uri[8:]).replace("/", "\\")
    return urllib.parse.unquote(uri)


def parse_http(st):
    state = st.get("state", "stopped")
    if state == "stopped":
        return None
    meta = (st.get("information") or {}).get("category", {}).get("meta", {}) or {}
    filename = meta.get("filename") or ""
    title = meta.get("title") or meta.get("now_playing") or filename or "Неизвестный трек"
    title = re.sub(AUDIO_EXT, "", title, flags=re.I)
    artist = meta.get("artist") or meta.get("album_artist") or ""
    album = meta.get("album") or ""
    if not artist and " - " in title:          # «Исполнитель - Трек.mp3» без тегов
        artist, title = [x.strip() for x in title.split(" - ", 1)]
    art = meta.get("artwork_url") or ""          # VLC сам достаёт обложку из файла
    cover_file = urllib.parse.unquote(art[8:]).replace("/", "\\") if art.startswith("file:///") else ""
    return {
        "title": title, "artist": artist, "album": album, "filename": filename, "path": "",
        "cover_file": cover_file,
        "playing": state == "playing",
        "time": int(st.get("time") or 0),
        "length": int(st.get("length") or 0),
        "src": "vlc", "app": "VLC", "via": "http",
    }


user32 = ctypes.windll.user32 if os.name == "nt" else None
kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
VLC_TITLE_RE = r" - (VLC media player|Медиапроигрыватель VLC|VLC)$"


def vlc_window_title():
    """Запасной вариант: название из заголовка окна VLC."""
    if not user32:
        return None
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if re.search(VLC_TITLE_RE, buf.value):
                found.append(buf.value)
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    raw = re.sub(VLC_TITLE_RE, "", found[0]).strip()
    t = re.sub(AUDIO_EXT, "", raw, flags=re.I)
    if not t:
        return None
    artist, title = "", t
    if " - " in t:
        artist, title = [x.strip() for x in t.split(" - ", 1)]
    return {"title": title, "artist": artist, "album": "", "filename": raw, "path": "",
            "playing": True, "time": 0, "length": 0, "src": "vlc", "app": "VLC", "via": "title"}


# ───────────────────────── системные медиасессии Windows ─────────────────────────
APP_NAMES = [
    ("spotify", "Spotify"), ("yandexmusic", "Яндекс Музыка"), ("yandex.music", "Яндекс Музыка"),
    ("desktop.music", "Яндекс Музыка"), ("music.yandex", "Яндекс Музыка"), ("aimp", "AIMP"),
    ("foobar", "foobar2000"), ("vlc", "VLC"), ("chrome", "Chrome"), ("msedge", "Edge"),
    ("firefox", "Firefox"), ("opera", "Opera"), ("yandex", "Яндекс Браузер"),
    ("telegram", "Telegram"), ("zunemusic", "Медиаплеер"), ("music.ui", "Медиаплеер"),
    ("itunes", "iTunes"), ("deezer", "Deezer"), ("vk", "VK"), ("soundcloud", "SoundCloud"),
]


def app_label(app_id):
    low = (app_id or "").lower()
    for key, name in APP_NAMES:
        if key in low:
            return name
    name = re.split(r"[!\\/]", app_id or "?")[-1]
    return re.sub(r"\.exe$", "", name, flags=re.I) or "?"


THUMBS_DIR = os.path.join(CFG_DIR, "thumbs")
_thumb_cache = {}          # (app_id, title, artist) -> (путь к файлу превью или "", когда проверяли)
THUMB_STATUS = {"error": "", "method": ""}     # для строки состояния в окне


def _ibuffer_bytes(buf):
    """Байты из WinRT IBuffer — несколькими способами (зависит от версии пакетов winrt)."""
    n = int(buf.length)
    errors = []
    try:                                           # 1) buffer protocol
        data = bytes(memoryview(buf))[:n]
        if len(data) == n:
            THUMB_STATUS["method"] = "memoryview"
            return data
    except Exception as e:
        errors.append(f"memoryview: {e}")
    try:                                           # 2) CryptographicBuffer.CopyToByteArray
        from winrt.windows.security.cryptography import CryptographicBuffer
        arr = CryptographicBuffer.copy_to_byte_array(buf)
        data = bytes(arr)
        if len(data) == n:
            THUMB_STATUS["method"] = "CryptographicBuffer"
            return data
    except Exception as e:
        errors.append(f"CryptographicBuffer: {e}")
    try:                                           # 3) DataReader побайтово (медленно, но надёжно)
        from winrt.windows.storage.streams import DataReader
        r = DataReader.from_buffer(buf)
        data = bytes(r.read_byte() for _ in range(n))
        THUMB_STATUS["method"] = "DataReader"
        return data
    except Exception as e:
        errors.append(f"DataReader: {e}")
    raise RuntimeError("; ".join(errors)[:200])


async def _thumb_file(props, key):
    """Превью из медиасессии Windows (у YouTube — кадр ролика) → файл."""
    hit = _thumb_cache.get(key)
    if hit and (hit[0] or time.monotonic() - hit[1] < 12):   # неудачу — повторить через 12 с
        return hit[0]
    path = ""
    try:
        from winrt.windows.storage.streams import Buffer, InputStreamOptions
        ref = props.thumbnail
        if ref is None:
            THUMB_STATUS["error"] = "Windows не даёт превью для этого видео"
        else:
            stream = await ref.open_read_async()
            size = int(stream.size)
            if not 200 < size < 6_000_000:
                THUMB_STATUS["error"] = f"странный размер превью: {size}"
            else:
                res = await stream.read_async(Buffer(size), size, InputStreamOptions.READ_AHEAD)
                data = _ibuffer_bytes(res)            # читать из того буфера, что ВЕРНУЛИ
                if len(data) > 200:
                    os.makedirs(THUMBS_DIR, exist_ok=True)
                    h = hashlib.md5(data).hexdigest()
                    ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
                    path = os.path.join(THUMBS_DIR, h[:16] + ext)
                    if not os.path.isfile(path):
                        with open(path, "wb") as f:
                            f.write(data)
                    THUMB_STATUS["error"] = ""
    except Exception as e:
        THUMB_STATUS["error"] = f"{type(e).__name__}: {e}"[:160]
        path = ""
    if len(_thumb_cache) > 200:
        _thumb_cache.clear()
    _thumb_cache[key] = (path, time.monotonic())
    return path


async def _smtc_read():
    mgr = await MediaManager.request_async()
    cur = mgr.get_current_session()
    cur_id = cur.source_app_user_model_id if cur else None
    out = []
    for s in mgr.get_sessions():
        try:
            status = int(s.get_playback_info().playback_status)   # 4 — играет, 5 — пауза
            if status not in (4, 5):
                continue
            props = await s.try_get_media_properties_async()
            title = (props.title or "").strip()
            if not title:
                continue
            artist = (props.artist or props.album_artist or "").strip()
            artist = re.sub(r"\s*-\s*Topic$", "", artist)          # YouTube «Кино - Topic»
            if not artist and " - " in title:
                artist, title = [x.strip() for x in title.split(" - ", 1)]
            elif artist and title.lower().startswith(artist.lower() + " - "):
                title = title[len(artist) + 3:].strip()           # «Кино - Звезда…» от канала «Кино»

            pos = length = 0
            try:
                tl = s.get_timeline_properties()
                length = int((tl.end_time - tl.start_time).total_seconds())
                pos = tl.position.total_seconds()
                if status == 4 and tl.last_updated_time:
                    lag = (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
                    if 0 <= lag < max(length, 1) + 60:
                        pos += lag
                if length <= 0 or pos > length + 5:
                    length = pos = 0
            except Exception:
                pass

            app_id = s.source_app_user_model_id or ""
            cover_file = ""
            if app_label(app_id) in BROWSERS:              # превью ролика (YouTube и т.п.)
                cover_file = await _thumb_file(props, (app_id, title, artist))
            out.append({
                "title": title, "artist": artist, "album": (props.album_title or "").strip(),
                "filename": "", "path": "",
                "playing": status == 4, "time": int(pos), "length": length,
                "src": app_id, "app": app_label(app_id), "via": "smtc",
                "current": app_id == cur_id, "cover_file": cover_file,
            })
        except Exception:
            continue
    return out


def smtc_sessions():
    if not HAS_SMTC:
        return []
    try:
        return asyncio.run(_smtc_read())
    except Exception:
        return []


# ───────────────────────── MP3 Плеер (прямой канал) ─────────────────────────
# MP3 Плеер сам пишет, что играет, в этот файл (раз в пару секунд).
# Так у нас есть точная позиция, путь к файлу (для чёрного списка по папкам)
# и строка из его локального .lrc — без угадывания по медиасессиям Windows.
MP3P_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MP3Player", "nowplaying.json")
MP3P_APP = "MP3 Плеер"


def read_mp3player():
    try:
        with open(MP3P_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if d.get("v") != 1 or d.get("state") not in ("playing", "paused"):
        return None
    age = time.time() - float(d.get("updated") or 0)
    if age > 8:                                   # плеер закрыт или завис
        return None
    title = (d.get("title") or "").strip()
    if not title:
        return None
    playing = d.get("state") == "playing"
    pos = float(d.get("position") or 0) + (age if playing else 0)
    length = int(d.get("length") or 0)
    path = d.get("path") or ""
    return {
        "title": title, "artist": (d.get("artist") or "").strip(), "album": (d.get("album") or "").strip(),
        "filename": os.path.basename(path), "path": path,
        "playing": playing, "time": int(min(pos, length) if length else pos), "length": length,
        "src": "mp3player", "app": MP3P_APP, "via": "mp3player",
        "has_lyrics": bool(d.get("has_lyrics")), "lyric": d.get("lyric"),
        "fav": "♥" if d.get("fav") else "", "ab": ("🔁 A–B " + d["ab"]) if d.get("ab") else "",
        "skin": d.get("skin") or "",
        "cover_file": d.get("cover_file") or "", "cover_hash": d.get("cover_hash") or "",
    }


def _same_song(a, b):
    return (a["title"].casefold() == b["title"].casefold()
            and (not a["artist"] or not b["artist"] or a["artist"].casefold() == b["artist"].casefold()))


# ───────────────────────── чёрный список ─────────────────────────
def is_blacklisted(track, patterns):
    """Строка с \\ / или : — папка/путь; иначе — кусок названия, исполнителя или имени файла."""
    hay = " ".join([track.get("artist", ""), track.get("title", ""),
                    track.get("filename", ""), track.get("app", "")]).lower()
    path = (track.get("path") or "").lower().replace("/", "\\")
    for p in patterns:
        p = p.strip().lower()
        if not p or p.startswith("#"):
            continue
        if "\\" in p or "/" in p or ":" in p:
            if path and p.replace("/", "\\").rstrip("\\") in path:
                return True
        elif p in hay or (path and p in path):
            return True
    return False


# ───────────────────────── обложки ─────────────────────────
_cover_cache = {}


def _norm(s):
    s = re.sub(r"[\(\[].*?[\)\]]", " ", (s or "").casefold())
    return re.sub(r"[^\w]+", " ", s).strip()


def find_cover(artist, title, album=""):
    """Обложка из iTunes. Если известен альбом — берём трек именно из этого альбома."""
    key = (artist.lower(), title.lower(), album.lower())
    if key in _cover_cache:
        return _cover_cache[key]
    url = None
    try:
        q = urllib.parse.quote(f"{artist} {title}".strip())
        with urllib.request.urlopen(
                f"https://itunes.apple.com/search?term={q}&entity=song&limit=15", timeout=5) as r:
            res = [x for x in (json.loads(r.read().decode()).get("results") or [])
                   if x.get("artworkUrl100")]
        alb, tit = _norm(album), _norm(title)
        best = None
        if alb:                                   # тот же альбом
            best = next((x for x in res if alb and (alb in _norm(x.get("collectionName"))
                                                    or _norm(x.get("collectionName")) in alb)), None)
        if not best and tit:                      # то же название песни
            best = next((x for x in res if tit and (tit in _norm(x.get("trackName"))
                                                    or _norm(x.get("trackName")) in tit)), None)
        if not best and not alb and res:
            best = res[0]
        if best:
            url = best["artworkUrl100"].replace("100x100bb", "512x512bb").replace("100x100", "512x512")
    except Exception:
        pass
    _cover_cache[key] = url
    return url


# ───────────────────────── обложки из самого файла ─────────────────────────
# Discord показывает в статусе только картинки по ссылке из интернета.
# Поэтому обложку из MP3/FLAC один раз загружаем на litterbox.catbox.moe (удаляется
# сама через 3 дня) и запоминаем ссылку по хешу картинки.
COVERS_DB = os.path.join(CFG_DIR, "file_covers_v2.json")      # v2: только проверенные ссылки
DISCORD_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"


def _looks_like_image(head):
    return (head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or head[:6] in (b"GIF87a", b"GIF89a"))


def check_image_url(url):
    """Скачать начало картинки так, как это делает Discord. True — картинка доступна."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DISCORD_UA, "Range": "bytes=0-63"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status in (200, 206) and _looks_like_image(r.read(64)):
                    return True
        except Exception:
            pass
        time.sleep(1.5)                          # хостингу нужна пара секунд после загрузки
    return False
UPLOAD_HOSTS = [
    ("https://litterbox.catbox.moe/resources/internals/api.php", {"reqtype": "fileupload", "time": "72h"},
     70 * 3600),
    ("https://catbox.moe/user/api.php", {"reqtype": "fileupload"}, 365 * 24 * 3600),
]


def _multipart(fields, file_field, filename, data, mime):
    boundary = "----mp3cover" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode())
    out.append(data)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class FileCovers:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = set()
        self.last_error = ""
        self.hashes = {}         # (путь, время, размер) -> md5, чтобы не читать файл каждый раз
        try:
            with open(COVERS_DB, "r", encoding="utf-8") as f:
                self.db = json.load(f)
        except Exception:
            self.db = {}

    def _save(self):
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            now = time.time()
            self.db = {k: v for k, v in self.db.items() if v.get("until", 0) > now}
            with open(COVERS_DB, "w", encoding="utf-8") as f:
                json.dump(self.db, f)
        except Exception:
            pass

    def url_for(self, path, key=None):
        """Ссылка на обложку или None (тогда загрузка уже пошла в фоне)."""
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 200:
            return None
        if not key:
            try:
                st = os.stat(path)
                ident = (path, st.st_mtime, st.st_size)
                key = self.hashes.get(ident)
                if not key:
                    with open(path, "rb") as f:
                        key = hashlib.md5(f.read()).hexdigest()
                    self.hashes[ident] = key
            except Exception:
                return None
        with self.lock:
            hit = self.db.get(key)
            if hit and hit.get("until", 0) > time.time():
                return hit["url"]
            if key in self.busy:
                return None
            self.busy.add(key)
        threading.Thread(target=self._upload, args=(path, key), daemon=True).start()
        return None

    def _upload(self, path, key):
        try:
            with open(path, "rb") as f:
                data = f.read()
            # тип — по содержимому (у VLC файл обложки бывает без расширения)
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                ext, mime = ".png", "image/png"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                ext, mime = ".webp", "image/webp"
            else:
                ext, mime = ".jpg", "image/jpeg"
            for url, fields, ttl in UPLOAD_HOSTS:
                try:
                    body, ctype = _multipart(fields, "fileToUpload", "cover" + ext, data, mime)
                    req = urllib.request.Request(url, data=body, method="POST")
                    req.add_header("Content-Type", ctype)
                    req.add_header("User-Agent", "MusicDiscord/1.0")
                    with urllib.request.urlopen(req, timeout=20) as r:
                        link = r.read().decode("utf-8", "replace").strip()
                    if link.startswith("https://") and not check_image_url(link):
                        self.last_error = f"{url.split('/')[2]} не отдаёт картинку Discord"
                        continue
                    if link.startswith("https://"):
                        with self.lock:
                            self.db[key] = {"url": link, "until": time.time() + ttl}
                            self._save()
                        self.last_error = ""
                        return
                    self.last_error = link[:120]
                except Exception as e:
                    self.last_error = f"{url.split('/')[2]}: {e}"
        finally:
            with self.lock:
                self.busy.discard(key)


FILE_COVERS = FileCovers()


# ───────────────────────── Discord IPC ─────────────────────────
COVER_SRC = {"last": ""}   # откуда взята обложка в последнем статусе (для строки состояния)
MP_CACHE = {}        # (client_id, ссылка) -> «mp:external/…», который вернул Discord
BAD_IMAGES = {}      # ссылка -> когда Discord её не принял (10 минут пробуем другую)


def image_ok(url):
    t = BAD_IMAGES.get(url)
    return not t or time.time() - t > 600


# ───────────────────────── своя обложка для трека / альбома ─────────────────────────
def _ckey(*parts):
    return "|".join(re.sub(r"\s+", " ", (p or "").casefold().replace("ё", "е")).strip() for p in parts)


def cover_keys(track):
    """Ключи «трек» и «альбом» для словаря custom_covers."""
    t = "t:" + _ckey(track.get("artist"), track.get("title"))
    a = "a:" + _ckey(track.get("artist"), track.get("album")) if (track.get("album") or "").strip() else None
    return t, a


def custom_cover(cfg, track):
    """(картинка, 'трек'/'альбом') — заданная вручную, или (None, None)."""
    cc = cfg.get("custom_covers") or {}
    t, a = cover_keys(track)
    if cc.get(t):
        return cc[t], "трек"
    if a and cc.get(a):
        return cc[a], "альбом"
    return None, None


class DiscordIPC:
    def __init__(self, client_id, pid=None):
        self.client_id = client_id
        self.pid = pid or os.getpid()      # у второй карточки — другой (живой) процесс
        self.pipe = None

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        else:
            raise ConnectionError("Discord не запущен")
        self._send(0, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if data.get("evt") == "ERROR" or op == 2:
            self.close()
            raise ConnectionError(data.get("data", {}).get("message") or data.get("message")
                                  or "Discord отклонил подключение")

    def _send(self, op, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.pipe.write(struct.pack("<II", op, len(payload)) + payload)

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise ConnectionError("Связь с Discord потеряна")
            buf += chunk
        return buf

    def _recv(self):
        op, length = struct.unpack("<II", self._read(8))
        return op, json.loads(self._read(length).decode("utf-8"))

    def set_activity(self, activity):
        # Ссылку на картинку Discord каждый раз переводит в свой адрес «mp:external/…».
        # При частых обновлениях (текст песни) он не успевает / упирается в лимит —
        # и вместо обложки «?». Поэтому запоминаем готовый адрес и шлём уже его.
        sent = {}
        if activity and activity.get("assets"):
            activity = dict(activity, assets=dict(activity["assets"]))
            for k in ("large_image", "small_image"):
                v = activity["assets"].get(k)
                if isinstance(v, str) and v.startswith("http"):
                    sent[k] = v
        self._send(1, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                       "args": {"pid": self.pid, "activity": activity}})
        while True:                                  # пропустить посторонние события
            op, data = self._recv()
            if data.get("cmd") == "SET_ACTIVITY" or data.get("evt") == "ERROR" or op != 1:
                break
        if data.get("evt") == "ERROR":
            raise RuntimeError(data.get("data", {}).get("message", "ошибка"))
        sent_btns = len((activity or {}).get("buttons") or [])
        echoed = (data.get("data") or {}).get("buttons")
        # Discord возвращает подписи принятых кнопок; свои кнопки он тебе не показывает
        self.buttons_state = ("" if not sent_btns else
                              "ok" if echoed else "unknown" if echoed is None else "rejected")
        got = ((data.get("data") or {}).get("assets") or {})
        for k, url in sent.items():
            if (self.client_id, url) in MP_CACHE:
                continue
            v = got.get(k)
            if isinstance(v, str) and v.startswith("mp:"):
                MP_CACHE[(self.client_id, url)] = v
                BAD_IMAGES.pop(url, None)
            elif got and v != url:                   # Discord ответил, но картинку выкинул
                BAD_IMAGES[url] = time.time()

    def close(self):
        try:
            if self.pipe:
                self.pipe.close()
        except Exception:
            pass
        self.pipe = None


def fit(s, lo=2, hi=128):
    s = (s or "").strip()
    if len(s) < lo:
        s = (s + "  ")[:lo] if s else "—" * lo
    return s[:hi]


# ───────────────────────── текст песни (LRCLIB) ─────────────────────────
def parse_lrc(text):
    """«[01:23.45] строка» → [(83.45, "строка"), …]"""
    out = []
    for line in (text or "").splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", line)
        words = re.sub(r"\[[^\]]*\]", "", line).strip()
        for m, sec in tags:
            out.append((int(m) * 60 + float(sec.replace(":", ".")), words))
    out.sort(key=lambda x: x[0])
    return out


def current_line(lines, pos):
    """Строка, которая звучит на секунде pos. None — до первой строки."""
    cur = None
    for t, words in lines:
        if t <= pos:
            cur = words
        else:
            break
    return cur


class Lyrics:
    """Ищет синхронный текст на lrclib.net в фоне и держит его в памяти."""
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, track):
        if not track.get("title"):
            return None
        key = (track.get("artist", "").casefold(), track["title"].casefold())
        with self.lock:
            if key in self.cache:
                v = self.cache[key]
                return v if isinstance(v, list) else None
            self.cache[key] = "loading"
        threading.Thread(target=self._fetch, args=(key, track.get("artist", ""), track["title"],
                                                   track.get("length") or 0), daemon=True).start()
        return None

    def _fetch(self, key, artist, title, length):
        lines = None
        try:
            params = {"track_name": title, "artist_name": artist} if artist else {"q": title}
            req = urllib.request.Request("https://lrclib.net/api/search?" + urllib.parse.urlencode(params))
            req.add_header("User-Agent", "MusicDiscord/1.0 (github.com)")
            with urllib.request.urlopen(req, timeout=8) as r:
                res = json.loads(r.read().decode("utf-8"))
            synced = [x for x in res if x.get("syncedLyrics") and not x.get("instrumental")]
            if length:
                synced = [x for x in synced if abs((x.get("duration") or 0) - length) <= 8]
                synced.sort(key=lambda x: abs((x.get("duration") or 0) - length))
            if synced:
                lines = parse_lrc(synced[0]["syncedLyrics"]) or None
        except Exception:
            lines = None
        with self.lock:
            self.cache[key] = lines


# ───────────────────────── шаблоны статуса ─────────────────────────
TEMPLATE_KEYS = ("title", "artist", "album", "app", "lyrics", "fav", "ab", "skin")
SEP = r"[—–\-·|/,:]"


class _Blank(dict):
    def __missing__(self, key):
        return ""


def render_tpl(tpl, track):
    """{title} {artist} {album} {app}; пустые части и лишние разделители убираются."""
    vals = _Blank({k: (track.get(k) or "").strip() for k in TEMPLATE_KEYS})
    try:
        out = (tpl or "").format_map(vals)
    except (ValueError, IndexError, KeyError):
        return ""
    out = re.sub(r"\(\s*\)|\[\s*\]|«\s*»|\"\s*\"", "", out)                 # пустые скобки
    out = re.sub(rf"\s*({SEP})(\s*{SEP})+\s*", r" \1 ", out)                    # «— ·» → «—»
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(rf"^(\s|{SEP})+|(\s|{SEP})+$", "", out)


LIST_SHOWS = {"name": 0, "state": 1, "details": 2}

# Значок приложения в углу обложки: имя картинки в Art Assets
APP_ICON_KEYS = {
    "Spotify": "spotify", "Яндекс Музыка": "yandexmusic", "AIMP": "aimp", "foobar2000": "foobar",
    "VLC": "vlc", "Telegram": "telegram", "iTunes": "itunes", "Deezer": "deezer", "VK": "vk",
    "SoundCloud": "soundcloud", "Медиаплеер": "mediaplayer", MP3P_APP: "mp3player",
}
BROWSERS = {"Chrome", "Edge", "Firefox", "Opera", "Яндекс Браузер"}


# Значки по ссылке — работают сразу, ничего не надо загружать в Discord.
APP_DOMAINS = {
    "Spotify": "open.spotify.com", "Яндекс Музыка": "music.yandex.ru", "AIMP": "aimp.ru",
    "foobar2000": "foobar2000.org", "VLC": "videolan.org", "Telegram": "telegram.org",
    "iTunes": "music.apple.com", "Deezer": "deezer.com", "VK": "vk.com",
    "SoundCloud": "soundcloud.com", "Медиаплеер": "microsoft.com",
}
MP3P_ICON = "https://raw.githubusercontent.com/SevereClaw/MP3-Player/main/assets/icon.png"
SITE_ALIASES = {"youtube": "youtube.com", "ютуб": "youtube.com", "twitch": "twitch.tv",
                "vk": "vk.com", "rutube": "rutube.ru", "yandex": "music.yandex.ru",
                "soundcloud": "soundcloud.com", "spotify": "open.spotify.com"}


def favicon_url(domain):
    """Значок сайта (128×128) с серверов Google — прямая ссылка на PNG."""
    return ("https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL"
            f"&url=https://{domain}&size=128")


def app_icon_key(track, cfg, link=None):
    """Картинка для значка в углу: ссылка (по приложению или сайту ролика) или имя из Art Assets."""
    app = track.get("app") or "VLC"
    if app == MP3P_APP:
        return MP3P_ICON
    if app in BROWSERS:
        host = (urllib.parse.urlsplit(link or "").hostname or "").lower()
        if host:
            return favicon_url(host.removeprefix("www.").removeprefix("m."))
        v = (cfg.get("browser_icon") or "youtube").strip().lower()
        if v.startswith("http"):
            return v
        return favicon_url(SITE_ALIASES.get(v, v if "." in v else "youtube.com"))
    if app in APP_DOMAINS:
        return favicon_url(APP_DOMAINS[app])
    return None


def build_activity(track, cfg, lyric=None):
    track = dict(track, lyrics=lyric or "")
    details = render_tpl(cfg.get("fmt_details", "{title}"), track) or track["title"]
    state = render_tpl(cfg.get("fmt_state", "{artist}"), track) or track["artist"] or track.get("app") or "VLC"
    if lyric is not None and cfg.get("lyrics", "off") != "off":
        line = "♪ " + lyric if lyric else "♪ ♪ ♪"
        if cfg["lyrics"] == "details":
            details = line
        else:
            state = line
    large_text = render_tpl(cfg.get("fmt_large", "{album}"), track) or track.get("app") or "VLC"
    act = {
        "type": 2,                    # «Слушает»
        "status_display_type": LIST_SHOWS.get(cfg.get("list_shows", "state"), 1),
        "details": fit(details),
        "state": fit(state),
        "assets": {"large_text": fit(large_text)},
        "instance": False,
    }
    # Сначала iTunes: эти ссылки Discord показывает надёжно. Картинки с временного
    # хостинга у части людей Discord не открывает («?»), поэтому обложка из файла —
    # когда в iTunes такого трека нет (или выбрано «сначала из файла»).
    file_first = cfg.get("cover_order", "itunes") == "file"
    want_web = (cfg.get("covers") or track.get("cover_file")) and (track["artist"] or track["title"])

    def web():
        c = find_cover(track["artist"], track["title"], track.get("album") or "") if want_web else None
        return c if c and image_ok(c) else None

    def from_file():
        if not (cfg.get("file_covers", True) and track.get("cover_file")):
            return None
        c = FILE_COVERS.url_for(track["cover_file"], track.get("cover_hash"))
        return c if c and image_ok(c) else None

    vinfo = VIDEO_LINKS.get(track) if is_video(track) else None
    add_video_link(act, vinfo)
    ytm = YT_MUSIC.get(track) if (cfg.get("ytmusic", True) and not is_video(track)) else None
    if ytm:                                        # название — ссылка на трек в YouTube Music
        add_video_link(act, ytm)
    mine, scope = custom_cover(cfg, track)
    if not mine and vinfo and vinfo.get("thumb"):  # ролик в браузере — превью с YouTube
        mine, scope = vinfo["thumb"], "превью YouTube"
    if mine:                                       # своя картинка — всегда главнее
        act["assets"]["large_image"] = mine
        COVER_SRC["last"] = scope if scope == "превью YouTube" else f"своя ({scope})"
    def from_ytm():
        return ytm["thumb"] if ytm and ytm.get("thumb") and image_ok(ytm["thumb"]) else None

    for get in (() if mine else (from_file, web, from_ytm) if file_first else (web, from_ytm, from_file)):
        cover = get()
        if cover:
            act["assets"]["large_image"] = cover
            COVER_SRC["last"] = ("из файла" if get is from_file else
                                 "YouTube Music" if get is from_ytm else "iTunes")
            break
    else:
        if not mine:
            COVER_SRC["last"] = ""
    fallback = (cfg.get("fallback_image") or "").strip()
    if fallback and "large_image" not in act["assets"]:
        act["assets"]["large_image"] = fallback

    badge = cfg.get("badge", "none")
    small = None
    if badge == "logo":
        small = fallback
    elif badge == "app":
        link = (vinfo or {}).get("url") if is_video(track) else None
        small = app_icon_key(track, cfg, link) or fallback
    if small and act["assets"].get("large_image") and small != act["assets"]["large_image"]:
        act["assets"]["small_image"] = small
        act["assets"]["small_text"] = fit((site_name((vinfo or {}).get("url")) if is_video(track) and vinfo
                                          else track.get("app")) or "VLC")

    if not track["playing"]:
        act["state"] = fit("⏸ На паузе · " + state)
    elif track["length"] > 0:
        start = int(time.time()) - track["time"]
        act["timestamps"] = {"start": start * 1000, "end": (start + track["length"]) * 1000}

    query = urllib.parse.quote(f"{track['artist']} {track['title']}".strip())
    btns = []
    for bid in cfg.get("buttons", []):
        if bid in BUTTONS and bid != "none" and len(btns) < 2:
            _, label, url = BUTTONS[bid]
            btns.append({"label": label[:32], "url": url.format(q=query)[:512]})
    if btns:
        act["buttons"] = btns
    if is_video(track):
        as_watching(act, vinfo)
    if not act["assets"]:
        del act["assets"]
    return act


MULTI_MODES = {
    "auto": "То, что играет (как раньше)",
    "music": "Музыку — видео, только если музыки нет",
    "video": "Видео (YouTube, браузер) — музыку, только если видео нет",
    "both": "Обе карточки: «Слушает» и «Смотрит»",
}


def is_video(track):
    """Браузер (YouTube и т.п.) — считаем видео; плееры и VLC — музыкой."""
    return track.get("app") in BROWSERS


# ───────────────────────── ссылка на видео и превью YouTube ─────────────────────────
# Браузер сообщает Windows только название и канал. Ссылку берём из открытых вкладок
# Firefox (файл сессии), а если не вышло — ищем на YouTube по названию. Превью —
# прямо с серверов YouTube (i.ytimg.com): ничего никуда загружать не нужно.
_YT_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")


def _norm_t(s):
    s = re.sub(r"^\(\d+\)\s*", "", (s or "")).casefold()
    s = re.sub(r"\s*[-–—]\s*youtube( music)?\s*$", "", s)
    return re.sub(r"[^\w]+", " ", s).strip()


def mozlz4_decode(data):
    """Файлы сессии Firefox (*.jsonlz4): «mozLz40\\0» + размер + блок LZ4."""
    if data[:8] != b"mozLz40\0":
        raise ValueError("не mozlz4")
    size = struct.unpack("<I", data[8:12])[0]
    src = data[12:]
    try:
        import lz4.block
        return lz4.block.decompress(src, uncompressed_size=size)
    except ImportError:
        pass
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            for k in range(ml):
                out.append(out[start + k])
    return bytes(out)


_FF_CACHE = {"path": None, "mtime": 0, "tabs": []}


def firefox_tabs():
    """[(заголовок, адрес)] открытых вкладок Firefox (все профили; самый свежий файл)."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox", "Profiles")
    best = None
    try:
        for prof in os.listdir(base):
            for name in ("recovery.jsonlz4", "previous.jsonlz4"):
                p = os.path.join(base, prof, "sessionstore-backups", name)
                if os.path.isfile(p):
                    m = os.path.getmtime(p)
                    if not best or m > best[1]:
                        best = (p, m)
                    break
    except OSError:
        return []
    if not best:
        return []
    if _FF_CACHE["path"] == best[0] and _FF_CACHE["mtime"] == best[1]:
        return _FF_CACHE["tabs"]
    with open(best[0], "rb") as f:
        js = json.loads(mozlz4_decode(f.read()).decode("utf-8", "replace"))
    tabs = []
    for w in js.get("windows") or []:
        for t in w.get("tabs") or []:
            ent = t.get("entries") or []
            idx = int(t.get("index") or len(ent)) - 1
            if 0 <= idx < len(ent):
                tabs.append((ent[idx].get("title") or "", ent[idx].get("url") or ""))
    _FF_CACHE.update(path=best[0], mtime=best[1], tabs=tabs)
    return tabs


def youtube_search(title, channel=""):
    """ID первого подходящего видео в поиске YouTube или None."""
    q = urllib.parse.quote(f"{title} {channel}".strip())
    req = urllib.request.Request(f"https://www.youtube.com/results?search_query={q}&hl=ru",
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                                          "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=8) as r:
        html = r.read().decode("utf-8", "replace")
    want = _norm_t(title)
    first = None
    for m in re.finditer(r'"videoRenderer":\{"videoId":"([\w-]{11})".{0,1500}?"title":\{"runs":\[\{"text":"(.*?)"\}',
                         html):
        vid = m.group(1)
        try:
            t = json.loads('"' + m.group(2) + '"')
        except Exception:
            t = m.group(2)
        first = first or vid
        nt = _norm_t(t)
        if want and (want in nt or nt in want):
            return vid
    return first


def best_thumb(vid):
    """Самое чёткое превью без чёрных полос, какое есть у ролика."""
    for name in ("maxresdefault.jpg", "hq720.jpg"):
        url = f"https://i.ytimg.com/vi/{vid}/{name}"
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": DISCORD_UA})
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return url
        except Exception:
            continue
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


class VideoLinks:
    """Ссылка и превью для ролика в браузере; ищет в фоне, отдаёт из кэша."""
    def __init__(self):
        self.cache = {}          # (заголовок, канал) -> {"url", "thumb", "src"} или {} (не нашли)
        self.busy = set()
        self.lock = threading.Lock()
        self.status = ""

    def get(self, track):
        key = (track.get("title") or "", track.get("artist") or "")
        if not key[0]:
            return None
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None:
                if hit.get("url"):
                    return hit
                if time.time() - hit.get("_t", 0) < 120:      # не нашли — повторим через 2 минуты
                    return None
            if key in self.busy:
                return None
            self.busy.add(key)
        threading.Thread(target=self._find, args=(key, track.get("app") or ""), daemon=True).start()
        return None

    def _find(self, key, app):
        title, channel = key
        info, err = {}, ""
        try:
            if app == "Firefox":
                want = _norm_t(title)
                for t, url in firefox_tabs():
                    nt = _norm_t(t)
                    if url.startswith("http") and want and (want in nt or nt == want):
                        info = {"url": url, "src": "вкладка Firefox"}
                        break
        except Exception as e:
            err = f"Firefox: {type(e).__name__}"
        vid = None
        if info.get("url"):
            m = _YT_ID.search(info["url"])
            vid = m.group(1) if m else None
        if not info:
            try:
                vid = youtube_search(title, channel)
                if vid:
                    info = {"url": f"https://www.youtube.com/watch?v={vid}", "src": "поиск YouTube"}
            except Exception as e:
                err = (err + "; " if err else "") + f"поиск YouTube: {type(e).__name__}"
        if vid:
            info["thumb"] = best_thumb(vid)
        self.status = err
        with self.lock:
            self.cache[key] = info if info else {"_t": time.time()}
            self.busy.discard(key)
            if len(self.cache) > 300:
                self.cache.clear()


VIDEO_LINKS = VideoLinks()


# ───────────────────────── YouTube Music: обложка и ссылка на трек ─────────────────────────
YTM_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
YTM_SONGS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"          # фильтр «Песни»
YTM_CTX = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20241023.01.00",
                      "hl": "ru", "gl": "RU"}}


def _walk(o, key):
    """Все словари o[...][key] в дереве JSON."""
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if key in x:
                yield x[key]
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def _runs(col):
    try:
        return [r.get("text", "") for r in col["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]]
    except Exception:
        return []


def _dur(text):
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d{2})", (text or "").strip())
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def ytm_parse(js):
    """Ответ поиска → [{'id','title','artist','album','dur','thumb'}]."""
    out = []
    for r in _walk(js, "musicResponsiveListItemRenderer"):
        cols = r.get("flexColumns") or []
        title = "".join(_runs(cols[0])) if cols else ""
        sub = _runs(cols[1]) if len(cols) > 1 else []
        vid = next((v for v in _walk(r, "videoId") if isinstance(v, str) and len(v) == 11), None)
        if not vid or not title:
            continue
        parts = [x.strip() for x in "".join(sub).split("•")]
        if parts and parts[0].casefold() in ("песня", "song", "видео", "video", "клип"):
            parts = parts[1:]
        thumbs = next(iter(_walk(r, "thumbnails")), None) or []
        thumb = thumbs[-1]["url"] if thumbs and isinstance(thumbs[-1], dict) else ""
        thumb = re.sub(r"=w\d+-h\d+[^/]*$", "=w544-h544-l90-rj", thumb)
        out.append({"id": vid, "title": title, "artist": parts[0] if parts else "",
                    "album": parts[1] if len(parts) > 2 else "",
                    "dur": _dur(parts[-1]) if parts else 0, "thumb": thumb})
    return out


def ytm_search(query, songs=True, timeout=8):
    body = {"context": YTM_CTX, "query": query}
    if songs:
        body["params"] = YTM_SONGS
    req = urllib.request.Request(YTM_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                                          "Origin": "https://music.youtube.com",
                                          "Referer": "https://music.youtube.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ytm_parse(json.loads(r.read().decode("utf-8")))


YTM_EXTRA = ("live", "концерт", "remix", "ремикс", "cover", "кавер", "instrumental", "минус",
             "karaoke", "караоке", "sped up", "slowed", "nightcore", "8d")


def core_title(title):
    """«Песня -opening movie version» / «Песня (Live)» → «Песня»."""
    t = re.split(r"\s+[-–—]\s*|\s*[\(\[]", title or "", maxsplit=1)[0]
    return t.strip() or (title or "")


def ytm_best(items, title, artist, length=0, album="", loose=False):
    """Самый похожий трек: название, исполнитель, альбом, длительность.
    loose — сравнивать только основу названия (без приписок в скобках и после « - »)."""
    want_t = _norm_t(core_title(title) if loose else title)
    want_a, want_al = _norm_t(artist), _norm_t(album)
    asked = (title or "").casefold()
    best, score = None, -1
    for it in items:
        nt, na = _norm_t(it["title"]), _norm_t(it["artist"])
        if not want_t or not (want_t in nt or nt in want_t):
            continue
        sc = 2
        if want_a and (want_a in na or na in want_a):
            sc += 3
        if want_al and want_al in _norm_t(it.get("album")):
            sc += 2
        low = it["title"].casefold() + " " + (it.get("album") or "").casefold()
        if any(w in low and w not in asked for w in YTM_EXTRA):
            sc -= 3                           # концерт / ремикс / кавер, а просили не их
        if length and it["dur"]:
            d = abs(it["dur"] - length)
            if d > 20:
                continue                      # другая версия
            sc += 2 if d <= 3 else 1
        if sc > score:
            best, score = it, sc
    return best


class YTMusic:
    """Трек на YouTube Music: обложка (квадратная) и ссылка. Ищет в фоне, отдаёт из кэша."""
    def __init__(self):
        self.cache, self.busy = {}, set()
        self.lock = threading.Lock()
        self.status = ""

    def get(self, track):
        title, artist = (track.get("title") or "").strip(), (track.get("artist") or "").strip()
        if not title:
            return None
        key = (artist.casefold(), title.casefold())
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None:
                if hit.get("id"):
                    return hit
                if time.time() - hit.get("_t", 0) < 600:   # не нашли — повторим через 10 минут
                    return None
            if key in self.busy:
                return None
            self.busy.add(key)
        threading.Thread(target=self._find, args=(key, title, artist, int(track.get("length") or 0),
                                                  (track.get("album") or "").strip()),
                         daemon=True).start()
        return None

    def pending(self, track):
        return ((track.get("artist") or "").strip().casefold(),
                (track.get("title") or "").strip().casefold()) in self.busy

    def _find(self, key, title, artist, length, album=""):
        found = None
        try:
            q = f"{artist} {title}".strip()
            res = ytm_search(q)
            found = ytm_best(res, title, artist, length, album)
            if not found:                                  # без фильтра «Песни»
                res2 = ytm_search(q, songs=False)
                found = ytm_best(res2, title, artist, length, album)
            if not found and core_title(title) != title:   # по основе названия, без приписок
                res3 = ytm_search(f"{artist} {core_title(title)}".strip())
                found = (ytm_best(res3, title, artist, length, album, loose=True)
                         or ytm_best(res + res3, title, artist, length, album, loose=True))
            self.status = ""
        except Exception as e:
            self.status = f"{type(e).__name__}"
        with self.lock:
            if found:
                found["url"] = f"https://music.youtube.com/watch?v={found['id']}"
                self.cache[key] = found
            else:
                self.cache[key] = {"_t": time.time()}
            self.busy.discard(key)
            if len(self.cache) > 500:
                self.cache.clear()


YT_MUSIC = YTMusic()


SITE_NAMES = [("youtube.com", "YouTube"), ("youtu.be", "YouTube"), ("twitch.tv", "Twitch"),
              ("vkvideo.ru", "VK Видео"), ("vk.com", "VK Видео"), ("rutube.ru", "Rutube"),
              ("kick.com", "Kick"), ("netflix.com", "Netflix"), ("anilib", "AniLib"),
              ("animego", "AnimeGO"), ("kinopoisk.ru", "Кинопоиск"), ("ok.ru", "OK Видео"),
              ("dzen.ru", "Дзен"), ("tiktok.com", "TikTok"), ("bilibili", "Bilibili")]


def site_name(url):
    """«Смотрит YouTube» — название сайта по ссылке, иначе просто «видео»."""
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return next((n for dom, n in SITE_NAMES if dom in host), "видео")


def as_watching(act, info):
    """Карточка ролика из браузера: «Смотрит YouTube» вместо «Слушает <приложение>»."""
    act["type"] = 3
    act["name"] = site_name((info or {}).get("url"))
    act["status_display_type"] = 2          # в списке участников — название ролика


def add_video_link(act, info):
    """Название и картинка карточки — ссылка на видео (кликается в профиле Discord)."""
    if not info or not info.get("url"):
        return
    act["details_url"] = info["url"][:256]
    act.setdefault("assets", {})["large_url"] = info["url"][:256]


def build_video_activity(track, cfg, thumb=None):
    title, channel = track["title"], track["artist"]
    act = {
        "type": 3,                    # «Смотрит»
        "status_display_type": 2,     # в списке участников — название ролика
        "details": fit(title),
        "state": fit(channel or track.get("app") or "YouTube"),
        "assets": {"large_text": fit(track.get("app") or "YouTube")},
        "instance": False,
    }
    info = VIDEO_LINKS.get(track)
    img = (info or {}).get("thumb") or thumb or (cfg.get("video_image") or "").strip()
    if img:
        act["assets"]["large_image"] = img
    add_video_link(act, info)
    as_watching(act, info)
    if cfg.get("badge", "none") == "app" and act["assets"].get("large_image"):
        act["assets"]["small_image"] = app_icon_key(track, cfg, (info or {}).get("url"))
        act["assets"]["small_text"] = fit(act["name"] if act.get("name") != "видео" else track.get("app") or "")
    if not track["playing"]:
        act["state"] = fit("⏸ На паузе · " + (channel or track.get("app") or ""))
    elif track["length"] > 0:
        start = int(time.time()) - track["time"]
        act["timestamps"] = {"start": start * 1000, "end": (start + track["length"]) * 1000}
    q = urllib.parse.quote(f"{channel} {title}".strip())
    link = (info or {}).get("url") or f"https://www.youtube.com/results?search_query={q}"
    act["buttons"] = [{"label": "▶ Смотреть" if info else "▶ Найти на YouTube", "url": link[:512]}]
    return act


# ───────────────────────── история прослушиваний ─────────────────────────
DB_PATH = os.path.join(CFG_DIR, "history.db")


def needed_seconds(length):
    """Правило Last.fm: трек засчитан, если прослушана половина или 4 минуты."""
    if length and length >= 30:
        return min(length / 2, 240)
    return 30


class History:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS plays(
            id INTEGER PRIMARY KEY, ts INTEGER, artist TEXT, title TEXT, album TEXT, app TEXT,
            seconds INTEGER, length INTEGER, akey TEXT, tkey TEXT, lastfm INTEGER DEFAULT -1)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts)")
        self.db.commit()

    def add(self, track, seconds, ts, lastfm_state):
        artist, title = track["artist"].strip(), track["title"].strip()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO plays(ts, artist, title, album, app, seconds, length, akey, tkey, lastfm)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(ts), artist, title, track.get("album", ""), track.get("app", ""), int(seconds),
                 int(track.get("length") or 0), artist.casefold(), title.casefold(), lastfm_state))
            self.db.commit()
            return cur.lastrowid

    def _q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def totals(self, since):
        r = self._q("SELECT COUNT(*), COALESCE(SUM(seconds),0), COUNT(DISTINCT NULLIF(akey,''))"
                    " FROM plays WHERE ts>=?", (since,))
        return r[0]

    def top_artists(self, since, n=5):
        return self._q("SELECT MAX(artist), COUNT(*) FROM plays WHERE ts>=? AND akey<>''"
                       " GROUP BY akey ORDER BY 2 DESC, MAX(ts) DESC LIMIT ?", (since, n))

    def top_tracks(self, since, n=5):
        return self._q("SELECT MAX(artist), MAX(title), COUNT(*) FROM plays WHERE ts>=?"
                       " GROUP BY akey, tkey ORDER BY 3 DESC, MAX(ts) DESC LIMIT ?", (since, n))

    def pending_lastfm(self, n=50):
        return self._q("SELECT id, ts, artist, title, album, length FROM plays"
                       " WHERE lastfm=0 ORDER BY ts LIMIT ?", (n,))

    def mark_lastfm(self, ids, state):
        with self.lock:
            self.db.executemany("UPDATE plays SET lastfm=? WHERE id=?", [(state, i) for i in ids])
            self.db.commit()

    def export_csv(self, path):
        rows = self._q("SELECT ts, artist, title, album, app, seconds FROM plays ORDER BY ts")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Дата", "Исполнитель", "Трек", "Альбом", "Приложение", "Слушал, сек"])
            for ts, *rest in rows:
                w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), *rest])
        return len(rows)


# ───────────────────────── Last.fm ─────────────────────────
class LastFMError(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


def lastfm_call(key, secret, method, params=None, sk=None, post=True):
    p = {k: str(v) for k, v in (params or {}).items() if v not in (None, "")}
    p.update(method=method, api_key=key)
    if sk:
        p["sk"] = sk
    sig = "".join(f"{k}{p[k]}" for k in sorted(p)) + secret
    p["api_sig"] = hashlib.md5(sig.encode("utf-8")).hexdigest()
    p["format"] = "json"
    url = "https://ws.audioscrobbler.com/2.0/"
    data = urllib.parse.urlencode(p).encode("utf-8")
    req = urllib.request.Request(url, data=data) if post else urllib.request.Request(url + "?" + data.decode())
    req.add_header("User-Agent", "MusicDiscord/1.0")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise LastFMError(f"HTTP {e.code}")
    if "error" in res:
        raise LastFMError(res.get("message", "ошибка Last.fm"), res["error"])
    return res


class LastFM:
    """Скробблинг в фоне. Неотправленное копится в истории и уходит при первой возможности."""
    def __init__(self, cfg, history):
        self.cfg, self.history = cfg, history
        self.jobs = queue.Queue()
        self.last_error = ""
        threading.Thread(target=self._loop, daemon=True).start()

    def enabled(self):
        c = self.cfg
        return bool(c.get("lastfm_on") and c.get("lastfm_key") and c.get("lastfm_secret")
                    and c.get("lastfm_session"))

    def _call(self, method, params):
        c = self.cfg
        return lastfm_call(c["lastfm_key"], c["lastfm_secret"], method, params, c["lastfm_session"])

    def _loop(self):
        while True:
            try:
                job = self.jobs.get(timeout=60)
            except queue.Empty:
                job = ("flush",)
            if not self.enabled() or not self.history:
                continue
            try:
                if job[0] == "now":
                    t = job[1]
                    self._call("track.updateNowPlaying", {
                        "artist": t["artist"], "track": t["title"], "album": t.get("album"),
                        "duration": t.get("length") or None})
                else:
                    self._flush()
                self.last_error = ""
            except LastFMError as e:
                self.last_error = str(e)
                if e.code == 9:                       # сессия больше не действует
                    self.cfg["lastfm_session"] = ""
            except Exception as e:
                self.last_error = str(e)              # нет сети — попробуем позже

    def _flush(self):
        while True:
            rows = self.history.pending_lastfm(50)
            if not rows:
                return
            good = [r for r in rows if r[2] and r[3]]
            bad = [r[0] for r in rows if not (r[2] and r[3])]
            if bad:
                self.history.mark_lastfm(bad, -1)     # без исполнителя Last.fm не примет
            if not good:
                continue
            params = {}
            for i, (_, ts, artist, title, album, length) in enumerate(good):
                params[f"artist[{i}]"], params[f"track[{i}]"] = artist, title
                params[f"timestamp[{i}]"] = ts
                if album:
                    params[f"album[{i}]"] = album
                if length:
                    params[f"duration[{i}]"] = length
            self._call("track.scrobble", params)
            self.history.mark_lastfm([r[0] for r in good], 1)

    def now_playing(self, track):
        if self.enabled() and track.get("artist"):
            self.jobs.put(("now", dict(track)))

    def flush(self):
        self.jobs.put(("flush",))


# ───────────────────────── глобальная горячая клавиша ─────────────────────────
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(text):
    mods, vk = 0, None
    for part in [p.strip().lower() for p in text.split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif re.fullmatch(r"f([1-9]|1[0-2])", part):
            vk = 0x6F + int(part[1:])
        elif len(part) == 1 and (part.isascii() and part.isalnum()):
            vk = ord(part.upper())
        else:
            raise ValueError(f"Непонятная клавиша «{part}» — нужна латинская буква, цифра или F1–F12")
    if vk is None or not mods:
        raise ValueError("Нужно сочетание, например Ctrl+Alt+H")
    return mods, vk


class Hotkey(threading.Thread):
    def __init__(self, combo, on_press):
        super().__init__(daemon=True)
        self.combo, self.on_press = combo, on_press
        self.tid = None
        self.result = queue.Queue()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        try:
            mods, vk = parse_hotkey(self.combo)
        except ValueError as e:
            self.result.put(str(e))
            return
        if not user32.RegisterHotKey(None, 1, mods | 0x4000, vk):
            self.result.put("Это сочетание уже занято другой программой")
            return
        self.result.put(None)
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:          # WM_HOTKEY
                self.on_press()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)   # WM_QUIT


# ───────────────────────── рабочий поток ─────────────────────────
class Worker(threading.Thread):
    def __init__(self, cfg, out_q, history=None, lastfm=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = out_q
        self.history = history
        self.lastfm = lastfm
        self.play = None               # текущее прослушивание для истории
        self.stop_ev = threading.Event()
        self.hidden = False            # режим «невидимки»
        self.ipc = None
        self.shown = False
        self.last_key = None
        self.last_start = None
        self.path_cache = (None, "")
        self.lyrics = Lyrics()
        self.pending_key, self.pending_since = None, 0.0   # защита от пролистывания
        self.last_set, self.last_lyric = 0.0, None
        self.last_src = None
        self.sessions = []
        # вторая карточка — «Смотрит» (режим «обе карточки»)
        self.vipc, self.v_key, self.v_start, self.v_shown = None, None, None, False
        self.v_note = ""

    def status(self, state, text, track=None):
        self.q.put({"state": state, "text": text, "track": track, "sessions": self.sessions})

    def read_vlc(self):
        try:
            st = vlc_http(self.cfg["vlc_port"], self.cfg["vlc_password"], "status.json")
        except Exception:
            return vlc_window_title(), False
        track = parse_http(st)
        if track:
            ident = (track["filename"], track["title"], track["artist"])
            if self.path_cache[0] != ident:
                try:
                    path = vlc_current_path(self.cfg["vlc_port"], self.cfg["vlc_password"])
                except Exception:
                    path = ""
                self.path_cache = (ident, path)
            track["path"] = self.path_cache[1]
        return track, True

    def run(self):
        if HAS_SMTC:
            try:
                ctypes.windll.combase.RoInitialize(1)   # WinRT в этом потоке (MTA)
            except Exception:
                pass
        while not self.stop_ev.is_set():
            try:
                self.tick()
            except Exception as e:
                if self.ipc:
                    self.ipc.close()
                self.ipc, self.shown, self.last_key = None, False, None
                self.status("error", f"{e} — повтор через 5 с")
                self.stop_ev.wait(5)
                continue
            self.stop_ev.wait(1.5)
        self.clear()

    def hide_activity(self):
        if self.ipc and self.shown:
            self.ipc.set_activity(None)
        self.shown, self.last_key = False, None

    def collect(self):
        """Все источники, где сейчас что-то играет или стоит на паузе."""
        mode = self.cfg.get("source_mode", "auto")
        cands, http_ok = [], False
        if mode in ("auto", "vlc"):
            vt, http_ok = self.read_vlc()
            if vt:
                cands.append(vt)
        mp3 = read_mp3player() if mode in ("auto", "system") else None
        if mp3:
            cands.append(mp3)
        if mode in ("auto", "system"):
            have_vlc = any(c["src"] == "vlc" for c in cands)
            for t in smtc_sessions():
                if have_vlc and t["app"] == "VLC":      # VLC 4 виден и там, и там
                    continue
                # MP3 Плеер виден и в медиасессиях Windows — берём только прямой канал
                if mp3 and ("mp3player" in t["src"].lower() or _same_song(t, mp3)):
                    continue
                cands.append(t)
        ignore = [a.strip().lower() for a in self.cfg.get("ignore_apps", []) if a.strip()]
        cands = [c for c in cands
                 if not any(i in c["app"].lower() or i in c["src"].lower() for i in ignore)]
        return cands, http_ok

    def pick(self, cands):
        """Что показывать: то, что играет; при равенстве — то, что уже было в статусе."""
        def rank(c):
            r = 100 if c["playing"] else 0
            if c["src"] == self.last_src:
                r += 50
            if c.get("via") in ("http", "mp3player"):     # точные данные — приоритет
                r += 20
            if c.get("via") == "title":            # по заголовку окна паузу не видно
                r -= 60
            if c.get("current"):
                r += 10
            return r
        return max(cands, key=rank) if cands else None

    def account(self, track):
        """Считает, сколько реально послушано, и записывает трек в историю."""
        now = time.monotonic()
        key = (track["src"], track["artist"].casefold(), track["title"].casefold()) if track else None
        p = self.play
        restarted = (p and key == p["key"] and track["length"] > 0
                     and track["time"] + 10 < p["pos"])          # тот же трек пошёл заново
        if not p or key != p["key"] or restarted:
            self.play = None
            if track:
                self.play = {"key": key, "pos": track["time"], "last": now, "listened": 0.0,
                             "logged": False, "started": time.time()}
                if track["playing"] and self.lastfm:
                    self.lastfm.now_playing(track)
            return
        step = now - p["last"]
        p["last"], p["pos"] = now, track["time"]
        if track["playing"] and step < 10:
            p["listened"] += step
        if not p["logged"] and p["listened"] >= needed_seconds(track["length"]):
            p["logged"] = True
            if self.history:
                lf = 0 if (self.lastfm and self.lastfm.enabled() and track["artist"]) else -1
                self.history.add(track, p["listened"], p["started"], lf)
                self.q.put({"cmd": "played"})
                if lf == 0:
                    self.lastfm.flush()

    def tick(self):
        cands, http_ok = self.collect()
        if not self.cfg.get("show_paused"):
            cands = [c for c in cands if c["playing"]]
        self.sessions = [(c["app"], c["artist"], c["title"], c["playing"]) for c in cands]

        blacklist = self.cfg.get("blacklist", [])
        visible = [c for c in cands if not is_blacklisted(c, blacklist)]

        # музыка и видео одновременно — что показывать
        multi = self.cfg.get("multi", "auto")
        has_vid = bool((self.cfg.get("client_id_video") or "").strip())
        both = multi == "both" and has_vid
        music = [c for c in visible if not is_video(c)]
        video = [c for c in visible if is_video(c)]
        vid_card = None                      # что показать карточкой «Смотрит»
        if both:
            main, vid_card = music, (self.pick(video) if video else None)
        elif multi == "music":
            main = music or video
        elif multi == "video" and video and has_vid:
            main, vid_card = [], self.pick(video)        # только видео — честное «Смотрит YouTube»
        elif multi == "video":
            main = video or music
        else:
            main = visible
        self.tick_video(vid_card, has_vid and multi in ("both", "video"))
        track = self.pick(main)
        self.account(track)            # в историю — даже в режиме невидимки

        if not track:
            self.hide_activity()
            if self.v_shown:
                self.status("live", ("Показываю видео" if self.cfg.get("multi") == "video"
                                     else "Музыки нет") + self.v_note, None)
            elif cands and not main and visible:
                self.status("idle", "Сейчас только видео — его карточка ниже" + self.v_note)
            elif cands:
                self.status("hidden", "В чёрном списке — не показываю", self.pick(cands))
            else:
                self.status("idle", "Ничего не играет")
            return

        if self.hidden:
            self.hide_activity()
            self.status("hidden", "Невидимка: статус скрыт", track)
            return

        # Защита от пролистывания: новый трек ждёт show_delay секунд,
        # а в Discord пока остаётся предыдущий.
        now = time.monotonic()
        tkey = (track["src"], track["title"], track["artist"])
        if tkey != self.pending_key:
            self.pending_key, self.pending_since = tkey, now
        try:
            delay = max(0.0, float(self.cfg.get("show_delay") or 0))
        except (TypeError, ValueError):
            delay = 0.0
        shown_now = self.last_key[:3] if self.last_key else None
        waited = now - self.pending_since
        if delay and tkey != shown_now and waited < delay:
            left = int(delay - waited + 0.99)
            self.status("wait", f"Новый трек — покажу через {left} с, если не пролистаешь", track)
            return

        if not self.ipc:
            self.ipc = DiscordIPC(self.cfg["client_id"])
            self.ipc.connect()

        lyric = None
        if self.cfg.get("lyrics", "off") != "off" and track["playing"] and track.get("has_lyrics"):
            lyric = track.get("lyric") or ""              # строка из .lrc рядом с треком (MP3 Плеер)
        elif self.cfg.get("lyrics", "off") != "off" and track["playing"] and track["length"] > 0:
            lines = self.lyrics.get(track)
            if lines:
                lyric = current_line(lines, track["time"] + 0.8)

        file_cover = None
        if self.cfg.get("file_covers", True) and track.get("cover_file"):
            file_cover = FILE_COVERS.url_for(track["cover_file"], track.get("cover_hash"))
        key = (track["src"], track["title"], track["artist"], track["playing"], file_cover,
               bool(file_cover) and image_ok(file_cover), self.cfg.get("cover_rev", 0),
               bool(VIDEO_LINKS.get(track)) if is_video(track) else
               bool(YT_MUSIC.get(track)) if self.cfg.get("ytmusic", True) else None)
        start = int(time.time()) - track["time"]
        seeked = (track["length"] > 0 and self.last_start is not None
                  and abs(start - self.last_start) > 3)
        lyric_due = lyric != self.last_lyric and now - self.last_set >= 4.2   # лимит Discord
        if key != self.last_key or seeked or lyric_due:
            self.ipc.set_activity(build_activity(track, self.cfg, lyric))
            self.shown, self.last_key, self.last_start = True, key, start
            self.last_src = track["src"]
            self.last_set, self.last_lyric = now, lyric
        note = " · без веб-интерфейса VLC" if track.get("via") == "title" else ""
        if self.last_lyric:
            note += f"\n♪ {self.last_lyric}"
        if is_video(track):
            vi = VIDEO_LINKS.get(track)
            note += (f" · превью и ссылка ✓ ({vi.get('src')})" if vi else
                     " · ищу ссылку на видео…" if (track["title"], track["artist"]) in VIDEO_LINKS.busy else
                     " · ссылку не нашёл" + (f" ({VIDEO_LINKS.status[:40]})" if VIDEO_LINKS.status else ""))
        if COVER_SRC["last"].startswith("своя") and not is_video(track):
            note += f" · обложка: {COVER_SRC['last']} ✓"
        elif COVER_SRC["last"] in ("iTunes", "YouTube Music") and not is_video(track):
            note += f" · обложка: {COVER_SRC['last']} ✓"
        if not is_video(track) and self.cfg.get("ytmusic", True):
            if YT_MUSIC.get(track):
                note += " · ссылка YouTube Music ✓"
            elif YT_MUSIC.pending(track):
                note += " · ищу на YouTube Music…"
            elif YT_MUSIC.status:
                note += f" · YouTube Music: нет связи ({YT_MUSIC.status})"
        elif track.get("cover_file") and self.cfg.get("file_covers", True) and not is_video(track):
            note += " · " + ("превью" if is_video(track) else "обложка из файла") + (
                (" ✓" if image_ok(file_cover) else " ✗ Discord не принял") if file_cover else
                (" — загружаю…" if not FILE_COVERS.last_error
                 else f" — не вышло ({FILE_COVERS.last_error[:40]})"))
        bs = getattr(self.ipc, "buttons_state", "") if self.ipc else ""
        if bs == "ok":
            note += " · кнопки ✓ (видят друзья, не ты)"
        elif bs == "rejected":
            note += " · кнопки Discord не принял"
        note += self.v_note
        self.status("live" if track["playing"] else "paused",
                    ("В эфире" if track["playing"] else "На паузе") + f" · {track['app']}" + note,
                    track)

    def tick_video(self, track, enabled):
        """Карточка «Смотрит …» через второе приложение Discord."""
        try:
            if not enabled or track is None or self.hidden:
                if self.vipc and self.v_shown:
                    self.vipc.set_activity(None)
                self.v_shown, self.v_key = False, None
                if not enabled and self.vipc:
                    self.vipc.close()
                    self.vipc = None
                self.v_note = ""
                return
            if not self.vipc:
                ppid = os.getppid() if hasattr(os, "getppid") else 0
                self.vipc = DiscordIPC(self.cfg["client_id_video"].strip(),
                                       pid=ppid if ppid and ppid != os.getpid() else os.getpid() + 1)
                self.vipc.connect()
            thumb = None
            if track.get("cover_file"):
                thumb = FILE_COVERS.url_for(track["cover_file"])
            start = int(time.time()) - track["time"]
            vinfo = VIDEO_LINKS.get(track)
            key = (track["src"], track["title"], track["artist"], track["playing"], thumb, bool(vinfo))
            seeked = track["length"] > 0 and self.v_start is not None and abs(start - self.v_start) > 3
            if key != self.v_key or seeked:
                self.vipc.set_activity(build_video_activity(track, self.cfg, thumb))
                self.v_key, self.v_start, self.v_shown = key, start, True
            if vinfo:
                pv = f"превью и ссылка ✓ ({vinfo.get('src')})"
            elif track["title"] in {k[0] for k in VIDEO_LINKS.busy}:
                pv = "ищу ссылку на видео…"
            elif thumb:
                pv = "превью ✓"
            elif track.get("cover_file"):
                pv = "превью: загружаю…" if not FILE_COVERS.last_error else f"превью: {FILE_COVERS.last_error[:40]}"
            else:
                pv = "превью: " + (THUMB_STATUS["error"][:50] or "нет")
            self.v_note = f"\n👁 Смотрит: {track['title'][:36]} · {pv}"
        except Exception as e:
            if self.vipc:
                self.vipc.close()
            self.vipc, self.v_key, self.v_shown = None, None, False
            self.v_note = f"\n👁 Видео-карточка: {e}"[:90]

    def clear(self):
        try:
            self.hide_activity()
        except Exception:
            pass
        try:
            self.tick_video(None, False)
        except Exception:
            pass
        if self.ipc:
            self.ipc.close()

    def stop(self):
        self.stop_ev.set()


# ───────────────────────── автозапуск ─────────────────────────
def set_autostart(on):
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --minimized'
    else:
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        cmd = f'"{pyw}" "{os.path.abspath(__file__)}" --minimized'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "VLCDiscord", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "VLCDiscord")
            except FileNotFoundError:
                pass


# ───────────────────────── оформление ─────────────────────────
# Палитра в духе меню Persona 2: ночная синева, бабочка, кремовый текст, алый акцент.
C = {
    "bg": "#060a18", "panel": "#0d1430", "panel2": "#131c42", "line": "#2b3b7a",
    "cream": "#efe5cc", "muted": "#8b93b6", "blue": "#3fa9f5", "glow": "#9fd8ff",
    "red": "#c62a3e", "gold": "#c9a75a",
}
SERIF = "Palatino Linotype"
SANS = "Segoe UI"


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def draw_butterfly(cv, cx, cy, s=1.0, fill=None, outline=None):
    fill, outline = fill or C["blue"], outline or C["glow"]
    def P(pts):
        return [v for x, y in pts for v in (cx + x * s, cy + y * s)]
    up = [(0, 0), (-6, -14), (-20, -22), (-28, -14), (-24, -3), (-10, 1)]
    lo = [(0, 2), (-14, 4), (-19, 13), (-12, 20), (-4, 13)]
    for wing in (up, lo):
        cv.create_polygon(P(wing), fill=fill, outline=outline, smooth=True, width=1)
        cv.create_polygon(P([(-x, y) for x, y in wing]), fill=fill, outline=outline,
                          smooth=True, width=1)
    cv.create_line(*P([(0, -8), (0, 14)]), fill=C["cream"], width=max(1, int(2 * s)))
    cv.create_line(*P([(0, -8), (-5, -17)]), fill=C["glow"])
    cv.create_line(*P([(0, -8), (5, -17)]), fill=C["glow"])


class PButton(tk.Label):
    """Кнопка-плашка: при наведении загорается синим."""
    def __init__(self, master, text, command, accent=False, **kw):
        self.base = C["red"] if accent else C["panel2"]
        self.hover = "#e0435a" if accent else C["line"]
        super().__init__(master, text=text, bg=self.base, fg=C["cream"], cursor="hand2",
                         font=(SERIF, 11, "bold" if accent else "normal"), padx=12, pady=6,
                         highlightthickness=1, highlightbackground=C["gold"] if accent else C["line"],
                         **kw)
        self.command = command
        self.bind("<Enter>", lambda e: self.config(bg=self.hover))
        self.bind("<Leave>", lambda e: self.config(bg=self.base))
        self.bind("<Button-1>", lambda e: self.command())

    def set_accent(self, accent):
        self.base = C["red"] if accent else C["panel2"]
        self.hover = "#e0435a" if accent else C["line"]
        self.config(bg=self.base, highlightbackground=C["gold"] if accent else C["line"])


class PToggle(tk.Label):
    """Переключатель с ромбиком ◆ / ◇."""
    def __init__(self, master, text, value, on_change):
        super().__init__(master, bg=C["panel"], fg=C["cream"], font=(SANS, 10), cursor="hand2",
                         anchor="w")
        self.text, self.value, self.on_change = text, value, on_change
        self.render()
        self.bind("<Button-1>", self.flip)

    def render(self):
        self.config(text=("◆  " if self.value else "◇  ") + self.text,
                    fg=C["cream"] if self.value else C["muted"])

    def flip(self, _=None):
        self.value = not self.value
        self.render()
        self.on_change()


def frame_box(master, title):
    """Панель с заголовком-плашкой и двойной рамкой, как окна меню."""
    outer = tk.Frame(master, bg=C["line"], padx=1, pady=1)
    inner = tk.Frame(outer, bg=C["panel"], padx=12, pady=10,
                     highlightthickness=1, highlightbackground=C["bg"])
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(inner, text=title.upper(), bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w", pady=(0, 6))
    return outer, inner


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


class LastFMDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app, self.cfg, self.token = app, app.cfg, None
        self.title("Last.fm")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(app)
        try:
            self.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        outer, box = frame_box(self, "Скробблинг в Last.fm")
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(box, text="1. Создай ключ API (один раз, любые название и описание):",
                 bg=C["panel"], fg=C["cream"], font=(SANS, 9)).pack(anchor="w")
        link = tk.Label(box, text="last.fm/api/account/create", bg=C["panel"], fg=C["blue"],
                        font=(SANS, 9, "underline"), cursor="hand2")
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda e: webbrowser.open("https://www.last.fm/api/account/create"))

        self.key_var = tk.StringVar(value=self.cfg.get("lastfm_key", ""))
        self.sec_var = tk.StringVar(value=self.cfg.get("lastfm_secret", ""))
        for label, var in (("API key", self.key_var), ("Shared secret", self.sec_var)):
            row = tk.Frame(box, bg=C["panel"])
            row.pack(fill="x", pady=(6, 0))
            tk.Label(row, text=label, bg=C["panel"], fg=C["cream"], font=(SANS, 10), width=13,
                     anchor="w").pack(side="left")
            app.entry(row, var, 34).pack(side="left", fill="x", expand=True, ipady=3)

        tk.Label(box, text="2. Войди и разреши доступ в браузере, потом нажми «Готово»:",
                 bg=C["panel"], fg=C["cream"], font=(SANS, 9)).pack(anchor="w", pady=(12, 4))
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x")
        PButton(row, "Войти в Last.fm", self.login).pack(side="left", fill="x", expand=True, padx=(0, 6))
        PButton(row, "Готово, я разрешил", self.finish, accent=True).pack(side="left", fill="x", expand=True)

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        self.tg_on = PToggle(box, "Отправлять прослушанное в Last.fm", self.cfg.get("lastfm_on", False),
                             self.save_on)
        self.tg_on.pack(fill="x")
        self.info = tk.Label(box, text="", bg=C["panel"], fg=C["muted"], font=(SANS, 9),
                             wraplength=380, justify="left")
        self.info.pack(anchor="w", pady=(8, 0))
        PButton(box, "Отключить аккаунт", self.logout).pack(anchor="e", pady=(8, 0))
        self.show_state()

    def show_state(self, extra=""):
        if self.cfg.get("lastfm_session"):
            text = f"Подключено: {self.cfg.get('lastfm_user')}"
            col = C["glow"]
        else:
            text, col = "Не подключено", C["muted"]
        self.info.config(text=text + (f"\n{extra}" if extra else ""), fg=col)
        self.app.update_lfm_label()

    def keys(self):
        k, s_ = self.key_var.get().strip(), self.sec_var.get().strip()
        if not (k and s_):
            messagebox.showwarning("Last.fm", "Сначала вставь API key и Shared secret.", parent=self)
            return None
        self.cfg["lastfm_key"], self.cfg["lastfm_secret"] = k, s_
        save_cfg(self.cfg)
        return k, s_

    def login(self):
        ks = self.keys()
        if not ks:
            return
        try:
            self.token = lastfm_call(*ks, "auth.getToken", post=False)["token"]
        except Exception as e:
            messagebox.showerror("Last.fm", f"Не получилось: {e}\nПроверь ключи.", parent=self)
            return
        webbrowser.open(f"https://www.last.fm/api/auth/?api_key={ks[0]}&token={self.token}")
        self.show_state("Открыл браузер. Разреши доступ и вернись сюда.")

    def finish(self):
        ks = self.keys()
        if not ks:
            return
        if not self.token:
            messagebox.showinfo("Last.fm", "Сначала нажми «Войти в Last.fm».", parent=self)
            return
        try:
            ses = lastfm_call(*ks, "auth.getSession", {"token": self.token}, post=False)["session"]
        except Exception as e:
            messagebox.showerror("Last.fm", f"Last.fm ещё не видит разрешения: {e}", parent=self)
            return
        self.cfg.update(lastfm_session=ses["key"], lastfm_user=ses["name"], lastfm_on=True)
        save_cfg(self.cfg)
        self.tg_on.value = True
        self.tg_on.render()
        self.show_state("Новые прослушивания будут уходить в Last.fm.")

    def save_on(self):
        self.cfg["lastfm_on"] = self.tg_on.value
        save_cfg(self.cfg)
        self.show_state()

    def logout(self):
        self.cfg.update(lastfm_session="", lastfm_user="", lastfm_on=False)
        save_cfg(self.cfg)
        self.tg_on.value = False
        self.tg_on.render()
        self.show_state()


# ───────────────────────── трей ─────────────────────────
class Tray:
    """Иконка возле часов: клик — открыть окно, правый клик — меню."""
    def __init__(self, app):
        q = app.q
        try:
            img = Image.open(resource("icon.ico"))
        except Exception:
            img = Image.new("RGB", (64, 64), C["blue"])
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda i, it: q.put({"cmd": "show"}), default=True),
            pystray.MenuItem("Пауза статуса", lambda i, it: q.put({"cmd": "toggle_hidden"}),
                             checked=lambda it: app.hidden),
            pystray.MenuItem("Если играют музыка и видео", pystray.Menu(*[
                pystray.MenuItem(text, (lambda k: lambda i, it: q.put({"cmd": "multi", "mode": k}))(key),
                                 checked=(lambda k: lambda it: app.cfg.get("multi", "auto") == k)(key),
                                 radio=True)
                for key, text in MULTI_MODES.items()])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda it: app.update_menu_text(),
                             lambda i, it: q.put({"cmd": "check_update", "manual": True})),
            pystray.MenuItem("Что нового", lambda i, it: q.put({"cmd": "whats_new"})),
            pystray.MenuItem("Обновляться автоматически", lambda i, it: q.put({"cmd": "toggle_auto_update"}),
                             checked=lambda it: app.cfg.get("auto_update", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda i, it: q.put({"cmd": "quit"})),
        )
        self.icon = pystray.Icon("MusicDiscord", img, APP_NAME, menu)
        self.icon.run_detached()
        self._title = APP_NAME

    def set_title(self, text):
        text = (text or APP_NAME)[:120]
        if text != self._title:
            self._title = text
            try:
                self.icon.title = text
            except Exception:
                pass

    def refresh(self):
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, text):
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


# ───────────────────────── своя обложка: окно ─────────────────────────
class CoverDialog(tk.Toplevel):
    def __init__(self, app, track):
        super().__init__(app)
        self.app, self.track = app, dict(track)
        self.title("Своя обложка")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(app)
        try:
            self.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        box = tk.Frame(self, bg=C["panel"], padx=16, pady=14, highlightthickness=1,
                       highlightbackground=C["line"])
        box.pack(fill="both", expand=True, padx=12, pady=12)
        name = f"{track.get('artist') or '—'} — {track.get('title') or '—'}"
        tk.Label(box, text=name, bg=C["panel"], fg=C["cream"], font=(SERIF, 12, "bold"),
                 wraplength=420, justify="left", anchor="w").pack(fill="x")
        mine, scope = custom_cover(app.cfg, track)
        now = f"Сейчас: своя ({scope})" if mine else f"Сейчас: {COVER_SRC['last'] or 'нет картинки'}"
        tk.Label(box, text=now, bg=C["panel"], fg=C["muted"], font=(SANS, 9), anchor="w").pack(fill="x")

        tk.Label(box, text="Ссылка на картинку (https://…) или имя картинки из Art Assets:",
                 bg=C["panel"], fg=C["cream"], font=(SANS, 10), anchor="w").pack(fill="x", pady=(12, 2))
        self.var = tk.StringVar(value=mine or "")
        ent = tk.Entry(box, textvariable=self.var, bg=C["panel2"], fg=C["cream"],
                       insertbackground=C["cream"], relief="flat", font=(SANS, 10), width=52)
        ent.pack(fill="x", ipady=4)
        ent.focus_set()
        tk.Label(box, text="Подсказка: найди обложку в интернете → правый клик → «Копировать адрес "
                           "картинки». Надёжнее всего — картинка из Art Assets твоего приложения "
                           "на discord.com/developers (её Discord показывает всегда).",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8), wraplength=420, justify="left",
                 anchor="w").pack(fill="x", pady=(4, 0))

        has_album = bool((track.get("album") or "").strip())
        self.scope = tk.StringVar(value=("album" if (scope == "альбом" or (not mine and has_album)) else "track"))
        srow = tk.Frame(box, bg=C["panel"])
        srow.pack(fill="x", pady=(10, 0))
        for val, text in (("album", f"Для всего альбома «{track.get('album')}»" if has_album else "Для альбома (нет в тегах)"),
                          ("track", "Только для этого трека")):
            rb = tk.Radiobutton(srow, text=text, variable=self.scope, value=val, bg=C["panel"], fg=C["cream"],
                                selectcolor=C["panel2"], activebackground=C["panel"], activeforeground=C["cream"],
                                font=(SANS, 10), anchor="w")
            if val == "album" and not has_album:
                rb.config(state="disabled")
            rb.pack(fill="x")

        self.msg = tk.Label(box, text="", bg=C["panel"], fg=C["glow"], font=(SANS, 9), wraplength=420,
                            justify="left", anchor="w")
        self.msg.pack(fill="x", pady=(8, 0))
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", pady=(10, 0))
        PButton(row, "Сохранить", self.save, accent=True).pack(side="left", fill="x", expand=True, padx=(0, 6))
        PButton(row, "Файл с компьютера…", self.pick_file).pack(side="left", padx=(0, 6))
        if mine:
            PButton(row, "Убрать свою", self.remove).pack(side="left")
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())

    def _done(self, text, close=False):
        if not self.winfo_exists():
            return
        self.msg.config(text=text)
        if close:
            self.after(900, self.destroy)

    def save(self):
        val = self.var.get().strip()
        if not val:
            self._done("Вставь ссылку или выбери файл.")
            return
        if not val.lower().startswith("http"):                 # имя из Art Assets
            self.app.set_custom_cover(self.track, val, self.scope.get())
            self._done("Сохранено ✓ (картинка из Art Assets)", close=True)
            return
        self._done("Проверяю, что Discord сможет открыть картинку…")

        def work():
            ok = check_image_url(val)
            self.after(0, lambda: self._saved(val, ok))
        threading.Thread(target=work, daemon=True).start()

    def _saved(self, url, ok):
        if not ok:
            self._done("По этой ссылке не картинка (или сайт не пускает Discord). Нужна прямая ссылка "
                       "на .jpg/.png — «Копировать адрес картинки».")
            return
        self.app.set_custom_cover(self.track, url, self.scope.get())
        self._done("Сохранено ✓ — в статусе обновится через пару секунд", close=True)

    def pick_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self, title="Картинка для обложки",
                                          filetypes=[("Картинки", "*.jpg *.jpeg *.png *.webp"), ("Все файлы", "*.*")])
        if not path:
            return
        self._done("Загружаю картинку на catbox.moe (навсегда)…")

        def work():
            url, err = upload_permanent(path)
            self.after(0, lambda: (self.var.set(url), self._saved(url, True)) if url
                       else self._done(f"Не получилось загрузить: {err}"))
        threading.Thread(target=work, daemon=True).start()

    def remove(self):
        self.app.set_custom_cover(self.track, None, None)
        self._done("Своя обложка убрана — снова как обычно", close=True)


def upload_permanent(path):
    """Загрузить картинку на catbox.moe (без срока) → (ссылка, ошибка)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return None, str(e)
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    mime = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    body, ctype = _multipart({"reqtype": "fileupload"}, "fileToUpload", "cover" + ext, data, mime)
    try:
        req = urllib.request.Request("https://catbox.moe/user/api.php", data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("User-Agent", "MusicDiscord/1.0")
        with urllib.request.urlopen(req, timeout=30) as r:
            link = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return None, str(e)[:120]
    if not link.startswith("https://"):
        return None, link[:120]
    return (link, "") if check_image_url(link) else (None, "хостинг не отдаёт картинку")


# ───────────────────────── интерфейс ─────────────────────────
class App(tk.Tk):
    STATES = {
        "live": ("В ЭФИРЕ", C["red"]), "paused": ("ПАУЗА", C["gold"]),
        "hidden": ("СКРЫТО", "#5b4a8c"), "idle": ("ТИШИНА", C["line"]),
        "error": ("ОШИБКА", "#7a1f2b"), "off": ("ВЫКЛ", C["line"]),
        "wait": ("ЖДУ…", "#2c5a8a"),
    }

    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        save_cfg(self.cfg)
        self.q = queue.Queue()
        self.worker = None
        self.hidden = False
        self.hotkey = None
        self.current = None
        try:
            self.history = History()
        except Exception:
            self.history = None
        self.lastfm = LastFM(self.cfg, self.history)
        self.hist_period = "week"

        self.build = updater.current_build()
        self.update_state = ""            # "", "checking", "downloading 42%", "ready N", "latest"
        self.title(APP_NAME + (f" · сборка {self.build}" if self.build else ""))
        self.geometry("540x790")
        self.minsize(540, 790)
        try:
            self.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        self.configure(bg=C["bg"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_header()
        self.build_now_playing()
        self.build_tabs()

        self.after(200, self.poll)
        self.after(20000, self.update_tick)
        self.after(4000, self.check_whats_new)
        self.register_hotkey(silent=True)
        self.tray = None
        if HAS_TRAY:
            try:
                self.tray = Tray(self)
            except Exception:
                self.tray = None
        self.bind("<Unmap>", self.on_minimize)
        if self.cfg["client_id"]:
            self.toggle()
        else:
            self.set_state("off", "Вставь Application ID на вкладке «Главное» и нажми «Начать».")
            self.show_tab("main")
        if "--minimized" in sys.argv:
            if self.tray:
                self.withdraw()
            else:
                self.iconify()
        if "--updated" in sys.argv and self.tray:
            self.after(2500, lambda: self.tray.notify(f"Обновлено до сборки {self.build}."))

    # ── шапка ──
    def build_header(self):
        cv = tk.Canvas(self, height=92, bg=C["bg"], highlightthickness=0)
        cv.pack(fill="x")
        self.header = cv
        cv.bind("<Configure>", lambda e: self.draw_header())

    def draw_header(self):
        cv = self.header
        cv.delete("all")
        w = cv.winfo_width()
        for i in range(12):                         # мягкий градиент сверху
            shade = 0x06 + i
            cv.create_rectangle(0, i * 8, w, i * 8 + 8, outline="",
                                fill=f"#{shade:02x}{shade + 4:02x}{min(0x18 + i * 3, 0x3a):02x}")
        for x in range(-40, w, 26):                  # тонкая диагональная штриховка
            cv.create_line(x, 92, x + 60, 0, fill="#0f1838")
        for r, col in ((34, "#0c1a3e"), (24, "#11265a")):
            cv.create_oval(52 - r, 46 - r, 52 + r, 46 + r, fill=col, outline="")
        draw_butterfly(cv, 52, 46, 1.15)
        cv.create_text(96, 30, text="MUSIC  →  DISCORD", anchor="w", fill=C["cream"],
                       font=(SERIF, 20, "bold italic"))
        cv.create_text(98, 58, text="the music you hear tonight, shown to everyone",
                       anchor="w", fill=C["muted"], font=(SERIF, 9, "italic"))
        cv.create_line(0, 89, w, 89, fill=C["gold"])
        cv.create_line(0, 91, w, 91, fill=C["line"])

    # ── карточка «сейчас играет» ──
    def build_now_playing(self):
        outer, box = frame_box(self, "Сейчас звучит")
        outer.pack(fill="x", padx=16, pady=(12, 6))

        badge_row = tk.Frame(box, bg=C["panel"])
        badge_row.pack(fill="x")
        self.badge = tk.Canvas(badge_row, width=110, height=22, bg=C["panel"], highlightthickness=0)
        self.badge.pack(side="left")
        self.status_lbl = tk.Label(badge_row, text="", bg=C["panel"], fg=C["muted"],
                                   font=(SANS, 9), anchor="w", wraplength=300, justify="left")
        self.status_lbl.pack(side="left", padx=8, fill="x")

        self.track_lbl = tk.Label(box, text="—", bg=C["panel"], fg=C["cream"],
                                  font=(SERIF, 15, "bold"), wraplength=420, justify="left", anchor="w")
        self.track_lbl.pack(fill="x", pady=(10, 0))
        self.artist_lbl = tk.Label(box, text="", bg=C["panel"], fg=C["glow"],
                                   font=(SERIF, 11, "italic"), wraplength=420, justify="left", anchor="w")
        self.artist_lbl.pack(fill="x")

        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", pady=(12, 0))
        self.start_btn = PButton(row, "▶  Начать", self.toggle, accent=True)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.hide_btn = PButton(row, "◐  Невидимка", self.toggle_hidden)
        self.hide_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        PButton(row, "🖼  Обложка", self.open_cover_dialog).pack(side="left")
        self.hk_hint = tk.Label(box, text="", bg=C["panel"], fg=C["muted"], font=(SANS, 8))
        self.hk_hint.pack(anchor="e", pady=(4, 0))
        self.update_hk_hint()

    def set_state(self, state, text):
        label, col = self.STATES.get(state, self.STATES["off"])
        b = self.badge
        b.delete("all")
        b.create_polygon(8, 0, 110, 0, 102, 22, 0, 22, fill=col, outline=C["gold"])
        b.create_text(55, 11, text=label, fill=C["cream"], font=(SERIF, 9, "bold"))
        self.status_lbl.config(text=text)

    # ── вкладки ──
    def build_tabs(self):
        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=16, pady=(6, 0))
        self.tab_btns, self.tabs = {}, {}
        for key, title in (("main", "Главное"), ("status", "Статус"), ("buttons", "Вид"),
                           ("sources", "Источники"), ("hide", "Скрытие"), ("history", "История")):
            lbl = tk.Label(bar, text=title, bg=C["bg"], fg=C["muted"], font=(SERIF, 10),
                           padx=9, pady=4, cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            self.tab_btns[key] = lbl
        tk.Frame(self, bg=C["gold"], height=1).pack(fill="x", padx=16)

        holder = tk.Frame(self, bg=C["bg"])
        holder.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.tabs["main"] = self.build_main_tab(holder)
        self.tabs["status"] = self.build_status_tab(holder)
        self.tabs["buttons"] = self.build_buttons_tab(holder)
        self.tabs["sources"] = self.build_sources_tab(holder)
        self.tabs["hide"] = self.build_hide_tab(holder)
        self.tabs["history"] = self.build_history_tab(holder)
        self.render_preview()
        self.show_tab("main")

    def show_tab(self, key):
        for k, f in self.tabs.items():
            f.pack_forget()
            self.tab_btns[k].config(fg=C["muted"], bg=C["bg"], font=(SERIF, 10))
        self.tabs[key].pack(fill="both", expand=True)
        self.tab_btns[key].config(fg=C["cream"], bg=C["panel2"], font=(SERIF, 10, "bold"))
        self.active_tab = key
        if key == "history":
            self.refresh_history()
        elif key == "status":
            self.refresh_presets()

    def entry(self, master, var, width=22):
        return tk.Entry(master, textvariable=var, width=width, font=(SANS, 10), bg=C["bg"],
                        fg=C["cream"], insertbackground=C["glow"], relief="flat",
                        highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["blue"])

    def build_main_tab(self, holder):
        outer, box = frame_box(holder, "Договор с Discord")
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x")
        tk.Label(row, text="Application ID", bg=C["panel"], fg=C["cream"], font=(SANS, 10)).pack(side="left")
        self.id_var = tk.StringVar(value=self.cfg["client_id"])
        self.entry(row, self.id_var).pack(side="left", padx=8, ipady=3, fill="x", expand=True)
        PButton(row, "?", self.help_id).pack(side="left")

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        self.tg_fcover = PToggle(box, "Обложка из самого файла (MP3 Плеер, VLC)",
                                 self.cfg.get("file_covers", True), self.save_opts)
        self.tg_cover = PToggle(box, "Искать обложки альбомов в интернете", self.cfg["covers"], self.save_opts)
        self.tg_ytm = PToggle(box, "Искать трек на YouTube Music (обложка и ссылка в названии)",
                              self.cfg.get("ytmusic", True), self.save_opts)
        self.tg_ffirst = PToggle(box, "Сначала обложка из файла, iTunes — запасной",
                                 self.cfg.get("cover_order", "itunes") == "file", self.save_opts)
        self.tg_pause = PToggle(box, "Показывать, когда на паузе", self.cfg["show_paused"], self.save_opts)
        self.tg_auto = PToggle(box, "Запускать вместе с Windows", self.cfg["autostart"], self.save_opts)
        for t in (self.tg_fcover, self.tg_cover, self.tg_ytm, self.tg_ffirst, self.tg_pause, self.tg_auto):
            t.pack(fill="x", pady=2)

        drow = tk.Frame(box, bg=C["panel"])
        drow.pack(fill="x", pady=(8, 0))
        tk.Label(drow, text="Показывать новый трек через", bg=C["panel"], fg=C["cream"],
                 font=(SANS, 10)).pack(side="left")
        self.delay_var = tk.StringVar(value=str(self.cfg.get("show_delay", 10)))
        sp = tk.Spinbox(drow, from_=0, to=60, width=4, textvariable=self.delay_var, font=(SANS, 10),
                        bg=C["bg"], fg=C["cream"], buttonbackground=C["panel2"], relief="flat",
                        insertbackground=C["glow"], highlightthickness=1, highlightbackground=C["line"],
                        command=self.save_delay)
        sp.pack(side="left", padx=6)
        sp.bind("<KeyRelease>", lambda e: self.save_delay())
        tk.Label(drow, text="с   (0 — сразу)", bg=C["panel"], fg=C["muted"], font=(SANS, 9)).pack(side="left")

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        tk.Label(box, text="ТЕКСТ ПЕСНИ В СТАТУСЕ", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w")
        lrow = tk.Frame(box, bg=C["panel"])
        lrow.pack(fill="x", pady=(4, 0))
        self.lyr_toggles = {}
        for key, text in (("off", "Выкл"), ("state", "Вместо строки 2"), ("details", "Вместо строки 1")):
            t = PToggle(lrow, text, self.cfg.get("lyrics", "off") == key, lambda k=key: self.set_lyrics(k))
            t.pack(side="left", padx=(0, 14))
            self.lyr_toggles[key] = t
        tk.Label(box, text="Строчки с lrclib.net. Discord меняет статус не чаще раза в ~4 с, "
                           "так что на быстрых песнях строки будут проскакивать.",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8), wraplength=470,
                 justify="left").pack(anchor="w", pady=(2, 0))

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        PButton(box, "⚙  Настроить VLC", self.setup_vlc).pack(fill="x")
        tk.Label(box, text="Закрой VLC и нажми один раз — появятся время трека, альбом и пауза.",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8), wraplength=400,
                 justify="left").pack(anchor="w", pady=(4, 0))
        return outer

    def build_buttons_tab(self, holder):
        outer, box = frame_box(holder, "Кнопки под статусом")
        tk.Label(box, text="Друзья нажмут — и откроется поиск трека.\n"
                           "Сам ты свои кнопки в Discord не увидишь — так он устроен.",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 9), justify="left").pack(anchor="w")
        names = [v[0] for v in BUTTONS.values()]
        self.btn_vars = []
        cur = (self.cfg.get("buttons") or []) + ["none", "none"]
        for i in range(2):
            row = tk.Frame(box, bg=C["panel"])
            row.pack(fill="x", pady=(10 if i == 0 else 4, 0))
            tk.Label(row, text=("Ⅰ" if i == 0 else "Ⅱ"), bg=C["panel"], fg=C["gold"],
                     font=(SERIF, 13, "bold"), width=3).pack(side="left")
            var = tk.StringVar(value=BUTTONS.get(cur[i], BUTTONS["none"])[0])
            om = tk.OptionMenu(row, var, *names, command=lambda _: self.save_buttons())
            om.config(bg=C["panel2"], fg=C["cream"], activebackground=C["line"],
                      activeforeground=C["cream"], font=(SANS, 10), relief="flat",
                      highlightthickness=1, highlightbackground=C["line"], width=24, anchor="w")
            om["menu"].config(bg=C["panel2"], fg=C["cream"], activebackground=C["blue"],
                              activeforeground=C["bg"], font=(SANS, 10))
            om.pack(side="left", fill="x", expand=True)
            self.btn_vars.append(var)

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=12)
        tk.Label(box, text="КАРТИНКА, ЕСЛИ ОБЛОЖКА НЕ НАШЛАСЬ", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w")
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", pady=(6, 0))
        self.fb_var = tk.StringVar(value=self.cfg.get("fallback_image", ""))
        self.entry(row, self.fb_var).pack(side="left", ipady=3, fill="x", expand=True)
        PButton(row, "OK", self.save_fallback).pack(side="left", padx=(8, 4))
        PButton(row, "?", self.help_image).pack(side="left")
        tk.Label(box, text="имя картинки из Discord (например  logo) или ссылка https://…  ·  пусто — без картинки",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8), wraplength=460, justify="left").pack(anchor="w", pady=(2, 0))

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=12)
        head = tk.Frame(box, bg=C["panel"])
        head.pack(fill="x")
        tk.Label(head, text="ЗНАЧОК В УГЛУ ОБЛОЖКИ", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(side="left")
        PButton(head, "?", self.help_icons).pack(side="right")
        self.badge_toggles = {}
        for key, text in (("none", "Без значка"), ("logo", "Мой логотип"),
                          ("app", "Значок приложения, где играет (Spotify, VLC…)")):
            t = PToggle(box, text, self.cfg.get("badge") == key, lambda k=key: self.set_badge(k))
            t.pack(fill="x", pady=1)
            self.badge_toggles[key] = t
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="Сайт, если ссылка не нашлась", bg=C["panel"], fg=C["cream"],
                 font=(SANS, 9)).pack(side="left")
        self.browser_var = tk.StringVar(value=self.cfg.get("browser_icon", "youtube"))
        self.entry(row, self.browser_var, 12).pack(side="left", padx=8, ipady=2)
        PButton(row, "OK", self.save_fallback).pack(side="left")
        return outer

    # ── вкладка «Статус» ──
    SAMPLE = {"title": "Группа крови", "artist": "Кино", "album": "Группа крови",
              "app": "Spotify", "playing": True, "time": 75, "length": 287,
              "fav": "♥", "ab": "", "skin": "Флешка"}

    # заготовки: (ключ, название, верхняя строка, нижняя строка, подсказка на обложке, что под ником)
    STATUS_PRESETS = (
        ("classic", "Название и исполнитель", "{title}", "{artist}", "{album}", "state"),
        ("artist_first", "Исполнитель — название", "{artist} — {title}", "{album}", "{album}", "details"),
        ("with_album", "С альбомом", "{title}", "{artist} · {album}", "{album}", "state"),
        ("with_app", "С названием плеера", "{title}", "{artist} · {app}", "{album}", "state"),
        ("fav", "С сердечком для любимых", "{fav} {title}", "{artist}", "{album}", "state"),
        ("minimal", "Только название", "{title}", "{app}", "{artist}", "details"),
    )
    LIST_LABELS = (("state", "нижнюю строку"), ("details", "верхнюю строку"), ("name", "имя приложения"))

    def current_preset(self):
        c = self.cfg
        for key, _n, d, st, lg, ls in self.STATUS_PRESETS:
            if (c.get("fmt_details"), c.get("fmt_state"), c.get("fmt_large"), c.get("list_shows", "state")) \
                    == (d, st, lg, ls):
                return key
        return "custom"

    def build_status_tab(self, holder):
        outer, box = frame_box(holder, "Как выглядит статус")
        tk.Label(box, text="Выбери вариант — применится сразу:", bg=C["panel"], fg=C["muted"],
                 font=(SANS, 9)).pack(anchor="w", pady=(0, 4))
        grid = tk.Frame(box, bg=C["panel"])
        grid.pack(fill="x")
        self.preset_cards = {}
        t = self.sample_track()
        for i, (key, name, d, st, _lg, _ls) in enumerate(self.STATUS_PRESETS):
            card = tk.Frame(grid, bg=C["panel2"], padx=10, pady=6, cursor="hand2",
                            highlightthickness=1, highlightbackground=C["line"])
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0 if i % 2 == 0 else 6, 0), pady=3)
            title = tk.Label(card, text=name, bg=C["panel2"], fg=C["cream"], font=(SANS, 10, "bold"),
                             anchor="w", cursor="hand2")
            title.pack(fill="x")
            ex = tk.Label(card, text="", bg=C["panel2"], fg=C["muted"], font=(SANS, 8), anchor="w",
                          justify="left", cursor="hand2")
            ex.pack(fill="x")
            for w in (card, title, ex):
                w.bind("<Button-1>", lambda e, k=key: self.apply_status_preset(k))
            self.preset_cards[key] = (card, title, ex)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        tk.Label(box, text="ТАК УВИДЯТ ДРУЗЬЯ", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w", pady=(10, 4))
        self.preview = tk.Frame(box, bg="#2b2d31", padx=10, pady=8)
        self.preview.pack(fill="x")

        # свой вариант — свёрнут
        self.custom_btn = tk.Label(box, text="", bg=C["panel"], fg=C["glow"], font=(SANS, 9, "underline"),
                                   cursor="hand2", anchor="w")
        self.custom_btn.pack(anchor="w", pady=(10, 0))
        self.custom_btn.bind("<Button-1>", lambda e: self.toggle_custom())
        self.custom_box = tk.Frame(box, bg=C["panel"])
        self.tpl_vars = {}
        self.focused_tpl = None
        for key, label in (("fmt_details", "Верхняя строка"), ("fmt_state", "Нижняя строка"),
                           ("fmt_large", "Подсказка на обложке")):
            row = tk.Frame(self.custom_box, bg=C["panel"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=C["panel"], fg=C["cream"], font=(SANS, 9),
                     width=19, anchor="w").pack(side="left")
            var = tk.StringVar(value=self.cfg.get(key, DEFAULT_CFG[key]))
            e = self.entry(row, var)
            e.pack(side="left", fill="x", expand=True, ipady=2)
            e.bind("<FocusIn>", lambda ev, w=e: setattr(self, "focused_tpl", w))
            e.bind("<KeyRelease>", lambda ev: self.render_preview())
            self.tpl_vars[key] = var
        chips = tk.Frame(self.custom_box, bg=C["panel"])
        chips.pack(fill="x", pady=(4, 0))
        tk.Label(chips, text="вставить:", bg=C["panel"], fg=C["muted"], font=(SANS, 8)).pack(side="left")
        for k, nice in (("title", "название"), ("artist", "исполнитель"), ("album", "альбом"),
                        ("app", "плеер"), ("fav", "♥"), ("lyrics", "текст"), ("ab", "A–B"), ("skin", "скин")):
            c = tk.Label(chips, text=nice, bg=C["panel2"], fg=C["glow"], font=(SANS, 8), padx=6, cursor="hand2")
            c.pack(side="left", padx=2)
            c.bind("<Button-1>", lambda ev, k=k: self.insert_placeholder(k))
        lrow = tk.Frame(self.custom_box, bg=C["panel"])
        lrow.pack(fill="x", pady=(8, 0))
        tk.Label(lrow, text="Под ником в списке:", bg=C["panel"], fg=C["cream"], font=(SANS, 9)).pack(side="left")
        self.list_toggles = {}
        for key, text in self.LIST_LABELS:
            tg = PToggle(lrow, text, self.cfg.get("list_shows", "state") == key,
                         lambda k=key: self.set_list_shows(k))
            tg.pack(side="left", padx=(10, 0))
            self.list_toggles[key] = tg
        PButton(self.custom_box, "Сохранить свой вариант", self.save_templates, accent=True).pack(fill="x", pady=(8, 0))
        self.custom_open = self.current_preset() == "custom"
        self.refresh_presets()
        return outer

    def sample_track(self):
        t = dict(self.SAMPLE)
        if self.current:
            t.update({k: self.current.get(k, "") or t.get(k, "") for k in ("title", "artist", "album", "app")})
        t["fav"] = "♥"
        return t

    def refresh_presets(self):
        if not hasattr(self, "preset_cards"):
            return
        cur = self.current_preset()
        t = self.sample_track()
        for key, name, d, st, _lg, _ls in self.STATUS_PRESETS:
            card, title, ex = self.preset_cards[key]
            on = key == cur
            bg = C["red"] if on else C["panel2"]
            for w in (card, title, ex):
                w.config(bg=bg)
            card.config(highlightbackground=C["gold"] if on else C["line"])
            title.config(text=("◆ " if on else "") + name)
            ex.config(text=(render_tpl(d, t) or t["title"])[:38] + "\n" + (render_tpl(st, t) or "")[:38],
                      fg=C["cream"] if on else C["muted"])
        self.custom_btn.config(text=("▾ Свой вариант (выбран)" if cur == "custom" else "▾ Свой вариант")
                               if self.custom_open else
                               ("▸ Свой вариант (выбран) — настроить" if cur == "custom" else "▸ Свой вариант…"))
        if self.custom_open:
            self.custom_box.pack(fill="x", pady=(4, 0))
        else:
            self.custom_box.pack_forget()
        self.render_preview()

    def toggle_custom(self):
        self.custom_open = not self.custom_open
        self.refresh_presets()

    def apply_status_preset(self, key):
        p = next(x for x in self.STATUS_PRESETS if x[0] == key)
        _k, name, d, st, lg, ls = p
        self.cfg.update(fmt_details=d, fmt_state=st, fmt_large=lg, list_shows=ls)
        for k, v in (("fmt_details", d), ("fmt_state", st), ("fmt_large", lg)):
            self.tpl_vars[k].set(v)
        for k, tg in self.list_toggles.items():
            tg.value = (k == ls)
            tg.render()
        self.push_cfg()
        self.status_lbl.config(text=f"Статус: «{name}»")
        self.refresh_presets()

    def insert_placeholder(self, key):
        e = self.focused_tpl
        if not e:
            return
        e.insert("insert", "{" + key + "}")
        e.focus_set()
        self.render_preview()

    def preview_cfg(self):
        c = dict(self.cfg)
        for k, v in getattr(self, "tpl_vars", {}).items():
            c[k] = v.get()
        return c

    def render_preview(self):
        if not hasattr(self, "preview"):
            return
        for w in self.preview.winfo_children():
            w.destroy()
        t = dict(self.SAMPLE)
        if self.current:
            t.update({k: self.current.get(k, "") for k in ("title", "artist", "album", "app", "fav", "ab", "skin")})
        cfg = self.preview_cfg()
        t["lyrics"] = "строчка текста песни" if cfg.get("lyrics", "off") != "off" else ""
        details = render_tpl(cfg["fmt_details"], t) or t["title"]
        state = render_tpl(cfg["fmt_state"], t) or t["artist"] or t["app"]
        if cfg.get("lyrics", "off") == "details":
            details = "♪ " + t["lyrics"]
        elif cfg.get("lyrics", "off") == "state":
            state = "♪ " + t["lyrics"]
        large = render_tpl(cfg["fmt_large"], t) or t["app"]
        shown = {"state": state, "details": details, "name": "<имя приложения в Discord>"}
        D_BG, D_FG, D_MUT = "#2b2d31", "#f2f3f5", "#b5bac1"

        tk.Label(self.preview, text="под ником: Слушает " + shown[cfg.get("list_shows", "state")],
                 bg=D_BG, fg=D_MUT, font=(SANS, 8, "italic"), anchor="w").pack(fill="x")
        body = tk.Frame(self.preview, bg=D_BG)
        body.pack(fill="x", pady=(4, 0))
        art = tk.Canvas(body, width=58, height=58, bg="#1e1f22", highlightthickness=0)
        art.pack(side="left", anchor="n")
        art.create_text(29, 29, text="♪", fill=D_MUT, font=(SANS, 20))
        if cfg.get("badge", "none") != "none":
            art.create_oval(40, 40, 56, 56, fill=C["blue"], outline=D_BG, width=2)
        txt = tk.Frame(body, bg=D_BG)
        txt.pack(side="left", fill="x", expand=True, padx=(10, 0))
        for text, font, fg in ((details, (SANS, 10, "bold"), D_FG), (state, (SANS, 9), D_FG),
                               (f"на обложке: {large}", (SANS, 8), D_MUT),
                               ("1:15 ━━━━━─────── 4:47", (SANS, 8), D_MUT)):
            tk.Label(txt, text=text[:60], bg=D_BG, fg=fg, font=font, anchor="w").pack(fill="x")
        for bid in self.cfg.get("buttons", []):
            if bid in BUTTONS and bid != "none":
                tk.Label(self.preview, text=BUTTONS[bid][1], bg="#4e5058", fg=D_FG,
                         font=(SANS, 9, "bold"), pady=4).pack(fill="x", pady=(5, 0))

    def set_list_shows(self, key):
        for k, t in self.list_toggles.items():
            t.value = (k == key)
            t.render()
        self.cfg["list_shows"] = key
        self.push_cfg()
        self.refresh_presets()

    def save_templates(self):
        for k, v in self.tpl_vars.items():
            tpl = v.get().strip()
            try:
                tpl.format_map(_Blank())
            except (ValueError, IndexError):
                messagebox.showwarning(APP_NAME, f"Ошибка в шаблоне: {tpl}\nПроверь фигурные скобки.")
                return
            self.cfg[k] = tpl
        self.push_cfg()
        self.status_lbl.config(text="Свой вариант статуса сохранён")
        self.refresh_presets()

    def reset_templates(self):
        for k, v in self.tpl_vars.items():
            v.set(DEFAULT_CFG[k])
        self.save_templates()
        self.render_preview()

    # ── вкладка «История» ──
    PERIODS = (("week", "Неделя", 7), ("month", "Месяц", 30), ("year", "Год", 365), ("all", "Всё время", None))

    def build_history_tab(self, holder):
        outer, box = frame_box(holder, None)
        top = tk.Frame(box, bg=C["panel"])
        top.pack(fill="x")
        self.period_lbls = {}
        for key, title, _ in self.PERIODS:
            l = tk.Label(top, text=title, bg=C["panel"], fg=C["muted"], font=(SERIF, 10),
                         padx=8, pady=2, cursor="hand2")
            l.pack(side="left")
            l.bind("<Button-1>", lambda e, k=key: self.set_period(k))
            self.period_lbls[key] = l
        self.hist_sum = tk.Label(box, text="", bg=C["panel"], fg=C["glow"], font=(SERIF, 11, "italic"),
                                 anchor="w")
        self.hist_sum.pack(fill="x", pady=(8, 4))
        cols = tk.Frame(box, bg=C["panel"])
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="c")
        cols.columnconfigure(1, weight=1, uniform="c")
        self.hist_artists = tk.Frame(cols, bg=C["panel"])
        self.hist_tracks = tk.Frame(cols, bg=C["panel"])
        self.hist_artists.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.hist_tracks.grid(row=0, column=1, sticky="nsew")

        bot = tk.Frame(box, bg=C["panel"])
        bot.pack(fill="x", pady=(8, 0))
        PButton(bot, "Экспорт в CSV", self.export_history).pack(side="left", fill="x", expand=True, padx=(0, 6))
        PButton(bot, "Last.fm…", self.open_lastfm).pack(side="left", fill="x", expand=True)
        self.lfm_lbl = tk.Label(box, text="", bg=C["panel"], fg=C["muted"], font=(SANS, 8), anchor="e")
        self.lfm_lbl.pack(fill="x", pady=(4, 0))
        return outer

    def set_period(self, key):
        self.hist_period = key
        self.refresh_history()

    def refresh_history(self):
        for k, l in self.period_lbls.items():
            on = k == self.hist_period
            l.config(fg=C["cream"] if on else C["muted"], bg=C["panel2"] if on else C["panel"],
                     font=(SERIF, 10, "bold" if on else "normal"))
        for f in (self.hist_artists, self.hist_tracks):
            for w in f.winfo_children():
                w.destroy()
        self.update_lfm_label()
        if not self.history:
            self.hist_sum.config(text="История недоступна")
            return
        days = {k: d for k, _, d in self.PERIODS}[self.hist_period]
        since = int(time.time() - days * 86400) if days else 0
        count, secs, artists = self.history.totals(since)
        h, m = divmod(int(secs) // 60, 60)
        self.hist_sum.config(
            text=(f"{count} {plural(count, 'трек', 'трека', 'треков')}  ·  {h} ч {m} мин  ·  "
                  f"{artists} {plural(artists, 'исполнитель', 'исполнителя', 'исполнителей')}")
            if count else "Пока пусто — трек засчитывается после половины или 4 минут")
        self.fill_top(self.hist_artists, "ИСПОЛНИТЕЛИ", list(self.history.top_artists(since)))
        self.fill_top(self.hist_tracks, "ТРЕКИ",
                      [(t + (f" — {a}" if a else ""), n) for a, t, n in self.history.top_tracks(since)])

    def fill_top(self, frame, title, rows):
        tk.Label(frame, text=title, bg=C["panel"], fg=C["gold"], font=(SERIF, 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 2))
        top = max([n for _, n in rows] or [1])
        for i, (name, n) in enumerate(rows, 1):
            row = tk.Frame(frame, bg=C["panel"])
            row.pack(fill="x", pady=(3, 0))
            tk.Label(row, text=f"{i}", bg=C["panel"], fg=C["gold"], font=(SERIF, 10, "bold"),
                     width=2, anchor="w").pack(side="left")
            tk.Label(row, text=str(n), bg=C["panel"], fg=C["muted"], font=(SANS, 9)).pack(side="right")
            short = name[:24] + ("…" if len(name) > 24 else "")
            tk.Label(row, text=short, bg=C["panel"], fg=C["cream"], font=(SANS, 9),
                     anchor="w").pack(side="left", fill="x", expand=True)
            bar = tk.Frame(frame, bg=C["bg"], height=3)
            bar.pack(fill="x", padx=(22, 0))
            tk.Frame(bar, bg=C["red"] if i == 1 else C["blue"]).place(relwidth=n / top, relheight=1)

    def update_lfm_label(self):
        c = self.cfg
        if c.get("lastfm_session") and c.get("lastfm_on"):
            err = self.lastfm.last_error
            self.lfm_lbl.config(text=f"Last.fm: {c.get('lastfm_user')} ✓" + (f"  ·  {err}" if err else ""),
                                fg=C["red"] if err else C["glow"])
        else:
            self.lfm_lbl.config(text="Last.fm не подключён", fg=C["muted"])

    def export_history(self):
        if not self.history:
            return
        path = os.path.join(os.path.expanduser("~"), "Documents", "История музыки.csv")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            n = self.history.export_csv(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не получилось: {e}")
            return
        self.status_lbl.config(text=f"Сохранено записей: {n} → Документы")
        try:
            os.startfile(path)
        except Exception:
            pass

    def open_lastfm(self):
        LastFMDialog(self)

    def build_sources_tab(self, holder):
        outer, box = frame_box(holder, "Откуда брать музыку")
        self.src_toggles = {}
        modes = (("auto", "Всё сразу — показывать то, что играет"),
                 ("vlc", "Только VLC"),
                 ("system", "Только приложения Windows (Spotify, браузер, AIMP…)"))
        for key, text in modes:
            t = PToggle(box, text, self.cfg.get("source_mode") == key,
                        lambda k=key: self.set_source_mode(k))
            t.pack(fill="x", pady=2)
            self.src_toggles[key] = t
        if not HAS_SMTC:
            tk.Label(box, text="⚠ Нет модулей winrt — приложения Windows не видны.\n"
                               "Собери exe через build.bat, там они ставятся сами.",
                     bg=C["panel"], fg=C["red"], font=(SANS, 9), justify="left").pack(anchor="w", pady=(6, 0))

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        tk.Label(box, text="ЕСЛИ ИГРАЮТ И МУЗЫКА, И ВИДЕО", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w")
        self.multi_toggles = {}
        for key, text in MULTI_MODES.items():
            t = PToggle(box, text, self.cfg.get("multi", "auto") == key, lambda k=key: self.set_multi(k))
            t.pack(fill="x", pady=2)
            self.multi_toggles[key] = t
        vrow = tk.Frame(box, bg=C["panel"])
        vrow.pack(fill="x", pady=(4, 0))
        tk.Label(vrow, text="ID приложения для видео", bg=C["panel"], fg=C["cream"],
                 font=(SANS, 10)).pack(side="left")
        self.vid_var = tk.StringVar(value=self.cfg.get("client_id_video", ""))
        self.entry(vrow, self.vid_var).pack(side="left", padx=8, ipady=3, fill="x", expand=True)
        PButton(vrow, "OK", self.save_video_id).pack(side="left")
        PButton(vrow, "?", self.help_video_id).pack(side="left", padx=(4, 0))
        tk.Label(box, text="нужно только для «обе карточки»; переключать можно и из трея",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8)).pack(anchor="w", pady=(2, 0))

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x")
        tk.Label(row, text="Не брать из", bg=C["panel"], fg=C["cream"], font=(SANS, 10)).pack(side="left")
        self.ign_var = tk.StringVar(value=", ".join(self.cfg.get("ignore_apps", [])))
        self.entry(row, self.ign_var).pack(side="left", padx=8, ipady=3, fill="x", expand=True)
        PButton(row, "OK", self.save_ignore).pack(side="left")
        tk.Label(box, text="через запятую, например:  chrome, telegram",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 8)).pack(anchor="w", pady=(2, 0))

        tk.Frame(box, bg=C["line"], height=1).pack(fill="x", pady=10)
        tk.Label(box, text="СЕЙЧАС ВИЖУ", bg=C["panel"], fg=C["gold"],
                 font=(SERIF, 9, "bold")).pack(anchor="w")
        self.sessions_box = tk.Frame(box, bg=C["panel"])
        self.sessions_box.pack(fill="x", pady=(4, 0))
        self._sessions_shown = None
        self.render_sessions([])
        return outer

    def render_sessions(self, sessions):
        snapshot = (sessions, bool(self.worker))
        if snapshot == self._sessions_shown:
            return
        self._sessions_shown = snapshot
        for w in self.sessions_box.winfo_children():
            w.destroy()
        if not sessions:
            tk.Label(self.sessions_box, text="ничего не играет" if self.worker else "запусти, чтобы увидеть",
                     bg=C["panel"], fg=C["muted"], font=(SANS, 9, "italic")).pack(anchor="w")
            return
        for app, artist, title, playing in sessions[:6]:
            row = tk.Frame(self.sessions_box, bg=C["panel"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text="●" if playing else "○", bg=C["panel"],
                     fg=C["red"] if playing else C["muted"], font=(SANS, 9)).pack(side="left")
            tk.Label(row, text=app, bg=C["panel"], fg=C["glow"], font=(SANS, 9, "bold"),
                     width=13, anchor="w").pack(side="left", padx=(4, 0))
            line = f"{artist} — {title}" if artist else title
            tk.Label(row, text=line[:48] + ("…" if len(line) > 48 else ""), bg=C["panel"],
                     fg=C["cream"] if playing else C["muted"], font=(SANS, 9), anchor="w").pack(side="left")

    def set_source_mode(self, key):
        for k, t in self.src_toggles.items():
            t.value = (k == key)
            t.render()
        self.cfg["source_mode"] = key
        self.push_cfg()

    def set_multi(self, key):
        if key == "both" and not (self.cfg.get("client_id_video") or "").strip():
            self.show_window()
            self.help_video_id()
        for k, t in getattr(self, "multi_toggles", {}).items():
            t.value = (k == key)
            t.render()
        self.cfg["multi"] = key
        self.push_cfg()
        if self.tray:
            self.tray.refresh()

    def save_video_id(self):
        vid = self.vid_var.get().strip()
        if vid and not vid.isdigit():
            messagebox.showwarning(APP_NAME, "Application ID — это только цифры.")
            return
        if vid and vid == self.cfg.get("client_id", ""):
            messagebox.showwarning(APP_NAME, "Нужен ДРУГОЙ ID, не тот, что для музыки: "
                                             "Discord показывает одну карточку на приложение.")
            return
        self.cfg["client_id_video"] = vid
        if vid and self.cfg.get("multi") != "both":
            self.set_multi("both")
        else:
            self.push_cfg()
        self.status_lbl.config(text="ID для видео сохранён")

    def help_video_id(self):
        messagebox.showinfo("Две карточки: музыка и видео",
            "Discord показывает одну карточку на одно приложение, поэтому для видео нужно второе:\n\n"
            "1. discord.com/developers/applications → New Application → назови «YouTube»\n"
            "   (это имя будет в строке «Смотрит YouTube»).\n"
            "2. Скопируй его Application ID и вставь в поле «ID приложения для видео» → OK.\n"
            "3. По желанию: Rich Presence → Art Assets — загрузи картинку с именем youtube;\n"
            "   она будет, если у ролика нет превью.\n\n"
            "Превью самого ролика плеер берёт из Windows (как в панели громкости).")

    def save_ignore(self):
        self.cfg["ignore_apps"] = [a.strip() for a in self.ign_var.get().split(",") if a.strip()]
        self.push_cfg()
        self.status_lbl.config(text="Список исключённых приложений сохранён")

    def build_hide_tab(self, holder):
        outer, box = frame_box(holder, "Невидимка и чёрный список")
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x")
        tk.Label(row, text="Горячая клавиша", bg=C["panel"], fg=C["cream"], font=(SANS, 10)).pack(side="left")
        self.hk_var = tk.StringVar(value=self.cfg["hotkey"])
        self.entry(row, self.hk_var, 14).pack(side="left", padx=8, ipady=3)
        PButton(row, "Применить", self.register_hotkey).pack(side="left")

        tk.Label(box, text="Не показывать никогда — по строке на правило:\n"
                           "• кусок названия/исполнителя, например  подкаст\n"
                           "• папка, например  D:\\Музыка\\Голосовые",
                 bg=C["panel"], fg=C["muted"], font=(SANS, 9), justify="left").pack(anchor="w", pady=(12, 4))
        self.bl_text = tk.Text(box, height=6, font=(SANS, 10), bg=C["bg"], fg=C["cream"],
                               insertbackground=C["glow"], relief="flat", wrap="none",
                               highlightthickness=1, highlightbackground=C["line"],
                               highlightcolor=C["blue"])
        self.bl_text.pack(fill="both", expand=True)
        self.bl_text.insert("1.0", "\n".join(self.cfg.get("blacklist", [])))

        row2 = tk.Frame(box, bg=C["panel"])
        row2.pack(fill="x", pady=(8, 0))
        PButton(row2, "Сохранить список", self.save_blacklist).pack(side="left", fill="x", expand=True, padx=(0, 6))
        PButton(row2, "✕ Скрыть этот трек", self.blacklist_current).pack(side="left", fill="x", expand=True)
        return outer

    # ── действия ──
    def help_id(self):
        messagebox.showinfo("Как получить Application ID",
            "1. Открой discord.com/developers/applications\n"
            "2. New Application → назови, например, «Музыка»\n"
            "3. Скопируй Application ID и вставь сюда.\n\n"
            "Это одноразово, больше там ничего делать не нужно.")

    def push_cfg(self):
        save_cfg(self.cfg)
        if self.worker:
            self.worker.cfg = self.cfg
            self.worker.last_key = None      # перерисовать статус с новыми настройками

    def save_delay(self):
        try:
            v = max(0, min(60, int(self.delay_var.get())))
        except ValueError:
            return
        if v != self.cfg.get("show_delay"):
            self.cfg["show_delay"] = v
            save_cfg(self.cfg)

    def set_lyrics(self, key):
        for k, t in self.lyr_toggles.items():
            t.value = (k == key)
            t.render()
        self.cfg["lyrics"] = key
        self.push_cfg()
        self.render_preview()

    def save_opts(self):
        self.cfg["covers"] = self.tg_cover.value
        self.cfg["file_covers"] = self.tg_fcover.value
        self.cfg["cover_order"] = "file" if self.tg_ffirst.value else "itunes"
        self.cfg["ytmusic"] = self.tg_ytm.value
        self.cfg["show_paused"] = self.tg_pause.value
        if self.tg_auto.value != self.cfg["autostart"]:
            try:
                set_autostart(self.tg_auto.value)
                self.cfg["autostart"] = self.tg_auto.value
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Не удалось изменить автозапуск: {e}")
                self.tg_auto.value = self.cfg["autostart"]
                self.tg_auto.render()
        self.push_cfg()

    def save_buttons(self):
        by_name = {v[0]: k for k, v in BUTTONS.items()}
        self.cfg["buttons"] = [by_name.get(v.get(), "none") for v in self.btn_vars]
        self.render_preview()
        self.push_cfg()

    def save_blacklist(self, silent=False):
        lines = [l.strip() for l in self.bl_text.get("1.0", "end").splitlines() if l.strip()]
        self.cfg["blacklist"] = lines
        self.push_cfg()
        if not silent:
            self.status_lbl.config(text=f"Чёрный список сохранён ({len(lines)})")

    def blacklist_current(self):
        t = self.current
        if not t:
            messagebox.showinfo(APP_NAME, "Сейчас ничего не играет.")
            return
        rule = t.get("path") or t.get("filename") or t["title"]
        if not messagebox.askyesno(APP_NAME, f"Больше никогда не показывать:\n\n{rule}"):
            return
        text = self.bl_text.get("1.0", "end").rstrip()
        self.bl_text.delete("1.0", "end")
        self.bl_text.insert("1.0", (text + "\n" if text else "") + rule)
        self.save_blacklist()

    def register_hotkey(self, silent=False):
        if not user32:
            return
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        combo = self.hk_var.get().strip() if hasattr(self, "hk_var") else self.cfg["hotkey"]
        hk = Hotkey(combo, lambda: self.q.put({"cmd": "toggle_hidden"}))
        hk.start()
        try:
            err = hk.result.get(timeout=2)
        except queue.Empty:
            err = "не удалось"
        if err:
            if not silent:
                messagebox.showwarning(APP_NAME, f"Горячая клавиша: {err}")
            return
        self.hotkey = hk
        self.cfg["hotkey"] = combo
        save_cfg(self.cfg)
        self.update_hk_hint()
        if not silent:
            self.status_lbl.config(text=f"Горячая клавиша: {combo}")

    def update_hk_hint(self):
        self.hk_hint.config(text=f"невидимка — {self.cfg['hotkey']}")

    def save_fallback(self):
        self.cfg["fallback_image"] = self.fb_var.get().strip()
        self.cfg["browser_icon"] = self.browser_var.get().strip().lower()
        self.push_cfg()
        self.status_lbl.config(text="Картинки сохранены")

    def set_badge(self, key):
        for k, t in self.badge_toggles.items():
            t.value = (k == key)
            t.render()
        self.cfg["badge"] = key
        self.push_cfg()
        self.render_preview()

    def help_icons(self):
        messagebox.showinfo("Значки приложений",
            "Значок ставится сам, загружать ничего не нужно:\n\n"
            "• видео в браузере — значок сайта (YouTube, Twitch, VK…), по ссылке на ролик;\n"
            "• музыка — значок плеера: Spotify, Яндекс Музыка, VLC, AIMP, foobar2000, "
            "Apple Music, Deezer, VK, SoundCloud, MP3 Плеер.\n\n"
            "«Сайт, если ссылка не нашлась» — какой значок ставить для браузера, когда "
            "программа не знает, что за сайт (например youtube или twitch).\n"
            "Если значка для приложения нет — ставится твой логотип.")

    def open_cover_dialog(self):
        tr = self.current
        if not tr or not (tr.get("title") or tr.get("artist")):
            messagebox.showinfo("Своя обложка", "Сначала включи музыку — обложка задаётся для того, "
                                                "что сейчас играет.")
            return
        CoverDialog(self, tr)

    def set_custom_cover(self, track, image, scope):
        """image=None — убрать свою. scope: 'track' / 'album'."""
        cc = self.cfg.setdefault("custom_covers", {})
        t, a = cover_keys(track)
        if image is None:
            cc.pop(t, None)
            if a:
                cc.pop(a, None)
        else:
            if scope == "album" and a:
                cc[a] = image
                cc.pop(t, None)
            else:
                cc[t] = image
        self.cfg["cover_rev"] = self.cfg.get("cover_rev", 0) + 1      # статус обновится сразу
        save_cfg(self.cfg)

    def help_image(self):
        messagebox.showinfo("Своя картинка",
            "1. Открой discord.com/developers/applications → своё приложение\n"
            "2. Rich Presence → Art Assets → Add Image(s)\n"
            "3. Загрузи картинку (лучше 1024×1024) и назови её, например, logo\n"
            "4. Впиши это имя сюда и нажми OK.\n\n"
            "Discord показывает новую картинку не сразу — иногда через 10–15 минут.\n"
            "Можно вместо имени вставить прямую ссылку на картинку https://…")

    def on_minimize(self, event):
        if event.widget is self and self.tray and self.state() == "iconic":
            self.withdraw()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def toggle_hidden(self):
        self.hidden = not self.hidden
        if self.worker:
            self.worker.hidden = self.hidden
        self.hide_btn.config(text="◑  Показывать" if self.hidden else "◐  Невидимка")
        self.hide_btn.set_accent(False)
        self.hide_btn.config(bg="#3a2f66" if self.hidden else C["panel2"])
        self.hide_btn.base = "#3a2f66" if self.hidden else C["panel2"]
        if self.tray:
            self.tray.refresh()
        if not self.worker:
            self.set_state("hidden" if self.hidden else "off",
                           "Невидимка включена" if self.hidden else "Остановлено")

    def setup_vlc(self):
        if vlc_is_running():
            messagebox.showwarning(APP_NAME, "Сначала закрой VLC, потом нажми ещё раз.")
            return
        try:
            configure_vlc(self.cfg["vlc_port"], self.cfg["vlc_password"])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не получилось: {e}")
            return
        messagebox.showinfo(APP_NAME,
            "Готово! Запусти VLC — теперь будут видны время трека, альбом и пауза.\n\n"
            "Если Windows спросит про брандмауэр — можно нажать «Отмена», "
            "интерфейс работает только на этом ПК.")

    def toggle(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
            self.start_btn.config(text="▶  Начать")
            self.start_btn.set_accent(True)
            self.set_state("off", "Остановлено")
            self.track_lbl.config(text="—")
            self.artist_lbl.config(text="")
            self.current = None
            self.render_sessions([])
            if self.tray:
                self.tray.set_title(APP_NAME)
            return
        cid = self.id_var.get().strip()
        if not cid.isdigit():
            self.show_tab("main")
            messagebox.showwarning(APP_NAME, "Application ID — это длинное число. Нажми «?».")
            return
        self.cfg["client_id"] = cid
        self.save_blacklist(silent=True)
        self.worker = Worker(self.cfg, self.q, self.history, self.lastfm)
        self.worker.hidden = self.hidden
        self.worker.start()
        self.start_btn.config(text="■  Остановить")
        self.start_btn.set_accent(False)
        self.set_state("idle", "Подключаюсь…")

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                cmd = msg.get("cmd")
                if cmd == "toggle_hidden":
                    self.toggle_hidden()
                    continue
                if cmd == "played":
                    if getattr(self, "active_tab", "") == "history":
                        self.refresh_history()
                    continue
                if cmd == "show":
                    self.show_window()
                    continue
                if cmd == "multi":
                    self.set_multi(msg["mode"])
                    continue
                if cmd == "quit":
                    self.quit_app()
                    return
                if cmd == "whats_new":
                    self.show_whats_new(max(0, self.build - 5), manual=True)
                    continue
                if cmd == "news":
                    self.news_window(msg["notes"], msg["title"])
                    continue
                if cmd == "check_update":
                    self.check_update(manual=msg.get("manual", False))
                    continue
                if cmd == "toggle_auto_update":
                    self.cfg["auto_update"] = not self.cfg.get("auto_update", True)
                    save_cfg(self.cfg)
                    if self.tray:
                        self.tray.refresh()
                    continue
                if cmd == "update_msg":
                    if self.tray and msg.get("text"):
                        self.tray.notify(msg["text"])
                    elif msg.get("text"):
                        messagebox.showinfo(APP_NAME, msg["text"])
                    if self.tray:
                        self.tray.refresh()
                    continue
                if cmd == "update_ready":
                    self.install_update(msg["path"], msg["build"])
                    return
                if not self.worker:
                    continue
                self.set_state(msg["state"], msg["text"])
                track = msg.get("track")
                self.current = track
                if track:
                    self.track_lbl.config(text=track["title"])
                    extra = "  ·  " + track["album"] if track["album"] else ""
                    self.artist_lbl.config(text=(track["artist"] or "Неизвестный исполнитель") + extra)
                else:
                    self.track_lbl.config(text="—")
                    self.artist_lbl.config(text="")
                self.render_sessions(msg.get("sessions") or [])
                if self.tray:
                    if track and msg["state"] in ("live", "paused"):
                        line = f"{track['artist']} — {track['title']}" if track["artist"] else track["title"]
                        self.tray.set_title(("♪ " if track["playing"] else "⏸ ") + line)
                    elif self.hidden:
                        self.tray.set_title(APP_NAME + " — статус скрыт")
                    else:
                        self.tray.set_title(APP_NAME)
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def on_close(self):
        if self.tray:                     # крестик прячет в трей
            self.withdraw()
            if not self.cfg.get("tray_hint_shown"):
                self.tray.notify("Я в трее возле часов. Правый клик — меню и выход.")
                self.cfg["tray_hint_shown"] = True
                save_cfg(self.cfg)
            return
        self.quit_app()

    # ------------------------------------------------------------ автообновление
    def update_menu_text(self):
        st = self.update_state
        if st.startswith("downloading"):
            return f"Скачиваю обновление… {st.split()[-1]}"
        if st == "checking":
            return "Проверяю обновления…"
        return f"Проверить обновления (сборка {self.build or '—'})"

    def check_whats_new(self):
        if not updater.can_update():
            return
        last = int(self.cfg.get("last_seen_build") or 0)
        self.cfg["last_seen_build"] = self.build
        save_cfg(self.cfg)
        if 0 < last < self.build:
            self.show_whats_new(last)

    def show_whats_new(self, after, manual=False):
        q = self.q

        def work():
            try:
                notes = updater.notes_since(UPDATE_REPO, after, self.build)
            except Exception as e:
                if manual:
                    q.put({"cmd": "update_msg", "text": f"Не удалось загрузить список изменений: {e}"})
                return
            if notes:
                q.put({"cmd": "news", "notes": notes,
                       "title": "Что нового" if manual else f"Обновлено до сборки {self.build}"})
            elif manual:
                q.put({"cmd": "update_msg", "text": "Список изменений пуст."})
        threading.Thread(target=work, daemon=True).start()

    def news_window(self, notes, title):
        w = tk.Toplevel(self)
        w.title(title)
        w.configure(bg=C["bg"])
        w.attributes("-topmost", True)
        try:
            w.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        box = tk.Frame(w, bg=C["panel"], padx=16, pady=12, highlightthickness=1,
                       highlightbackground=C["line"])
        box.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(box, text=title.upper(), bg=C["panel"], fg=C["gold"], font=(SERIF, 13, "bold"),
                 anchor="w").pack(fill="x")
        txt = tk.Text(box, bg=C["panel"], fg=C["cream"], relief="flat", wrap="word", width=54, height=14,
                      font=(SANS, 10), highlightthickness=0)
        txt.tag_configure("h", foreground=C["glow"], font=(SANS, 10, "bold"), spacing1=8)
        for build, text in notes:
            txt.insert("end", f"Сборка {build}\n", "h")
            for ln in text.splitlines():
                if ln.strip():
                    txt.insert("end", ("  " + ln if ln.startswith("•") else "  • " + ln) + "\n")
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, pady=(6, 8))
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x")
        PButton(row, "Все сборки на GitHub", lambda: webbrowser.open(
            f"https://github.com/{UPDATE_REPO}/releases")).pack(side="left")
        PButton(row, "Понятно", w.destroy, accent=True).pack(side="right")
        if self.state() in ("withdrawn", "iconic"):
            w.lift()

    def update_tick(self):
        if updater.can_update() and self.cfg.get("auto_update", True) \
                and updater.due(self.cfg.get("update_checked"), hours=6):
            self.check_update(manual=False)
        self.after(30 * 60 * 1000, self.update_tick)

    def check_update(self, manual=False):
        if not updater.can_update():
            if manual:
                messagebox.showinfo(APP_NAME, "Обновление работает только в собранной программе "
                                              "из релизов GitHub.")
            return
        if self.update_state in ("checking",) or self.update_state.startswith("downloading"):
            return
        self.update_state = "checking"
        threading.Thread(target=self._update_worker, args=(manual,), daemon=True).start()

    def _update_worker(self, manual):
        q = self.q
        try:
            rel = updater.latest(UPDATE_REPO, UPDATE_ASSET)
            self.cfg["update_checked"] = time.time()
            if not rel or rel["build"] <= self.build:
                self.update_state = "latest"
                if manual:
                    q.put({"cmd": "update_msg", "text": f"У тебя последняя версия (сборка {self.build})."})
                return
            if not manual and not self.cfg.get("auto_update", True):
                self.update_state = ""
                return
            q.put({"cmd": "update_msg", "text": f"Вышла сборка {rel['build']} — скачиваю и обновлюсь сам."})
            dest = os.path.join(updater.work_dir("MusicDiscord"), f"Music-Discord-{rel['build']}.exe")

            def prog(p):
                self.update_state = f"downloading {int(p)}%"
            updater.download(rel["url"], dest, rel["size"], prog)
            self.update_state = f"ready {rel['build']}"
            q.put({"cmd": "update_ready", "path": dest, "build": rel["build"]})
        except Exception as e:
            self.update_state = ""
            if manual:
                q.put({"cmd": "update_msg", "text": f"Не удалось проверить обновление: {e}"})

    def install_update(self, path, build):
        save_cfg(self.cfg)
        try:
            hidden = self.state() in ("withdrawn", "iconic")
            updater.apply_exe(path, "MusicDiscord",
                              restart_args=(["--minimized"] if hidden else []) + ["--updated"])
        except Exception as e:
            self.update_state = ""
            messagebox.showerror(APP_NAME, f"Не удалось обновиться: {e}")
            self.after(200, self.poll)
            return
        self.quit_app()

    def quit_app(self):
        if self.tray:
            self.tray.stop()
        if self.hotkey:
            self.hotkey.stop()
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
