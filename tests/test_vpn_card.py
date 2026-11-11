# -*- coding: utf-8 -*-
"""Карточка VPN: значок NekoBoxP3R крупно, флаг в углу, пинг и трафик."""
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


check("1,5 КБ", vd.human_size(1536), "1,5 КБ")
check("1,2 ГБ", vd.human_size(int(1.2 * 1024 ** 3)), "1,2 ГБ")
check("850 МБ", vd.human_size(850 * 1024 ** 2), "850 МБ")
st = {"connected": True, "country": "DE", "server": "Trojan Germany", "mode": "vpn", "since": 1,
      "version": "0.20.3", "up": 80 * 1024 ** 2, "down": int(1.2 * 1024 ** 3), "ping": 85}
a = vd.build_vpn_activity(st, {})
check("крупно — окно NekoBoxP3R", a["assets"]["large_image"], vd.NEKO_CARD)
check("в углу — флаг", a["assets"]["small_image"], "https://flagcdn.com/w160/de.png")
check("строка 1", a["details"], "🛡 🇩🇪 Германия · Trojan Germany")
check("строка 2", a["state"], "📶 85 мс · ↓ 1,2 ГБ ↑ 80 МБ")
old = {k: v for k, v in st.items() if k not in ("up", "down", "ping")}
b = vd.build_vpn_activity(old, {})
check("старый NekoBoxP3R — как раньше", b["state"], "Trojan Germany · VPN для всех программ")
check("выключено", vd.build_vpn_activity(st, {"vpn_stats": False})["state"], "Trojan Germany · VPN для всех программ")
print("ИТОГ:", "ошибок нет" if not fails else f"ошибки: {fails}")
sys.exit(1 if fails else 0)
