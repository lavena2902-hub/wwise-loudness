# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本 - 生成与 v3 一致的 onedir 产物

注意：OTTOAgent 运行环境会注入 TCL_LIBRARY / TK_LIBRARY 指向它自己的
_tcl_data（Tcl 8.6.12），与系统 Python 3.13 的 Tcl 8.6.15 版本冲突。
这会导致 PyInstaller 找不到 tkinter 组件、打不出 tk86t.dll，
最终产物启动即崩溃。所以打包前必须清除这些变量。
"""
import os
import subprocess
import sys
import json
from pathlib import Path

from updater import DEFAULT_VERSION, normalize_repo, version_tuple

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = 'X3-Wwise响度与增益管理器'


def main():
    import argparse
    parser = argparse.ArgumentParser(description='构建独立 Windows 桌面程序')
    parser.add_argument('--output', default=os.path.join(HERE, 'work', 'package'))
    parser.add_argument('--version', default=DEFAULT_VERSION)
    parser.add_argument('--repo', default='')
    parser.add_argument('--ffmpeg', help='ffmpeg.exe 所在目录')
    options = parser.parse_args()
    output = os.path.abspath(options.output)
    version_tuple(options.version)
    repository = normalize_repo(options.repo) if options.repo else ''
    ffmpeg = options.ffmpeg or os.path.join(HERE, 'ffmpeg_src')
    if not os.path.isdir(ffmpeg):
        ffmpeg = os.path.join(os.path.dirname(HERE), '_internal', 'ffmpeg')
    if not os.path.isfile(os.path.join(ffmpeg, 'ffmpeg.exe')):
        print('找不到 ffmpeg.exe：请保留程序目录中的 _internal/ffmpeg。')
        return 2
    os.makedirs(output, exist_ok=True)
    py = sys.executable
    print('使用解释器:', py)

    # 关键：清掉 OTTOAgent 注入的 Tcl/Tk 路径，否则 tkinter 打包失败
    env = os.environ.copy()
    removed = []
    for k in ('TCL_LIBRARY', 'TK_LIBRARY', 'TCL_LIBRARY_ORIG', 'TK_LIBRARY_ORIG'):
        if k in env:
            removed.append('%s=%s' % (k, env[k]))
            del env[k]
    if removed:
        print('已清除干扰变量:')
        for r in removed:
            print('   ', r)

    # 自检：确认 tkinter 真的可用
    chk = subprocess.run(
        [py, '-c', 'import tkinter; r=tkinter.Tk(); r.withdraw(); '
                   'print(tkinter.TkVersion); r.destroy()'],
        capture_output=True, text=True, env=env)
    if chk.returncode != 0:
        print('!! tkinter 自检失败：')
        print(chk.stderr[:1200])
        return 2
    print('tkinter 自检通过, TkVersion =', chk.stdout.strip())

    config = Path(output) / 'build-config.json'
    config.write_text(json.dumps({'version': options.version.lstrip('v'), 'repository': repository}), encoding='utf-8')
    helper_output = Path(output) / 'helper'
    helper_args = [py, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
        '--name', 'update-helper', '--distpath', str(helper_output / 'dist'),
        '--workpath', str(helper_output / 'build'), '--specpath', str(helper_output),
        os.path.join(HERE, 'update_helper.py')]
    helper_result = subprocess.call(helper_args, cwd=HERE, env=env)
    if helper_result:
        return helper_result

    args = [
        py, '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--onedir',
        '--windowed',
        '--name', NAME,
        '--distpath', os.path.join(output, 'dist'),
        '--workpath', os.path.join(output, 'build'),
        '--specpath', output,
        '--add-data', ffmpeg + os.pathsep + 'ffmpeg',
        '--add-data', str(config) + os.pathsep + '.',
        '--add-binary', str(helper_output / 'dist' / 'update-helper.exe') + os.pathsep + '.',
        '--hidden-import', 'waapi',
        '--collect-all', 'waapi',
        os.path.join(HERE, 'app.py'),
    ]
    print('执行打包 ...')
    rc = subprocess.call(args, cwd=HERE, env=env)
    print('PyInstaller 返回:', rc)
    if rc == 0:
        manual = Path(HERE).parent / '使用说明.md'
        if manual.is_file():
            import shutil
            shutil.copy2(manual, Path(output) / 'dist' / NAME / manual.name)
    return rc


if __name__ == '__main__':
    sys.exit(main())
