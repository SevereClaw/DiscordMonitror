# -*- coding: utf-8 -*-
"""Проверка автообновления на Windows (запускает автосборка GitHub).
  python tests/test_updater.py start  — готовит «старую» программу и запускает замену
  python tests/test_updater.py check  — проверяет, что файлы заменились"""
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402

ROOT = os.path.join(tempfile.gettempdir(), "upd-test", "Папка с пробелом")
APP = os.path.join(ROOT, "app")
EXE1 = os.path.join(ROOT, "one", "Music-Discord.exe")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def start():
    # сборка папкой: старая версия + архив с новой
    write(os.path.join(APP, "MP3Player.exe"), "old")
    write(os.path.join(APP, "_internal", "lib.dll"), "old")
    write(os.path.join(APP, "_internal", "gone.dll"), "old")
    zpath = os.path.join(ROOT, "MP3Player.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("MP3Player/MP3Player.exe", "new")
        z.writestr("MP3Player/_internal/lib.dll", "new")
        z.writestr("MP3Player/_internal/added.dll", "new")
    new_dir = updater.unpack_folder(zpath, "upd-test-folder", "MP3Player.exe")
    sys.executable = os.path.join(APP, "MP3Player.exe")
    updater.apply_folder(new_dir, "upd-test-folder", "MP3Player.exe", restart=False)
    # один exe
    write(EXE1, "old")
    new_exe = os.path.join(ROOT, "new.exe")
    write(new_exe, "new")
    sys.executable = EXE1
    updater.apply_exe(new_exe, "upd-test-exe", restart=False)
    print("scripts started")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def check():
    deadline = time.time() + 40
    want = {os.path.join(APP, "MP3Player.exe"): "new",
            os.path.join(APP, "_internal", "lib.dll"): "new",
            os.path.join(APP, "_internal", "added.dll"): "new",
            EXE1: "new"}
    while time.time() < deadline:
        ok = all(os.path.isfile(p) and read(p) == v for p, v in want.items())
        if ok:
            break
        time.sleep(1)
    for name in ("upd-test-folder", "upd-test-exe"):
        log = os.path.join(updater.work_dir(name), "apply.log")
        if os.path.isfile(log):
            print(name, "log:\n" + open(log, encoding="utf-16", errors="replace").read())
    bad = [p for p, v in want.items() if not (os.path.isfile(p) and read(p) == v)]
    if os.path.exists(os.path.join(APP, "_internal", "gone.dll")):
        bad.append("старый _internal не убран")
    if os.path.exists(os.path.join(APP, "_internal.old")):
        bad.append("_internal.old остался")
    if bad:
        print("FAIL:", bad)
        sys.exit(1)
    print("OK: обе замены сработали")


if __name__ == "__main__":
    start() if sys.argv[1] == "start" else check()
