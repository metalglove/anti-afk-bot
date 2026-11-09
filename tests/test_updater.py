"""Tests for updater.py. Run with: python -m unittest discover tests

The test that runs the update script itself only runs on Windows (it needs cmd and robocopy).
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import updater  # noqa: E402


def release(tag, *names):
    return {'tag_name': tag, 'html_url': 'page',
            'assets': [{'name': n, 'browser_download_url': f'https://example.com/{n}', 'size': 1} for n in names]}


class PickUpdate(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updater.parse_version('v1.2'), (1, 2, 0))
        self.assertEqual(updater.parse_version('1.10.3'), (1, 10, 3))
        self.assertIsNone(updater.parse_version('dev'))
        self.assertIsNone(updater.parse_version('v1.2.0-beta'))

    def test_newer_release_with_zip(self):
        found = updater.pick_update(release('v1.2.0', 'notes.txt', 'AntiAFK-v1.2.0-windows.zip'), '1.1.9')
        self.assertEqual(found['version'], '1.2.0')
        self.assertTrue(found['url'].endswith('-windows.zip'))

    def test_no_update(self):
        rel = release('v1.2.0', 'AntiAFK-v1.2.0-windows.zip')
        self.assertIsNone(updater.pick_update(rel, '1.2.0'))
        self.assertIsNone(updater.pick_update(rel, '1.10.0'))  # compared as numbers, not text
        self.assertIsNone(updater.pick_update(rel, 'dev'))  # running from source
        self.assertIsNone(updater.pick_update(release('v2.0.0', 'source.zip'), '1.0.0'))  # no app ZIP


class Install(unittest.TestCase):
    def test_unpacks_and_starts_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, 'AntiAFK-v2.0.0-windows.zip')
            with zipfile.ZipFile(zip_path, 'w') as z:
                z.writestr('AntiAFK/AntiAFK.exe', 'new exe')
                z.writestr('AntiAFK/_internal/lib.dll', 'lib')
                z.writestr('AntiAFK/settings.json', '{}')
            app = os.path.join(tmp, 'app')
            os.mkdir(app)
            started = []
            real_popen, real_app_dir = subprocess.Popen, updater.app_dir
            subprocess.Popen = lambda command, **kw: started.append(command)
            updater.app_dir = lambda: app
            try:
                updater.install({'url': 'file:///' + zip_path.replace(os.sep, '/').lstrip('/'), 'size': 1})
            finally:
                subprocess.Popen, updater.app_dir = real_popen, real_app_dir
            # cmd /d /s /c ""script" "pid" "source" "target" "work" "exe""
            pid, source, target, work, exe = started[0].split('"')[4::2][:5]
            self.assertEqual((pid, target, exe), (str(os.getpid()), app, 'AntiAFK.exe'))
            self.assertEqual(sorted(os.listdir(source)), ['AntiAFK.exe', '_internal'])  # settings.json dropped
            self.assertTrue(os.path.exists(os.path.join(work, 'update.cmd')))


@unittest.skipUnless(sys.platform == 'win32', "needs cmd and robocopy")
class UpdateScript(unittest.TestCase):
    def test_swaps_files_and_keeps_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, 'work ä')  # spaces and accents in folder names
            new = os.path.join(work, 'new')
            app = os.path.join(tmp, 'My Apps', 'AntiAFK é')
            files = {
                new: {'AntiAFK.exe': 'new exe', 'rules.json': 'new rules', '_internal/lib.dll': 'new lib',
                      # stands in for AntiAFK.exe, so the test can see that it was started
                      'launch.cmd': f'@echo started> "{os.path.join(tmp, "started.txt")}"'},
                app: {'AntiAFK.exe': 'old exe', 'rules.json': 'my rules', '_internal/lib.dll': 'old lib',
                      '_internal/stale.dll': 'old', 'settings.json': 'my settings', 'matches.csv': 'my matches'},
            }
            for root, content in files.items():
                for name, text in content.items():
                    path = os.path.join(root, name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'w') as f:
                        f.write(text)
            script = os.path.join(work, 'update.cmd')
            with open(script, 'w', newline='\r\n') as f:
                f.write(updater.UPDATE_SCRIPT)
            subprocess.run(updater.script_command(script, 999999, new, app, work, 'launch.cmd'), timeout=60)

            def read(name):
                with open(os.path.join(app, name)) as f:
                    return f.read()
            self.assertEqual(read('AntiAFK.exe'), 'new exe')
            self.assertEqual(read('rules.json'), 'new rules')
            self.assertEqual(read('rules.json.bak'), 'my rules')
            self.assertEqual(read('_internal/lib.dll'), 'new lib')
            self.assertFalse(os.path.exists(os.path.join(app, '_internal', 'stale.dll')))
            self.assertEqual(read('settings.json'), 'my settings')
            self.assertEqual(read('matches.csv'), 'my matches')
            for _ in range(50):  # the new app is started in the background
                if os.path.exists(os.path.join(tmp, 'started.txt')) and not os.path.exists(work):
                    break
                time.sleep(0.2)
            self.assertTrue(os.path.exists(os.path.join(tmp, 'started.txt')))
            self.assertFalse(os.path.exists(work), "the temporary folder is cleaned up")


if __name__ == '__main__':
    unittest.main()
