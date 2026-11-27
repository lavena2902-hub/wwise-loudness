# -*- coding: utf-8 -*-
"""
Wwise 响度归一化流水线核心模块

逻辑与 wwise-loudness skill 中的 loudness_normalize.py 一致，
重构为带进度回调的类，供 tkinter GUI 调用。

响度调整只写 Sound 或所选音乐层级的 MakeUpGain，影响该对象的全部源。
音频源 VolumeOffset 只读取并计入 Output；兼容旧源增益撤回快照。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import event_report
from concurrent.futures import ThreadPoolExecutor, as_completed

CALL_TIMEOUT = 60          # 单次 WAAPI 调用的线程超时（秒）
AUDIO_TYPES = {'Sound', 'MusicTrack'}
MUSIC_TYPES = {'MusicTrack', 'MusicSegment', 'MusicSwitchContainer',
               'MusicPlaylistContainer'}
LOUDNESS_METRICS = {'momentary': 'Max LUFS-M', 'integrated': 'LUFS-I'}
MUSIC_WRITE_LEVELS = {'track': 'Music Track', 'segment': 'Music Segment',
                      'container': 'Music Container'}

import platform
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == 'win32' else 0


def _base_dir():
    """打包后为 exe 所在目录；开发期为脚本所在目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """优先使用 app 内置的 ffmpeg，其次 PATH。

    PyInstaller 6.x 的 onedir 布局把 --add-data 放在 `_internal/` 下，
    而 v5 及更早放在 exe 同级。两处都要找，否则打包后会退化成依赖 PATH。
    """
    base = _base_dir()
    cands = [
        os.path.join(base, 'ffmpeg', 'ffmpeg.exe'),
        os.path.join(base, 'ffmpeg', 'ffmpeg'),
        os.path.join(base, '_internal', 'ffmpeg', 'ffmpeg.exe'),
        os.path.join(base, '_internal', 'ffmpeg', 'ffmpeg'),
        os.path.join(base, 'ffmpeg.exe'),
        os.path.join(base, '_internal', 'ffmpeg.exe'),
    ]
    # PyInstaller 运行时也会把数据解到 sys._MEIPASS
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        cands.insert(0, os.path.join(meipass, 'ffmpeg', 'ffmpeg.exe'))
    for p in cands:
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which('ffmpeg') or 'ffmpeg'


class WaapiError(Exception):
    pass


# ============================================================
# per-language 辅助：语言解析 / media 采集 / 短文件测量
# ============================================================

def lang_from_path(fp):
    """从 originalFilePath 解析语言码。

    WAAPI 中 'Language' 不是可读 accessor（报 Unknown accessor Language），
    语言信息只存在于文件路径里：...\\Originals\\Voices\\zh-CN\\...
    """
    if not fp:
        return None
    m = re.search(r'[\\/]Originals[\\/]Voices[\\/]([^\\/]+)[\\/]', fp, re.I)
    if m:
        return m.group(1)
    # 非 Voices 目录（如 SFX）：退回 Originals 下一级
    m = re.search(r'[\\/]Originals[\\/]([^\\/]+)[\\/]', fp, re.I)
    return m.group(1) if m else None


# ---- ffmpeg 输出解析小工具 ----

def _parse_m_frames(stderr):
    """从 ebur128 逐帧输出提取 M: 值，过滤 < -70 的静音帧。"""
    vals = []
    for line in (stderr or '').split('\n'):
        m = re.search(r'lavfi\.r128\.M=([-\d.]+)', line)
        if m:
            v = float(m.group(1))
            if v > -70:
                vals.append(v)
    return vals


def _parse_duration(stderr):
    """从 ffmpeg 头部 'Duration: 00:00:00.36' 解析时长（秒）。失败返回 None。"""
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', stderr or '')
    if not m:
        return None
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except (TypeError, ValueError):
        return None


def _parse_integrated(stderr):
    """读取全文件最终的门控综合响度，优先使用三位小数的元数据。"""
    values = re.findall(r'lavfi\.r128\.I=([-+\d.eE]+)', stderr or '')
    if not values:
        values = re.findall(r'^\s*I:\s*([-+\d.eE]+)\s+LUFS',
                            stderr or '', re.M)
    if not values:
        return None
    value = float(values[-1])
    return value if math.isfinite(value) and value > -70 else None


def _parse_true_peak(stderr):
    r"""从 ebur128 Summary 段解析 True Peak。

    实测 ffmpeg 输出（peak=true 走 "True peak:" 段，peak=sample 走
    "Sample peak:" 段，两者都是 "Peak:   -5.6 dBFS" 形式）。
    逐帧行里含 "FTP K:" / "TPK:"，其结尾是 K 冒号，
    模式要求 "Peak:"，故不会误配；但为稳妥仍只在段头之后取值。
    """
    import math  # noqa: F401  (保持与下方 bias 计算一致的可读性)
    pk = None
    in_summary = False
    for line in (stderr or '').split('\n'):
        if re.search(r'(True peak|Sample peak)\s*:', line):
            in_summary = True
            continue
        if not in_summary:
            continue
        mm = re.match(r'\s*Peak:\s*([-\d.]+)', line)
        if mm:
            v = float(mm.group(1))
            if v > -120:
                pk = v if pk is None else max(pk, v)
    if pk is None:
        for line in (stderr or '').split('\n'):
            mm = re.search(r'(?<![A-Za-z])Peak:\s*([-\d.]+)', line)
            if mm:
                v = float(mm.group(1))
                if v > -120:
                    pk = v if pk is None else max(pk, v)
    return pk


import math  # noqa: E402


class Pipeline:
    """响度归一化流水线。所有耗时步骤都在调用方的后台线程中执行。"""

    def __init__(self, host='127.0.0.1', port=8080, workers=6,
                 log=None, progress=None):
        self.host = host
        self.port = port
        self.workers = workers
        self.log = log or (lambda msg: print(msg))
        self.progress = progress or (lambda done, total, msg: None)
        self.ffmpeg = find_ffmpeg()
        self.client = None
        self.sounds = []
        self.rows = []
        self.loudness = {}
        self.peaks = {}
        self.meta = {}
        self.plan = []
        self.skipped = []
        self.clipped = []
        self.medias = []          # per-language 模式：media 行
        self.mode = 'object'       # Loudness writes only object MakeUpGain.
        self.music_write_level = 'track'
        self._selected_objects = []
        self.routing_skipped = []
        self.loudness_metric = 'momentary'
        self.reduce_only = False
        self.unchanged = []
        self._selected_media = None
        self.undo_file = os.path.join(_base_dir(), 'undo_state.json')
        self.last_write_report = event_report.load_report(os.path.join(_base_dir(), 'last_write_report.json'))

    # ---------------- 连接 ----------------

    def connect(self):
        try:
            from waapi import WaapiClient
        except Exception as exc:
            raise WaapiError(
                '无法加载 waapi-client，请确认已安装该依赖：%s' % exc)
        try:
            self.client = WaapiClient(url='ws://%s:%s/waapi' % (self.host, self.port))
        except Exception as exc:
            raise WaapiError(
                '无法连接 WAAPI (ws://%s:%s)：%s\n'
                '请确认 Wwise 已打开且 WAAPI 已启用'
                '（Project → User Preferences → WAAPI）'
                % (self.host, self.port, exc))
        info = self._call('ak.wwise.core.getInfo')
        ver = '?'
        if isinstance(info, dict):
            ver = (info.get('version') or {}).get('displayName', '?')
        self.log('已连接 Wwise %s' % ver)
        return ver

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def _call(self, uri, args=None):
        """带线程超时的 WAAPI 调用。WAAPI 对悬挂连接敏感，必须防���死。"""
        if not self.client:
            raise WaapiError('尚未连接 Wwise')
        box = {}

        def _run():
            try:
                box['val'] = self.client.call(uri, args or {})
            except Exception as exc:
                box['err'] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(CALL_TIMEOUT)
        if t.is_alive():
            raise WaapiError(
                'WAAPI 调用超时（%s）。\n可能原因：1) Wwise 中有模态弹窗未关闭；'
                '2) WAAPI 服务卡死（请在 WAAPI 设置中关闭开关等3秒再重开）'
                % uri)
        if 'err' in box:
            msg = str(box['err'])
            low = msg.lower()
            if 'modal dialog' in low or 'locked' in low:
                raise WaapiError(
                    'Wwise 正等待用户关闭模态对话框，请先在 Wwise 中处理弹窗后重试')
            raise WaapiError('WAAPI 错误（%s）：%s' % (uri, msg))
        return box.get('val')

    def _sounds_from_selected(self, sid):
        """从单个选中对象解析出它实际影响的 Sound 列表。

        支持四种选区形态（实测 Wwise 2022.1.3）：
          1) 选中 Sound 本身
          2) 选中 Folder / ActorMixer 等容器 —— 用 descendants 枚举
          3) 选中 Event —— Event 的 descendant 只有 Action，
             **Action 的 Sound 在 Target 里，不在 descendants 里**，必须
             用 references 查 'target' 才能拿到
          4) 选中 WorkUnit / 多层混合 —— 以上都试一遍再合并

        原 v3 只做了 2)，导致「选 Event 时读到 0 个 Sound」。
        """
        picked = []

        # 自身是否 Sound
        own = self._call('ak.wwise.core.object.get', {
            'from': {'id': [sid]},
            'options': {'return': ['id', 'name', 'type']}
        })
        own_ret = (own or {}).get('return') or []
        own_type = own_ret[0].get('type') if own_ret else None
        if own_type in AUDIO_TYPES:
            picked.append(own_ret[0])

        # 后代里的 Sound（容器场景）
        desc = self._call('ak.wwise.core.object.get', {
            'from': {'id': [sid]},
            'transform': [{'select': ['descendants']}],
            'options': {'return': ['id', 'name', 'type']}
        })
        desc_ret = (desc or {}).get('return') or []
        picked.extend([o for o in desc_ret if o.get('type') in AUDIO_TYPES])

        # Event -> Action -> Target 链路。
        # 实测 2022.1.3：Action 的 Sound 目标用 object.get 的 **'target' 属性**
        # 直接读最可靠（返回 {"id":..., "name":...}）。
        # 注意 transform 的枚举是 'referencesTo'（不是 'references'），
        # 而 descendants 取不到 target —— target 是引用不是子对象。
        if own_type in ('Event', 'Action') or any(
                o.get('type') == 'Action' for o in desc_ret):
            action_ids = [o['id'] for o in desc_ret if o.get('type') == 'Action']
            if own_type == 'Action':
                action_ids.append(sid)
            for aid in action_ids:
                ref = self._call('ak.wwise.core.object.get', {
                    'from': {'id': [aid]},
                    'options': {'return': ['id', 'name', 'type', 'target']}
                })
                for a in (ref or {}).get('return') or []:
                    tgt = a.get('target')
                    if not isinstance(tgt, dict) or not tgt.get('id'):
                        continue
                    tid = tgt['id']
                    # 先查 target 自身类型
                    tobj = self._call('ak.wwise.core.object.get', {
                        'from': {'id': [tid]},
                        'options': {'return': ['id', 'name', 'type']}
                    })
                    tret = (tobj or {}).get('return') or []
                    ttype = tret[0].get('type') if tret else None
                    if ttype in AUDIO_TYPES:
                        picked.append(tret[0])
                    elif ttype:
                        # Target 也可能是容器，继续往下展开
                        sub = self._call('ak.wwise.core.object.get', {
                            'from': {'id': [tid]},
                            'transform': [{'select': ['descendants']}],
                            'options': {'return': ['id', 'name', 'type']}
                        })
                        picked.extend([o for o in (sub or {}).get('return') or []
                                       if o.get('type') in AUDIO_TYPES])
        return picked

    # ---------------- 读取选区 ----------------

    def read_selection(self):
        sel = self._call('ak.wwise.ui.getSelectedObjects')
        items = (sel or {}).get('return') or (sel or {}).get('objects') or []
        if not items:
            raise WaapiError('Wwise 中没有选中任何对象')

        sel_ids = [o['id'] for o in items if isinstance(o, dict) and o.get('id')]
        self.log('选中 %d 个对象，解析 Sound / Music Track ...' % len(items))

        sounds = []
        self._selected_objects = []
        direct_sources = set()
        expanded_owners = set()
        for i, sid in enumerate(sel_ids):
            identity = self._call('ak.wwise.core.object.get', {
                'from': {'id': [sid]},
                'options': {'return': ['id', 'name', 'type', 'parent']}})
            obj = ((identity or {}).get('return') or [{}])[0]
            self._selected_objects.append(obj)
            if obj.get('type') == 'AudioFileSource':
                parent_id = (obj.get('parent') or {}).get('id')
                if parent_id:
                    owners = self._sounds_from_selected(parent_id)
                    sounds.extend(owners)
                    direct_sources.add(sid)
            else:
                owners = self._sounds_from_selected(sid)
                sounds.extend(owners)
                expanded_owners.update(o['id'] for o in owners)
            self.progress(i + 1, len(sel_ids), '读取选中对象')
        self._selected_media = (direct_sources, expanded_owners) if direct_sources else None

        seen = set()
        uniq = []
        for s in sounds:
            if s['id'] not in seen:
                seen.add(s['id'])
                uniq.append({'id': s['id'], 'name': s.get('name'), 'type': s.get('type')})
        self.sounds = uniq
        if not uniq:
            raise WaapiError(
                '选中的对象里没有找到 Sound 或 Music Track。\n\n'
                '支持选中：Sound、Music Track、音频源、音乐容器、Work Unit、Event'
                '（会沿 Action→Target 自动解析）。')
        self.log('共 %d 个音频对象（含 %d 个 Music Track）' %
                 (len(uniq), sum(s.get('type') == 'MusicTrack' for s in uniq)))
        return self.sounds

    # ---------------- 路由采集（对象级） ----------------

    def _music_write_scope(self):
        """Resolve music owners and read every Track affected by a shared owner gain."""
        if self.music_write_level not in MUSIC_WRITE_LEVELS:
            raise WaapiError('不支持的音乐写入层级')
        tracks = [s for s in self.sounds if s.get('type') == 'MusicTrack']
        if self.music_write_level == 'track' or not tracks:
            return list(self.sounds), {}, []
        ids = [s['id'] for s in tracks]
        fields = ['id', 'name', 'type', 'path', 'parent', 'MakeUpGain']
        own = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids}, 'options': {'return': fields}})
        ancestors = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids}, 'transform': [{'select': ['ancestors']}],
            'options': {'return': fields}})
        graph = {o['id']: o for o in (own or {}).get('return', [])
                 + (ancestors or {}).get('return', [])}
        def chain(oid):
            seen = set()
            while oid and oid not in seen:
                seen.add(oid)
                node = graph.get(oid)
                if node is None:
                    raise WaapiError('音乐层级读取不完整，请重新读取选区')
                yield node
                oid = (node.get('parent') or {}).get('id')
        kinds = ({'MusicSegment'} if self.music_write_level == 'segment'
                 else {'MusicPlaylistContainer', 'MusicSwitchContainer'})
        explicit = {o['id'] for o in self._selected_objects if o.get('type') in kinds}
        candidates, skipped = {}, []
        for track in tracks:
            eligible = [o for o in chain(track['id']) if o.get('type') in kinds]
            chosen = [o for o in eligible if o['id'] in explicit]
            owner = chosen[-1] if chosen else (eligible[0] if eligible else None)
            if owner is None:
                skipped.append((track.get('name'), '没有匹配的 ' + MUSIC_WRITE_LEVELS[self.music_write_level]))
            else:
                candidates[owner['id']] = owner
        # Parent and child containers must not both be written in one batch.
        roots = {oid: owner for oid, owner in candidates.items()
                 if not any(o['id'] in candidates for o in list(chain(oid))[1:])}
        for owner in roots.values():
            gain = owner.get('MakeUpGain')
            if not isinstance(gain, (int, float)) or not math.isfinite(gain):
                raise WaapiError('无法读取音乐写入对象 Make-Up Gain：' + str(owner.get('name')))
        audio = {s['id']: s for s in self.sounds if s.get('type') != 'MusicTrack'}
        owners = {}
        if roots:
            result = self._call('ak.wwise.core.object.get', {
                'from': {'id': list(roots)}, 'transform': [{'select': ['descendants']}],
                'options': {'return': ['id', 'name', 'type', 'parent']}})
            descendants = (result or {}).get('return', [])
            graph.update({o['id']: {**graph.get(o['id'], {}), **o} for o in descendants})
            for obj in descendants:
                if obj.get('type') != 'MusicTrack':
                    continue
                owner = next((o for o in chain(obj['id']) if o['id'] in roots), None)
                if owner:
                    owners[obj['id']] = roots[owner['id']]
                    audio[obj['id']] = obj
        if not audio:
            raise WaapiError('选区没有可调整的 ' + MUSIC_WRITE_LEVELS[self.music_write_level])
        self.log('音乐写入层级：%s；%d 个目标，测量其全部 %d 个 Track' % (
            MUSIC_WRITE_LEVELS[self.music_write_level], len(roots), len(owners)))
        for name, reason in skipped:
            self.log('  跳过 %s：%s' % (name, reason))
        return list(audio.values()), owners, skipped

    def collect_routing(self):
        if not self.sounds:
            raise WaapiError('请先读取选区')
        audio, write_owners, self.routing_skipped = self._music_write_scope()
        ids = [s['id'] for s in audio]
        self.log('采集路由信息（OutputBus / Volume / 源文件路径）...')

        r = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids},
            'options': {'return': ['id', 'name', 'OutputBus', 'MakeUpGain',
                                   'Volume', 'parent']}
        })
        info = {}
        for o in (r or {}).get('return') or []:
            ob = o.get('OutputBus')
            info[o['id']] = {
                'name': o.get('name'),
                'bus_id': ob.get('id') if isinstance(ob, dict) else None,
                'bus_name': ob.get('name') if isinstance(ob, dict) else None,
                'volume': o.get('Volume', 0) or 0,
                'makeup_gain': o.get('MakeUpGain', 0) or 0,
                'type': next((s.get('type') for s in audio if s['id'] == o['id']), None),
                'parent_id': (o.get('parent') or {}).get('id')
                             if isinstance(o.get('parent'), dict) else None,
            }

        from collections import Counter
        for bn, cnt in Counter(v['bus_name'] for v in info.values()).most_common():
            self.log('  %s: %d 条' % (bn, cnt))

        r2 = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids},
            'transform': [{'select': ['children']}],
            'options': {'return': ['id', 'name', 'type', 'parent',
                                   'originalFilePath', 'VolumeOffset']}
        })
        src_map = {}
        for o in (r2 or {}).get('return') or []:
            p = o.get('parent') or {}
            pid = p.get('id') if isinstance(p, dict) else None
            fp = o.get('originalFilePath')
            if pid and pid in info and fp and o.get('type') == 'AudioFileSource':
                src_map.setdefault(pid, []).append({
                    'media_id': o['id'], 'wav_path': fp,
                    'source_gain': o.get('VolumeOffset', 0) or 0})
        for sources in src_map.values():
            sources.sort(key=lambda s: s['media_id'])
        self.log('  源文件匹配 %d/%d' % (len(src_map), len(info)))

        # 父级链增益
        self.log('采集父级链增益 ...')
        obj_gain, obj_parent = {}, {}
        frontier = set(v['parent_id'] for v in info.values() if v['parent_id'])
        visited = set()
        while frontier:
            visited.update(frontier)
            r3 = self._call('ak.wwise.core.object.get', {
                'from': {'id': list(frontier)},
                'options': {'return': ['id', 'name', 'type', 'Volume',
                                       'MakeUpGain', 'parent']}
            })
            frontier = set()
            for o in (r3 or {}).get('return') or []:
                obj_gain[o['id']] = ((o.get('Volume', 0) or 0)
                                     + (o.get('MakeUpGain', 0) or 0))
                p = o.get('parent')
                obj_parent[o['id']] = p.get('id') if isinstance(p, dict) else None
                if obj_parent[o['id']] and obj_parent[o['id']] not in visited:
                    frontier.add(obj_parent[o['id']])

        def pcg(sid):
            total, cur, seen = 0.0, info[sid]['parent_id'], set()
            while cur and cur not in seen:
                seen.add(cur)
                total += obj_gain.get(cur, 0.0)
                cur = obj_parent.get(cur)
            return total

        # Bus 链增益
        self.log('采集 Bus 链增益 ...')
        distinct = {}
        for v in info.values():
            if v['bus_id']:
                distinct[v['bus_id']] = v['bus_name']
        bus_gain_map = {}
        for bid, bname in distinct.items():
            gain, cur, seen = 0.0, bid, set()
            while cur and cur not in seen:
                seen.add(cur)
                rb = self._call('ak.wwise.core.object.get', {
                    'from': {'id': [cur]},
                    'options': {'return': ['id', 'name', 'type', 'Volume', 'BusVolume',
                                           'MakeUpGain', 'parent']}
                })
                ret = (rb or {}).get('return') or []
                if not ret:
                    break
                b = ret[0]
                gain += (b.get('Volume', 0) or 0) + (b.get('BusVolume', 0) or 0) + (b.get('MakeUpGain', 0) or 0)
                if str(b.get('type', '')) in ('Bus', 'AuxBus'):
                    p = b.get('parent')
                    cur = p.get('id') if isinstance(p, dict) else None
                else:
                    break
            bus_gain_map[bid] = {'name': bname, 'gain': gain}
            self.log('  %s: 链增益 %+.1f dB' % (bname, gain))

        rows = []
        for sid, v in info.items():
            owner = write_owners.get(sid)
            rows.append({
                'id': sid,
                'name': v['name'],
                'type': v['type'],
                'write_id': owner['id'] if owner else sid,
                'write_name': owner['name'] if owner else v['name'],
                'write_type': owner['type'] if owner else v['type'],
                'write_makeup_gain': owner['MakeUpGain'] if owner else v['makeup_gain'],
                'bus_id': v['bus_id'],
                'bus_name': v['bus_name'],
                'bus_gain': bus_gain_map.get(v['bus_id'], {}).get('gain', 0.0),
                'parent_chain_gain': pcg(sid),
                'volume': v['volume'],
                'makeup_gain': v.get('makeup_gain', 0.0),
                'wav_path': src_map.get(sid, [{}])[0].get('wav_path'),
                'source_gain': src_map.get(sid, [{}])[0].get('source_gain', 0),
                'sources': src_map.get(sid, []),
            })
        self.rows = rows
        return rows

    # ---------------- media 采集（per-language） ----------------

    def collect_medias(self, languages=None):
        """采集每个 Sound 下所有 AudioFileSource media（按语言）。

        与 collect_routing 的关键差异：
          1. 不短路 —— 保留全部语言的 media（对象级只取第一个）
          2. 只认 AudioFileSource —— children 里混着大量 SourcePlugin(Silence)
             占位对象，它们 originalFilePath=None，不是音频文件
          3. 携带 Sound.MakeUpGain，作为固定链计入公式
        """
        if not self.sounds:
            raise WaapiError('请先读取选区')
        ids = [s['id'] for s in self.sounds]
        self.log('采集路由信息（含每语言 media）...')

        r = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids},
            'options': {'return': ['id', 'name', 'OutputBus', 'MakeUpGain',
                                   'Volume', 'parent']}
        })
        info = {}
        for o in (r or {}).get('return') or []:
            ob = o.get('OutputBus')
            info[o['id']] = {
                'name': o.get('name'),
                'bus_id': ob.get('id') if isinstance(ob, dict) else None,
                'bus_name': ob.get('name') if isinstance(ob, dict) else None,
                'volume': o.get('Volume', 0) or 0,
                'makeup_gain': o.get('MakeUpGain', 0) or 0,
                'parent_id': (o.get('parent') or {}).get('id')
                             if isinstance(o.get('parent'), dict) else None,
            }

        from collections import Counter
        for bn, cnt in Counter(v['bus_name'] for v in info.values()).most_common():
            self.log('  %s: %d 条' % (bn, cnt))

        self.log('采集 media（AudioFileSource，按语言）...')
        r2 = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids},
            'transform': [{'select': ['children']}],
            'options': {'return': ['id', 'name', 'type', 'parent',
                                   'originalFilePath', 'VolumeOffset']}
        })
        skipped_plugin = 0
        medias = []
        for o in (r2 or {}).get('return') or []:
            p = o.get('parent') or {}
            pid = p.get('id') if isinstance(p, dict) else None
            if not pid or pid not in info:
                continue
            if o.get('type') != 'AudioFileSource':
                skipped_plugin += 1
                continue
            if self._selected_media:
                direct, expanded = self._selected_media
                if pid not in expanded and o['id'] not in direct:
                    continue
            fp = o.get('originalFilePath')
            if not fp:
                skipped_plugin += 1
                continue
            lg = lang_from_path(fp)
            if languages and lg not in languages:
                continue
            medias.append({
                'media_id': o['id'],
                'media_name': o.get('name'),
                'sound_id': pid,
                'sound_name': info[pid]['name'],
                'lang': lg,
                'wav_path': fp,
                'old_volume_offset': o.get('VolumeOffset'),
                'bus_id': info[pid]['bus_id'],
                'bus_name': info[pid]['bus_name'],
                'volume': info[pid]['volume'],
                'makeup_gain': info[pid]['makeup_gain'],
            })
        if skipped_plugin:
            self.log('  忽略 %d 个非 AudioFileSource 子对象（Silence 等占位）'
                     % skipped_plugin)
        for lg, n in Counter(m['lang'] for m in medias).most_common():
            self.log('  %s: %d media' % (lg, n))
        self.log('  合计 %d 个 media（来自 %d 个 Sound）'
                 % (len(medias), len(set(m['sound_id'] for m in medias))))
        if not medias:
            raise WaapiError('没有采集到任何 AudioFileSource，请检查选区')

        # 父级链 / Bus 链增益（与对象级共用逻辑）
        self.log('采集父级链增益 ...')
        obj_gain, obj_parent = {}, {}
        frontier = set(v['parent_id'] for v in info.values() if v['parent_id'])
        visited = set()
        while frontier:
            visited.update(frontier)
            r3 = self._call('ak.wwise.core.object.get', {
                'from': {'id': list(frontier)},
                'options': {'return': ['id', 'name', 'type', 'Volume',
                                       'MakeUpGain', 'parent']}
            })
            frontier = set()
            for o in (r3 or {}).get('return') or []:
                obj_gain[o['id']] = ((o.get('Volume', 0) or 0)
                                     + (o.get('MakeUpGain', 0) or 0))
                p = o.get('parent')
                obj_parent[o['id']] = p.get('id') if isinstance(p, dict) else None
                if obj_parent[o['id']] and obj_parent[o['id']] not in visited:
                    frontier.add(obj_parent[o['id']])

        def pcg(sid):
            total, cur, seen = 0.0, info[sid]['parent_id'], set()
            while cur and cur not in seen:
                seen.add(cur)
                total += obj_gain.get(cur, 0.0)
                cur = obj_parent.get(cur)
            return total

        self.log('采集 Bus 链增益 ...')
        distinct = {}
        for v in info.values():
            if v['bus_id']:
                distinct[v['bus_id']] = v['bus_name']
        bus_gain_map = {}
        for bid, bname in distinct.items():
            gain, cur, seen = 0.0, bid, set()
            while cur and cur not in seen:
                seen.add(cur)
                rb = self._call('ak.wwise.core.object.get', {
                    'from': {'id': [cur]},
                    'options': {'return': ['id', 'name', 'type', 'Volume', 'BusVolume',
                                           'MakeUpGain', 'parent']}
                })
                ret = (rb or {}).get('return') or []
                if not ret:
                    break
                b = ret[0]
                gain += (b.get('Volume', 0) or 0) + (b.get('BusVolume', 0) or 0) + (b.get('MakeUpGain', 0) or 0)
                if str(b.get('type', '')) in ('Bus', 'AuxBus'):
                    p = b.get('parent')
                    cur = p.get('id') if isinstance(p, dict) else None
                else:
                    break
            bus_gain_map[bid] = {'name': bname, 'gain': gain}
            self.log('  %s: 链增益 %+.1f dB' % (bname, gain))

        for m in medias:
            m['parent_chain_gain'] = round(pcg(m['sound_id']), 2)
            m['bus_gain'] = bus_gain_map.get(m['bus_id'], {}).get('gain', 0.0)
        self.medias = medias
        return medias

    # ---------------- 测量 ----------------

    def _measure_one(self, wav, pad_short=False, pad_to=1.5):
        """测量单个文件，返回 {'max_m','peak','dur','padded','pad_bias'}。

        pad_short=True 时启用短文件补救：EBU R128 瞬时响度需要 400ms 门控窗口，
        时长不足的文件测不出任何有效帧。补救做法是尾部 pad 静音到 >= pad_to 秒
        再测，然后按稀释模型扣除偏低量：

            bias = 10*log10(min(dur,0.4)/0.4)      # dur<0.4 时为负
            真实响度 = padded 读数 - bias

        该模型已用同 seed 白噪声对照实验验证（L=0.1~0.8s 共 15 点）：
        实测偏差与理论值最大误差 0.060 dB，平均 0.020 dB。
        补救只在正常测量返回 None 时触发，对可测文件行为不变。
        """
        res = {'max_m': None, 'integrated': None, 'peak': None, 'dur': None,
               'padded': False, 'pad_bias': None}
        try:
            cmd = [self.ffmpeg, '-nostdin', '-hide_banner', '-i', wav,
                   '-af', 'ebur128=peak=true:metadata=1,ametadata=print:key=lavfi.r128.M,ametadata=print:key=lavfi.r128.I', '-f', 'null', '-']
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=600, creationflags=_NO_WINDOW)
            res['dur'] = _parse_duration(r.stderr)
            if r.returncode != 0:
                res['error'] = 'ffmpeg 解码失败'
                return res
            res['integrated'] = _parse_integrated(r.stderr)
            mvals = _parse_m_frames(r.stderr)
            if mvals:
                res['max_m'] = max(mvals)
            res['peak'] = _parse_true_peak(r.stderr)

            if res['max_m'] is None and pad_short and self.loudness_metric == 'momentary':
                af = 'apad=whole_dur=%s,ebur128=peak=true:metadata=1,ametadata=print:key=lavfi.r128.M' % pad_to
                cmd2 = [self.ffmpeg, '-nostdin', '-hide_banner', '-i', wav,
                        '-af', af, '-f', 'null', '-']
                r2 = subprocess.run(cmd2, capture_output=True, text=True,
                                    timeout=120, creationflags=_NO_WINDOW)
                mvals2 = _parse_m_frames(r2.stderr)
                if mvals2:
                    raw = max(mvals2)
                    bias = 0.0
                    dur = res['dur']
                    if dur and 0 < dur < 0.4:
                        bias = 10.0 * math.log10(dur / 0.4)
                        raw -= bias
                    res['max_m'] = round(raw, 3)
                    res['padded'] = True
                    res['pad_bias'] = round(bias, 2)
                    pk2 = _parse_true_peak(r2.stderr)
                    if pk2 is not None:
                        res['peak'] = pk2
        except Exception as exc:
            res['max_m'] = res['integrated'] = None
            res['error'] = str(exc)
        return res

    def measure(self, pad_short=False, pad_to=1.5, metric='momentary'):
        """测量全部源文件。对象级用 rows，per-language 用 medias。"""
        if metric not in LOUDNESS_METRICS:
            raise WaapiError('不支持的响度标准：' + str(metric))
        self.loudness_metric = metric
        source = self.medias if self.mode == 'perlang' else self.rows
        if not source:
            raise WaapiError('请先读取选区与路由')
        if not self.ffmpeg or not os.path.isfile(self.ffmpeg):
            self.log('警告：未找到内置 ffmpeg，将尝试 PATH 中的 ffmpeg')
        if self.mode == 'object':
            source = [dict(s, name=row['name']) for row in self.rows
                      for s in row.get('sources', [row])]

        jobs, missing = [], []
        for row in source:
            wav = row.get('wav_path')
            key = row.get('media_id') or row.get('id')
            if not wav or not os.path.isfile(wav):
                missing.append((row.get('sound_name') or row.get('name'), wav))
                continue
            jobs.append((key, wav))
        if missing:
            self.log('警告：%d 个源文件缺失：%s%s'
                     % (len(missing),
                        ', '.join(str(m[0]) for m in missing[:5]),
                        ' ...' if len(missing) > 5 else ''))

        self.log('测量 %d 个文件的 %s（%d 线程），再叠加源增益与完整路由链计算 Output ...'
                 % (len(jobs), LOUDNESS_METRICS[metric], self.workers))
        out, peaks, meta = {}, {}, {}
        done = 0
        failed = []
        if jobs:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futures = {ex.submit(self._measure_one, w, pad_short, pad_to):
                           (key, w) for key, w in jobs}
                for fut in as_completed(futures):
                    key, wav = futures[fut]
                    v = fut.result()
                    out[key] = v.get('integrated' if metric == 'integrated' else 'max_m')
                    peaks[key] = v.get('peak')
                    meta[key] = {'padded': v.get('padded'),
                                 'dur': v.get('dur'),
                                 'pad_bias': v.get('pad_bias'),
                                 'max_m': v.get('max_m'),
                                 'integrated': v.get('integrated'),
                                 'error': v.get('error')}
                    if out[key] is None:
                        failed.append((key, wav))
                    done += 1
                    self.progress(done, len(jobs), '测量响度')

        if self.mode == 'object':
            object_out, object_peaks, object_meta = {}, {}, {}
            for row in self.rows:
                sources = row.get('sources', [row])
                valid = [s for s in sources if out.get(s.get('media_id', s.get('id'))) is not None]
                # A shared gain affects every source; a missing source prevents reliable planning.
                if len(valid) != len(sources) or not valid:
                    object_out[row['id']] = None
                    continue
                chosen = max(valid, key=lambda s: out[s.get('media_id', s.get('id'))] + s.get('source_gain', 0))
                key = chosen.get('media_id', chosen.get('id'))
                row.update(wav_path=chosen['wav_path'], source_gain=chosen.get('source_gain', 0),
                           representative_source_id=key)
                object_out[row['id']] = out[key]
                effective_peaks = [peaks[s.get('media_id', s.get('id'))] + s.get('source_gain', 0)
                                   for s in valid if peaks.get(s.get('media_id', s.get('id'))) is not None]
                object_peaks[row['id']] = (max(effective_peaks) - row['source_gain']) if effective_peaks else None
                object_meta[row['id']] = meta[key]
            out, peaks, meta = object_out, object_peaks, object_meta
        self.loudness, self.peaks, self.meta = out, peaks, meta
        if failed:
            self.log('  %d 个文件无法测量（静音或时长<400ms）：%s%s'
                     % (len(failed),
                        ', '.join(str(f[0]) for f in failed[:5]),
                        ' ...' if len(failed) > 5 else ''))
        padded_n = sum(1 for m in meta.values() if m.get('padded'))
        if padded_n:
            self.log('  其中 %d 条为短文件（<400ms），已用尾部 pad + 稀释补偿测得'
                     % padded_n)
        return out

    # ---------------- 计划 ----------------

    def _loudness_fields(self, source, current, output):
        fields = {'metric': self.loudness_metric, 'src_lufs': source,
                  'current_output_lufs': round(current, 3),
                  'output_lufs': round(output, 3)}
        if self.loudness_metric == 'momentary':
            fields.update(src_max_m=source, output_max_m=round(output, 2))
        else:
            fields.update(src_integrated_lufs=source, output_integrated_lufs=round(output, 3))
        return fields

    def _keep(self, row, source, current, target, why):
        self.unchanged.append({**row, **self._loudness_fields(source, current, current),
                               'target': target, 'note': why})
        return why

    def make_plan(self, targets, clamp=(-24.0, 24.0), peak_guard=True, reduce_only=False):
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in targets.values()):
            raise WaapiError('目标响度必须是有限数字')
        self.reduce_only = bool(reduce_only)
        self.unchanged = []
        if self.mode == 'perlang':
            return self._make_plan_perlang(targets, clamp, peak_guard)
        if self.music_write_level != 'track':
            return self._make_music_owner_plan(targets, clamp, peak_guard)

        plan, skipped, clipped = [], [], []
        for row in self.rows:
            target = targets.get(row['bus_name'])
            src_m = self.loudness.get(row['id'])
            if target is None:
                skipped.append((row['name'], "Bus '%s' 未定义目标" % row['bus_name']))
                continue
            if src_m is None:
                skipped.append((row['name'], '无法测量（静音/过短/缺文件）'))
                continue
            fixed = (row.get('source_gain', 0) + row['volume']
                     + row['parent_chain_gain'] + row['bus_gain'])
            old = row.get('makeup_gain', 0)
            current = src_m + fixed + old
            if self.reduce_only and current <= target + 1e-6:
                why = '保持不变：当前 Output %.2f %s ≤ 目标 %.2f' % (
                    current, LOUDNESS_METRICS[self.loudness_metric], target)
                skipped.append((row['name'], self._keep(row, src_m, current, target, why)))
                continue
            new_mg = round(target - src_m - fixed, 1)
            limited = max(clamp[0], min(clamp[1], new_mg))
            note = '增益已限制为 %+.1f dB' % limited if limited != new_mg else None
            new_mg = limited
            tp = self.peaks.get(row['id'])
            if peak_guard and tp is not None and tp + fixed + new_mg > 0:
                new_mg = math.floor((-tp - fixed) * 10) / 10
                note = '削波护栏已降低增益'
            if self.reduce_only:
                new_mg = min(new_mg, old)
                if new_mg >= old:
                    why = '保持不变：增益范围限制，无法继续降低'
                    skipped.append((row['name'], self._keep(row, src_m, current, target, why)))
                    continue
            out_after = src_m + fixed + new_mg
            plan.append({
                **row,
                **self._loudness_fields(src_m, current, out_after),
                'fixed_chain': round(fixed, 3),
                'target': target,
                'new_makeup_gain': new_mg,
                'music_write_level': self.music_write_level,
                'note': note,
            })
        self.plan, self.skipped, self.clipped = plan, skipped, clipped
        self.log('计划：%d 条待写入，跳过 %d 条' % (len(plan), len(skipped)))
        return plan

    @staticmethod
    def _scope_snapshot(rows):
        fields = ('id', 'type', 'bus_id', 'volume', 'makeup_gain',
                  'parent_chain_gain', 'bus_gain', 'sources', 'write_id',
                  'write_type', 'write_makeup_gain')
        return [{k: row.get(k) for k in fields} for row in sorted(rows, key=lambda r: r['id'])]

    def _make_music_owner_plan(self, targets, clamp, peak_guard):
        groups = {}
        for row in self.rows:
            groups.setdefault(row['write_id'], []).append(row)
        plan, skipped = [], list(self.routing_skipped)
        for oid, members in groups.items():
            routes, error = [], None
            for row in members:
                src = self.loudness.get(row['id'])
                target = targets.get(row['bus_name'])
                if src is None or target is None:
                    error = '组内 %s %s；跳过整个写入对象' % (row['name'],
                        '无法测量' if src is None else 'Bus 未定义目标')
                    break
                fixed = row.get('source_gain', 0) + row['volume'] + row['parent_chain_gain'] + row['bus_gain']
                current = src + fixed + row['makeup_gain']
                routes.append(dict(row=row, src=src, target=target, current=current,
                                   delta=target-current, fixed=fixed))
            owner = members[0]
            if error:
                skipped.append((owner['write_name'], error))
                continue
            controlling = min(routes, key=lambda r: r['delta'])
            row = controlling['row']
            old = owner['write_makeup_gain']
            fixed = controlling['current'] - controlling['src'] - old
            view = dict(row, id=oid, name=owner['write_name'], type=owner['write_type'],
                        makeup_gain=old, fixed_chain=round(fixed, 3),
                        music_write_level=self.music_write_level,
                        scope_snapshot=self._scope_snapshot(members), member_count=len(members))
            if self.reduce_only and controlling['delta'] >= -1e-6:
                why = '保持不变：全部 %d 个音频对象的 Output 均未超标' % len(members)
                skipped.append((view['name'], self._keep(view, controlling['src'],
                               controlling['current'], controlling['target'], why)))
                continue
            desired = math.floor((old + controlling['delta'] + 1e-9) * 10) / 10
            new = max(clamp[0], min(clamp[1], desired))
            notes = []
            if new != desired:
                notes.append('增益范围限制')
            if peak_guard:
                for route in routes:
                    peak = self.peaks.get(route['row']['id'])
                    if peak is not None:
                        current_peak = peak + route['fixed'] + route['row']['makeup_gain']
                        cap = math.floor((old-current_peak+1e-9)*10)/10
                        if cap < new:
                            new = cap
                            notes.append('削波护栏已降低增益')
            if self.reduce_only:
                new = min(new, old)
            if new == old:
                why = '保持不变：增益范围限制或无需调整'
                skipped.append((view['name'], self._keep(view, controlling['src'],
                               controlling['current'], controlling['target'], why)))
                continue
            outputs = [dict(id=r['row']['id'], name=r['row']['name'], bus_name=r['row']['bus_name'],
                            current_output_lufs=round(r['current'], 3), target=r['target'],
                            output_lufs=round(r['current']+new-old, 3)) for r in routes]
            plan.append({**view, **self._loudness_fields(controlling['src'], controlling['current'],
                         controlling['current']+new-old), 'target': controlling['target'],
                         'new_makeup_gain': new, 'member_outputs': outputs,
                         'scope_note': '影响 %d 个音频对象，按各自 Bus 目标计算' % len(members),
                         'note': '；'.join(dict.fromkeys(notes)) or None})
        self.plan, self.skipped, self.clipped = plan, skipped, []
        self.log('计划：%d 个 Make-Up Gain 写入目标，跳过 %d 个' % (len(plan), len(skipped)))
        return plan

    def _make_plan_perlang(self, targets, clamp, peak_guard):
        """per-language：写每个 media 的 VolumeOffset。

        公式（Sound.MakeUpGain 视为固定链，计入但���改写）：
          新 VolumeOffset = Bus目标 − 源MaxM − Sound.Volume − Sound.MakeUpGain
                            − 父级链增益 − Bus链增益
        """
        plan, skipped, clipped = [], [], []
        for m in self.medias:
            target = targets.get(m['bus_name'])
            src_m = self.loudness.get(m['media_id'])
            tag = '%s[%s]' % (m['sound_name'], m['lang'])
            if target is None:
                skipped.append((tag, "Bus '%s' 未定义目标" % m['bus_name']))
                continue
            if src_m is None:
                mm = self.meta.get(m['media_id']) or {}
                dur = mm.get('dur')
                if dur is not None and 0 < dur < 0.4:
                    why = ('源文件时长 %.3fs < 400ms，测不出瞬时响度'
                           '（可勾选「短文件补救」）' % dur)
                else:
                    why = '无法测量（静音/过短/缺文件）'
                skipped.append((tag, why))
                continue

            fixed_chain = (m['volume'] + m['makeup_gain']
                           + m['parent_chain_gain'] + m['bus_gain'])
            old = m.get('old_volume_offset')
            if self.reduce_only and (not isinstance(old, (int, float)) or not math.isfinite(old)):
                skipped.append((tag, '无法读取当前源增益，不能判断 Output 是否超标'))
                continue
            current = src_m + fixed_chain + (old or 0)
            if self.reduce_only and current <= target + 1e-6:
                why = '保持不变：当前 Output %.2f %s ≤ 目标 %.2f' % (
                    current, LOUDNESS_METRICS[self.loudness_metric], target)
                skipped.append((tag, self._keep(m, src_m, current, target, why)))
                continue
            want = target - src_m - fixed_chain
            new_vo = round(want, 3)
            note = None

            lo, hi = clamp
            if new_vo < lo or new_vo > hi:
                clamped = max(lo, min(hi, new_vo))
                note = ('增益 %+.1f 超出范围 [%+.1f,%+.1f]，已限制为 %+.1f'
                        % (new_vo, lo, hi, clamped))
                new_vo = clamped

            tp = self.peaks.get(m['media_id'])
            if peak_guard and tp is not None:
                est = tp + fixed_chain + new_vo
                if est > 0.0:
                    capped = math.floor((new_vo - est) * 1000) / 1000
                    clipped.append((tag, tp, new_vo, est, capped))
                    extra = '预计峰值 %+.2f dBTP 超 0，已降为 %+.1f' % (est, capped)
                    note = (note + '; ' + extra) if note else extra
                    new_vo = capped

            if self.reduce_only:
                new_vo = min(new_vo, old)
                if new_vo >= old:
                    why = '保持不变：增益范围限制，无法继续降低'
                    skipped.append((tag, self._keep(m, src_m, current, target, why)))
                    continue

            mm = self.meta.get(m['media_id']) or {}
            if mm.get('padded'):
                dur = mm.get('dur')
                bias = mm.get('pad_bias')
                pn = ('短文件补救（时长 %.3fs <400ms，pad 后测得，稀释补偿 %+.2f dB）'
                      % (dur, bias) if dur and bias is not None
                      else '短文件补救（pad 后测得）')
                note = (note + '; ' + pn) if note else pn

            out_after = src_m + fixed_chain + new_vo
            plan.append({
                **m,
                **self._loudness_fields(src_m, current, out_after),
                'fixed_chain': round(fixed_chain, 2),
                'target': target,
                'new_volume_offset': new_vo,
                'desired_volume_offset': round(want, 3),
                'note': note,
                'padded': bool(mm.get('padded')),
                'pad_bias': mm.get('pad_bias'),
                'true_peak': tp,
            })
        self.plan, self.skipped, self.clipped = plan, skipped, clipped
        self.log('计划：%d 条待写入，跳过 %d 条%s'
                 % (len(plan), len(skipped),
                    '，削波护栏修正 %d 条' % len(clipped) if clipped else ''))
        return plan

    # ---------------- 写入 ----------------

    def apply(self):
        if self.mode == 'perlang':
            raise WaapiError('响度调整只允许写入 Sound 或所选音乐层级的 Make-Up Gain；请重新读取选区并测量。')
        if not self.plan:
            raise WaapiError('没有可写入的计划')

        planned = list(self.plan)
        if any(p.get('music_write_level', 'track') != self.music_write_level for p in planned):
            raise WaapiError('音乐写入层级已更改，请重新读取选区并测量')
        fresh_rows = self.collect_routing()
        fresh = {r['id']: r for r in fresh_rows}
        fields = ('wav_path', 'source_gain', 'sources', 'bus_id', 'volume',
                  'parent_chain_gain', 'bus_gain')
        for p in planned:
            if 'scope_snapshot' in p:
                members = [r for r in fresh_rows if r.get('write_id') == p['id']]
                if p['music_write_level'] != self.music_write_level or self._scope_snapshot(members) != p['scope_snapshot']:
                    raise WaapiError('预览后音乐写入层级、组内对象或增益链发生变化，请重新测量预览')
                continue
            row = fresh.get(p['id'])
            if row and p.get('representative_source_id'):
                chosen = next((s for s in row['sources'] if s['media_id'] == p['representative_source_id']), None)
                if chosen:
                    row.update(wav_path=chosen['wav_path'], source_gain=chosen['source_gain'])
            if row is None or any(row.get(k) != p.get(k) for k in fields):
                raise WaapiError('预览后路由或增益链发生变化，请重新测量预览')
        return self._write_verified([
            {'id': p['id'], 'name': p['name'], 'old': p['makeup_gain'],
             'new': p['new_makeup_gain']} for p in planned], 'MakeUpGain')

    def _apply_perlang(self):
        raise WaapiError('音频源增益写入已禁用；请使用 Sound 或所选音乐层级的 Make-Up Gain。')

    def collect_play_actions(self):
        if not self.sounds:
            raise WaapiError('请先读取选区')
        ids = [s['id'] for s in self.sounds]
        music_ids = [s['id'] for s in self.sounds if s.get('type') == 'MusicTrack']
        if music_ids:
            ancestors = self._call('ak.wwise.core.object.get', {
                'from': {'id': music_ids}, 'transform': [{'select': ['ancestors']}],
                'options': {'return': ['id', 'type']}})
            ids.extend(o['id'] for o in (ancestors or {}).get('return', [])
                       if o.get('type') in MUSIC_TYPES)
            ids = list(dict.fromkeys(ids))
        r = self._call('ak.wwise.core.object.get', {
            'from': {'id': ids}, 'transform': [{'select': ['referencesTo']}],
            'options': {'return': ['id', 'name', 'path', 'type']}})
        actions = []
        for ref in (r or {}).get('return', []):
            if ref.get('type') != 'Action':
                continue
            q = self._call('ak.wwise.core.object.get', {
                'from': {'id': [ref['id']]},
                'options': {'return': ['id', 'name', 'path', 'target',
                                       'ActionType', 'Probability']}})
            for a in (q or {}).get('return', []):
                if a.get('ActionType') == 1 and (a.get('target') or {}).get('id') in ids:
                    actions.append(a)
        self.actions = list({a['id']: a for a in actions}.values())
        return self.actions

    def probability_plan(self, value):
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise WaapiError('Probability 必须在 0～100 之间')
        actions = self.collect_play_actions()
        if not actions:
            raise WaapiError('未找到指向选区音频或上级音乐容器的 Play Action')
        return [{'id': a['id'], 'name': a.get('path', a.get('name')),
                 'old': a.get('Probability'), 'new': value} for a in actions]

    def reset_gain_plan(self, props):
        if not props or any(p not in ('MakeUpGain', 'Volume') for p in props):
            raise WaapiError('请至少选择一种增益属性')
        selection = self._call('ak.wwise.ui.getSelectedObjects')
        objects = (selection or {}).get('return') or (selection or {}).get('objects') or []
        if not objects:
            raise WaapiError('请先在 Wwise 选中需要归零的音频对象')
        supported = {'Sound', 'ActorMixer', 'RandomSequenceContainer',
                     'SwitchContainer', 'BlendContainer', 'Bus', 'AuxBus',
                     'MusicSegment', 'MusicTrack', 'MusicSwitchContainer',
                     'MusicPlaylistContainer'}
        entries, skipped = [], []
        for obj in objects:
            r = self._call('ak.wwise.core.object.get', {
                'from': {'id': [obj['id']]},
                'options': {'return': ['id', 'name', 'path', 'type']}})
            rows = (r or {}).get('return') or []
            if len(rows) != 1:
                raise WaapiError('选区读取不完整，请重新读取')
            o = rows[0]
            if o['type'] not in supported:
                skipped.append((o.get('name'), '对象类型不支持 Sound 级增益：' + o['type']))
                continue
            q = self._call('ak.wwise.core.object.get', {
                'from': {'id': [o['id']]},
                'options': {'return': ['id'] + list(props)}})
            row = ((q or {}).get('return') or [{}])[0]
            for prop in props:
                value = row.get(prop)
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise WaapiError('无法读取增益属性：%s %s' % (o.get('name'), prop))
                if abs(value) < .0001:
                    skipped.append((o.get('name'), prop + ' 已为 0'))
                else:
                    entries.append(dict(id=o['id'], name=o.get('path', o.get('name')),
                                        prop=prop, old=value, new=0.0))
        return entries, skipped

    def _write_verified(self, entries, prop=None):
        entries = [dict(e, prop=prop or e['prop']) for e in entries]
        if any(e['prop'] == 'VolumeOffset' for e in entries):
            raise WaapiError('音频源增益写入已禁用；响度调整只写对象 Make-Up Gain。')
        def key(e):
            return e['id'] + ':' + e['prop']
        def read():
            values = {}
            for field in sorted({e['prop'] for e in entries}):
                ids = list({e['id'] for e in entries if e['prop'] == field})
                r = self._call('ak.wwise.core.object.get', {
                    'from': {'id': ids}, 'options': {'return': ['id', field]}})
                values.update({o['id'] + ':' + field: o.get(field) for o in (r or {}).get('return', [])})
            return values
        before = read()
        for e in entries:
            old = before.get(key(e))
            if old is None or e['old'] is None or abs(old - e['old']) > 0.0001:
                raise WaapiError('预览后属性发生变化或读取不完整，请重新预览：' + str(e['name']))
        state = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'mode': self.mode,
                 'count': len(entries), 'entries': entries}
        record_dir = os.environ.get('X3_WAAPI_STATE_DIR') or os.path.join(
            os.path.expanduser('~'), 'Desktop', 'waapi-state')
        os.makedirs(record_dir, exist_ok=True)
        record = os.path.join(record_dir, str(time.time_ns()) + '-' + (prop or 'reset-gains') + '.json')
        def save(path, data):
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        save(record, state)
        save(self.undo_file, state)
        write_error = None
        try:
            self._call('ak.wwise.core.object.set', {'objects': [
                {'object': oid, **{'@' + e['prop']: e['new'] for e in entries if e['id'] == oid}}
                for oid in dict.fromkeys(e['id'] for e in entries)]})
        except WaapiError as exc:
            write_error = str(exc)
            self.log('写入未明确返回，正在独立回读；请处理 Wwise 弹窗。')
        try:
            after = read()
        except Exception as exc:
            state.update(status='indeterminate', error=str(exc), write_error=write_error)
            state['event_report_path'] = self._complete_write_report(entries, before, None, 'indeterminate', record, state['time'])
            save(record, state)
            raise WaapiError('无法确认写入结果，请处理 Wwise 弹窗后回读，勿重复应用。记录：' + record)
        mismatch = [(e['name'], after.get(key(e)), e['new']) for e in entries
                    if after.get(key(e)) is None or abs(after[key(e)] - e['new']) > 0.0001]
        state.update(status='verified' if not mismatch else 'mismatch',
                     readback=after, write_error=write_error)
        state['event_report_path'] = self._complete_write_report(entries, before, after, state['status'], record, state['time'])
        save(record, state)
        if mismatch:
            raise WaapiError('回读不匹配：%s。撤回快照已保留。' % mismatch)
        self.log('回读验证成功：%d/%d %s；记录 %s' % (len(entries), len(entries), prop, record))
        return len(entries), []

    def _complete_write_report(self, entries, before, after, status, record, write_time):
        """A receipt may fail independently; never replay or relabel the write."""
        changed = []
        for entry in entries:
            key = entry['id'] + ':' + entry['prop']
            old = before.get(key)
            actual = (after or {}).get(key)
            if (isinstance(old, (int, float)) and isinstance(actual, (int, float))
                    and math.isfinite(actual) and abs(actual - old) > .0001):
                changed.append(dict(entry, old=old, new=actual, requested_new=entry['new'],
                                    verified=abs(actual - entry['new']) <= .0001))
        report = dict(schema_version=1, time=write_time, status=status,
                      music_write_level=self.music_write_level, metric=self.loudness_metric,
                      changed_entries=changed, events=[], unmatched_entries=list(changed),
                      unconfirmed_entries=[e for e in entries if after is None or
                          (after or {}).get(e['id'] + ':' + e['prop']) is None],
                      lookup_complete=after is not None, warnings=[], record_path=record)
        self.last_write_report = report
        if changed:
            self.log('正在查询 %d 个实际改动对象关联的 Event ...' % len({e['id'] for e in changed}))
            try:
                event_report.collect_events(self._call, report)
            except Exception as exc:
                report['lookup_complete'] = False
                report['warnings'].append('Event 关联查询失败：' + str(exc))
                self.log(report['warnings'][-1])
        if report['unconfirmed_entries']:
            report['warnings'].append('%d 条属性的实际写入结果未确认' % len(report['unconfirmed_entries']))
        report_path = os.path.splitext(record)[0] + '-events.json'
        excel_path = os.path.splitext(record)[0] + '-events.xlsx'
        report['report_path'] = report_path
        try:
            event_report.export_xlsx(report, excel_path)
            report['xlsx_path'] = excel_path
            self.log('受影响 Event：%d 个；Excel 已保存：%s' % (len(report['events']), excel_path))
        except Exception as exc:
            report['warnings'].append('Excel 自动保存失败，可在事件名单页另存：' + str(exc))
            self.log(report['warnings'][-1])
        try:
            event_report.save_report_json(report, report_path)
            event_report.save_report_json(report, os.path.join(os.path.dirname(self.undo_file), 'last_write_report.json'))
        except Exception as exc:
            report['warnings'].append('事件名单记录保存失败：' + str(exc))
            self.log(report['warnings'][-1])
        return report_path

    # ---------------- 撤回 ----------------

    def has_undo(self):
        return os.path.isfile(self.undo_file)

    def undo_info(self):
        """返回快照摘要（时间、条数、属性），无快照返回 None"""
        if not self.has_undo():
            return None
        try:
            with open(self.undo_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            return {
                'time': state.get('time', '?'),
                'count': state.get('count', 0),
                'mode': state.get('mode', 'object'),
                'prop': ' / '.join(sorted({e.get('prop', 'MakeUpGain') for e in state.get('entries', [])})) or 'MakeUpGain',
            }
        except Exception:
            return None

    def revert(self):
        """把属性恢复为上次写入前的快照值（自动识别 MakeUpGain / VolumeOffset）"""
        if not self.has_undo():
            raise WaapiError('没有可撤回的快照（尚未在本机执行过写入）')
        with open(self.undo_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
        entries = state.get('entries') or []
        if not entries:
            raise WaapiError('快照为空，无法撤回')

        # 按属性分组（兼容旧快照的 old_makeup_gain 字段）
        todo = []
        for e in entries:
            prop = e.get('prop') or 'MakeUpGain'
            old = e.get('old')
            if old is None:
                old = e.get('old_makeup_gain')
            if old is None:
                continue
            todo.append({'id': e['id'], 'name': e.get('name'), 'prop': prop,
                         'old': old})
        if not todo:
            raise WaapiError('快照中没有可还原的值')

        self.log('撤回 %d 条到 %s 写入前的值 ...'
                 % (len(todo), state.get('time', '?')))

        # 先比对当前值，提示是否已被人为改动
        props = sorted(set(t['prop'] for t in todo))
        for prop in props:
            group = [t for t in todo if t['prop'] == prop]
            r = self._call('ak.wwise.core.object.get', {
                'from': {'id': [t['id'] for t in group]},
                'options': {'return': ['id', 'name', prop]}
            })
            cur = {o['id']: o.get(prop) for o in (r or {}).get('return') or []}
            drifted = [t for t in group
                       if cur.get(t['id']) is not None
                       and abs(cur[t['id']] - t['old']) > 0.05]
            if drifted:
                self.log('  注意：%d 条当前值与快照值不同（可能已被人为改动）'
                         % len(drifted))
            objects = [{'object': t['id'], '@' + prop: float(t['old'])}
                       for t in group]
            self._call('ak.wwise.core.object.set', {'objects': objects})

        # 验证
        ok = 0
        for prop in props:
            group = [t for t in todo if t['prop'] == prop]
            r = self._call('ak.wwise.core.object.get', {
                'from': {'id': [t['id'] for t in group]},
                'options': {'return': ['id', 'name', prop]}
            })
            exp = {t['id']: t['old'] for t in group}
            for o in (r or {}).get('return') or []:
                actual = o.get(prop)
                if actual is not None and abs(actual - exp.get(o['id'], 0)) <= 0.0001:
                    ok += 1
        self.log('撤回验证：%d/%d 正确' % (ok, len(todo)))
        if ok != len(todo):
            raise WaapiError('撤回回读不完整，已保留快照')
        try:
            os.remove(self.undo_file)
        except Exception:
            pass
        return ok

    # ---------------- 报告 ----------------

    def save_report(self, path):
        data = {
            'mode': self.mode,
            'music_write_level': self.music_write_level,
            'metric': self.loudness_metric,
            'reduce_only': self.reduce_only,
            'unchanged': self.unchanged,
            'plan': self.plan,
            'skipped': [{'name': n, 'why': w} for n, w in self.skipped],
        }
        if self.mode == 'perlang':
            data['clipped'] = [
                {'name': n, 'src_peak': tp, 'desired': w, 'est_peak': e,
                 'capped': c} for n, tp, w, e, c in self.clipped]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
