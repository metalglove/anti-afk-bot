"""Updates the downloaded app (AntiAFK.exe) from GitHub Releases.

check() asks GitHub for the latest release and returns it if it's newer than this
version. install() downloads its ZIP, unpacks it, and starts a small script that waits
for the app to close, copies the new files over the app folder (keeping settings.json
and matches.csv) and starts the new version. Running from source (python app.py) has
no version, so it never offers updates.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

try:
    from version import VERSION  # written by the release build
except ImportError:
    VERSION = 'dev'

REPO = 'metalglove/anti-afk-bot'
RELEASES_PAGE = f'https://github.com/{REPO}/releases/latest'
LATEST_API = f'https://api.github.com/repos/{REPO}/releases/latest'
ASSET_SUFFIX = '-windows.zip'
EXE_NAME = 'AntiAFK.exe'
KEEP_FILES = ('settings.json', 'matches.csv')  # the user's own files, never replaced
TIMEOUT = 15

# Run by cmd with: pid, new files folder, app folder, temp folder, exe name.
# Arguments (not text in the script) so folder names with accents work.
UPDATE_SCRIPT = r"""@echo off
setlocal
set /a tries=0
:wait
tasklist /FI "PID eq %~1" /NH 2>nul | find "%~1" >nul || goto copy
set /a tries+=1
if %tries% geq 60 goto copy
ping -n 2 127.0.0.1 >nul
goto wait
:copy
if exist "%~3\rules.json" (fc /b "%~3\rules.json" "%~2\rules.json" >nul 2>nul || copy /y "%~3\rules.json" "%~3\rules.json.bak" >nul)
robocopy "%~2\_internal" "%~3\_internal" /MIR /R:10 /W:1 /NFL /NDL /NJH /NJS >nul
robocopy "%~2" "%~3" /R:10 /W:1 /XD _internal /XF settings.json matches.csv /NFL /NDL /NJH /NJS >nul
start "" "%~3\%~5"
(goto) 2>nul & rmdir /s /q "%~4"
"""


def is_app_build():
    """True when running as the downloaded AntiAFK.exe (not from source)."""
    return getattr(sys, 'frozen', False) and parse_version(VERSION) is not None


def app_dir():
    return os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))


def parse_version(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); None if it isn't a plain version."""
    m = re.fullmatch(r'v?(\d+)\.(\d+)(?:\.(\d+))?', (text or '').strip())
    return tuple(int(part or 0) for part in m.groups()) if m else None


def _get(url, accept='application/vnd.github+json'):
    request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'AntiAFK-updater'})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def pick_update(release, current=VERSION):
    """The update in a GitHub release (API JSON), or None if it's not newer or has no ZIP."""
    latest, have = parse_version(release.get('tag_name')), parse_version(current)
    if latest is None or have is None or latest <= have:
        return None
    for asset in release.get('assets', []):
        if asset.get('name', '').lower().endswith(ASSET_SUFFIX):
            return {'version': release['tag_name'].lstrip('v'), 'url': asset['browser_download_url'],
                    'size': asset.get('size', 0), 'page': release.get('html_url', RELEASES_PAGE)}
    return None


def check():
    """The newer release as a dict (version, url, size, page), or None. Raises OSError/ValueError
    when GitHub can't be reached."""
    with _get(LATEST_API) as response:
        return pick_update(json.load(response))


def can_write(folder):
    """True if new files can be put in the app folder (not e.g. Program Files)."""
    probe = os.path.join(folder, '.update-check')
    try:
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def find_app_folder(root):
    """The folder inside an unpacked ZIP that holds AntiAFK.exe."""
    for folder, _, files in os.walk(root):
        if EXE_NAME in files:
            return folder
    return None


def install(update, progress=None):
    """Download and unpack the update, then start the script that swaps it in once this app
    closes. The caller must close the app right after. Raises OSError/ValueError on failure.
    progress(fraction) is called while downloading."""
    target = app_dir()
    if not can_write(target):
        raise OSError(f"Can't write to {target}. Move the AntiAFK folder to your Desktop or "
                      f"Documents and start it from there.")
    work = tempfile.mkdtemp(prefix='antiafk-update-')
    zip_path = os.path.join(work, 'update.zip')
    with _get(update['url'], accept='application/octet-stream') as response, open(zip_path, 'wb') as out:
        total = int(response.headers.get('Content-Length') or update.get('size') or 0)
        done = 0
        while chunk := response.read(256 * 1024):
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(done / total, 1.0))
    unpacked = os.path.join(work, 'new')
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(unpacked)
    os.remove(zip_path)
    source = find_app_folder(unpacked)
    if source is None:
        raise ValueError(f"The download doesn't contain {EXE_NAME}.")
    for name in KEEP_FILES:  # a release never ships these, but don't overwrite them if it does
        path = os.path.join(source, name)
        if os.path.exists(path):
            os.remove(path)
    script = os.path.join(work, 'update.cmd')
    with open(script, 'w', newline='\r\n') as f:
        f.write(UPDATE_SCRIPT)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen(script_command(script, os.getpid(), source, target, work, EXE_NAME),
                     creationflags=flags, close_fds=True, cwd=tempfile.gettempdir())


def script_command(script, *args):
    """The command line that runs the update script. cmd /c mangles a list of quoted paths
    (folders with spaces), so wrap them all in one more pair of quotes and use /s."""
    quoted = ' '.join(f'"{arg}"' for arg in (script, *args))
    return f'cmd /d /s /c "{quoted}"'
