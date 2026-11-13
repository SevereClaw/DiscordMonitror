# -*- coding: utf-8 -*-
"""Прокрутка главного окна: в низком окне всё содержимое достаётся колёсиком."""
import os
import sys
import tempfile
import time
import types

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="md-test-")     # чистые настройки
for m in ["pystray"]:
    sys.modules.setdefault(m, types.ModuleType(m))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vlc_discord as vd  # noqa: E402

if os.name != "nt":
    print("не Windows — проверка окна пропущена")
    sys.exit(0)

fails = []


def check(name, got, want=True):
    ok = got == want
    print(("OK  " if ok else "FAIL"), name, "->", repr(got)[:110])
    if not ok:
        fails.append(name)


app = vd.App()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.update()
        time.sleep(0.05)


class Wheel:
    def __init__(self, widget, delta):
        self.widget, self.delta = widget, delta


pump(1.2)
if getattr(app, "setup_win", None):
    app.setup_win.close()
check("окно можно сделать низким", app.minsize()[1] <= 400)
app.geometry("540x380+0+0")
pump(0.6)
cv = app.scroll_cv
check("окно стало 380", app.winfo_height(), 380)

for key in app.tabs:
    app.show_tab(key)
    pump(0.5)
    need, view = app.body.winfo_reqheight(), cv.winfo_height()
    check(f"«{key}»: содержимое выше окна — полоса есть", (need > view, app.bar_shown), (True, True))
    cv.yview_moveto(0)
    app.on_wheel(Wheel(app.body, -120))
    top = cv.yview()[0]
    check(f"«{key}»: колёсико вниз крутит", top > 0)
    for _ in range(80):
        app.on_wheel(Wheel(app.body, -120))
    check(f"«{key}»: докручивается до самого низа", round(cv.yview()[1], 3), 1.0)
    for _ in range(80):
        app.on_wheel(Wheel(app.body, 120))
    check(f"«{key}»: и обратно наверх", cv.yview()[0], 0.0)

app.show_tab("hide")
pump(0.4)
cv.yview_moveto(0)
app.on_wheel(Wheel(app.bl_text, -120))               # пустой список сам не листается → едет страница
check("над пустым списком крутится страница", cv.yview()[0] > 0)
app.bl_text.insert("1.0", "\n".join(f"строка {i}" for i in range(60)))
pump(0.3)
cv.yview_moveto(0)
app.on_wheel(Wheel(app.bl_text, -120))               # длинный список листается сам
check("над длинным списком страница стоит", cv.yview()[0], 0.0)
app.bl_text.delete("1.0", "end")

app.on_wheel(Wheel(app.body, -40))                   # тачпад: мелкие шаги копятся
check("мелкий шаг тачпада тоже крутит", cv.yview()[0] > 0)

app.show_tab("main")
need = app.body.winfo_reqheight()
if need + 120 < app.winfo_screenheight():
    app.geometry(f"540x{need + 40}+0+0")
    pump(0.6)
    check("высокое окно — полосы нет", app.bar_shown, False)
    app.on_wheel(Wheel(app.body, -120))
    check("…и крутить нечего", cv.yview()[0], 0.0)
else:
    print("экран ниже содержимого — проверка высокого окна пропущена")

app.geometry("540x430+0+0")
pump(0.5)
app.quit_app()
check("высота окна запомнилась", vd.load_cfg().get("window_h"), 430)

print("\nИТОГО:", "всё хорошо" if not fails else f"упало: {fails}")
sys.exit(1 if fails else 0)
