# -*- coding: utf-8 -*-
"""Определение сайта ролика по вкладкам: Rutube, аниме-сайты, скрытые сайты."""
import os
import sys
import types

for m in ["pystray"]:
    sys.modules.setdefault(m, types.ModuleType(m))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vlc_discord as vd  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(("OK  " if ok else "FAIL"), name, "->", got)
    if not ok:
        fails.append(name)


H = vd.HIDDEN_SITES_DEFAULT
tabs = [("Почта", "https://mail.ru/"),
        ("Магическая битва 2 сезон 5 серия смотреть онлайн — AnimeGO", "https://animego.org/anime/jujutsu-5"),
        ("Как устроен двигатель — смотреть видео онлайн от «Гараж» в хорошем качестве", "https://rutube.ru/video/abc/")]
check("аниме — по словам", vd.match_tab("Магическая битва 2 сезон 5 серия", "", tabs, H), tabs[1][1])
check("rutube — целиком", vd.match_tab("Как устроен двигатель", "", tabs, H), tabs[2][1])
one = [tabs[0], ("Наруто: Ураганные хроники 120 серия — Jut.su", "https://jut.su/naruuto/episode-120.html")]
check("плеер с чужим названием — единственный видеосайт", vd.match_tab("Kodik Player", "", one, H), one[1][1])
check("скрытый сайт открыт — не гадаем",
      vd.match_tab("Kodik Player", "", one + [("x", "https://rt.pornhub.com/view")], H), None)
check("название сайта Rutube", vd.site_name("https://rutube.ru/video/abc/"), "Rutube")
check("название сайта Jut.su", vd.site_name("https://jut.su/x"), "Jut.su")
check("незнакомый сайт — адресом", vd.site_name("https://www.anime-sama.fr/x"), "anime-sama.fr")

# неизвестный сайт: пока ищем — скрыт, не нашли — показываем
tr = {"app": "Chrome", "title": "Какое-то аниме 3 серия", "artist": "", "album": ""}
cfg = {"hide_unknown_sites": True}
vd.VIDEO_LINKS.busy.add((tr["title"], ""))           # «ищем»
check("пока ищем — скрыто", vd.is_hidden_site(tr, cfg), True)
vd.VIDEO_LINKS.cache[(tr["title"], "")] = {"_t": 9e18}   # «не нашли»
check("не нашли — показываем", vd.is_hidden_site(tr, cfg), False)
check("слово из скрытых в названии — скрыто",
      vd.is_hidden_site(dict(tr, title="hentai 3"), cfg) if vd.VIDEO_LINKS.cache.setdefault(("hentai 3", ""), {"_t": 9e18}) else None, True)

print("ИТОГ:", "ошибок нет" if not fails else f"ошибки: {fails}")
sys.exit(1 if fails else 0)
