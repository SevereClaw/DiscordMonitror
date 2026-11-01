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


def notes_since(repo, after_build, upto_build=None, limit=15, timeout=10):
    """Описания сборок новее after_build: [(номер, текст), …] — от новой к старой."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases?per_page=30",
                                 headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rels = json.loads(r.read().decode("utf-8"))
    out = []
    for rel in rels:
        m = re.search(r"(\d+)\s*$", rel.get("tag_name") or "")
        if not m:
            continue
        b = int(m.group(1))
        if b > after_build and (upto_build is None or b <= upto_build):
            out.append((b, clean_notes(rel.get("body") or "")))
    out.sort(reverse=True)
    return out[:limit]


def clean_notes(body):
    """Из описания релиза — только пункты «что нового» (без строки про скачивание)."""
    lines = []
    for ln in body.splitlines():
        t = ln.strip()
        if not t or t.lower().startswith(("скачай", "download")):
            continue
        if t.lower().startswith("сборка №") and ":" in t:        # старый формат описания
            t = "• " + t.split(":", 1)[1].strip()
        if t[0] in "-*•":
            t = "• " + t.lstrip("-*• ").strip()
        lines.append(t)
    return "\n".join(lines)


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


_SCRIPT = r"""$ErrorActionPreference = 'Continue'
$here = Split-Path -LiteralPath $PSCommandPath
$log = Join-Path $here 'apply.log'
function Log($t) { Add-Content -LiteralPath $log -Value $t -Encoding UTF8 }
Set-Content -LiteralPath $log -Value "start $(Get-Date)" -Encoding UTF8
$p = Get-Content -LiteralPath (Join-Path $here 'apply.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Process -Id $p.pid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300 }
Stop-Process -Id $p.pid -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 800
$ok = $false
if ($p.mode -eq 'exe') {
    for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
        try { Copy-Item -LiteralPath $p.src -Destination $p.target -Force -ErrorAction Stop; $ok = $true }
        catch { Log "copy: $_"; Start-Sleep -Milliseconds 700 }
    }
    if ($ok) { Remove-Item -LiteralPath $p.src -Force -ErrorAction SilentlyContinue }
} else {
    $dst = $p.dir
    $int = Join-Path $dst '_internal'
    $old = Join-Path $dst '_internal.old'
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 15 -and -not $ok; $i++) {
        try {
            if (Test-Path -LiteralPath $int) { Rename-Item -LiteralPath $int '_internal.old' -ErrorAction Stop }
            Get-ChildItem -LiteralPath $p.src -Force | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $dst -Recurse -Force -ErrorAction Stop }
            $ok = $true
        } catch {
            Log "copy: $_"
            if (-not (Test-Path -LiteralPath $int) -and (Test-Path -LiteralPath $old)) {
                Rename-Item -LiteralPath $old '_internal' -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 800
        }
    }
    if ($ok) {
        Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $p.src -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Log "copied: $ok"
if ($p.restart) {
    if ($p.args.Count -gt 0) { Start-Process -FilePath $p.target -ArgumentList $p.args }
    else { Start-Process -FilePath $p.target }
    Log 'restarted'
}
"""


def _run_script(params, app):
    """Скрипт — только латиница; пути (бывают по-русски) — в apply.json в UTF-8."""
    d = work_dir(app)
    with open(os.path.join(d, "apply.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False)
    path = os.path.join(d, "apply.ps1")
    with open(path, "w", encoding="ascii", newline="\r\n") as f:
        f.write(_SCRIPT)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", path]
    # Без DETACHED_PROCESS: с ним PowerShell не стартует. Скрипт и так переживает
    # выход программы — на Windows дочерний процесс не умирает вместе с родителем.
    base = 0x00000200 | 0x08000000                       # NEW_PROCESS_GROUP | NO_WINDOW
    for flags in (base | 0x01000000, base):              # + BREAKAWAY_FROM_JOB, если можно
        try:
            subprocess.Popen(cmd, env=_clean_env(), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return path
        except OSError:
            continue
    raise OSError("не удалось запустить PowerShell")


def apply_exe(new_exe, app, restart_args=(), restart=True):
    """Один exe (Music → Discord): заменить и перезапустить."""
    return _run_script({"mode": "exe", "pid": os.getpid(), "src": os.path.abspath(new_exe),
                        "target": sys.executable, "restart": bool(restart),
                        "args": list(restart_args)}, app)


def unpack_folder(zip_path, app, exe_name):
    """Распаковать архив сборки «папкой»; вернуть папку, где лежит exe."""
    import shutil
    dest = os.path.join(work_dir(app), "new")
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
    return _run_script({"mode": "folder", "pid": os.getpid(), "src": os.path.abspath(new_dir),
                        "dir": app_dir, "target": os.path.join(app_dir, exe_name),
                        "restart": bool(restart), "args": list(restart_args)}, app)


def due(last_check, hours=6):
    return time.time() - float(last_check or 0) > hours * 3600
