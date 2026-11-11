#!/usr/bin/env python3
"""Build reproducible, offline 320px widget previews and full-document size evidence.

Run from a checkout; no installed/deployed plugin or Telegram credentials are used.
Output (HTML, extracted JS and measurements) goes only to the chosen artifact dir.
A browser can open the HTML files or use their contents with page.set_content().
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location('widget_preview_plugin', ROOT / '__init__.py',
                                                 submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def fixtures():
    p = load_plugin()
    result = {}
    for name in ('weather', 'tracker', 'compare', 'approve', 'chart'):
        meta, body = p.wg.load_template([ROOT / 'templates'], name)
        data = copy.deepcopy(meta['example'])
        actions = p.wg.actions_from_data(data, meta.get('actions_from', ''))
        if name == 'chart':
            for kind in ('line', 'bar'):
                chart = dict(data, type=kind)
                doc, warnings = p.charts.build_chart(chart, [])
                result['chart-' + kind] = {'html': doc, 'data': {}, 'update': chart, 'warnings': warnings}
        else:
            result[name] = {'html': p.wg.build_html(body, actions), 'data': data, 'warnings': []}
    rng = random.Random(712)
    maximum = {'type': 'line', 'title': 'Example: 24 points × 3 series', 'unit': 'kW',
               'x': [f'{i:02}:00' for i in range(24)],
               'series': [{'name': f'Series {j+1}', 'values': [rng.uniform(-99, 99) for _ in range(24)]}
                          for j in range(3)], 'highlight': 13}
    actions = p.wg.normalize_actions([{'key': 'details', 'label': 'Details'}, {'key': 'later', 'label': 'Later'}])
    for kind in ('line', 'bar'):
        doc, warnings = p.charts.build_chart(dict(maximum, type=kind), actions)
        result['chart-max-' + kind] = {'html': doc, 'data': {}, 'warnings': warnings}
    return p, result


THEMES = {
    'light': {'bg-color': '#ffffff', 'text-color': '#202b36', 'hint-color': '#687887',
              'secondary-bg-color': '#eef1f4', 'button-color': '#2481cc', 'button-text-color': '#ffffff',
              'accent-text-color': '#2481cc', 'destructive-text-color': '#c94b4b'},
    'dark': {'bg-color': '#17212b', 'text-color': '#f5f5f5', 'hint-color': '#a7b4c0',
             'secondary-bg-color': '#253442', 'button-color': '#68b5ef', 'button-text-color': '#17212b',
             'accent-text-color': '#68b5ef', 'destructive-text-color': '#f28694'},
}


def preview_html(doc, data, theme):
    state = json.dumps(json.dumps(data, ensure_ascii=False, separators=(',', ':')), ensure_ascii=False).replace('<', '\\u003c')
    css = ';'.join('--tg-theme-' + k + ':' + v for k, v in THEMES[theme].items())
    # Test harness only: no actual API calls, no external fonts/libraries/network.
    mock = ('<script>window.__errors=[];window.onerror=function(m){window.__errors.push(String(m))};'
            'window.__events={};window.__requests=[];window.TelegramWidget={chatData:' + state + ','
            'onEvent:function(n,f){(window.__events[n]||(window.__events[n]=[])).push(f)},ready:function(){},'
            'request:function(s){window.__requests.push(s);return Promise.resolve({result:\'{"ok":1,"again":1}\'})}};'
            'window.__update=function(s){window.TelegramWidget.chatData=JSON.stringify(s);'
            '(window.__events.chatDataChanged||[]).forEach(function(f){f()})};</script>')
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Offline widget preview</title><style>:root{' + css + '}body{font:14px system-ui;margin:0;width:320px}'
            'button{padding:8px;border:0;border-radius:7px;background:var(--tg-theme-button-color);'
            'color:var(--tg-theme-button-text-color);font:inherit}button:disabled{opacity:.55}'
            '*{box-sizing:border-box;animation:none!important;transition:none!important}</style></head><body>' + mock + doc + '</body></html>')


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    p, cases = fixtures()
    measurements, previews = {}, []
    checked = 0
    for name, case in cases.items():
        raw, packed = p.wg.check_size(case['html'])
        measurements[name] = {'html_bytes': raw, 'compressed_bytes': packed,
                              'chat_data_bytes': len(p.wg.encode_data(case['data']).encode()), 'warnings': case['warnings']}
        for i, script in enumerate(re.findall(r'<script>(.*?)</script>', case['html'], re.S)):
            file = directory / f'{name}-{i}.js'
            file.write_text(script, encoding='utf-8')
            if shutil.which('node'):
                subprocess.run(['node', '--check', str(file)], check=True, capture_output=True, text=True)
                checked += 1
        for theme in THEMES:
            file = directory / f'{name}-{theme}.html'
            file.write_text(preview_html(case['html'], case['data'], theme), encoding='utf-8')
            previews.append(str(file))
    result = {'measurements': measurements, 'node_scripts_checked': checked, 'previews': previews,
              'note': 'Local browser fixtures only; real Telegram rendering is NOT verified.'}
    (directory / 'measurements.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False, indent=2))
