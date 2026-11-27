"""GitHub Releases update protocol and verified package staging. No WAAPI calls."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

APP_ID = 'x3-wwise-loudness'
EXE_NAME = 'X3-Wwise响度与增益管理器.exe'
PACKAGE_MANIFEST = 'release-package.json'
DEFAULT_VERSION = '1.0.0'
MAX_ZIP_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * MAX_ZIP_BYTES
ALLOWED_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                 'objects.githubusercontent.com'}


class UpdateError(Exception):
    pass


def version_tuple(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match:
        raise UpdateError('版本号须为三段数字，例如 1.0.1。')
    return tuple(map(int, match.groups()))


def normalize_repo(value):
    value = str(value).strip().rstrip('/')
    if value.startswith('https://github.com/'):
        value = value[len('https://github.com/'):]
    if value.endswith('.git'):
        value = value[:-4]
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+', value):
        raise UpdateError('请填写公开 GitHub 仓库，例如 账号/仓库，或完整仓库地址。')
    return value


def bundled_path(name):
    return Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / name


def build_info():
    path = bundled_path('build-config.json')
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    version = data.get('version', DEFAULT_VERSION)
    version_tuple(version)
    repository = normalize_repo(data['repository']) if data.get('repository') else ''
    return {'version': version, 'repository': repository}


def read_config(base):
    info = build_info()
    path = Path(base) / 'update_config.json'
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        info.update(repository=normalize_repo(data['repository']) if data.get('repository') else '',
                    auto_check=bool(data.get('auto_check', True)),
                    last_check=float(data.get('last_check', 0)))
    info.setdefault('auto_check', True)
    info.setdefault('last_check', 0)
    return info


def write_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def save_config(base, config):
    write_json(Path(base) / 'update_config.json', {key: config[key]
               for key in ('repository', 'auto_check', 'last_check')})


def trusted_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise UpdateError('更新下载地址须使用 GitHub 的 HTTPS 服务。')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def open_url(url):
    request = urllib.request.Request(trusted_url(url), headers={
        'User-Agent': 'X3-Wwise-Updater/1', 'Accept': 'application/vnd.github+json'})
    try:
        return urllib.request.build_opener(SafeRedirect()).open(request, timeout=25)
    except urllib.error.HTTPError as exc:
        messages = {404: '没有找到已发布的正式版本。请核对公开仓库地址及 Releases。',
                    403: 'GitHub 暂时限制请求，或仓库无法公开访问。请稍后重试。',
                    429: '检查更新过于频繁，请稍后重试。'}
        raise UpdateError(messages.get(exc.code, 'GitHub 请求失败，状态码 %s。' % exc.code)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError('无法连接 GitHub，请检查网络后重试。%s' % exc) from exc


def fetch_json(url, limit=512 * 1024):
    with open_url(url) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise UpdateError('更新信息超过大小限制。')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise UpdateError('更新信息格式不正确。') from exc
    if not isinstance(data, dict):
        raise UpdateError('更新信息必须是 JSON 对象。')
    return data


def check_release(repository, current_version, fetch=fetch_json):
    repository = normalize_repo(repository)
    # GitHub's latest/download route excludes draft and pre-release versions and
    # avoids unauthenticated API limits for teams sharing an office IP address.
    manifest = fetch(f'https://github.com/{repository}/releases/latest/download/update.json')
    if manifest.get('schema') != 1 or manifest.get('application') != APP_ID:
        raise UpdateError('更新信息的程序或格式不匹配。')
    newest = version_tuple(manifest.get('version'))
    if newest <= version_tuple(current_version):
        return None
    version = '.'.join(map(str, newest))
    package = manifest.get('windows_x64', {})
    name, digest, size = package.get('asset'), package.get('sha256', ''), package.get('size')
    if (name != f'X3-Wwise-{version}-windows-x64.zip' or
            not re.fullmatch(r'[a-fA-F0-9]{64}', str(digest)) or
            not isinstance(size, int) or not 0 < size <= MAX_ZIP_BYTES):
        raise UpdateError('Windows 更新附件或校验信息不完整。')
    return {'version': version, 'repository': repository,
            'url': f'https://github.com/{repository}/releases/download/v{version}/{name}',
            'sha256': digest.lower(), 'size': size,
            'notes': str(manifest.get('notes') or '此版本暂无更新说明。')[:16000]}


def file_hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def managed_name(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or path.is_absolute() or str(path) != name or
            any(part in ('', '.', '..') or ':' in part or part.endswith((' ', '.')) for part in path.parts) or
            any(re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part) for part in path.parts)):
        raise UpdateError('更新包包含非法文件路径。')
    if name not in (EXE_NAME, '使用说明.md', PACKAGE_MANIFEST) and not name.startswith('_internal/'):
        raise UpdateError('更新包包含不允许覆盖的文件：' + name)
    return path


def validate_payload(root, expected_version):
    root = Path(root).resolve()
    manifest_path = root / PACKAGE_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise UpdateError('更新包缺少文件清单。')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = manifest.get('files', {})
    if (manifest.get('application') != APP_ID or manifest.get('schema') != 1 or
            version_tuple(manifest.get('version')) != version_tuple(expected_version) or
            not isinstance(files, dict) or EXE_NAME not in files or
            not any(name.startswith('_internal/') for name in files)):
        raise UpdateError('更新包的程序、版本或文件清单不匹配。')
    seen = set()
    for name, digest in files.items():
        managed_name(name)
        if name == PACKAGE_MANIFEST or name.casefold() in seen:
            raise UpdateError('更新包包含重复文件。')
        seen.add(name.casefold())
        path = root / name
        if (not re.fullmatch(r'[a-fA-F0-9]{64}', str(digest)) or path.is_symlink() or
                not path.resolve().is_relative_to(root) or not path.is_file() or file_hash(path) != digest.lower()):
            raise UpdateError('更新文件校验失败：' + name)
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise UpdateError('更新包不能包含符号链接。')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files) | {PACKAGE_MANIFEST}:
        raise UpdateError('更新包包含未列入清单的文件。')
    return manifest


def extract_package(archive, destination, expected_version):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    seen, expanded = set(), 0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 20000:
            raise UpdateError('更新包文件数量超过限制。')
        required = sum(info.file_size for info in bundle.infolist())
        if required > MAX_EXPANDED_BYTES or shutil.disk_usage(destination).free < required + 32 * 1024 * 1024:
            raise UpdateError('更新包超过大小限制或磁盘空间不足。')
        for info in bundle.infolist():
            if info.is_dir():
                continue
            managed_name(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or info.filename.casefold() in seen or info.flag_bits & 1:
                raise UpdateError('更新包包含链接、重复路径或加密文件。')
            seen.add(info.filename.casefold())
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise UpdateError('更新包解压大小超过限制。')
            path = destination / info.filename
            if not path.resolve().is_relative_to(destination):
                raise UpdateError('更新包路径越界。')
            path.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output)
    return validate_payload(destination, expected_version)


def stage_release(release, progress=lambda *_: None, cancel=None, opener=open_url):
    cancel = cancel or threading.Event()
    cache = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'X3-Wwise' / 'updates'
    cache.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='update-', dir=cache)).resolve()
    assert stage.is_relative_to(cache.resolve())
    try:
        archive = stage / 'package.zip'
        digest, count = hashlib.sha256(), 0
        with opener(release['url']) as response, archive.open('wb') as output:
            while True:
                if cancel.is_set():
                    raise UpdateError('已取消下载，当前版本保持不变。')
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                count += len(chunk)
                if count > release['size']:
                    raise UpdateError('更新包大小与发布信息不匹配。')
                output.write(chunk)
                digest.update(chunk)
                progress(count, release['size'])
        if count != release['size'] or digest.hexdigest() != release['sha256']:
            raise UpdateError('更新包下载不完整或 SHA-256 校验失败，请重新下载。')
        if cancel.is_set():
            raise UpdateError('已取消下载，当前版本保持不变。')
        progress(release['size'], release['size'], '正在校验并解压更新…')
        extract_package(archive, stage / 'payload', release['version'])
        archive.unlink()
        return stage
    except Exception:
        shutil.rmtree(stage)
        raise


def start_installer(stage, target, release):
    if not getattr(sys, 'frozen', False):
        raise UpdateError('请使用打包后的 EXE 执行自动安装。')
    source = bundled_path('update-helper.exe')
    if not source.is_file():
        raise UpdateError('缺少更新安装组件，请获取完整的程序包。')
    stage, target = Path(stage).resolve(), Path(target).resolve()
    helper = stage / 'update-helper.exe'
    shutil.copy2(source, helper)
    request = stage / 'install.json'
    write_json(request, {'target': str(target), 'version': release['version'],
                         'parent_pid': os.getpid(), 'application': APP_ID})
    child = subprocess.Popen([str(helper), '--request', str(request)], cwd=stage,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (stage / 'ready.json').is_file():
            return
        if child.poll() is not None:
            break
        time.sleep(.1)
    (stage / 'cancel').touch()
    raise UpdateError('无法启动安装组件，程序仍保留当前版本。请查看更新日志后重试。')
