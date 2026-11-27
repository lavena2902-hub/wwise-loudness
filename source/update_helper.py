"""Standalone Windows installer; runs from a staging directory outside the app."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from updater import APP_ID, EXE_NAME, PACKAGE_MANIFEST, UpdateError, validate_payload, write_json


def wait_for_parent(pid, cancel, timeout=90):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Process already exited.
            return
        raise UpdateError('无法等待原程序退出，请关闭程序后重试。')
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cancel.exists():
                raise UpdateError('安装已取消。')
            result = kernel.WaitForSingleObject(handle, 200)
            if result == 0:
                return
            if result != 258:
                raise UpdateError('等待原程序退出失败。')
        raise UpdateError('原程序未退出，更新已停止。')
    finally:
        kernel.CloseHandle(handle)


def install_transaction(payload, target, version, health_check):
    payload, target = Path(payload).resolve(), Path(target).resolve()
    if (target == target.parent or target == Path.home().resolve() or
            payload.is_relative_to(target) or target.is_relative_to(payload) or
            not (target / EXE_NAME).is_file() or (target / '_internal').is_symlink()):
        raise UpdateError('安装目录无效，更新已停止。')
    validate_payload(payload, version)
    token = uuid.uuid4().hex
    candidate = target.parent / ('.x3-update-' + token)
    backup = target.parent / ('.x3-backup-' + token)
    assert candidate.resolve().parent == target.parent and backup.resolve().parent == target.parent
    components = [EXE_NAME, '_internal', PACKAGE_MANIFEST]
    if (payload / '使用说明.md').is_file():
        components.append('使用说明.md')
    moved, installed = [], []
    try:
        shutil.copytree(payload, candidate)
        validate_payload(candidate, version)
        backup.mkdir()
        for name in components:
            old = target / name
            if old.is_symlink():
                raise UpdateError('安装目录包含不支持的链接：' + name)
            if old.exists():
                os.replace(old, backup / name)
                moved.append(name)
        for name in components:
            os.replace(candidate / name, target / name)
            installed.append(name)
        health_check(target / EXE_NAME, version)
    except Exception as exc:
        recovery_errors = []
        for name in reversed(installed):
            try:
                os.replace(target / name, candidate / name)
            except OSError as error:
                recovery_errors.append(str(error))
        for name in reversed(moved):
            try:
                os.replace(backup / name, target / name)
            except OSError as error:
                recovery_errors.append(str(error))
        if recovery_errors:
            raise UpdateError('更新失败，部分文件未能恢复。备份目录：%s；%s' % (backup, '；'.join(recovery_errors))) from exc
        raise UpdateError('更新未完成，已恢复旧版。请关闭同目录的其他程序窗口，并检查磁盘空间与目录写入权限。\n%s' % exc) from exc
    finally:
        # Delete only our explicitly checked staging sibling. Keep the backup recoverable.
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
    return backup


def verify_startup(executable, version, stage):
    receipt = stage / 'health.json'
    process = subprocess.Popen([str(executable), '--update-health-check', str(receipt)], cwd=executable.parent)
    try:
        code = process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        # PyInstaller onefile/onedir bootloaders may have a child process.
        # Terminate only this health-check process tree so it cannot lock rollback.
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        process.wait(timeout=10)
        raise UpdateError('新版启动检查超时。')
    if code or not receipt.is_file() or json.loads(receipt.read_text(encoding='utf-8')).get('version') != version:
        raise UpdateError('新版启动检查失败。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    options = parser.parse_args()
    request_path = Path(options.request).resolve()
    stage = request_path.parent
    target = None
    try:
        request = json.loads(request_path.read_text(encoding='utf-8'))
        if request.get('application') != APP_ID:
            raise UpdateError('安装请求不匹配。')
        target = Path(request['target']).resolve()
        validate_payload(stage / 'payload', request['version'])
        write_json(stage / 'ready.json', {'ready': True})
        wait_for_parent(int(request['parent_pid']), stage / 'cancel')
        if (stage / 'cancel').exists():
            raise UpdateError('安装已取消。')
        backup = install_transaction(stage / 'payload', target, request['version'],
            lambda executable, version: verify_startup(executable, version, stage))
        write_json(stage / 'result.json', {'status': 'updated', 'version': request['version'], 'backup': str(backup)})
        staged_payload = (stage / 'payload').resolve()
        assert staged_payload.parent == stage.resolve()
        shutil.rmtree(staged_payload)
        subprocess.Popen([str(target / EXE_NAME)], cwd=target)
    except Exception as exc:
        write_json(stage / 'result.json', {'status': 'failed', 'error': str(exc)})
        ctypes.windll.user32.MessageBoxW(0, str(exc) + '\n\n更新日志：' + str(stage / 'result.json'), 'X3-Wwise 更新', 16)
        if target and (target / EXE_NAME).is_file() and (stage / 'ready.json').exists() and not (stage / 'cancel').exists():
            subprocess.Popen([str(target / EXE_NAME)], cwd=target)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
