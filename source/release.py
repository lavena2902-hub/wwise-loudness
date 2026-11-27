"""Create GitHub Releases assets from an onedir build; never include user state."""
import argparse
import json
from pathlib import Path
import zipfile

from updater import APP_ID, EXE_NAME, PACKAGE_MANIFEST, file_hash, version_tuple, validate_payload


def create_release(build, output, version, notes='', bootstrap=False):
    build, output = Path(build).resolve(), Path(output).resolve()
    version_tuple(version)
    version = version.lstrip('v')
    config = json.loads((build / '_internal' / 'build-config.json').read_text(encoding='utf-8'))
    if config.get('version') != version or (not config.get('repository') and not bootstrap):
        raise ValueError('构建版本不匹配或未设置 GitHub 仓库；请使用 build.py --version --repo。')
    files = [build / EXE_NAME] + sorted((build / '_internal').rglob('*'))
    if (build / '使用说明.md').is_file():
        files.append(build / '使用说明.md')
    files = [path for path in files if path.is_file()]
    manifest = {'application': APP_ID, 'schema': 1, 'version': version,
                'files': {path.relative_to(build).as_posix(): file_hash(path) for path in files}}
    (build / PACKAGE_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    # Detect accidental user-state contamination before any distributable is written.
    for path in files:
        if path.is_symlink():
            raise ValueError('发布包不能包含符号链接。')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'X3-Wwise-{version}-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files + [build / PACKAGE_MANIFEST]:
            bundle.write(path, path.relative_to(build).as_posix())
    release = {'schema': 1, 'application': APP_ID, 'version': version, 'notes': notes,
               'windows_x64': {'asset': archive.name, 'sha256': file_hash(archive), 'size': archive.stat().st_size}}
    manifest_path = output / 'update.json'
    manifest_path.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding='utf-8')
    return archive, manifest_path


def main():
    parser = argparse.ArgumentParser(description='生成 GitHub Releases 更新附件')
    parser.add_argument('--build', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--notes-file')
    parser.add_argument('--bootstrap', action='store_true', help='创建尚未预设仓库的一次性初始安装包')
    options = parser.parse_args()
    notes = Path(options.notes_file).read_text(encoding='utf-8') if options.notes_file else ''
    for path in create_release(options.build, options.output, options.version, notes, options.bootstrap):
        print(path)


if __name__ == '__main__':
    main()
