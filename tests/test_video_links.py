# -*- coding: utf-8 -*-
"""Ссылка на видео: файл сессии Firefox (настоящий формат mozlz4) и живой поиск YouTube."""
import json
import os
import struct
import sys
import tempfile
import types

for m in ["pystray"]:
    sys.modules.setdefault(m, types.ModuleType(m))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lz4.block  # noqa: E402
import vlc_discord as vd  # noqa: E402

fails = []
session = {"windows": [{"tabs": [
    {"index": 2, "entries": [{"title": "YouTube", "url": "https://www.youtube.com/"},
                             {"title": "(3) Rick Astley - Never Gonna Give You Up (Official Video) - YouTube",
                              "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s"}]},
    {"index": 1, "entries": [{"title": "Почта " * 2000, "url": "https://mail.example/"}]}]}]}
raw = json.dumps(session, ensure_ascii=False).encode("utf-8")
blob = b"mozLz40\0" + struct.pack("<I", len(raw)) + lz4.block.compress(raw, store_size=False)

prof = os.path.join(tempfile.mkdtemp(), "Mozilla", "Firefox", "Profiles", "abc.default-release",
                    "sessionstore-backups")
os.makedirs(prof)
with open(os.path.join(prof, "recovery.jsonlz4"), "wb") as f:
    f.write(blob)
os.environ["APPDATA"] = prof.split(os.sep + "Mozilla")[0]

real = vd.mozlz4_decode(blob)                      # через lz4
saved = sys.modules.pop("lz4.block"), sys.modules.pop("lz4")
sys.modules["lz4"] = None                          # без lz4 — свой распаковщик
pure = vd.mozlz4_decode(blob)
sys.modules["lz4.block"], sys.modules["lz4"] = saved
print("распаковка:", len(real), "байт; свой распаковщик совпал:", real == pure)
if real != raw or pure != raw:
    fails.append("mozlz4")

tabs = vd.firefox_tabs()
print("вкладки:", [t[1] for t in tabs])
tr = {"title": "Rick Astley - Never Gonna Give You Up (Official Video)", "artist": "Rick Astley",
      "app": "Firefox"}
vd.VIDEO_LINKS._find((tr["title"], tr["artist"]), "Firefox")
info = vd.VIDEO_LINKS.cache.get((tr["title"], tr["artist"]))
print("из Firefox:", info)
if not info or "dQw4w9WgXcQ" not in info.get("url", "") or "dQw4w9WgXcQ" not in info.get("thumb", ""):
    fails.append("ссылка из Firefox")

try:
    vid = vd.youtube_search("Rick Astley Never Gonna Give You Up", "Rick Astley")
    print("поиск YouTube:", vid)
    if not vid:
        print("(поиск ничего не вернул — YouTube мог показать проверку для сервера)")
except Exception as e:
    print("поиск YouTube недоступен с сервера:", e)

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("OK: ссылка на видео находится")
