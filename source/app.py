# -*- coding: utf-8 -*-
"""
Wwise 音频与音乐响度管理器 - GUI 版。
按路由后的 Max LUFS-M / LUFS-I 预览，只写 Sound 或所选音乐层级的 Make-Up Gain。
"""
import os
import math
import queue
import sys
import threading
import time
import traceback


def _fatal_error(msg):
    """崩溃保护：写 error.log 并用 Win32 弹窗提示（不依赖 tkinter）"""
    try:
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(base, 'error.log')
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(msg)
    except Exception:
        pass
    if '--update-health-check' not in sys.argv:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, msg[:2000], 'X3-Wwise响度批量管理器 - 启动错误', 16)
        except Exception:
            pass
    print(msg, file=sys.stderr)
    sys.exit(1)


try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception as e:
    _fatal_error(
        '无法加载 tkinter（GUI 组件）。\n\n错误详情：%s\n\n'
        '这通常意味着打包产物缺少 tkinter/tcl 组件，'
        '请联系工具提供者获取修复版本。' % e)

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import pipeline                                    # noqa: E402
import event_report                                # noqa: E402
import updater                                     # noqa: E402
from pipeline import Pipeline, WaapiError, find_ffmpeg   # noqa: E402

APP_TITLE = 'X3-Wwise 响度与增益管理器'


class App:
    def __init__(self, root, check_updates=True):
        self.root = root
        root.title(APP_TITLE)
        root.geometry('1280x820')
        root.minsize(1040, 680)

        self.q = queue.Queue()
        self.pipe = None
        self.bus_targets = {}
        self._bus_targets_by_metric = {'momentary': {}, 'integrated': {}}
        self._active_metric = 'momentary'
        self._buses = []
        self._target_entries = {}
        self.working = False
        self.lang_vars = {}
        self.has_selection = False
        self.has_plan = False
        self.has_preview = False
        self.last_write_report = event_report.load_report(os.path.join(BASE, 'last_write_report.json'))
        self.update_info = updater.build_info()
        self.update_config_error = ''
        try:
            self.update_config = updater.read_config(BASE)
        except Exception as exc:
            self.update_config = {**self.update_info, 'auto_check': True, 'last_check': 0}
            self.update_config_error = '更新设置无法读取，请重新保存更新源：' + str(exc)
        self.available_release = None
        self.update_check_running = False
        self.update_downloading = False
        self.update_cancel = threading.Event()

        self._build_ui()
        self._build_update_ui()
        self._render_write_report(self.last_write_report, select=False)
        self._poll_queue()

        ff = find_ffmpeg()
        if ff and os.path.isfile(ff):
            self._status('测量引擎就绪')
            self.log('ffmpeg: ' + ff)
        else:
            self._status('警告：未找到 ffmpeg，请将 ffmpeg.exe 放到程序目录或加入 PATH')
        if check_updates:
            self.root.after(2500, self._auto_check_update)

    # ---------------- UI ----------------

    def _build_ui(self):
        self.colors = {
            'page': '#F3F5F8', 'panel': '#FFFFFF', 'ink': '#202B3B',
            'muted': '#596779', 'line': '#D9E0E9', 'accent': '#245EC7',
            'hover': '#184CA9', 'select': '#E7EFFD', 'warning': '#8A5300',
        }
        c = self.colors
        self.root.configure(background=c['page'])
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10),
                        background=c['page'], foreground=c['ink'])
        style.configure('TFrame', background=c['page'])
        style.configure('Panel.TFrame', background=c['panel'])
        style.configure('TLabel', background=c['page'], foreground=c['ink'])
        style.configure('Panel.TLabel', background=c['panel'])
        style.configure('Hint.TLabel', foreground=c['muted'])
        style.configure('PanelHint.TLabel', background=c['panel'], foreground=c['muted'])
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 17, 'bold'))
        style.configure('PageTitle.TLabel', font=('Microsoft YaHei UI', 13, 'bold'))
        style.configure('Section.TLabel', font=('Microsoft YaHei UI', 11, 'bold'))
        style.configure('PanelTitle.TLabel', background=c['panel'],
                        font=('Microsoft YaHei UI', 13, 'bold'))
        style.configure('TButton', padding=(12, 8), background=c['panel'],
                        bordercolor=c['line'], lightcolor=c['panel'],
                        darkcolor=c['panel'], focusthickness=2, focuscolor=c['accent'])
        style.map('TButton', background=[('disabled', '#E9EDF2'), ('active', '#E7ECF4')],
                  foreground=[('disabled', '#738093')])
        style.configure('Primary.TButton', background=c['accent'], foreground='white',
                        bordercolor=c['accent'], lightcolor=c['accent'], darkcolor=c['accent'],
                        font=('Microsoft YaHei UI', 10, 'bold'), focuscolor='white')
        style.map('Primary.TButton', background=[('disabled', '#E0E6EF'), ('pressed', '#143E89'), ('active', c['hover'])],
                  foreground=[('disabled', '#738093'), ('!disabled', 'white')],
                  bordercolor=[('disabled', '#E0E6EF'), ('!disabled', c['accent'])])
        style.configure('TEntry', padding=(7, 6), fieldbackground=c['panel'],
                        bordercolor=c['line'], lightcolor=c['line'], darkcolor=c['line'])
        style.map('TEntry', bordercolor=[('focus', c['accent'])],
                  fieldbackground=[('disabled', '#E9EDF2')])
        style.configure('TCombobox', padding=(7, 6), fieldbackground=c['panel'],
                        bordercolor=c['line'], arrowsize=14)
        style.map('TCombobox', fieldbackground=[('disabled', '#E9EDF2'), ('readonly', c['panel'])],
                  foreground=[('disabled', '#738093')], bordercolor=[('focus', c['accent'])])
        style.configure('Panel.TCheckbutton', background=c['panel'])
        style.map('Panel.TCheckbutton', background=[('active', c['panel'])])
        style.configure('Quiet.TButton', padding=(6, 4), background=c['page'],
                        borderwidth=0, foreground=c['muted'])
        for kind in ('TCheckbutton', 'TRadiobutton'):
            style.configure(kind, padding=(0, 4), focuscolor=c['accent'])
            style.map(kind, background=[('active', c['page'])],
                      foreground=[('disabled', '#738093')])
        style.configure('TNotebook', background=c['page'], borderwidth=0)
        style.configure('TNotebook.Tab', padding=(18, 10), background=c['page'],
                        foreground=c['muted'], font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('TNotebook.Tab', background=[('selected', c['panel']), ('active', '#E7ECF4')],
                  foreground=[('selected', c['accent'])])
        style.configure('Treeview', background=c['panel'], fieldbackground=c['panel'],
                        foreground=c['ink'], rowheight=34, borderwidth=0,
                        font=('Microsoft YaHei UI', 9))
        style.configure('Treeview.Heading', background='#EDF1F6', foreground=c['muted'],
                        padding=(8, 9), relief='flat', font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Treeview', background=[('selected', c['select'])],
                  foreground=[('selected', '#174787')])
        style.configure('Horizontal.TProgressbar', background=c['accent'],
                        troughcolor='#E4E9F0', borderwidth=0, thickness=4)
        self._ui_inputs = []
        self._target_labels = []
        self._lang_buttons = []

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        header = ttk.Frame(self.root, padding=(24, 14, 24, 12))
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(0, weight=1)
        title = ttk.Frame(header)
        title.grid(row=0, column=0, sticky='w')
        ttk.Label(title, text=APP_TITLE, style='Title.TLabel').pack(anchor='w')
        ttk.Label(title, text='v%s · 批量测量、核对预览，再写入工程' % self.update_info['version'], style='Hint.TLabel').pack(anchor='w', pady=(4, 0))
        connection = ttk.Frame(header)
        connection.grid(row=0, column=1, sticky='e', padx=(16, 0))
        self.lbl_conn = ttk.Label(connection, text='未连接 Wwise', style='Hint.TLabel')
        self.lbl_conn.pack(side='left', padx=(0, 16))
        self.btn_connect = ttk.Button(connection, text='连接 Wwise', command=self.on_connect)
        self.btn_connect.pack(side='left')
        self.btn_advanced = ttk.Button(connection, text='高级设置', command=self._show_advanced)
        self.btn_advanced.pack(side='left', padx=(8, 0))
        self.btn_update = ttk.Button(connection, text='检查更新', command=self.on_updates)
        self.btn_update.pack(side='left', padx=(8, 0))
        self.host_var = tk.StringVar(value='127.0.0.1')
        self.port_var = tk.StringVar(value='8080')
        self.workers_var = tk.StringVar(value='6')
        self.mode_var = tk.StringVar(value='object')
        self.music_level_var = tk.StringVar(value='track')
        self.pad_var = tk.BooleanVar(value=True)
        self.guard_var = tk.BooleanVar(value=True)
        self.metric_var = tk.StringVar(value='momentary')
        self.reduce_var = tk.BooleanVar(value=True)

        self.tabs = ttk.Notebook(self.root)
        self.tabs.grid(row=1, column=0, sticky='nsew', padx=20)
        loud_page = ttk.Frame(self.tabs)
        reset_page = ttk.Frame(self.tabs, padding=24)
        prob_page = ttk.Frame(self.tabs, padding=24)
        self.write_events_page = ttk.Frame(self.tabs, padding=16, style='Panel.TFrame')
        self.tabs.add(loud_page, text='响度调整')
        self.tabs.add(reset_page, text='增益归零')
        self.tabs.add(prob_page, text='概率修改')
        self.tabs.add(self.write_events_page, text='写入事件名单')
        self.settings_page = loud_page
        self.reset_page = reset_page
        self.prob_page = prob_page
        loud_page.columnconfigure(0, weight=1)
        loud_page.rowconfigure(0, weight=1)

        split = tk.PanedWindow(loud_page, orient='horizontal', sashwidth=8,
                              background=c['page'], borderwidth=0,
                              sashrelief='flat', opaqueresize=True)
        split.grid(row=0, column=0, sticky='nsew', pady=(16, 0))
        self.split = split
        left_host = ttk.Frame(split)
        split.add(left_host, minsize=300, width=340, stretch='never')
        left_host.rowconfigure(0, weight=1)
        left_host.columnconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(left_host, background=c['page'],
                                         highlightthickness=0, width=320)
        left_scroll = ttk.Scrollbar(left_host, orient='vertical', command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=left_scroll.set)
        self.settings_canvas.grid(row=0, column=0, sticky='nsew')
        left_scroll.grid(row=0, column=1, sticky='ns')
        left = ttk.Frame(self.settings_canvas, padding=(4, 0, 16, 12))
        self.settings_content = left
        self._settings_window = self.settings_canvas.create_window((0, 0), window=left, anchor='nw')
        left.bind('<Configure>', lambda e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox('all')))
        self.settings_canvas.bind('<Configure>', self._settings_width)
        self.root.bind('<MouseWheel>', self._scroll_settings, add='+')

        ttk.Label(left, text='选区与语言', style='Section.TLabel').pack(anchor='w')
        level_row = ttk.Frame(left)
        level_row.pack(fill='x', pady=(10, 0))
        ttk.Label(level_row, text='音乐写入层级').pack(side='left', padx=(0, 8))
        self.music_level_choice = tk.StringVar(value=pipeline.MUSIC_WRITE_LEVELS['track'])
        self.music_level_combo = ttk.Combobox(level_row, textvariable=self.music_level_choice,
            values=list(pipeline.MUSIC_WRITE_LEVELS.values()), state='readonly', width=16)
        self.music_level_combo.pack(side='right', fill='x', expand=True)
        self.music_level_combo.bind('<<ComboboxSelected>>', self._choose_music_level)
        self._ui_inputs.append(self.music_level_combo)
        self.music_level_hint = ttk.Label(left, text='只写所选层级的 Make-Up Gain。\nMusic Container 包含 Playlist / Switch；Sound 仍调整自身。',
                                          style='Hint.TLabel', wraplength=290)
        self.music_level_hint.pack(anchor='w', fill='x', pady=(6, 10))
        self.btn_read = ttk.Button(left, text='读取 Wwise 选区', command=self.on_read, state='disabled')
        self.btn_read.pack(fill='x')
        self.lbl_sel = ttk.Label(left, text='尚未读取选区', wraplength=290, style='Hint.TLabel')
        self.lbl_sel.pack(anchor='w', fill='x', pady=(8, 12))
        self.language_group = ttk.Frame(left)
        self.language_group.pack(fill='x')
        lang_head = ttk.Frame(self.language_group)
        lang_head.pack(fill='x')
        ttk.Label(lang_head, text='处理语言').pack(side='left')
        ttk.Label(lang_head, text='默认全选', style='Hint.TLabel').pack(side='right')
        self.frm_langs = ttk.Frame(self.language_group)
        self.frm_langs.pack(fill='x', pady=(4, 0))
        ttk.Label(self.frm_langs, text='读取选区后显示可用语言', style='Hint.TLabel').pack(anchor='w')
        self.target_separator = ttk.Separator(left)
        self.target_separator.pack(fill='x', pady=16)
        target_head = ttk.Frame(left)
        target_head.pack(fill='x')
        ttk.Label(target_head, text='Bus 目标响度', style='Section.TLabel').pack(side='left')
        self.target_unit = ttk.Label(target_head, text='LUFS-M', style='Hint.TLabel')
        self.target_unit.pack(side='right')
        metric_row = ttk.Frame(left)
        metric_row.pack(fill='x', pady=(6, 0))
        for label, value in [('Max LUFS-M', 'momentary'), ('综合 LUFS-I', 'integrated')]:
            radio = ttk.Radiobutton(metric_row, text=label, value=value, variable=self.metric_var)
            radio.pack(side='left', padx=(0, 12))
            self._ui_inputs.append(radio)
        self.metric_hint = ttk.Label(left, text='400ms 窗口的最大瞬时响度。',
                                     style='Hint.TLabel', wraplength=290)
        self.metric_hint.pack(anchor='w', fill='x', pady=(4, 8))
        self.frm_targets = ttk.Frame(left)
        self.frm_targets.pack(fill='x')
        ttk.Label(self.frm_targets, text='读取选区后显示 Bus 与目标输入', style='Hint.TLabel', wraplength=290).pack(anchor='w', pady=8)
        self.reduce_chk = ttk.Checkbutton(left, text='仅降低超标项', variable=self.reduce_var)
        self.reduce_chk.pack(anchor='w', pady=(6, 0))
        self.reduce_hint = ttk.Label(left, text='按当前 Output 判断，未超标保持原增益。',
                  style='Hint.TLabel', wraplength=290)
        self.reduce_hint.pack(anchor='w', fill='x', pady=(2, 0))
        self._ui_inputs.append(self.reduce_chk)
        ttk.Separator(left).pack(fill='x', pady=16)
        self.btn_options = ttk.Button(left, text='展开测量选项', style='Quiet.TButton', command=self._toggle_options)
        self.btn_options.pack(anchor='w')
        self.measure_options = ttk.Frame(left)
        self.mode_summary = ttk.Label(self.measure_options, text='写入 Sound / Music Track 的 Make-Up Gain', style='Hint.TLabel', wraplength=290)
        self.mode_summary.pack(anchor='w', fill='x', pady=(8, 6))
        self.pad_chk = ttk.Checkbutton(self.measure_options, text='短文件补救（估算）', variable=self.pad_var)
        self.pad_chk.pack(anchor='w')
        self.guard_chk = ttk.Checkbutton(self.measure_options, text='削波护栏', variable=self.guard_var)
        self.guard_chk.pack(anchor='w')
        self._ui_inputs.extend([self.pad_chk, self.guard_chk])

        right = ttk.Frame(split, style='Panel.TFrame', padding=(18, 16))
        self.preview_page = right
        split.add(right, minsize=560, stretch='always')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        preview_head = ttk.Frame(right, style='Panel.TFrame')
        preview_head.grid(row=0, column=0, sticky='ew')
        ttk.Label(preview_head, text='测量与修改预览', style='PanelTitle.TLabel').pack(side='left')
        self.btn_report = ttk.Button(preview_head, text='导出 JSON 预览', command=self.on_report, state='disabled')
        self.btn_report.pack(side='right')
        self.show_measure_details = tk.BooleanVar(value=False)
        self.details_chk = ttk.Checkbutton(preview_head, text='显示测量细节', variable=self.show_measure_details,
                                           style='Panel.TCheckbutton', command=self._toggle_preview_details)
        self.details_chk.pack(side='right', padx=(0, 12))
        self.preview_hint = ttk.Label(right, text='核对路由后的 Output 响度与增益，确认后再应用。', style='PanelHint.TLabel')
        self.preview_hint.grid(row=1, column=0, sticky='ew', pady=(6, 14))
        tableframe = ttk.Frame(right, style='Panel.TFrame')
        tableframe.grid(row=2, column=0, sticky='nsew')
        tableframe.columnconfigure(0, weight=1)
        tableframe.rowconfigure(0, weight=1)
        columns = ('sound', 'lang', 'source', 'chain', 'current', 'target', 'gain', 'output', 'status')
        self.preview = ttk.Treeview(tableframe, columns=columns, show='headings', selectmode='browse')
        for col, label, width in zip(columns,
                ['写入对象', '语言', '源 LUFS-M', '固定链 dB', '当前 Output', '目标', '新增益 dB', '预计 Output', '状态'],
                [164, 62, 92, 80, 94, 62, 86, 94, 112]):
            self.preview.heading(col, text=label)
            self.preview.column(col, width=width, minwidth=width, stretch=(col in ('sound', 'status')),
                                anchor='w' if col in ('sound', 'lang', 'status') else 'e')
        self._toggle_preview_details()
        self.preview.grid(row=0, column=0, sticky='nsew')
        vs = ttk.Scrollbar(tableframe, orient='vertical', command=self.preview.yview)
        hs = ttk.Scrollbar(tableframe, orient='horizontal', command=self.preview.xview)
        self.preview.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        self.preview.tag_configure('even', background='#F7F9FC')
        self.preview.tag_configure('warning', foreground=c['warning'])
        self.preview.tag_configure('skip', foreground='#A13232')
        self.preview.tag_configure('keep', foreground=c['muted'])
        self.preview.tag_configure('stale', foreground='#657286')
        self.preview.bind('<<TreeviewSelect>>', self._preview_selection)
        self.preview_empty = ttk.Frame(tableframe, style='Panel.TFrame', padding=12)
        ttk.Label(self.preview_empty, text='从 Wwise 选区开始', style='PanelTitle.TLabel').pack()
        self.empty_copy = ttk.Label(self.preview_empty,
            text='连接 Wwise → 读取选区 → 测量并预览\n结果会显示在这里，测量不会写入工程。',
            style='PanelHint.TLabel', justify='center')
        self.empty_copy.pack(pady=(10, 0))
        self.preview_empty.place(relx=.5, rely=.46, anchor='center')
        self.detail_var = tk.StringVar(value='选择结果行，查看完整对象名称和状态。')
        self.preview_detail = ttk.Entry(right, textvariable=self.detail_var, state='readonly',
                                        font=('Microsoft YaHei UI', 9))
        self.preview_detail.grid(row=3, column=0, sticky='ew', pady=(12, 0))

        ops = ttk.Frame(loud_page, padding=(4, 14, 4, 14))
        ops.grid(row=1, column=0, sticky='ew')
        ops.columnconfigure(0, weight=1)
        self.step_var = tk.StringVar(value='先连接 Wwise，再读取选区。')
        step_label = ttk.Label(ops, textvariable=self.step_var, style='Hint.TLabel', wraplength=580)
        step_label.grid(row=0, column=0, sticky='w', padx=(0, 16))
        step_label.bind('<Configure>', lambda e: step_label.configure(wraplength=max(200, e.width)))
        self.btn_measure = ttk.Button(ops, text='测量并预览', style='Primary.TButton', command=self.on_measure, state='disabled')
        self.btn_measure.grid(row=0, column=1, padx=(0, 8))
        self.btn_apply = ttk.Button(ops, text='应用到 Wwise', command=self.on_apply, state='disabled')
        self.btn_apply.grid(row=0, column=2)

        ttk.Label(reset_page, text='选中对象增益归零', style='PageTitle.TLabel').pack(anchor='w')
        ttk.Label(reset_page, text='直接读取 Wwise 当前选区，仅处理选中对象本身。', style='Hint.TLabel').pack(anchor='w', pady=(6, 24))
        self.reset_makeup = tk.BooleanVar(value=True)
        self.reset_volume = tk.BooleanVar(value=True)
        for text, var in [('Make-Up Gain', self.reset_makeup), ('Voice Volume', self.reset_volume)]:
            cb = ttk.Checkbutton(reset_page, text=text, variable=var)
            cb.pack(anchor='w', pady=4)
            self._ui_inputs.append(cb)
        self.btn_reset = ttk.Button(reset_page, text='预览增益归零', command=self.on_reset, state='disabled')
        self.btn_reset.pack(anchor='w', pady=(20, 12))
        self.reset_hint = ttk.Label(reset_page, text='请先连接 Wwise，再选择需要归零的对象。', style='Hint.TLabel')
        self.reset_hint.pack(anchor='w', pady=(0, 16))
        ttk.Label(reset_page, text='预览确认后归零，并回读验证。\n支持 Sound、音乐对象、音频容器、Actor-Mixer 和 Bus；不处理源 VolumeOffset。\n归零后如需调整响度，请重新读取选区并测量。',
                  style='Hint.TLabel', wraplength=760).pack(anchor='w')
        ttk.Label(prob_page, text='Play Action 概率', style='PageTitle.TLabel').pack(anchor='w')
        ttk.Label(prob_page, text='独立操作。只调整响度时，无需启用此功能。', style='Hint.TLabel').pack(anchor='w', pady=(6, 24))
        self.prob_enabled = tk.BooleanVar(value=False)
        self.prob_chk = ttk.Checkbutton(prob_page, text='启用概率修改', variable=self.prob_enabled,
                                        command=lambda: self._set_buttons_working(self.working))
        self.prob_chk.pack(anchor='w', pady=8)
        probrow = ttk.Frame(prob_page)
        probrow.pack(anchor='w', pady=12)
        ttk.Label(probrow, text='Probability (%)').pack(side='left', padx=(0, 12))
        self.prob_var = tk.StringVar(value='60')
        self.prob_entry = ttk.Entry(probrow, textvariable=self.prob_var, width=8, state='disabled')
        self.prob_entry.pack(side='left')
        self.btn_prob = ttk.Button(probrow, text='预览 / 写入概率', command=self.on_probability, state='disabled')
        self.btn_prob.pack(side='left', padx=12)
        self.prob_hint = ttk.Label(prob_page, text='请先连接 Wwise，并在响度页读取选区。', style='Hint.TLabel')
        self.prob_hint.pack(anchor='w', pady=(0, 8))
        ttk.Label(prob_page, text='在响度页读取选区后，查找指向 Sound、Music Track 或上级音乐容器的 Play Action。\n写入前会显示匹配路径和前后数值。', style='Hint.TLabel', wraplength=760).pack(anchor='w', pady=12)

        receipt = self.write_events_page
        receipt.columnconfigure(0, weight=1)
        receipt.rowconfigure(4, weight=1)
        receipt_head = ttk.Frame(receipt, style='Panel.TFrame')
        receipt_head.grid(row=0, column=0, sticky='ew')
        ttk.Label(receipt_head, text='写入后的受影响 Event', style='PanelTitle.TLabel').pack(side='left')
        self.event_summary = ttk.Label(receipt, text='写入并回读后生成名单，Excel 自动保存到桌面 waapi-state。',
                                       style='PanelHint.TLabel', wraplength=930)
        self.event_summary.grid(row=2, column=0, sticky='ew', pady=(6, 10))
        self.event_summary.bind('<Configure>', lambda e: self.event_summary.configure(wraplength=max(300, e.width)))
        event_toolbar = ttk.Frame(receipt, style='Panel.TFrame')
        event_toolbar.grid(row=3, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(event_toolbar, text='查找', style='Panel.TLabel').pack(side='left', padx=(0, 8))
        self.event_filter_var = tk.StringVar()
        self.event_filter = ttk.Entry(event_toolbar, textvariable=self.event_filter_var, width=28)
        self.event_filter.pack(side='left')
        self.event_filter_var.trace_add('write', lambda *_: self._filter_events())
        self.event_filter_count = ttk.Label(event_toolbar, text='0 个 Event', style='PanelHint.TLabel')
        self.event_filter_count.pack(side='left', padx=12)
        self.btn_event_export = ttk.Button(event_toolbar, text='另存 Excel', command=self.on_event_export, state='disabled')
        self.btn_event_export.pack(side='right')
        self.btn_event_copy = ttk.Button(event_toolbar, text='复制 Event 名称', command=self.on_event_copy, state='disabled')
        self.btn_event_copy.pack(side='right', padx=(0, 8))
        event_table = ttk.Frame(receipt, style='Panel.TFrame')
        event_table.grid(row=4, column=0, sticky='nsew')
        event_table.columnconfigure(0, weight=1)
        event_table.rowconfigure(0, weight=1)
        self.event_tree = ttk.Treeview(event_table, columns=('name', 'path', 'count', 'objects'), show='headings', selectmode='browse')
        for column, title, width in [('name', 'Event 名称', 200), ('path', 'Wwise 路径', 320),
                                     ('count', '改动对象数', 92), ('objects', '改动对象', 240)]:
            self.event_tree.heading(column, text=title)
            self.event_tree.column(column, width=width, minwidth=width, stretch=column in ('name', 'path'))
        self.event_tree.grid(row=0, column=0, sticky='nsew')
        event_vs = ttk.Scrollbar(event_table, orient='vertical', command=self.event_tree.yview)
        event_hs = ttk.Scrollbar(event_table, orient='horizontal', command=self.event_tree.xview)
        self.event_tree.configure(yscrollcommand=event_vs.set, xscrollcommand=event_hs.set)
        event_vs.grid(row=0, column=1, sticky='ns')
        event_hs.grid(row=1, column=0, sticky='ew')
        self.event_tree.tag_configure('even', background='#F3F5F8')
        self.event_tree.bind('<<TreeviewSelect>>', self._event_selection)
        self.event_empty = ttk.Label(event_table, text='写入并回读后，受影响的 Event 会显示在这里。',
                                      style='PanelHint.TLabel', justify='center')
        self.event_empty.place(relx=.5, rely=.5, anchor='center')
        self.event_detail_var = tk.StringVar(value='选择一行查看完整 Event 名称与路径。')
        ttk.Entry(receipt, textvariable=self.event_detail_var, state='readonly').grid(row=5, column=0, sticky='ew', pady=(8, 6))
        event_file_row = ttk.Frame(receipt, style='Panel.TFrame')
        event_file_row.grid(row=6, column=0, sticky='ew')
        ttk.Label(event_file_row, text='Excel 文件', style='PanelHint.TLabel').pack(side='left', padx=(0, 8))
        self.btn_event_open = ttk.Button(event_file_row, text='打开 Excel', command=self.on_event_open, state='disabled')
        self.btn_event_open.pack(side='right', padx=(8, 0))
        self.event_file_var = tk.StringVar(value='尚未生成 Excel')
        ttk.Entry(event_file_row, textvariable=self.event_file_var, state='readonly').pack(side='left', fill='x', expand=True)

        footer = ttk.Frame(self.root, padding=(24, 10, 24, 12))
        footer.grid(row=2, column=0, sticky='ew')
        footer.columnconfigure(1, weight=1)
        self.btn_log = ttk.Button(footer, text='展开操作日志', command=self._toggle_log)
        self.btn_log.grid(row=0, column=0, sticky='w')
        self.status_var = tk.StringVar(value='就绪')
        statusbar = ttk.Label(footer, textvariable=self.status_var, style='Hint.TLabel', wraplength=560)
        statusbar.grid(row=0, column=1, sticky='ew', padx=16)
        statusbar.bind('<Configure>', lambda e: statusbar.configure(wraplength=max(200, e.width)))
        self.lbl_progress = ttk.Label(footer, text='', style='Hint.TLabel')
        self.lbl_progress.grid(row=0, column=2, padx=(0, 12))
        self.btn_revert = ttk.Button(footer, text='撤回上次写入', command=self.on_revert, state='disabled')
        self.btn_revert.grid(row=0, column=3, sticky='e')
        self.undo_summary = ttk.Label(footer, text='', style='Hint.TLabel')
        self.undo_summary.grid(row=1, column=3, sticky='e', pady=(4, 0))
        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.grid(row=4, column=0, sticky='ew')
        self.logframe = ttk.Frame(self.root, padding=(24, 0, 24, 12))
        self.logframe.grid(row=3, column=0, sticky='ew')
        self.logframe.grid_remove()
        log_scroll = ttk.Scrollbar(self.logframe, orient='vertical')
        self.txt = tk.Text(self.logframe, height=5, wrap='word', state='disabled',
                           font=('Microsoft YaHei UI', 9), background=c['panel'], foreground=c['ink'],
                           relief='flat', padx=12, pady=8, yscrollcommand=log_scroll.set)
        log_scroll.configure(command=self.txt.yview)
        log_scroll.pack(side='right', fill='y')
        self.txt.pack(fill='both', expand=True)
        self.root.bind('<Configure>', self._resize_workspace, add='+')

        self.advanced_window = tk.Toplevel(self.root)
        self.advanced_window.title('高级设置')
        self.advanced_window.geometry('560x400')
        self.advanced_window.minsize(520, 360)
        self.advanced_window.withdraw()
        self.advanced_window.transient(self.root)
        self.advanced_window.protocol('WM_DELETE_WINDOW', self.advanced_window.withdraw)
        self.advanced_window.bind('<Escape>', lambda e: self.advanced_window.withdraw())
        advanced = ttk.Frame(self.advanced_window, padding=24)
        advanced.pack(fill='both', expand=True)
        ttk.Label(advanced, text='增益写入方式', style='Section.TLabel').pack(anchor='w', pady=(0, 8))
        self.lbl_mode_hint = ttk.Label(advanced, wraplength=480, style='Hint.TLabel')
        self.lbl_mode_hint.pack(anchor='w', pady=(8, 20))
        ttk.Label(advanced, text='WAAPI 连接与测量', style='Section.TLabel').pack(anchor='w', pady=(0, 8))
        connrow = ttk.Frame(advanced)
        connrow.pack(fill='x')
        for col, (label, var, width) in enumerate([('主机', self.host_var, 20), ('端口', self.port_var, 8), ('测量线程', self.workers_var, 8)]):
            ttk.Label(connrow, text=label, style='Hint.TLabel').grid(row=0, column=col, sticky='w', padx=(0, 12), pady=(0, 6))
            entry = ttk.Entry(connrow, textvariable=var, width=width)
            entry.grid(row=1, column=col, sticky='w', padx=(0, 12))
            self._ui_inputs.append(entry)
        ttk.Label(advanced, text='连接参数在下次连接 Wwise 时生效。', style='Hint.TLabel').pack(anchor='w', pady=(10, 0))
        ttk.Button(advanced, text='完成', command=self.advanced_window.withdraw).pack(side='bottom', anchor='e', pady=(16, 0))
        self._on_mode_change()
        for var in (self.pad_var, self.guard_var, self.reduce_var):
            var.trace_add('write', self._settings_changed)
        self.metric_var.trace_add('write', lambda *args: self._on_metric_change())
        self.music_level_var.trace_add('write', lambda *args: self._on_music_level_change())
        self._set_buttons_working(False)

    def _choose_music_level(self, _event=None):
        for value, label in pipeline.MUSIC_WRITE_LEVELS.items():
            if label == self.music_level_choice.get():
                self.music_level_var.set(value)
                break

    def _toggle_options(self):
        if self.measure_options.winfo_manager():
            self.measure_options.pack_forget()
            self.btn_options.configure(text='展开测量选项')
        else:
            self.measure_options.pack(fill='x')
            self.btn_options.configure(text='收起测量选项')

    def _toggle_preview_details(self):
        columns = ('sound', 'lang', 'source', 'chain', 'current', 'target', 'gain', 'output', 'status')
        self.preview.configure(displaycolumns=columns if self.show_measure_details.get() else
                               ('sound', 'current', 'target', 'gain', 'output', 'status'))
        self.preview.xview_moveto(0)

    def _show_advanced(self):
        self.advanced_window.deiconify()
        self.advanced_window.lift()

    def _toggle_log(self):
        if self.logframe.winfo_manager():
            self.logframe.grid_remove()
            self.btn_log.configure(text='展开操作日志')
        else:
            self.logframe.grid()
            self.btn_log.configure(text='收起操作日志')

    def _resize_workspace(self, event):
        if event.widget == self.root:
            rows = 2 if event.height < 760 else 5
            if int(self.txt.cget('height')) != rows:
                self.txt.configure(height=rows)

    def _settings_width(self, event):
        self.settings_canvas.itemconfigure(self._settings_window, width=event.width)
        width = max(240, event.width - 24)
        self.lbl_sel.configure(wraplength=width)
        if hasattr(self, 'metric_hint'):
            self.metric_hint.configure(wraplength=width)
        for name in ('music_level_hint', 'mode_summary', 'reduce_hint'):
            getattr(self, name).configure(wraplength=width)
        for label in self._target_labels:
            if label.winfo_exists():
                label.configure(wraplength=max(90, width - 132))
        self._layout_languages(width)

    def _layout_languages(self, width):
        if not self._lang_buttons:
            return
        widest = max(cb.winfo_reqwidth() for cb in self._lang_buttons)
        columns = 2 if 2 * (widest + 12) <= width else 1
        for i, cb in enumerate(self._lang_buttons):
            cb.grid_configure(row=i // columns, column=i % columns)
        self._lang_note.grid_configure(row=(len(self._lang_buttons) + columns - 1) // columns,
                                       columnspan=columns)

    def _scroll_settings(self, event):
        widget = event.widget
        while widget is not None:
            if widget == self.settings_canvas:
                if self.settings_content.winfo_reqheight() > self.settings_canvas.winfo_height():
                    self.settings_canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def _preview_selection(self, event=None):
        selected = self.preview.selection()
        if selected:
            values = self.preview.item(selected[0], 'values')
            unit = 'LUFS-I' if self.metric_var.get() == 'integrated' else 'LUFS-M'
            self.detail_var.set('%s · %s · 源 %s %s · 固定链 %s dB · %s' %
                                (values[0], values[1], values[2], unit, values[3], values[-1]))
            self.preview_detail.xview_moveto(0)

    def _settings_changed(self, *args):
        self._invalidate_preview('设置已更改，请重新测量后应用。')

    def _invalidate_preview(self, reason):
        had_plan = self.has_plan
        self.has_plan = False
        self.has_preview = False
        if self.preview.get_children():
            self.preview_hint.configure(text=reason)
            for item in self.preview.get_children():
                tags = self.preview.item(item, 'tags')
                self.preview.item(item, tags=tuple(t for t in tags if t != 'stale') + ('stale',))
                if 'skip' not in tags:
                    self.preview.set(item, 'status', '需重新测量')
            self.detail_var.set(reason)
        if had_plan:
            self._status(reason)
        self._set_buttons_working(self.working)


    def _render_preview(self):
        self.preview.delete(*self.preview.get_children())
        if not self.pipe:
            return
        def number(value):
            return '' if value is None else ('%.2f' % value if isinstance(value, (int, float)) else value)
        for i, p in enumerate(self.pipe.plan):
            note = p.get('note')
            tags = ('even',) if i % 2 == 0 else ()
            if note or p.get('padded'):
                tags += ('warning',)
            self.preview.insert('', 'end', values=(
                p.get('sound_name', p.get('name')), p.get('lang', '共用'),
                number(p.get('src_lufs', p.get('src_max_m'))),
                number(p.get('fixed_chain', round(p.get('volume', 0) + p.get('parent_chain_gain', 0) + p.get('bus_gain', 0), 3))),
                number(p.get('current_output_lufs')),
                number(p.get('target')), number(p.get('new_volume_offset', p.get('new_makeup_gain'))),
                number(p.get('output_lufs', p.get('output_max_m'))),
                '短文件估算' if p.get('padded') else '有限制 · 见日志' if note else
                '待写入 · 影响 %d 个音频对象' % p['member_count'] if p.get('member_count', 0) > 1 else '待写入'), tags=tags)
        kept = getattr(self.pipe, 'unchanged', [])
        kept_tags = set()
        for p in kept:
            name = p.get('sound_name', p.get('name'))
            lang = p.get('lang', '共用')
            tag = '%s[%s]' % (name, lang) if 'media_id' in p else name
            kept_tags.add((tag, p['note']))
            fixed = p.get('fixed_chain', p.get('volume', 0) + p.get('parent_chain_gain', 0) + p.get('bus_gain', 0)
                          + (p.get('makeup_gain', 0) if 'media_id' in p else p.get('source_gain', 0)))
            old = p.get('old_volume_offset', p.get('makeup_gain'))
            self.preview.insert('', 'end', values=(name, lang, number(p['src_lufs']),
                number(fixed), number(p['current_output_lufs']), number(p['target']),
                number(old), number(p['output_lufs']), p['note']), tags=('keep',))
        for name, why in self.pipe.skipped:
            if (name, why) not in kept_tags:
                self.preview.insert('', 'end', values=(name, '', '', '', '', '', '', '', why), tags=('skip',))
        if self.preview.get_children():
            self.preview_empty.place_forget()
        else:
            self.preview_empty.place(relx=.5, rely=.46, anchor='center')
            self.empty_copy.configure(text='本次没有可预览的结果。请检查选区与操作日志。')
        self.preview_hint.configure(text='%d 条可写入 · %d 条保持不变 · %d 条跳过 · %s / 增益 dB' % (
            len(self.pipe.plan), len(kept), len(self.pipe.skipped) - len(kept),
            'LUFS-I' if self.metric_var.get() == 'integrated' else 'LUFS-M'))
        self.detail_var.set('选择一行查看完整名称。棕色文字表示估算或增益限制，详情见日志。')

    def _on_mode_change(self):
        """Keep the user-authorized write target fixed to object Make-Up Gain."""
        self.mode_var.set('object')
        level = pipeline.MUSIC_WRITE_LEVELS[self.music_level_var.get()]
        scope_hint = {
            'track': 'Track 的 Make-Up Gain，影响全部音频源。',
            'segment': 'Segment 的 Make-Up Gain，全部 Track（含较安静轨道）一起变化。',
            'container': 'Playlist / Switch 的 Make-Up Gain，全部 Track（含较安静轨道）一起变化。',
        }[self.music_level_var.get()]
        self.music_level_hint.configure(text=scope_hint + ' Sound 调整自身。')
        self.music_level_choice.set(level)
        self.lbl_mode_hint.configure(text='写入 Sound / %s 的 Make-Up Gain。所选音乐层级在主页面修改。源增益只读取并计入 Output。' % level)
        self.mode_summary.configure(text='写入 Sound / %s 的 Make-Up Gain' % level)
        if self.has_selection:
            self.has_selection = False
            self.lbl_sel.configure(text='增益模式已更改，请重新读取选区')
        self._invalidate_preview('增益模式已更改，请重新读取选区并测量。')

    def _on_music_level_change(self):
        self.has_selection = False
        self.lbl_sel.configure(text='音乐写入层级已更改，请重新读取选区')
        self._on_mode_change()
        self._invalidate_preview('音乐写入层级已更改，请重新读取选区并测量。')

    def _on_metric_change(self):
        metric = self.metric_var.get()
        if metric != self._active_metric:
            self._bus_targets_by_metric[self._active_metric].update(
                {name: var.get() for name, var in self._target_entries.items()})
            self._active_metric = metric
            self.bus_targets = dict(self._bus_targets_by_metric[metric])
            self._render_targets(self._buses)
        unit = 'LUFS-I' if metric == 'integrated' else 'LUFS-M'
        self.target_unit.configure(text=unit)
        self.preview.heading('source', text='源 ' + unit)
        self.metric_hint.configure(text=(
            '整段综合响度，按完整增益与路由链计算 Output。'
            if metric == 'integrated' else '400ms 最大瞬时响度，按完整增益与路由链计算 Output。'))
        self._invalidate_preview('响度标准已更改，请重新测量后应用。')

    # ---------------- 日志 / 状态 ----------------

    def _build_update_ui(self):
        self.update_window = tk.Toplevel(self.root)
        self.update_window.title('程序更新')
        self.update_window.geometry('600x520')
        self.update_window.minsize(540, 460)
        self.update_window.withdraw()
        self.update_window.transient(self.root)
        self.update_window.protocol('WM_DELETE_WINDOW', self._close_update_window)
        panel = ttk.Frame(self.update_window, padding=24)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='程序更新', style='PageTitle.TLabel').pack(anchor='w')
        ttk.Label(panel, text='当前版本 v' + self.update_info['version'], style='Hint.TLabel').pack(anchor='w', pady=(6, 16))
        ttk.Label(panel, text='GitHub 更新仓库').pack(anchor='w')
        self.update_repo_var = tk.StringVar(value=self.update_config['repository'])
        self.update_repo_entry = ttk.Entry(panel, textvariable=self.update_repo_var)
        self.update_repo_entry.pack(fill='x', pady=(6, 4))
        ttk.Label(panel, text='填写公开仓库地址或「账号/仓库」。发布者设置好后，使用者无需更改。',
                  style='Hint.TLabel', wraplength=540).pack(anchor='w')
        self.update_auto_var = tk.BooleanVar(value=self.update_config['auto_check'])
        self.update_auto_check = ttk.Checkbutton(panel, text='启动时自动检查更新', variable=self.update_auto_var)
        self.update_auto_check.pack(anchor='w', pady=(10, 8))
        settings = ttk.Frame(panel)
        settings.pack(fill='x')
        self.btn_update_save = ttk.Button(settings, text='保存更新设置', command=self.on_save_update_config)
        self.btn_update_save.pack(side='left')
        self.btn_update_check = ttk.Button(settings, text='检查更新', command=self.on_check_update)
        self.btn_update_check.pack(side='left', padx=8)
        self.update_status_var = tk.StringVar(value=self.update_config_error or '检查 GitHub Releases 中的正式版本。')
        ttk.Label(panel, textvariable=self.update_status_var, style='Hint.TLabel', wraplength=540).pack(fill='x', pady=(16, 8))
        self.update_progress = ttk.Progressbar(panel, mode='determinate')
        self.update_progress.pack(fill='x', pady=(0, 12))
        self.update_notes = tk.Text(panel, height=5, wrap='word', state='disabled',
            font=('Microsoft YaHei UI', 9), background=self.colors['panel'], relief='flat', padx=10, pady=8)
        self.update_notes.pack(fill='both', expand=True)
        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=(16, 0))
        self.btn_update_install = ttk.Button(actions, text='下载并更新', style='Primary.TButton',
            state='disabled', command=self.on_install_update)
        self.btn_update_install.pack(side='right')
        self.btn_update_cancel = ttk.Button(actions, text='取消下载', state='disabled', command=self.update_cancel.set)
        self.btn_update_cancel.pack(side='right', padx=8)

    def _close_update_window(self):
        if self.update_downloading:
            self.update_cancel.set()
        self.update_window.withdraw()

    def _update_controls(self):
        if not hasattr(self, 'update_window'):
            return
        busy = self.update_check_running or self.update_downloading or self.working
        for widget in (self.update_repo_entry, self.update_auto_check, self.btn_update_save, self.btn_update_check):
            widget.configure(state='disabled' if busy else 'normal')
        self.btn_update_install.configure(state='normal' if self.available_release and not busy and getattr(sys, 'frozen', False) else 'disabled')
        self.btn_update_cancel.configure(state='normal' if self.update_downloading else 'disabled')

    def on_updates(self):
        if self.working:
            return
        self.update_window.deiconify()
        self.update_window.lift()
        if not self.update_config['repository']:
            self.update_status_var.set('尚未设置更新源，请填写发布者提供的公开 GitHub 仓库并保存。')
        elif not self.update_check_running and not self.available_release:
            self.on_check_update()

    def on_save_update_config(self):
        if self.working or self.update_check_running:
            return
        try:
            repository = updater.normalize_repo(self.update_repo_var.get())
            config = {**self.update_config, 'repository': repository,
                      'auto_check': self.update_auto_var.get(), 'last_check': 0}
            updater.save_config(BASE, config)
            self.update_config = config
            self.update_repo_var.set(repository)
            self.available_release = None
            self.btn_update.configure(text='检查更新')
            self.update_status_var.set('更新设置已保存，点击「检查更新」获取正式版本。')
            self._update_controls()
        except Exception as exc:
            self.update_status_var.set('无法保存更新设置：' + str(exc))

    def _auto_check_update(self):
        if (self.working or not self.update_config['repository'] or not self.update_config['auto_check'] or
                time.time() - self.update_config['last_check'] < 3600):
            return
        self.on_check_update(automatic=True)

    def on_check_update(self, automatic=False):
        if self.working or self.update_check_running or self.update_downloading:
            return
        if not self.update_config['repository']:
            self.update_status_var.set('请先填写并保存公开 GitHub 仓库。')
            return
        self.update_check_running = True
        self.update_status_var.set('正在检查更新…')
        self._update_controls()
        repository = self.update_config['repository']
        def worker():
            try:
                release = updater.check_release(repository, self.update_info['version'])
                self.q.put(('update_checked', release))
            except Exception as exc:
                self.q.put(('update_failed', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def on_install_update(self):
        if self.working or not self.available_release or not getattr(sys, 'frozen', False):
            return
        release = self.available_release
        if not messagebox.askyesno('安装更新', '将下载 v%s，校验完成后关闭本工具、安装并重新启动。\n\n'
                '请先保存 Wwise 工程。撤回快照与写入历史会保留。\n确认更新？' % release['version'], parent=self.update_window):
            return
        self.update_downloading = True
        self.update_cancel.clear()
        self.update_status_var.set('正在下载更新…')
        self._set_buttons_working(True)
        def progress(done, total, message=''):
            self.q.put(('update_progress', done, total, message))
        def worker():
            try:
                stage = updater.stage_release(release, progress, self.update_cancel)
                if self.update_cancel.is_set():
                    raise updater.UpdateError('已取消安装，当前版本保持不变。')
                self.q.put(('update_progress', release['size'], release['size'], '正在启动安装组件…'))
                updater.start_installer(stage, BASE, release)
                self.q.put(('update_restart',))
            except Exception as exc:
                self.q.put(('update_failed', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def log(self, msg):
        self.q.put(('log', msg))

    def _status(self, msg):
        self.status_var.set(msg)
        if self.working:
            self.step_var.set(msg)
        elif self.has_plan:
            self.step_var.set('核对预览结果后，点击「应用到 Wwise」。')
        elif self.has_selection:
            self.step_var.set('设置目标响度，然后点击「测量并预览」。')
        else:
            self.step_var.set('在 Wwise 中选好对象，再点击「读取选区」。' if self.pipe and self.pipe.client else '先连接 Wwise，再读取选区。')

    def _append_log(self, msg):
        self.txt.configure(state='normal')
        self.txt.insert('end', '[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
        self.txt.see('end')
        self.txt.configure(state='disabled')

    def set_progress(self, done, total, msg):
        self.q.put(('progress', done, total, msg))

    # ---------------- 队列轮询 ----------------

    def _poll_queue(self):
        """轮询工作线程消息。

        关键：单条消息处理异常**绝不能**打断 after 链，否则整个 UI
        从此永久僵死（日志仍在追加但界面再不刷新）。
        所以每条消息单独 try/except，且 self.root.after 放在 finally 里。
        """
        try:
            while True:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_msg(item)
                except Exception:
                    # 单条消息失败只记日志，不影响后续消息与 UI 刷新
                    try:
                        self._append_log('UI 更新异常：%s'
                                         % traceback.format_exc(limit=1).strip())
                    except Exception:
                        pass
        finally:
            try:
                self.root.after(100, self._poll_queue)
            except Exception:
                pass

    def _handle_msg(self, item):
        kind = item[0]
        if kind == 'update_checked':
            self.update_check_running = False
            self.available_release = item[1]
            self.update_config['last_check'] = time.time()
            try:
                updater.save_config(BASE, self.update_config)
            except Exception as exc:
                self._append_log('更新检查设置未保存：' + str(exc))
            release = self.available_release
            self.update_status_var.set('发现新版本 v%s，可下载并更新。' % release['version'] if release else '当前 v%s 已是最新正式版本。' % self.update_info['version'])
            self.update_notes.configure(state='normal')
            self.update_notes.delete('1.0', 'end')
            self.update_notes.insert('1.0', release['notes'] if release else '')
            self.update_notes.configure(state='disabled')
            self.btn_update.configure(text='发现新版本' if release else '检查更新')
            self._update_controls()
        elif kind == 'update_progress':
            self.update_progress.configure(value=100 * item[1] / item[2] if item[2] else 0)
            self.update_status_var.set(item[3] or '正在下载：%.1f / %.1f MB' % (item[1] / 1048576, item[2] / 1048576))
        elif kind == 'update_failed':
            downloading = self.update_downloading
            self.update_downloading = False
            self.update_check_running = False
            self.update_status_var.set(item[1])
            self._append_log('程序更新：' + item[1])
            if downloading:
                self._set_buttons_working(False)
            self._update_controls()
        elif kind == 'update_restart':
            if self.pipe and self.pipe.client:
                try:
                    self.pipe.client.disconnect()
                except Exception:
                    pass
            self.root.destroy()
        elif kind == 'log':
            self._append_log(item[1])
        elif kind == 'progress':
            _, done, total, msg = item
            pct = int(done * 100 / total) if total else 0
            self.progress.configure(value=pct)
            self.lbl_progress.configure(text='%s %d/%d' % (msg, done, total))
        elif kind == 'connected':
            ver = item[1]
            self.lbl_conn.configure(text='已连接 ' + ver, foreground='#287149')
            self._status('已连接 Wwise %s，请在 Wwise 中选好对象后点「读取」' % ver)
            self.progress.configure(value=0)
        elif kind == 'selection':
            # item = ('selection', n_sound, bus_list, lang_list, n_media, mode)
            _, n_sound, buses, langs, n_media, mode = item
            self.has_selection = True
            self.preview.delete(*self.preview.get_children())
            self.preview_empty.place(relx=.5, rely=.46, anchor='center')
            self.empty_copy.configure(text='选区已就绪。设置左侧目标后，点击「测量并预览」。')
            self.preview_hint.configure(text='选区已更新，等待测量。')
            self.detail_var.set('选择结果行，查看完整对象名称和状态。')
            self.has_plan = False
            self.has_preview = False
            if mode == 'perlang':
                self.lbl_sel.configure(
                    text='%d 个 Sound / Music Track，%d 个 Bus，%d 个 media'
                         % (n_sound, len(buses), n_media))
            else:
                self.lbl_sel.configure(text='%d 个 Sound / Music Track，%d 个 Bus'
                                            % (n_sound, len(buses)))
            self._render_targets(buses)
            self._render_langs(langs)
            self._status('已读取选区，可测量')
            self.progress.configure(value=0)
        elif kind == 'plan_ready':
            n_plan, n_skip = item[1], item[2]
            self.has_plan = bool(n_plan)
            self.has_preview = bool(n_plan or n_skip)
            self._render_preview()
            self.tabs.select(self.settings_page)
            self._status('计划就绪：%d 条待写入，%d 条跳过' % (n_plan, n_skip))
            self.progress.configure(value=100)
        elif kind == 'applied':
            n = item[1]
            location = item[2] if len(item) > 2 else '对象属性'
            self._invalidate_preview('写入已完成。再次调整前，请重新读取选区并测量。')
            self.has_selection = False
            self.lbl_sel.configure(text='写入已完成，请重新读取选区')
            self._status('完成：%d 条 %s 已写入并回读验证。请 Ctrl+S 保存' % (n, location))
            if len(item) > 3 and item[3]:
                self._render_write_report(item[3])
            else:
                messagebox.showinfo(
                    '完成',
                    '已写入并回读验证 %d 条 %s。\n\n记得在 Wwise 中按 Ctrl+S 保存项目！' % (n, location))
        elif kind == 'write_report':
            self._render_write_report(item[1])
        elif kind == 'event_exported':
            self.event_file_var.set(item[1])
            self.last_write_report['xlsx_path'] = item[1]
            self._status('事件名单 Excel 已保存：' + item[1])
        elif kind == 'reverted':
            n = item[1]
            self._invalidate_preview('已撤回写入，请重新读取选区并测量。')
            self.has_selection = False
            self.lbl_sel.configure(text='撤回已完成，请重新读取选区')
            self._status('已撤回 %d 条。请在 Wwise 中 Ctrl+S 保存' % n)
            messagebox.showinfo(
                '撤回完成',
                '已恢复 %d 条到写入前的值。\n\n'
                '记得在 Wwise 中按 Ctrl+S 保存项目！' % n)
        elif kind == 'error':
            self._append_log('错误：' + item[1])
            if not (self.pipe and self.pipe.client):
                self.lbl_conn.configure(text='未连接 Wwise', foreground='#A13232')
            self._status('操作失败，请查看日志后重试')
            messagebox.showerror('错误', item[1])
        elif kind == 'reset_preview':
            self.root.after(150, lambda: self._confirm_reset(item[1], item[2]))
        elif kind == 'prob_preview':
            self.root.after(150, lambda: self._confirm_probability(item[1]))
        elif kind == 'idle':
            self.working = False
            self._set_buttons_working(False)
            self.progress.configure(value=0)
            self.lbl_progress.configure(text='')

    # ---------------- 目标 / 语言渲染 ----------------

    def _render_targets(self, buses):
        self._buses = list(buses)
        for w in self.frm_targets.winfo_children():
            w.destroy()
        self._target_entries = {}
        self._target_labels = []
        if not buses:
            ttk.Label(self.frm_targets, text='未发现 Bus', style='Hint.TLabel').pack(anchor='w')
            return
        for bus_name, count in buses:
            row = ttk.Frame(self.frm_targets)
            row.pack(fill='x', pady=(4, 8))
            row.columnconfigure(0, weight=1)
            label = ttk.Label(row, text=bus_name, wraplength=max(90, self.settings_canvas.winfo_width() - 156), anchor='w')
            label.grid(row=0, column=0, sticky='ew', padx=(0, 8))
            self._target_labels.append(label)
            ttk.Label(row, text='%d 条音频' % count, style='Hint.TLabel').grid(row=1, column=0, sticky='w')
            unit = 'LUFS-I' if self.metric_var.get() == 'integrated' else 'LUFS-M'
            ttk.Label(row, text=unit, style='Hint.TLabel').grid(row=0, column=2, rowspan=2, padx=(8, 0))
            default = '-13.0' if self.metric_var.get() == 'momentary' and 'ultra' in str(bus_name).lower() else '-24.0'
            var = tk.StringVar(value=self.bus_targets.get(bus_name, default))
            entry = ttk.Entry(row, textvariable=var, width=7, justify='right')
            entry.grid(row=0, column=1, rowspan=2)
            self._ui_inputs.append(entry)
            self._target_entries[bus_name] = var
            var.trace_add('write', self._settings_changed)

    def _render_langs(self, langs):
        for w in self.frm_langs.winfo_children():
            w.destroy()
        self.lang_vars = {}
        self._lang_buttons = []
        if not langs and self.mode_var.get() == 'object':
            self.language_group.pack_forget()
        else:
            self.language_group.pack(fill='x', before=self.target_separator)
        if not langs:
            ttk.Label(self.frm_langs, text='共用增益模式不区分语言' if self.mode_var.get() == 'object' else '没有可用的语言源',
                      style='Hint.TLabel').pack(anchor='w')
            return
        for i, (lg, cnt) in enumerate(langs):
            var = tk.BooleanVar(value=True)
            self.lang_vars[lg] = var
            cb = ttk.Checkbutton(self.frm_langs, text='%s (%d)' % (lg, cnt), variable=var)
            cb.grid(row=i // 2, column=i % 2, sticky='w', padx=(0, 12))
            self._lang_buttons.append(cb)
            self._ui_inputs.append(cb)
            var.trace_add('write', self._settings_changed)
        self._lang_note = ttk.Label(self.frm_langs, text='全不勾选时，同样处理全部语言。', style='Hint.TLabel')
        self._lang_note.grid(row=len(langs), column=0, sticky='w', pady=(6, 0))
        self._layout_languages(max(240, self.settings_canvas.winfo_width() - 24))

    # ---------------- 按钮状态 ----------------

    def _set_buttons_working(self, on):
        """按当前真实状态刷新按钮可用性。

        修复：原版收尾时把所有按钮一律置 normal，导致未连接就能点「测量」、
        没有计划就能点「应用」。现在每步都根据 has_selection / has_plan /
        连接状态 / 快照存在性 精确控制。
        """
        self.working = on
        connected = bool(self.pipe and self.pipe.client)
        self._ui_inputs = [w for w in self._ui_inputs if w.winfo_exists()]
        for widget in self._ui_inputs:
            widget.configure(state='disabled' if on else 'readonly' if isinstance(widget, ttk.Combobox) else 'normal')
        if not on and self.metric_var.get() == 'integrated':
            self.pad_chk.configure(state='disabled')
        self.btn_advanced.configure(state='disabled' if on else 'normal')
        self.btn_update.configure(state='disabled' if on else 'normal')
        self._update_controls()
        self.btn_measure.configure(style='TButton' if self.has_plan else 'Primary.TButton')
        self.btn_apply.configure(style='Primary.TButton' if self.has_plan else 'TButton')
        self.prob_chk.configure(state='disabled' if on else 'normal')
        self.prob_entry.configure(state='normal' if not on and self.prob_enabled.get() else 'disabled')
        has_receipt = bool(self.last_write_report)
        self.btn_event_export.configure(state='normal' if has_receipt and not on else 'disabled')
        excel = (self.last_write_report or {}).get('xlsx_path')
        self.btn_event_open.configure(state='normal' if excel and os.path.isfile(excel) and not on else 'disabled')
        self.btn_event_copy.configure(state='normal' if self.event_tree.get_children() and not on else 'disabled')
        self.reset_hint.configure(text='正在处理，请稍候。' if on else
            '在 Wwise 中选好对象，再预览需要归零的属性。' if connected else '请先连接 Wwise，再选择需要归零的对象。')
        self.prob_hint.configure(text='正在处理，请稍候。' if on else
            '请先连接 Wwise，并在响度页读取选区。' if not connected else
            '请在响度页读取选区。' if not self.has_selection else
            '勾选启用后，输入 Probability 并查看写入预览。' if not self.prob_enabled.get() else
            '先核对匹配的 Play Action，再确认写入。')
        if on:
            for b in (self.btn_connect, self.btn_read, self.btn_measure,
                      self.btn_apply, self.btn_revert, self.btn_report, self.btn_prob, self.btn_reset):
                b.configure(state='disabled')
            return

        self.btn_reset.configure(state='normal' if connected else 'disabled')
        self.btn_connect.configure(state='normal')
        self.btn_read.configure(state='normal' if connected else 'disabled')
        self.btn_measure.configure(
            state='normal' if (connected and self.has_selection) else 'disabled')
        self.btn_prob.configure(state='normal' if connected and self.has_selection and self.prob_enabled.get() else 'disabled')
        plan_ok = connected and self.has_plan
        self.btn_apply.configure(state='normal' if plan_ok else 'disabled')
        self.btn_report.configure(state='normal' if connected and (self.has_preview or self.has_plan) else 'disabled')

        if connected and self.pipe.has_undo():
            info = self.pipe.undo_info() or {}
            self.btn_revert.configure(state='normal', text='撤回上次写入')
            self.undo_summary.configure(text='%s 条 · %s' % (info.get('count', '?'), info.get('time', '?')))
        else:
            self.btn_revert.configure(state='disabled', text='撤回上次写入')
            self.undo_summary.configure(text='')

    def _collect_targets(self):
        targets = {}
        for bus_name, var in self._target_entries.items():
            s = var.get().strip()
            try:
                targets[bus_name] = float(s)
                if not math.isfinite(targets[bus_name]):
                    raise ValueError()
            except ValueError:
                raise WaapiError("Bus '%s' 的目标值不是数字：%s" % (bus_name, s))
        self.bus_targets = targets
        self._bus_targets_by_metric[self.metric_var.get()].update(targets)
        return targets

    def _checked_langs(self):
        picked = [lg for lg, v in self.lang_vars.items() if v.get()]
        if not picked or len(picked) == len(self.lang_vars):
            return None      # 全选或全不选 => 不过滤
        return picked

    # ---------------- 操作 ----------------

    def on_connect(self):
        if self.working:
            return
        host = self.host_var.get().strip() or '127.0.0.1'
        try:
            port = int(self.port_var.get().strip())
            workers = int(self.workers_var.get() or 6)
            if not 1 <= port <= 65535 or not 1 <= workers <= 64:
                raise ValueError()
        except ValueError:
            messagebox.showerror('连接设置', '端口须为 1–65535，测量线程须为 1–64 的整数。')
            return
        self._invalidate_preview('连接已变更，请重新读取选区并测量。')
        self.has_selection = False
        self.lbl_sel.configure(text='尚未读取选区')
        self.lbl_conn.configure(text='正在连接…', foreground=self.colors['muted'])
        previous = self.pipe
        self.pipe = None
        self._set_buttons_working(True)
        self._status('连接中...')

        def worker():
            try:
                if previous and previous.client:
                    try:
                        previous.client.disconnect()
                    except Exception:
                        pass
                self.pipe = Pipeline(host=host, port=port,
                                     workers=workers,
                                     log=self.log, progress=self.set_progress)
                ver = self.pipe.connect()
                self.q.put(('connected', ver))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))

        threading.Thread(target=worker, daemon=True).start()

    def on_read(self):
        if self.working or not self.pipe:
            return
        self._invalidate_preview('正在读取选区，原预览已失效。')
        self.has_selection = False
        self._set_buttons_working(True)
        self._status('读取选区...')
        mode = 'object'
        music_level = self.music_level_var.get()

        def worker():
            try:
                self.pipe.mode = mode
                self.pipe.music_write_level = music_level
                sounds = self.pipe.read_selection()
                if mode == 'perlang':
                    medias = self.pipe.collect_medias()
                    from collections import Counter
                    bus_cnt = Counter(m['bus_name'] for m in medias)
                    lang_cnt = Counter(m['lang'] for m in medias)
                    self.q.put(('selection', len(sounds),
                                list(bus_cnt.most_common()),
                                list(lang_cnt.most_common()),
                                len(medias), mode))
                    self.log('读取完成：%d 个 Sound / Music Track，%d 个 Bus，%d 个 media'
                             % (len(sounds), len(bus_cnt), len(medias)))
                else:
                    self.pipe.collect_routing()
                    from collections import Counter
                    bus_cnt = Counter(r['bus_name'] for r in self.pipe.rows)
                    self.q.put(('selection', len(self.pipe.rows),
                                list(bus_cnt.most_common()), [], 0, mode))
                    self.log('读取完成：%d 个 Sound / Music Track，%d 个 Bus'
                             % (len(self.pipe.rows), len(bus_cnt)))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))

        threading.Thread(target=worker, daemon=True).start()

    def on_measure(self):
        if self.working or not self.pipe:
            return
        try:
            targets = self._collect_targets()
        except WaapiError as exc:
            messagebox.showerror('错误', str(exc))
            return
        self._invalidate_preview('正在重新测量，请等待新结果。')
        self._set_buttons_working(True)
        self._status('测量响度中...')
        mode = 'object'
        music_level = self.music_level_var.get()
        pad = self.pad_var.get()
        guard = self.guard_var.get()
        metric = self.metric_var.get()
        reduce_only = self.reduce_var.get()
        languages = self._checked_langs()

        def worker():
            try:
                self.pipe.mode = mode
                self.pipe.music_write_level = music_level
                if mode == 'perlang':
                    self.pipe.collect_medias(languages=languages)
                self.pipe.measure(pad_short=pad, metric=metric)
                self.pipe.make_plan(targets, peak_guard=guard, reduce_only=reduce_only)

                from collections import defaultdict
                by_bus = defaultdict(list)
                if mode == 'perlang':
                    for p in self.pipe.plan:
                        by_bus[p['bus_name']].append(p['new_volume_offset'])
                    for bus, vals in by_bus.items():
                        self.log('%s: %d 条，VolumeOffset %.1f~%.1f dB'
                                 % (bus, len(vals), min(vals), max(vals)))
                else:
                    for p in self.pipe.plan:
                        by_bus[p['bus_name']].append(p['new_makeup_gain'])
                    for bus, vals in by_bus.items():
                        self.log('%s: %d 条，MakeUpGain %.1f~%.1f dB'
                                 % (bus, len(vals), min(vals), max(vals)))
                if self.pipe.skipped:
                    self.log('  跳过 %d 条' % len(self.pipe.skipped))
                    for n, why in self.pipe.skipped:
                        self.log('    %s: %s' % (n, why))

                for p in self.pipe.plan:
                    if p.get('scope_note'):
                        self.log('%s：%s' % (p['name'], p['scope_note']))
                    if p.get('note'):
                        self.log('%s [%s]：%s' % (p.get('sound_name', p.get('name')), p.get('lang', 'shared'), p['note']))
                    self.log('%s [%s] %s 源=%s 固定链=%s 当前Output=%s 目标=%s 新增益=%s 预计Output=%s' % (p.get('sound_name', p.get('name')), p.get('lang', 'shared'), pipeline.LOUDNESS_METRICS[metric], p['src_lufs'], p.get('fixed_chain', ''), p['current_output_lufs'], p['target'], p.get('new_volume_offset', p.get('new_makeup_gain')), p['output_lufs']))
                self.q.put(('plan_ready', len(self.pipe.plan),
                            len(self.pipe.skipped)))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))

        threading.Thread(target=worker, daemon=True).start()

    def on_apply(self):
        if self.working or not self.pipe or not self.has_plan or not self.pipe.plan:
            return
        n = len(self.pipe.plan)
        prop = 'Sound / %s 的 Make-Up Gain' % pipeline.MUSIC_WRITE_LEVELS[self.pipe.music_write_level]
        if not messagebox.askyesno(
                '确认写入',
                '将向 Wwise 写入 %d 条 %s。\n\n'
                '此操作会修改当前项目（可在 Wwise 中撤销）。\n'
                '确认继续？' % (n, prop)):
            return
        self._set_buttons_working(True)
        self._status('写入中...')

        def worker():
            previous_report = self.pipe.last_write_report
            try:
                ok, mismatch = self.pipe.apply()
                for m in mismatch[:20]:
                    self.log('  不匹配 %s: actual=%s expected=%s' % m)
                self.q.put(('applied', ok, prop, self.pipe.last_write_report))
            except Exception as exc:
                if self.pipe.last_write_report is not previous_report:
                    self.q.put(('write_report', self.pipe.last_write_report))
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))

        threading.Thread(target=worker, daemon=True).start()

    def on_reset(self):
        if self.working or not self.pipe:
            return
        props = []
        if self.reset_makeup.get(): props.append('MakeUpGain')
        if self.reset_volume.get(): props.append('Volume')
        self._set_buttons_working(True)
        def worker():
            try:
                entries, skipped = self.pipe.reset_gain_plan(props)
                self.q.put(('reset_preview', entries, skipped))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))
        threading.Thread(target=worker,daemon=True).start()

    def _confirm_reset(self, entries, skipped):
        lines = ['%s · %s: %s → 0' % (e['name'], 'Voice Volume' if e['prop']=='Volume' else 'Make-Up Gain', e['old']) for e in entries]
        lines += ['跳过 %s：%s' % pair for pair in skipped]
        self.log('\n'.join(lines))
        if not entries:
            messagebox.showinfo('无需归零','选区没有需要修改的增益。\n' + '\n'.join(lines))
            return
        if not messagebox.askyesno('归零预览', '\n'.join(lines) + '\n\n确认将以上属性归零？'):
            return
        self._set_buttons_working(True)
        def worker():
            previous_report = self.pipe.last_write_report
            try:
                ok, _ = self.pipe._write_verified(entries)
                self.q.put(('applied', ok, '对象增益（归零）', self.pipe.last_write_report))
            except Exception as exc:
                if self.pipe.last_write_report is not previous_report:
                    self.q.put(('write_report', self.pipe.last_write_report))
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))
        threading.Thread(target=worker,daemon=True).start()

    def on_probability(self):
        if self.working or not self.pipe or not self.prob_enabled.get():
            return
        try:
            value = float(self.prob_var.get())
        except ValueError:
            messagebox.showerror('错误', 'Probability 必须是数字')
            return
        self._set_buttons_working(True)
        def worker():
            try:
                entries = self.pipe.probability_plan(value)
                self.q.put(('prob_preview', entries))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))
        threading.Thread(target=worker, daemon=True).start()

    def _confirm_probability(self, entries):
        if not self.prob_enabled.get():
            return
        preview = '\n'.join('%s: %s → %s' % (e['name'], e['old'], e['new']) for e in entries)
        self.log(preview)
        if not messagebox.askyesno('Probability 预览', preview + '\n\n确认写入这些 Play Action？'):
            return
        self._set_buttons_working(True)
        def worker():
            previous_report = self.pipe.last_write_report
            try:
                ok, _ = self.pipe._write_verified(entries, 'Probability')
                self.q.put(('applied', ok, 'Play Action 的 Probability', self.pipe.last_write_report))
            except Exception as exc:
                if self.pipe.last_write_report is not previous_report:
                    self.q.put(('write_report', self.pipe.last_write_report))
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))
        threading.Thread(target=worker, daemon=True).start()

    def on_revert(self):
        if self.working or not self.pipe:
            return
        if not self.pipe.has_undo():
            messagebox.showinfo('无可撤回', '没有可撤回的快照（尚未在本机执行过写入）')
            return
        info = self.pipe.undo_info() or {}
        prop = info.get('prop', 'MakeUpGain')
        if not messagebox.askyesno(
                '确认撤回',
                '将把 %s 条记录的 %s\n恢复到 %s 写入前的值。\n\n'
                '（撤回后快照会被清除，不可二次撤回）\n确认继续？'
                % (info.get('count', '?'), prop, info.get('time', '?'))):
            return
        self._set_buttons_working(True)
        self._status('撤回中...')

        def worker():
            try:
                ok = self.pipe.revert()
                self.q.put(('reverted', ok))
            except Exception as exc:
                self.q.put(('error', str(exc)))
            finally:
                self.q.put(('idle',))

        threading.Thread(target=worker, daemon=True).start()

    def on_report(self):
        if not self.pipe or not (self.has_plan or self.has_preview):
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.json', initialfile='loudness_report.json',
            filetypes=[('JSON', '*.json'), ('All', '*.*')])
        if not path:
            return
        try:
            self.pipe.save_report(path)
            self._status('报告已保存：' + path)
            messagebox.showinfo('已导出', '报告已保存到：\n' + path)
        except Exception as exc:
            messagebox.showerror('错误', str(exc))

    def _render_write_report(self, report, select=True):
        self.last_write_report = report
        self._event_rows = []
        if report:
            events = report.get('events', [])
            for index, event in enumerate(events):
                names = list(dict.fromkeys(e['name'] for e in event['changes']))
                values = (event['name'], event['path'], event['object_count'], '；'.join(names))
                self._event_rows.append((str(index), values, ' '.join(map(str, values)).casefold()))
            summary = '%s · %s · %d 个已确认改动对象 · %d 个 Event。请在 Wwise 中 Ctrl+S 保存。' % (
                report.get('time', ''), event_report.STATUS_LABELS.get(report.get('status'), '写入记录'),
                len({entry['id'] for entry in report.get('changed_entries', [])}), len(events))
            if not report.get('lookup_complete'):
                summary += '\nEvent 查询不完整，请查看 Excel 和操作日志。'
            elif not events:
                summary += '\n本次未找到关联 Play Event，实际改动对象仍列在 Excel「修改明细」。'
            if report.get('warnings'):
                summary += '\n' + '；'.join(report['warnings'])[:240]
            self.event_summary.configure(text=summary)
            self.event_file_var.set(report.get('xlsx_path') or 'Excel 自动保存失败，请点击「另存 Excel」。')
            self.event_detail_var.set('此名单记录本次写入时的改动。选择一行查看完整 Event 名称与路径。')
            if select:
                self.tabs.select(self.write_events_page)
        if self.event_filter_var.get():
            self.event_filter_var.set('')
        else:
            self._filter_events()
        self._set_buttons_working(self.working)

    def _filter_events(self):
        query = self.event_filter_var.get().strip().casefold()
        rows = getattr(self, '_event_rows', [])
        self.event_tree.delete(*self.event_tree.get_children())
        shown = 0
        for iid, values, search_text in rows:
            if query and query not in search_text:
                continue
            self.event_tree.insert('', 'end', iid=iid, values=values,
                                   tags=('even',) if shown % 2 == 0 else ())
            shown += 1
        self.event_filter_count.configure(text='%d / %d 个 Event' % (shown, len(rows)) if query else '%d 个 Event' % len(rows))
        if shown:
            self.event_empty.place_forget()
        else:
            self.event_empty.configure(text='没有匹配的 Event，请修改查找内容。' if query else
                '本次没有关联 Play Event，实际改动见 Excel「修改明细」。' if self.last_write_report else
                '写入并回读后，受影响的 Event 会显示在这里。')
            self.event_empty.place(relx=.5, rely=.5, anchor='center')
        self.btn_event_copy.configure(state='normal' if shown and not self.working else 'disabled')
        self.event_detail_var.set('按 Event 名称、路径或改动对象查找。选择一行可复制完整内容。')

    def _event_selection(self, _event=None):
        selected = self.event_tree.selection()
        if selected:
            values = self.event_tree.item(selected[0], 'values')
            self.event_detail_var.set('%s · %s · %s' % (values[0], values[1], values[3]))

    def on_event_copy(self):
        events = (self.last_write_report or {}).get('events', [])
        names = [events[int(iid)]['name'] for iid in self.event_tree.get_children()]
        if names:
            self.root.clipboard_clear()
            self.root.clipboard_append('\n'.join(names))
            self._status('已复制 %d 个 Event 名称' % len(names))

    def on_event_open(self):
        path = (self.last_write_report or {}).get('xlsx_path')
        if path and os.path.isfile(path):
            try:
                os.startfile(path)
            except OSError as exc:
                messagebox.showerror('无法打开 Excel', str(exc))

    def on_event_export(self):
        if self.working or not self.last_write_report:
            return
        path = filedialog.asksaveasfilename(defaultextension='.xlsx',
            initialfile='Wwise受影响事件.xlsx', filetypes=[('Excel 工作簿', '*.xlsx')])
        if not path:
            return
        report = self.last_write_report
        self._set_buttons_working(True)
        def worker():
            try:
                event_report.export_xlsx(report, path)
                self.q.put(('event_exported', path))
            except Exception as exc:
                self.q.put(('error', 'Excel 导出失败：' + str(exc)))
            finally:
                self.q.put(('idle',))
        threading.Thread(target=worker, daemon=True).start()


def main():
    root = tk.Tk()
    health_path = None
    if len(sys.argv) == 3 and sys.argv[1] == '--update-health-check':
        health_path = sys.argv[2]
        root.withdraw()
    try:
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except Exception:
        pass
    App(root, check_updates=not health_path)
    if health_path:
        root.update_idletasks()
        ffmpeg = find_ffmpeg()
        if not ffmpeg or not os.path.isfile(ffmpeg):
            raise RuntimeError('新版缺少测量引擎。')
        updater.write_json(health_path, {'version': updater.build_info()['version'], 'ok': True})
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        _fatal_error(traceback.format_exc())
