"""Contract, chart and transport regressions; all API calls are local fakes."""
import copy
import json
import math
import random
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import test_widget as base
from test_widget import ROOT, load_plugin


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = load_plugin()
        cls.w = cls.p.wg

    def test_all_builtins_declare_and_enforce_schema(self):
        for template in self.w.list_templates([ROOT / 'templates']):
            with self.subTest(template=template['name']):
                self.assertIn('schema', template)
                meta, body = self.w.load_template([ROOT / 'templates'], template['name'])
                self.w.validate_template_data(meta, template['example'])
                for bad in ({}, dict(template['example'], surprise=1)):
                    with self.assertRaises(self.w.WidgetError):
                        self.w.validate_template_data(meta, bad)
                self.assertIn('chatDataChanged', self.w.build_html(body, []))
                self.assertIn('HTE', body)

    def test_precise_field_errors(self):
        invalid = {
            'weather': [('t', '25'), ('c', True), ('city', 'x' * 65), ('h', [['no', '<img>', 2]])],
            'tracker': [('s', ['x'] * 9), ('i', 1.5), ('i', 99)],
            'compare': [('o', [{'k': 'bad key', 'n': 'N'}]), ('o', []), ('note', 123)],
            'approve': [('d', [['only one']]), ('confirm', 12), ('risk', 'x' * 241)],
        }
        for name, cases in invalid.items():
            meta, _ = self.w.load_template([ROOT / 'templates'], name)
            for key, value in cases:
                with self.subTest(name=name, field=key, value=value):
                    with self.assertRaisesRegex(self.w.WidgetError, 'data\\.' + key):
                        self.w.validate_template_data(meta, dict(meta['example'], **{key: value}))
        with self.assertRaises(self.w.WidgetError):
            self.w.encode_data({'n': float('nan')})

    @unittest.skipUnless(shutil.which('node'), 'node not installed')
    def test_javascript_updates_escape_and_rebind(self):
        documents = {}
        for t in self.w.list_templates([ROOT / 'templates']):
            if t['name'] == 'chart':
                continue
            meta, body = self.w.load_template([ROOT / 'templates'], t['name'])
            acts = self.w.actions_from_data(t['example'], meta.get('actions_from', ''))
            documents[t['name']] = {'html': self.w.build_html(body, acts), 'data': t['example']}
        result = subprocess.run(['node', str(ROOT / 'tests/widget_runtime.cjs')],
                                input=json.dumps(documents), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('runtime regressions passed', result.stdout)


class Charts(unittest.TestCase):
    def setUp(self):
        self.p = load_plugin()
        self.assertTrue(hasattr(self.p, 'charts'), 'chart module must be integrated')
        self.c = self.p.charts
        self.spec = {'type': 'line', 'title': 'Demo', 'x': ['Mon', 'Tue', 'Wed'],
                     'series': [{'name': 'A', 'values': [1, None, 3]}]}

    def test_chart_validation(self):
        self.c.validate_spec(self.spec)
        cases = [dict(self.spec, type='pie'), dict(self.spec, x=['x']),
                 dict(self.spec, series=[]), dict(self.spec, series=self.spec['series'] * 4),
                 dict(self.spec, x=['x'] * 25), dict(self.spec, title='x' * 81),
                 dict(self.spec, y_min=4, y_max=2), dict(self.spec, highlight=3),
                 dict(self.spec, unknown=1)]
        for value in (True, '2', float('inf'), float('nan'), 10 ** 500):
            cases.append(dict(self.spec, series=[{'name': 'A', 'values': [value, 2, 3]}]))
        for bad in cases:
            with self.subTest(bad=repr(bad)[:150]), self.assertRaises(ValueError):
                self.c.validate_spec(bad)

    def test_escape_subset_and_null_gaps(self):
        evil = '<>&\"\''
        spec = dict(self.spec, title=evil, unit=evil, x=[evil, 'gap', 'end'],
                    series=[{'name': evil, 'values': [1, None, 3]}])
        svg = self.c.render_svg(spec)
        root = ET.fromstring(svg)
        self.assertNotIn(evil, svg)
        self.assertIn(evil, ''.join(root.itertext()))
        self.assertNotIn('nan', svg.lower())
        self.assertNotIn('inf', svg.lower())
        paths = [e.attrib['d'] for e in root.iter() if e.tag.endswith('path')]
        self.assertTrue(any(d.count('M') == 2 for d in paths), paths)
        self.assertTrue({e.tag.split('}')[-1] for e in root.iter()} <=
                        {'svg', 'g', 'path', 'rect', 'line', 'circle', 'text'})

    def test_nice_axes_zero_baseline_and_edge_values(self):
        for values in ([0, 0, 0], [-5, -3, -1], [1e-12, 2e-12, 3e-12], [1e100] * 3, [None] * 3):
            for kind in ('line', 'bar'):
                spec = dict(self.spec, type=kind, series=[{'name': 'A', 'values': values}])
                lo, hi, ticks = self.c.axis(spec)
                self.assertLess(lo, hi)
                self.assertTrue(all(math.isfinite(x) for x in [lo, hi] + ticks))
                self.assertLessEqual(len(ticks), 7)
                if kind == 'bar':
                    self.assertLessEqual(lo, 0)
                    self.assertGreaterEqual(hi, 0)
                ET.fromstring(self.c.render_svg(spec))
        with self.assertRaises(ValueError):
            self.c.validate_spec(dict(self.spec, y_min=2))
        with self.assertRaises(ValueError):
            self.c.validate_spec(dict(self.spec, type='bar', y_min=1))

    @unittest.skipUnless(shutil.which('node'), 'node not installed')
    def test_chart_javascript_matches_compiled_geometry(self):
        specs = []
        for kind in ('line', 'bar'):
            for values in ([1, None, 3], [-5, 0, 20], [0, 0, 0], [None] * 3,
                           [1e-12, 2e-12, 3e-12], [1e100] * 3, [123456, 150000, 234567]):
                specs.append(dict(self.spec, type=kind, title='<>&\"\'', unit='µA',
                                  series=[{'name': 'A', 'values': values}], highlight=1))
        specs.append(dict(self.spec, y_min=-1, y_max=4))
        documents = [{'html': self.c.build_chart(s, [])[0], 'data': s} for s in specs]
        proc = subprocess.run(['node', str(ROOT / 'tests/widget_runtime.cjs'), '--chart'],
                              input=json.dumps(documents), text=True, capture_output=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for spec, svg in zip(specs, json.loads(proc.stdout)):
            with self.subTest(spec=spec):
                a, b = ET.fromstring(self.c.render_svg(spec)), ET.fromstring(svg)
                self.assertEqual(len(list(a)), len(list(b)))
                for ae, be in zip(a, b):
                    self.assertEqual(ae.tag, be.tag)
                    self.assertEqual(ae.text, be.text)
                    self.assertEqual(ae.attrib, be.attrib)

    def test_budget_fallback_discloses_sampling_and_keeps_original_scale(self):
        spec = {'type': 'line', 'x': [str(i) for i in range(24)],
                'series': [{'name': 'A', 'values': [999 if i == 5 else i for i in range(24)]}]}
        full = self.c.build_chart(spec, [])[0]
        # Force actual point sampling after label thinning; the omitted peak must not rescale the axis.
        compact = self.c.render_svg(spec, label_count=2)
        _, body = self.p.wg.load_template([ROOT / 'templates'], 'chart')
        label_only = self.p.wg.build_html(body.replace('<!--hte-chart-->', compact), [])
        limit = self.p.wg.size_report(label_only)[1] - 30
        with mock.patch.object(self.p.wg, 'MAX_COMPRESSED', limit):
            doc, warnings = self.c.build_chart(spec, [])
        self.assertTrue(any('sampled' in w for w in warnings), warnings)
        svg = doc[doc.index('<svg'):doc.index('</svg>') + 6]
        full_svg = full[full.index('<svg'):full.index('</svg>') + 6]
        ticks = lambda svg: [e.text for e in ET.fromstring(svg) if e.tag.endswith('text') and e.attrib.get('x') == '52']
        self.assertEqual(ticks(svg), ticks(full_svg))

    def test_max_points_full_document_size_and_degradation(self):
        rng = random.Random(712)
        spec = {'type': 'line', 'title': '24 points / three series', 'unit': 'kW',
                'x': [str(i) for i in range(24)],
                'series': [{'name': str(j), 'values': [rng.uniform(-99, 99) for _ in range(24)]}
                           for j in range(3)]}
        for kind in ('line', 'bar'):
            spec['type'] = kind
            doc, warnings = self.c.build_chart(spec, self.p.wg.normalize_actions(['A', 'B']))
            self.p.wg.check_size(doc)
            self.assertIn('chatDataChanged', doc)
            self.assertIn('<svg', doc)
        noisy = copy.deepcopy(spec)
        alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        noisy['x'] = [''.join(rng.choices(alphabet, k=24)) for _ in range(24)]
        doc, warnings = self.c.build_chart(noisy, self.p.wg.normalize_actions(['A', 'B']))
        self.p.wg.check_size(doc)
        with mock.patch.object(self.p.wg, 'MAX_COMPRESSED', 100):
            with self.assertRaisesRegex(ValueError, 'text'):
                self.c.build_chart(noisy, [])


class TemplateFlow(unittest.TestCase):
    setUp = base.PluginFlow.setUp
    tearDown = base.PluginFlow.tearDown

    def test_choice_state_headroom(self):
        actions = self.p.wg.normalize_actions([{'key': 'k' * 32, 'label': 'A'}])
        state = {'note': 'x' * 990}
        self.p.wg.encode_data(state)  # Fits now, but a choice would overflow.
        with self.assertRaisesRegex(self.p.wg.WidgetError, 'chosen'):
            self.p.wg.check_action_data(state, actions)
        self.p.wg.check_action_data({'note': 'ok'}, actions)

    def tool(self, **args):
        with mock.patch.object(self.p, '_template_dirs', lambda: [ROOT / 'templates']):
            return json.loads(self.p.telegram_widget(args))

    def example(self, name):
        return self.p.wg.load_template([ROOT / 'templates'], name)[0]['example']

    def test_send_and_update_validate_even_without_actions(self):
        bad = dict(self.example('weather'), t='<img>')
        self.assertFalse(self.tool(action='send', template='weather', chat_data=bad)['success'])
        self.assertEqual(self.calls, [])
        good = self.example('tracker')
        self.assertTrue(self.tool(action='send', template='tracker', data=good)['success'])
        rec = self.p._state['widgets'].get('100:777')
        self.assertEqual(rec['template'], 'tracker')
        count = len(self.calls)
        res = self.tool(action='update', message_id=777, data=dict(good, i=90))
        self.assertFalse(res['success'])
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.tool(action='update', message_id=777, data=dict(good, i=3))['success'])

    def test_update_preserves_action_identity_and_expiry(self):
        good = self.example('approve')
        self.assertTrue(self.tool(action='send', template='approve', data=good, expires_in=100)['success'])
        exp = self.p._state['widgets'].get('100:777')['expires_at']
        edited = copy.deepcopy(good)
        edited['d'] = [['Recipient', 'updated@example.test']]
        self.assertTrue(self.tool(action='update', message_id=777, data=edited)['success'])
        self.assertEqual(json.loads(self.calls[-1][1]['chat_data'])['exp'], exp)
        edited['o'][0]['v'] = 'different authority'
        count = len(self.calls)
        self.assertFalse(self.tool(action='update', message_id=777, data=edited)['success'])
        self.assertEqual(len(self.calls), count)

    def test_chart_send_uses_svg_without_1kb_initial_state_limit(self):
        spec = {'type': 'bar', 'title': 'Example', 'x': [str(i) for i in range(24)],
                'series': [{'name': str(j), 'values': [i / 19.123 for i in range(24)]} for j in range(3)]}
        self.assertGreater(len(json.dumps(spec)), 1024)
        res = self.tool(action='send', template='chart', data=spec)
        self.assertTrue(res['success'], res)
        payload = self.calls[-1][1]
        self.assertIn('<svg', payload['html'])
        self.assertLessEqual(len(payload.get('chat_data', '').encode()), 1024)
        small = {'type': 'line', 'x': ['A', 'B'], 'series': [{'name': 'A', 'values': [1, 2]}]}
        res = self.tool(action='update', message_id=777, data=small)
        self.assertTrue(res['success'], res)
        self.assertEqual(json.loads(self.calls[-1][1]['chat_data']), small)
        count = len(self.calls)
        res = self.tool(action='update', message_id=777, data=spec)
        self.assertFalse(res['success'])
        self.assertIn('1024', res['error'])
        self.assertEqual(len(self.calls), count)


if __name__ == '__main__':
    unittest.main()
