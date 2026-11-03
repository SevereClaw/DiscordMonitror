# -*- coding: utf-8 -*-
"""YouTube Music: живой поиск трека — обложка (квадратная) и ссылка."""
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vlc_discord as vd  # noqa: E402

CASES = [  # исполнитель, название, длительность (с) — как их отдаёт Windows
    ("Rosa Walton", "I Really Want to Stay at Your House", 246),
    ("Atlus", "Colors Flying High -opening movie version", 96),
    ("Король и Шут", "Лесник", 0),
    ("Lotus Juice", "Full Moon Full Life", 0),
]
found = 0
for a, t, d in CASES:
    tr = {"artist": a, "title": t, "length": d}
    vd.YT_MUSIC.get(tr)
    for _ in range(60):
        if not vd.YT_MUSIC.pending(tr):
            break
        time.sleep(0.25)
    hit = vd.YT_MUSIC.get(tr)
    print(f"{a} — {t}:", (f"{hit['title']} / {hit['artist']} / {hit['album']} / {hit['dur']} c\n   {hit['url']}\n   {hit['thumb']}"
                          if hit else f"не найдено ({vd.YT_MUSIC.status or 'нет совпадения'})"))
    if hit:
        found += 1
        req = urllib.request.Request(hit["thumb"], headers={"User-Agent": vd.DISCORD_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            head = r.read(16)
        ok = vd._looks_like_image(head)
        print("   обложка открывается как картинка:", ok)
        if not ok:
            found -= 1
act = vd.build_activity(dict(artist="Rosa Walton", title="I Really Want to Stay at Your House", album="",
                             playing=True, length=246, time=10, app="MP3 Плеер", src="t"),
                        {"covers": False, "lyrics": "off", "ytmusic": True, "file_covers": False})
print("карточка:", act.get("details_url"), act.get("assets"))
bad_icons = 0
for name, url in (("YouTube", vd.favicon_url("youtube.com")), ("Twitch", vd.favicon_url("twitch.tv")),
                  ("Spotify", vd.favicon_url("open.spotify.com")), ("MP3 Плеер", vd.MP3P_ICON)):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": vd.DISCORD_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = vd._looks_like_image(r.read(16))
    except Exception as e:
        ok = False
        print("  ", e)
    print(f"значок {name}: {'картинка ✓' if ok else 'НЕ ОТКРЫВАЕТСЯ'}")
    bad_icons += not ok
print(f"найдено {found} из {len(CASES)}")
if bad_icons:
    sys.exit(1)
sys.exit(0 if found >= 3 and act.get("details_url") else 1)
