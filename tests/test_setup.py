# -*- coding: utf-8 -*-
"""Подключение к Discord: разбор Application ID, понятные ошибки и мастер по шагам.
На Windows поднимает поддельный Discord (именованный канал discord-ipc-0) и проходит
мастер целиком: ID из буфера обмена → проверка → программа запущена с этим ID."""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import types

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="md-test-")     # чистые настройки
for m in ["pystray"]:
    sys.modules.setdefault(m, types.ModuleType(m))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vlc_discord as vd  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(("OK  " if ok else "FAIL"), name, "->", repr(got)[:110])
    if not ok:
        fails.append(name)


ID = "1234567890123456789"

# ── что вставляют в поле ──
check("чистый ID", vd.parse_app_id(ID), (ID, ""))
check("с пробелами и переводом строки", vd.parse_app_id(f"  {ID[:6]} {ID[6:]}\n"), (ID, ""))
check("в кавычках", vd.parse_app_id(f'"{ID}"'), (ID, ""))
check("ссылка на страницу приложения",
      vd.parse_app_id(f"https://discord.com/developers/applications/{ID}/information"), (ID, ""))
check("17 цифр — тоже ID", vd.parse_app_id("1" * 17)[0], "1" * 17)
for name, bad, word in (
        ("пусто", "   ", "пустое"),
        ("мало цифр", "12345", "17–20"),
        ("много цифр", "1" * 25, "17–20"),
        ("Public Key", "ab12" * 16, "Public Key"),
        ("токен бота", ".".join(["x" * 26, "y" * 6, "z" * 38]), "токен"),   # три части через точку
        ("Client Secret", "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0dF", "Client Secret"),
        ("слова", "Музыка", "только цифры")):
    cid, why = vd.parse_app_id(bad)
    check(f"не ID: {name}", (cid, word in why), ("", True))

if os.name != "nt":
    print("не Windows — проверки с каналом Discord и окном пропущены")
    sys.exit(1 if fails else 0)

import _winapi  # noqa: E402

PIPE = r"\\.\pipe\discord-ipc-0"


def fake_discord(op, reply):
    """Один раз отвечает на рукопожатие, как Discord. Возвращает поток."""
    h = _winapi.CreateNamedPipe(PIPE, _winapi.PIPE_ACCESS_DUPLEX, 0, 1, 65536, 65536, 0, _winapi.NULL)
    got = {}

    def run():
        try:
            try:
                _winapi.ConnectNamedPipe(h)
            except OSError:
                pass                                 # клиент успел подключиться раньше
            head, _ = _winapi.ReadFile(h, 8)
            _, n = struct.unpack("<II", head)
            body, _ = _winapi.ReadFile(h, n)
            got["hello"] = json.loads(body.decode("utf-8"))
            payload = json.dumps(reply).encode("utf-8")
            _winapi.WriteFile(h, struct.pack("<II", op, len(payload)) + payload)
            time.sleep(1.5)                          # дать клиенту дочитать
        finally:
            _winapi.CloseHandle(h)
    t = threading.Thread(target=run, daemon=True)
    t.got = got
    t.start()
    return t


READY = {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1, "user": {"username": "tester", "global_name": "Тестер"}}}


def kind_of(cid):
    ipc = vd.DiscordIPC(cid)
    try:
        ipc.connect()
        return "ok", ipc.user
    except vd.DiscordError as e:
        return e.kind, str(e)
    finally:
        ipc.close()


check("Discord на сервере сборки не запущен", vd.discord_running(), False)
k, text = kind_of(ID)
check("нет Discord → no_discord", k, "no_discord")
check("…и про браузер сказано", "браузер" in text, True)

t = fake_discord(2, {"code": 4000, "message": "Invalid Client ID"})
k, text = kind_of(ID)
t.join(5)
check("не тот ID → bad_id", k, "bad_id")
check("…и совет скопировать заново", "Copy" in text, True)
check("Discord получил наш ID", t.got.get("hello", {}).get("client_id"), ID)

t = fake_discord(2, {"code": 4004, "message": "Something else"})
k, text = kind_of(ID)
t.join(5)
check("другая ошибка → rejected с текстом Discord", (k, "Something else" in text), ("rejected", True))

t = fake_discord(1, READY)
check("всё хорошо → имя аккаунта", kind_of(ID), ("ok", "Тестер"))
t.join(5)

# ── мастер по шагам ──
app = vd.App()


def pump(sec, until=lambda: False):
    end = time.time() + sec
    while time.time() < end and not until():
        app.update()
        time.sleep(0.05)


pump(1.5)                                            # без настроек мастер открывается сам
wiz = getattr(app, "setup_win", None)
check("первый запуск: мастер открылся сам", bool(wiz and wiz.alive), True)
if not wiz:
    wiz = app.open_setup()
check("повторное открытие — то же окно", app.open_setup() is wiz, True)
check("поле пустое — подсказка ждёт ID", "Жду" in wiz.id_hint.cget("text"), True)

wiz.check()                                          # без ID проверка не идёт
check("без ID — не проверяем", (wiz.busy, "шаг 2" in wiz.id_hint.cget("text")), (False, True))

app.clipboard_clear()
app.clipboard_append("ab12" * 16)                    # скопировали Public Key — не берём
pump(1.2)
check("Public Key из буфера не подставился", wiz.id_var.get(), "")
wiz.paste()
check("«Вставить» объясняет про Public Key", "Public Key" in wiz.id_hint.cget("text"), True)
wiz.id_var.set("")

app.clipboard_clear()
app.clipboard_append(f"https://discord.com/developers/applications/{ID}/information")
pump(3, lambda: wiz.id_var.get() == ID)
check("ID пойман из буфера обмена", wiz.id_var.get(), ID)

wiz.check()                                          # Discord «закрыт»
pump(8, lambda: not wiz.busy)
check("Discord закрыт — понятный совет",
      ("Что делать" in wiz.result.cget("text"), wiz.done, app.worker is None), (True, False, True))

t = fake_discord(1, READY)
wiz.check()
pump(8, lambda: wiz.done)
t.join(5)
check("проверка прошла", wiz.done, True)
check("в окне — аккаунт", "Тестер" in wiz.result.cget("text"), True)
check("ID сохранён в настройках", vd.load_cfg().get("client_id"), ID)
check("ID в главном окне", app.id_var.get(), ID)
check("показ статуса запущен", app.worker is not None, True)
wiz.check()                                          # кнопка теперь «Готово — закрыть»
pump(0.3)
check("мастер закрылся", app.setup_win, None)

app.id_var.set("ab12" * 16)                          # «Начать» с мусором в поле → снова мастер
app.toggle()                                         # остановить
app.toggle()
pump(0.5)
check("плохой ID в поле → мастер с объяснением",
      bool(app.setup_win) and "Public Key" in app.setup_win.id_hint.cget("text"), True)
app.quit_app()

print("\nИТОГО:", "всё хорошо" if not fails else f"упало: {fails}")
sys.exit(1 if fails else 0)
