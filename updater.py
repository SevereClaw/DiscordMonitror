# -*- coding: utf-8 -*-
"""Автообновление из релизов GitHub (общий для MP3 Плеера и Music → Discord).

Номер сборки вписывает автосборка GitHub в build_info.py (BUILD = N), релизы
называются build-N. Программа сравнивает свой номер с последним релизом, качает
новый файл и запускает маленький скрипт PowerShell: он ждёт, пока программа
закроется, кладёт новые файлы на место старых и запускает её снова.
Запуск из исходников (python …py) — BUILD = 0, обновления выключены."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SelfUpdater/1.0"


def current_build():
    try:
        import build_info
        return int(build_info.BUILD)
    except Exception:
        return 0


def can_update():
    return sys.platform == "win32" and getattr(sys, "frozen", False) and current_build() > 0


def latest(repo, asset_name, timeout=10):
    """Последний релиз: {'build', 'url', 'size', 'title', 'notes'} или None."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.loads(r.read().decode("utf-8"))
    m = re.search(r"(\d+)\s*$", rel.get("tag_name") or "")
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == asset_name), None)
    if not m or not asset:
        return None
    return {"build": int(m.group(1)), "url": asset["browser_download_url"],
            "size": int(asset.get("size") or 0), "title": rel.get("name") or "",
            "notes": (rel.get("body") or "").strip()}


def download(url, dest, size=0, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    got = 0
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or size or 0)
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(min(100.0, got * 100.0 / total))
    if size and got != size:
        os.remove(tmp)
        raise IOError(f"скачалось {got} из {size} байт")
    os.replace(tmp, dest)
    return dest


def work_dir(app):
    d = os.path.join(tempfile.gettempdir(), f"{app}-update")
    os.makedirs(d, exist_ok=True)
    return d


def _clean_env():
    """Окружение без следов PyInstaller — иначе новая копия возьмёт DLL из папки старой."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("_PYI") or k in ("_MEIPASS2", "TCL_LIBRARY", "TK_LIBRARY", "TKPATH",
                                         "PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH",
                                         "QML2_IMPORT_PATH"):
            env.pop(k, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _run_script(body, app):
    path = os.path.join(work_dir(app), "apply.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:      # BOM — чтобы PowerShell 5 понял кириллицу
        f.write(body)
    flags = 0x00000008 | 0x00000200 | 0x08000000          # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", path],
                     env=_clean_env(), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_WAIT = """$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path (Split-Path $PSCommandPath) 'apply.log'
"start $(Get-Date)" | Out-File $log
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Process -Id {pid} -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {{
    Start-Sleep -Milliseconds 300 }}
Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
"""


def apply_exe(new_exe, app, restart_args=(), restart=True):
    """Один exe (Music → Discord): заменить и перезапустить."""
    target = sys.executable
    args = " ".join(_ps_quote(a) for a in restart_args)
    start = (f"Start-Process -FilePath {_ps_quote(target)} " + (f"-ArgumentList {args}" if args else "")
             if restart else "")
    body = _WAIT.format(pid=os.getpid()) + f"""
$ok = $false
for ($i = 0; $i -lt 20 -and -not $ok; $i++) {{
    try {{ Copy-Item -LiteralPath {_ps_quote(new_exe)} -Destination {_ps_quote(target)} -Force -ErrorAction Stop; $ok = $true }}
    catch {{ "copy: $_" | Out-File $log -Append; Start-Sleep -Milliseconds 700 }}
}}
"copied: $ok" | Out-File $log -Append
{start}
if ($ok) {{ Remove-Item -LiteralPath {_ps_quote(new_exe)} -Force }}
"""
    _run_script(body, app)


def unpack_folder(zip_path, app, exe_name):
    """Распаковать архив сборки «папкой»; вернуть папку, где лежит exe."""
    dest = os.path.join(work_dir(app), "new")
    if os.path.isdir(dest):
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    for root, _dirs, files in os.walk(dest):
        if exe_name in files:
            return root
    raise IOError(f"в архиве нет {exe_name}")


def apply_folder(new_dir, app, exe_name, restart_args=(), restart=True):
    """Сборка папкой (MP3 Плеер): заменить содержимое папки программы и перезапустить."""
    app_dir = os.path.dirname(sys.executable)
    target = os.path.join(app_dir, exe_name)
    args = " ".join(_ps_quote(a) for a in restart_args)
    start = (f"Start-Process -FilePath {_ps_quote(target)} " + (f"-ArgumentList {args}" if args else "")
             if restart else "")
    body = _WAIT.format(pid=os.getpid()) + f"""
$src = {_ps_quote(new_dir)}
$dst = {_ps_quote(app_dir)}
$old = Join-Path $dst '_internal.old'
Remove-Item -LiteralPath $old -Recurse -Force
$ok = $false
for ($i = 0; $i -lt 15 -and -not $ok; $i++) {{
    try {{
        if (Test-Path (Join-Path $dst '_internal')) {{ Rename-Item -LiteralPath (Join-Path $dst '_internal') '_internal.old' -ErrorAction Stop }}
        Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force -ErrorAction Stop
        $ok = $true
    }} catch {{
        "copy: $_" | Out-File $log -Append
        if (-not (Test-Path (Join-Path $dst '_internal')) -and (Test-Path $old)) {{ Rename-Item -LiteralPath $old '_internal' }}
        Start-Sleep -Milliseconds 800
    }}
}}
"copied: $ok" | Out-File $log -Append
if ($ok) {{ Remove-Item -LiteralPath $old -Recurse -Force; Remove-Item -LiteralPath $src -Recurse -Force }}
{start}
"""
    _run_script(body, app)


def due(last_check, hours=6):
    return time.time() - float(last_check or 0) > hours * 3600
