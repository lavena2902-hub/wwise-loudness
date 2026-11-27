"""Security, integrity, recovery and client-state tests; no live Wwise or GitHub writes."""
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import updater as u
from update_helper import install_transaction
from release import create_release


def payload(root, version='1.0.1', marker=b'new'):
    root.mkdir()
    (root / '_internal').mkdir()
    (root / u.EXE_NAME).write_bytes(marker)
    (root / '_internal' / 'library.bin').write_bytes(marker)
    (root / '_internal' / 'build-config.json').write_text(json.dumps(
        {'version': version, 'repository': 'test-owner/test-repo'}), encoding='utf-8')
    (root / '使用说明.md').write_text('新版说明', encoding='utf-8')
    files = {p.relative_to(root).as_posix(): u.file_hash(p) for p in root.rglob('*') if p.is_file()}
    u.write_json(root / u.PACKAGE_MANIFEST, {'schema': 1, 'application': u.APP_ID, 'version': version, 'files': files})
    return root


def archive(root, path):
    with zipfile.ZipFile(path, 'w') as z:
        for p in root.rglob('*'):
            if p.is_file():
                z.write(p, p.relative_to(root).as_posix())
    return path


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='x3-update-test-')
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def release_data(self):
        package = {'name': 'X3-Wwise-1.0.10-windows-x64.zip', 'size': 100,
            'browser_download_url': 'https://github.com/test-owner/test-repo/releases/download/v1.0.10/X3-Wwise-1.0.10-windows-x64.zip'}
        manifest_asset = {'name': 'update.json', 'browser_download_url': 'https://github.com/test-owner/test-repo/releases/download/v1.0.10/update.json'}
        release = {'tag_name': 'v1.0.10', 'draft': False, 'prerelease': False, 'assets': [package, manifest_asset], 'body': 'Release notes'}
        manifest = {'schema': 1, 'application': u.APP_ID, 'version': '1.0.10',
            'windows_x64': {'asset': package['name'], 'sha256': 'a' * 64, 'size': 100}}
        return release, manifest

    def test_versions_and_release_binding(self):
        release, manifest = self.release_data()
        fetched = []
        def fetch(url):
            fetched.append(url)
            return manifest
        manifest['notes'] = 'Release notes'
        result = u.check_release('https://github.com/test-owner/test-repo', '1.0.9', fetch)
        self.assertEqual(result['version'], '1.0.10')
        self.assertEqual(result['notes'], 'Release notes')
        self.assertEqual(result['url'], release['assets'][0]['browser_download_url'])
        self.assertTrue(all('/releases/latest/download/update.json' in url for url in fetched))
        self.assertIsNone(u.check_release('test-owner/test-repo', '1.0.10', fetch))
        self.assertIsNone(u.check_release('test-owner/test-repo', '2.0.0', fetch))
        manifest['version'] = '1.0.11'
        with self.assertRaises(u.UpdateError):
            u.check_release('test-owner/test-repo', '1.0.9', fetch)
        manifest['version'] = '1.0.10'
        manifest['windows_x64']['asset'] = 'https://github.com/other/repo/package.zip'
        with self.assertRaises(u.UpdateError):
            u.check_release('test-owner/test-repo', '1.0.9', fetch)
        for bad in ('1.2', '1.2.3-beta', '1.02.3', '../1.2.3'):
            with self.assertRaises(u.UpdateError): u.version_tuple(bad)

    def test_network_origin_and_manifest_limits(self):
        for bad in ('http://github.com/a/b', 'https://localhost/file', 'https://github.com.evil.com/file',
                    'https://user:pass@github.com/a', 'https://github.com:8080/a'):
            with self.assertRaises(u.UpdateError): u.trusted_url(bad)
        self.assertEqual(u.normalize_repo('https://github.com/a/b.git'), 'a/b')
        with patch.object(u, 'open_url', return_value=io.BytesIO(b'{}' * 6)):
            with self.assertRaises(u.UpdateError): u.fetch_json('unused', limit=5)
        with patch.object(u, 'open_url', return_value=io.BytesIO(b'not json')):
            with self.assertRaises(u.UpdateError): u.fetch_json('unused')

    def test_archive_rejects_paths_symlinks_duplicates_and_state(self):
        for name in ('../outside', '/outside', 'C:/outside', '_internal/../outside',
                     '_internal\\outside', 'undo_state.json', '_internal/CON.txt', '_internal/file:stream'):
            bad = self.root / 'bad.zip'
            with zipfile.ZipFile(bad, 'w') as z: z.writestr(name, b'bad')
            with self.assertRaises(u.UpdateError):
                u.extract_package(bad, self.root / ('extract-' + str(len(list(self.root.iterdir())))), '1.0.1')
        bad = self.root / 'link.zip'
        info = zipfile.ZipInfo('_internal/link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(bad, 'w') as z: z.writestr(info, '../outside')
        with self.assertRaises(u.UpdateError): u.extract_package(bad, self.root / 'link', '1.0.1')
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('_internal/a', 'a'); z.writestr('_internal/A', 'a')
        with self.assertRaises(u.UpdateError): u.extract_package(bad, self.root / 'duplicate', '1.0.1')
        self.assertFalse((self.root / 'outside').exists())

    def test_download_hash_cancel_and_tamper(self):
        source = payload(self.root / 'new')
        package = archive(source, self.root / 'valid.zip')
        release = {'url': 'test', 'size': package.stat().st_size, 'sha256': u.file_hash(package), 'version': '1.0.1'}
        cache = self.root / 'cache'
        cache.mkdir()
        with patch.dict('os.environ', {'LOCALAPPDATA': str(cache)}):
            stage = u.stage_release(release, opener=lambda _: io.BytesIO(package.read_bytes()))
            self.assertTrue((stage / 'payload' / u.EXE_NAME).is_file())
            with self.assertRaises(u.UpdateError):
                u.stage_release({**release, 'sha256': '0' * 64}, opener=lambda _: io.BytesIO(package.read_bytes()))
            event = threading.Event(); event.set()
            with self.assertRaises(u.UpdateError):
                u.stage_release(release, cancel=event, opener=lambda _: io.BytesIO(package.read_bytes()))
            self.assertEqual(len(list((cache / 'X3-Wwise' / 'updates').iterdir())), 1)
        (source / '_internal' / 'library.bin').write_bytes(b'tampered')
        with self.assertRaises(u.UpdateError): u.validate_payload(source, '1.0.1')

    def make_installed(self):
        old = payload(self.root / 'installed', '1.0.0', b'old')
        (old / '_internal' / 'obsolete.bin').write_bytes(b'old')
        for name in ('undo_state.json', 'last_write_report.json', 'update_config.json', 'user-notes.txt'):
            (old / name).write_text('preserve ' + name, encoding='utf-8')
        return old

    def test_install_preserves_user_state_and_keeps_backup(self):
        old, new = self.make_installed(), payload(self.root / 'new')
        calls = []
        backup = install_transaction(new, old, '1.0.1', lambda exe, version: calls.append(version))
        self.assertEqual(calls, ['1.0.1'])
        self.assertEqual((old / u.EXE_NAME).read_bytes(), b'new')
        self.assertFalse((old / '_internal' / 'obsolete.bin').exists())
        self.assertEqual((backup / u.EXE_NAME).read_bytes(), b'old')
        for name in ('undo_state.json', 'last_write_report.json', 'update_config.json', 'user-notes.txt'):
            self.assertEqual((old / name).read_text(encoding='utf-8'), 'preserve ' + name)

    def test_failed_startup_or_locked_files_restore_old(self):
        old, new = self.make_installed(), payload(self.root / 'new')
        before = {p.relative_to(old).as_posix(): p.read_bytes() for p in old.rglob('*') if p.is_file()}
        def fail(*_): raise RuntimeError('startup failed')
        with self.assertRaisesRegex(u.UpdateError, '已恢复旧版'):
            install_transaction(new, old, '1.0.1', fail)
        self.assertEqual(before, {p.relative_to(old).as_posix(): p.read_bytes() for p in old.rglob('*') if p.is_file()})
        original = u.os.replace
        locked_paths = []
        def locked(source, destination):
            if Path(source).resolve() == (old / '_internal').resolve():
                locked_paths.append(Path(source).resolve())
                raise PermissionError('file locked')
            return original(source, destination)
        with patch('update_helper.os.replace', side_effect=locked):
            with self.assertRaisesRegex(u.UpdateError, '已恢复旧版'):
                install_transaction(new, old, '1.0.1', lambda *_: None)
        self.assertEqual(locked_paths, [(old / '_internal').resolve()])
        self.assertEqual(before, {p.relative_to(old).as_posix(): p.read_bytes() for p in old.rglob('*') if p.is_file()})

    def test_publisher_excludes_personal_records(self):
        build = payload(self.root / 'build')
        (build / 'undo_state.json').write_text('private')
        (build / 'last_write_report.json').write_text('private')
        (build / 'update_config.json').write_text('private')
        package, manifest = create_release(build, self.root / 'release', '1.0.1', '测试说明')
        with zipfile.ZipFile(package) as z:
            self.assertFalse(any(name.endswith('state.json') or name == 'update_config.json' for name in z.namelist()))
            self.assertNotIn('last_write_report.json', z.namelist())
        data = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual(data['windows_x64']['sha256'], u.file_hash(package))
        self.assertEqual(data['notes'], '测试说明')
        u.extract_package(package, self.root / 'unpacked', '1.0.1')

    def test_update_window_keeps_actions_visible_at_all_sizes(self):
        import tkinter as tk
        import app
        root = tk.Tk(); root.withdraw()
        try:
            with patch.object(app, 'BASE', str(self.root)):
                ui = app.App(root, check_updates=False)
                root.deiconify()
                ui.update_window.deiconify()
                for width, height in ((560, 500), (640, 580), (800, 700)):
                    ui.update_window.geometry('%dx%d' % (width, height))
                    for downloading in (False, True):
                        ui.update_downloading = downloading
                        ui._update_controls()
                        ui.update_status_var.set('网络错误：' + '请检查网络连接后重试。' * 100)
                        ui._set_update_notes('# 版本更新说明\n\n## 1.0.2\n' + '- 完整更新说明\n' * 100)
                        root.update()
                        for button in (ui.btn_update_install, ui.btn_update_cancel,
                                       ui.btn_update_save, ui.btn_update_check):
                            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                                 ui.update_window.winfo_rooty() + ui.update_window.winfo_height())
                        self.assertGreaterEqual(ui.update_notes.winfo_height(), 60)
                        self.assertLess(ui.update_notes.yview()[1], 1)
                        self.assertLess(ui.update_status_text.yview()[1], 1)
                        self.assertEqual(str(ui.update_status_text['state']), 'disabled')
                        self.assertEqual(str(ui.btn_update_cancel['state']), 'normal' if downloading else 'disabled')
                self.assertNotIn('#', ui.update_notes.get('1.0', 'end'))
                self.assertTrue(ui.update_notes.tag_ranges('heading'))
                ui.update_cancel.clear()
                ui.btn_update_cancel.invoke()
                self.assertTrue(ui.update_cancel.is_set())
                ui._handle_msg(('update_failed', '已取消下载，请重新检查更新。'))
                self.assertFalse(ui.update_downloading)
                self.assertEqual(str(ui.btn_update_check['state']), 'normal')
                ui._handle_msg(('update_checked', None))
                self.assertIn('当前版本无需更新', ui.update_notes.get('1.0', 'end'))
        finally:
            root.destroy()

    def test_gui_update_states_preserve_wwise_preview(self):
        import tkinter as tk
        from types import SimpleNamespace
        import app
        root = tk.Tk(); root.withdraw()
        try:
            with patch.object(app, 'BASE', str(self.root)):
                ui = app.App(root, check_updates=False)
                ui.pipe = SimpleNamespace(client=object(), has_undo=lambda: False)
                ui.has_selection = ui.has_plan = ui.has_preview = True
                ui._set_buttons_working(False)
                ui.update_check_running = True
                ui._handle_msg(('update_failed', '网络不可用'))
                self.assertTrue(ui.has_plan and ui.has_selection and not ui.working)
                ui._handle_msg(('update_checked', {'version': '1.0.1', 'notes': '测试更新'}))
                self.assertTrue(ui.has_plan)
                with patch('sys.frozen', True, create=True):
                    ui._update_controls()
                    self.assertEqual(str(ui.btn_update_install['state']), 'normal')
                    ui._set_buttons_working(True)
                    self.assertEqual(str(ui.btn_update_install['state']), 'disabled')
                    self.assertEqual(str(ui.btn_update['state']), 'disabled')
                    ui._set_buttons_working(False)
                root.geometry('1040x680'); root.deiconify(); root.update()
                header = ui.btn_update.master
                title = header.master.grid_slaves(row=0, column=0)[0]
                self.assertLessEqual(title.winfo_rootx() + title.winfo_width(), header.winfo_rootx())
                self.assertLessEqual(header.winfo_rootx() + header.winfo_width(), root.winfo_rootx() + root.winfo_width())
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
