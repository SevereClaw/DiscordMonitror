# -*- coding: utf-8 -*-
"""Проверка обновлений без api.github.com (у него лимит 60 запросов в час на IP)."""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402

REPO, ASSET = "SevereClaw/DiscordMonitror", "Music-Discord.exe"
fails = []
tag = updater.latest_tag(REPO)
print("тег по переадресации:", tag)
if not tag:
    fails.append("tag")
rel = updater.latest(REPO, ASSET)
print("последний релиз:", rel and {k: rel[k] for k in ("build", "url", "title")})
print("описание:", (rel or {}).get("notes", "")[:200])
if not rel or not rel["url"].startswith("https://github.com/"):
    fails.append("latest")
else:
    req = urllib.request.Request(rel["url"], headers={"User-Agent": updater.UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as r:
        print("файл доступен:", r.status, r.headers.get("Content-Length"))
        if r.status != 200:
            fails.append("asset")
notes = updater.notes_since(REPO, max(0, (rel or {}).get("build", 1) - 3))
print("что нового:", [(b, t[:60]) for b, t in notes])
if not notes:
    fails.append("notes")
print("ИТОГ:", "ошибок нет" if not fails else f"ошибки: {fails}")
sys.exit(1 if fails else 0)
