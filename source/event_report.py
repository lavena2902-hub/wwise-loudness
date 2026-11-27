# -*- coding: utf-8 -*-
"""Read-only Event impact discovery and the application's Excel write receipt."""
import json
import math
import os
import re
from collections import defaultdict


AUDIO_HIERARCHY = {'Sound', 'ActorMixer', 'RandomSequenceContainer',
                   'SwitchContainer', 'BlendContainer', 'MusicTrack',
                   'MusicSegment', 'MusicPlaylistContainer', 'MusicSwitchContainer'}
STATUS_LABELS = {'verified': '写入已回读验证', 'mismatch': '部分属性回读不匹配',
                 'indeterminate': '写入结果未确认'}


def load_report(path):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        if isinstance(value, dict) and value.get('schema_version') == 1:
            return value
    except (OSError, ValueError):
        pass
    return None


def save_report_json(report, path):
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def collect_events(call, report):
    """Associate actual changed objects with Events via Play Action targets.

    An ancestor gain also affects descendant targets. Conversely an Event that
    plays an ancestor can play a changed descendant. Sibling targets are excluded.
    Only read requests are made; failure leaves an explicitly incomplete receipt.
    """
    entries = report['changed_entries']
    changed_ids = list(dict.fromkeys(e['id'] for e in entries))
    if not changed_ids:
        return
    fields = ['id', 'name', 'type', 'path', 'parent']

    def query(ids, selection=None, extra=()):
        result = {}
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            args = {'from': {'id': batch}, 'options': {'return': fields + list(extra)}}
            if selection:
                args['transform'] = [{'select': [selection]}]
            response = call('ak.wwise.core.object.get', args)
            if not isinstance(response, dict) or not isinstance(response.get('return'), list):
                raise ValueError('Event 关联查询未返回完整结果')
            for obj in response['return']:
                result[obj['id']] = obj
            if not selection and set(batch) - set(result):
                raise ValueError('部分改动对象或 Event 未能读取')
        return result

    own = query(changed_ids)
    for entry in entries:
        obj = own[entry['id']]
        entry.update(name=obj.get('name') or entry['name'],
                     type=obj.get('type', ''), path=obj.get('path', ''))
    audio_ids = [oid for oid, obj in own.items() if obj.get('type') in AUDIO_HIERARCHY]
    graph = dict(own)
    if audio_ids:
        graph.update(query(audio_ids, 'ancestors'))
        graph.update(query(audio_ids, 'descendants'))

    def chain(oid):
        seen = set()
        while oid and oid not in seen:
            seen.add(oid)
            obj = graph.get(oid)
            if obj is None:
                raise ValueError('对象父级关系读取不完整')
            yield oid
            parent = obj.get('parent')
            oid = parent.get('id') if isinstance(parent, dict) else None

    scope = defaultdict(set)
    changed_audio = set(audio_ids)
    for oid, obj in graph.items():
        if obj.get('type') in AUDIO_HIERARCHY:
            scope[oid].update(changed_audio.intersection(chain(oid)))
    for oid in audio_ids:
        for ancestor in chain(oid):
            if graph[ancestor].get('type') in AUDIO_HIERARCHY:
                scope[ancestor].add(oid)
    scope = {oid: owners for oid, owners in scope.items() if owners}
    references = query(scope, 'referencesTo') if scope else {}
    action_ids = [oid for oid, obj in references.items() if obj.get('type') == 'Action']
    action_ids.extend(oid for oid, obj in own.items() if obj.get('type') == 'Action')
    actions = query(action_ids, extra=('target', 'ActionType')) if action_ids else {}
    event_links = defaultdict(lambda: defaultdict(dict))
    for aid, action in actions.items():
        target = (action.get('target') or {}).get('id')
        affected = set(scope.get(target, ())) if action.get('ActionType') == 1 else set()
        if aid in own and own[aid].get('type') == 'Action':
            affected.add(aid)
        if not affected:
            continue
        event_id = (action.get('parent') or {}).get('id')
        if not event_id:
            raise ValueError('Play Action 缺少所属 Event')
        for oid in affected:
            event_links[event_id][oid][aid] = action.get('path') or action.get('name') or aid
    event_objects = query(event_links) if event_links else {}
    events, associated = [], set()
    for eid, links in event_links.items():
        obj = event_objects[eid]
        if obj.get('type') != 'Event':
            raise ValueError('Play Action 所属对象不是 Event')
        affected = []
        for oid in sorted(links):
            associated.add(oid)
            affected.extend(dict(entry, action_paths=sorted(links[oid].values()))
                            for entry in entries if entry['id'] == oid)
        events.append(dict(id=eid, name=obj.get('name', eid), path=obj.get('path', ''),
                           object_count=len(links), changes=affected))
    report['events'] = sorted(events, key=lambda event: (event['name'].casefold(), event['path'], event['id']))
    report['unmatched_entries'] = [entry for entry in entries if entry['id'] not in associated]
    unsupported = [obj.get('name') or oid for oid, obj in own.items()
                   if obj.get('type') not in AUDIO_HIERARCHY | {'Action'}]
    if unsupported:
        report['lookup_complete'] = False
        report['warnings'].append('以下对象的 Event 影响范围未展开：' + '、'.join(unsupported))


def export_xlsx(report, path):
    """Export typed values, frozen/filterable tables, and literal Wwise names."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    events_sheet = workbook.active
    events_sheet.title = '事件名单'
    details_sheet = workbook.create_sheet('修改明细')
    status = STATUS_LABELS.get(report.get('status'), '未知写入状态')
    events = report.get('events', [])
    changes = report.get('changed_entries', [])
    info = '%s    %s    %d 个已确认改动对象    %d 个关联 Event' % (
        report.get('time', ''), status, len({e['id'] for e in changes}), len(events))
    warning = '；'.join(report.get('warnings', []))
    if not report.get('lookup_complete', False):
        warning = 'Event 查询不完整。' + warning
    if report.get('status') == 'indeterminate':
        warning = '未能回读，实际改动数量未知。' + warning

    def text(value):
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
        return value if len(value) <= 32767 else value[:32735] + '…完整记录见 JSON 写入记录'

    def append(sheet, values):
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.value = text(cell.value)
                cell.data_type = 's'  # Event names such as '=Music' remain literal text.

    def setup(sheet, title, headings, widths):
        for values in [[title], [info], [warning or '来源：本次属性写入后的独立回读与 Wwise Play Action 关联查询。'],
                       ['事件名单表示播放会受这些对象改动影响的 Event。'], [], headings]:
            append(sheet, values)
        for row in range(1, 5):
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(headings))
        sheet['A1'].font = Font(name='Microsoft YaHei', size=16, bold=True, color='202B3B')
        sheet['A2'].font = Font(name='Microsoft YaHei', size=10, color='596779')
        sheet['A3'].font = Font(name='Microsoft YaHei', size=10, color='A13232' if warning else '596779')
        sheet['A4'].font = Font(name='Microsoft YaHei', size=10, color='596779')
        for row, height in [(1, 30), (2, 26), (3, 40 if warning else 25), (4, 25), (6, 28)]:
            sheet.row_dimensions[row].height = height
        for row in (2, 3, 4):
            sheet.cell(row, 1).alignment = Alignment(wrap_text=True, vertical='center')
        for cell in sheet[6]:
            cell.fill = PatternFill('solid', fgColor='245EC7')
            cell.font = Font(name='Microsoft YaHei', size=10, bold=True, color='FFFFFF')
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        for i, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        sheet.freeze_panes = 'A7'
        sheet.sheet_view.showGridLines = False

    setup(events_sheet, 'Wwise 写入后的受影响事件',
          ['序号', 'Event 名称', 'Event 路径', '改动对象数', '改动对象', 'Event ID'], [8, 38, 66, 14, 48, 40])
    for index, event in enumerate(events, 1):
        names = list(dict.fromkeys(e['name'] for e in event['changes']))
        append(events_sheet, [index, event['name'], event['path'], event['object_count'], '\n'.join(names), event['id']])
    if not events:
        append(events_sheet, [None, 'Event 查询不完整' if not report.get('lookup_complete') else '本次未找到关联 Play Event'])
    setup(details_sheet, 'Wwise 实际改动明细',
          ['Event 名称', 'Event 路径', '写入对象', '对象类型', '属性与单位', '写入前', '回读后', '变化量', '验证结果', '对象路径'],
          [38, 60, 40, 26, 25, 14, 14, 14, 22, 66])
    detail_rows = [(event, entry) for event in events for entry in event['changes']]
    mapped = {entry['id'] for event in events for entry in event['changes']}
    detail_rows.extend((None, entry) for entry in changes if entry['id'] not in mapped)
    if len(detail_rows) > 1048569 or len(events) > 1048569:
        raise ValueError('明细超过 Excel 工作表行数上限，请查看 JSON 写入记录')
    for event, entry in detail_rows:
        prop = {'MakeUpGain': 'Make-Up Gain (dB)', 'Volume': 'Voice Volume (dB)',
                'Probability': 'Probability (%)'}.get(entry['prop'], entry['prop'])
        old, new = entry['old'], entry['new']
        append(details_sheet, [event['name'] if event else '未找到关联 Event' if report.get('lookup_complete') else '关联未确认',
                              event['path'] if event else '', entry['name'], entry.get('type', ''), prop,
                              old, new, new - old, '回读一致' if entry.get('verified') else '与计划不匹配', entry.get('path', '')])
    for sheet in workbook:
        sheet.auto_filter.ref = 'A6:%s%d' % (get_column_letter(sheet.max_column), sheet.max_row)
        for row in sheet.iter_rows(min_row=7):
            lines = 1
            for cell in row:
                cell.font = Font(name='Microsoft YaHei', size=10, color='202B3B')
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if cell.row % 2 == 0:
                    cell.fill = PatternFill('solid', fgColor='F3F5F8')
                if isinstance(cell.value, (int, float)):
                    cell.number_format = '0.000' if sheet is details_sheet and cell.column in (6, 7, 8) else '#,##0'
                if isinstance(cell.value, str):
                    width = sheet.column_dimensions[cell.column_letter].width
                    lines = max(lines, sum(max(1, math.ceil(len(line) * 1.5 / width)) for line in cell.value.split('\n')))
            sheet.row_dimensions[row[0].row].height = min(409, max(30, lines * 16))
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = 'landscape'
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = '1:6'
    temporary = path + '.tmp.xlsx'
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    finally:
        workbook.close()
        if os.path.exists(temporary):
            os.remove(temporary)
    return path
