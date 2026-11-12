"""Small validated line/bar specs → Telegram's inline SVG subset, without dependencies.

Initial geometry is compiled in Python, so a 24×3 chart does not consume the 1 KB
chat_data budget. The shipped template includes an equivalent tiny JS renderer
for subsequent full-spec chatDataChanged updates (which must fit that budget).
"""
from __future__ import annotations

import copy
import html
import math
from pathlib import Path
from typing import Any

from . import widget as wg

_TEMPLATE = Path(__file__).parent / 'templates' / 'chart.html'


def validate_spec(spec: Any) -> dict[str, Any]:
    meta, _ = wg.parse_template(_TEMPLATE.read_text(encoding='utf-8'))
    wg.validate_template_data(meta, spec)
    n = len(spec['x'])
    for i, series in enumerate(spec['series']):
        if len(series['values']) != n:
            raise wg.WidgetError(f'data.series[{i}].values: length must equal data.x ({n})')
    values = [v for series in spec['series'] for v in series['values'] if v is not None]
    for value in values + [spec[k] for k in ('y_min', 'y_max') if k in spec]:
        if value and abs(value) < 1e-100:
            raise wg.WidgetError('data: nonzero chart numbers must have magnitude >= 1e-100; rescale units')
    low, high = min(values, default=0), max(values, default=1)
    if spec['type'] == 'bar':
        low, high = min(low, 0), max(high, 0)
    if 'y_min' in spec and spec['y_min'] > low:
        raise wg.WidgetError('data.y_min: must include every value and the bar zero baseline')
    if 'y_max' in spec and spec['y_max'] < high:
        raise wg.WidgetError('data.y_max: must include every value and the bar zero baseline')
    if 'y_min' in spec and 'y_max' in spec and spec['y_min'] >= spec['y_max']:
        raise wg.WidgetError('data.y_min: must be less than data.y_max')
    if spec.get('highlight', 0) >= n:
        raise wg.WidgetError('data.highlight: must be an index in data.x')
    return spec


def axis(spec: dict[str, Any]) -> tuple[float, float, list[float]]:
    """Nice 1/2/2.5/5 ticks, inclusive explicit bounds; bars always include zero."""
    values = [v for series in spec['series'] for v in series['values'] if v is not None]
    low, high = min(values, default=0), max(values, default=1)
    if spec['type'] == 'bar':
        low, high = min(low, 0), max(high, 0)
    lo, hi = spec.get('y_min', low), spec.get('y_max', high)
    if hi == lo:
        pad = abs(lo) * .1 or 1
        if 'y_min' not in spec:
            lo -= pad
        if 'y_max' not in spec:
            hi += pad
    raw = (hi - lo) / 4
    base = 10 ** math.floor(math.log10(raw))
    step = next(k for k in (1, 2, 2.5, 5, 10) if raw / base <= k) * base
    if 'y_min' not in spec:
        lo = math.floor(lo / step) * step
    if 'y_max' not in spec:
        hi = math.ceil(hi / step) * step
    start, end = math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9)
    ticks = [k * step for k in range(start, end + 1)]
    return lo, hi, ticks


def _short(value: str, width: int) -> str:
    # Approximate double-width labels without browser text measurement; same rule in JS.
    used, out = 0, ''
    for c in value:
        used += 2 if ord(c) > 255 else 1
        if used > width:
            return out + '…'
        out += c
    return out


def _num(value: float) -> str:
    return f'{value:.1f}'.rstrip('0').rstrip('.') if value else '0'


def _tick(value: float) -> str:
    if not value:
        return '0'
    if abs(value) >= 1e6 or abs(value) < .001:
        return f'{value:.1e}'.replace('e+0', 'e').replace('e-0', 'e-').replace('e+', 'e')
    return f'{float(f"{value:.3g}"):.9f}'.rstrip('0').rstrip('.')


def render_svg(spec: dict[str, Any], *, label_count: int = 5) -> str:
    """Escaped, bounded SVG. Nulls break lines; no clipPath/filter/foreignObject."""
    validate_spec(spec)
    lo, hi, ticks = axis(spec)
    n, series = len(spec['x']), spec['series']
    bar = spec['type'] == 'bar'
    def x(i: int) -> float:
        return 58 + (i + .5) * 250 / n if bar else 58 + (i * 250 / (n - 1) if n > 1 else 125)
    def y(v: float) -> float:
        return 178 - (v - lo) / (hi - lo) * 128
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 222">']
    def text(tx: float, ty: float, value: Any, cls: str = '', anchor: str = 'start') -> None:
        parts.append(f'<text x="{_num(tx)}" y="{_num(ty)}" class="{cls}" text-anchor="{anchor}">{html.escape(str(value), quote=True)}</text>')
    text(10, 20, _short(spec.get('title', ''), 42), 'title')
    text(308, 37, _short(spec.get('unit', ''), 24), anchor='end')
    for tick in ticks:
        ty = y(tick)
        parts.append(f'<path class="grid" d="M58 {_num(ty)}H308"/>')
        text(52, ty + 3, _tick(tick), anchor='end')
    if bar:
        parts.append(f'<path class="base" d="M58 {_num(y(0))}H308"/>')
    if 'highlight' in spec:
        hx = _num(x(spec['highlight']))
        parts.append(f'<path class="base" stroke-dasharray="2 3" d="M{hx} 50V178"/>')
    labels = min(n, label_count)
    indices = [math.floor(j * (n - 1) / max(labels - 1, 1) + .5) for j in range(labels)]
    for i in indices:
        text(x(i), 195, _short(spec['x'][i], 8), anchor='end' if i == n - 1 else 'middle')
    for k, row in enumerate(series):
        values = row['values']
        if bar:
            width = 250 / n * .8 / len(series)
            for i, v in enumerate(values):
                if v is None:
                    continue
                px, py = x(i) - 250 / n * .4 + k * width, min(y(v), y(0))
                parts.append(f'<rect class="f{k}" x="{_num(px)}" y="{_num(py)}" width="{_num(width)}" height="{_num(abs(y(v) - y(0)))}"/>')
        else:
            path, gap = '', True
            for i, v in enumerate(values):
                if v is None:
                    gap = True
                    continue
                path += ('M' if gap else 'L') + _num(x(i)) + ' ' + _num(y(v))
                gap = False
                if (i == 0 or values[i - 1] is None) and (i == n - 1 or values[i + 1] is None):
                    parts.append(f'<circle class="f{k}" cx="{_num(x(i))}" cy="{_num(y(v))}" r="2.5"/>')
            parts.append(f'<path class="s{k}" d="{path}"/>')
        # one legend row right under the x labels (series side by side), so no dead space below
        lx = 12 + k * 100
        parts.append(f'<path class="s{k}" d="M{lx} 213h16"/>')
        text(lx + 22, 216, _short(row['name'], 12), 'legend')
    parts.append('</svg>')
    return ''.join(parts)


def build_chart(spec: dict[str, Any], actions: list[dict[str, str]]) -> tuple[str, list[str]]:
    """Check the FULL document, including both runtimes; disclose any simplification.

    Do not silently average values. Thin labels first, then evenly sample points,
    retaining endpoints, highlight and gaps (no misleading new line across a null).
    """
    validate_spec(spec)
    _, body = wg.parse_template(_TEMPLATE.read_text(encoding='utf-8'))
    work, warnings = copy.deepcopy(spec), []
    for labels in (5, 2):
        doc = wg.build_html(body.replace('<!--hte-chart-->', render_svg(work, label_count=labels)), actions)
        if all(a <= b for a, b in zip(wg.size_report(doc), (wg.MAX_HTML, wg.MAX_COMPRESSED))):
            return doc, warnings
        warnings = ['Chart x-axis labels reduced to fit the widget budget.']
    # Sampling is intentionally a last resort, and reported to the caller/caption author.
    n = len(spec['x'])
    for limit in (12, 6, 3):
        if n <= limit:
            continue
        indices = {math.floor(j * (n - 1) / (limit - 1) + .5) for j in range(limit)}
        if 'highlight' in spec:
            indices.add(spec['highlight'])
        indices = sorted(indices)
        work = copy.deepcopy(spec)
        work['y_min'], work['y_max'], _ = axis(spec)  # omitted extrema must not rescale the plot
        work['x'] = [spec['x'][i] for i in indices]
        for row, source in zip(work['series'], spec['series']):
            # Never interpolate across omitted nulls. Keep a gap rather than fabricate continuity.
            row['values'] = [source['values'][i] if j == 0 or spec['type'] == 'bar' or
                             all(v is not None for v in source['values'][indices[j-1]:i+1])
                             else None for j, i in enumerate(indices)]
        if 'highlight' in work:
            work['highlight'] = indices.index(spec['highlight'])
        doc = wg.build_html(body.replace('<!--hte-chart-->', render_svg(work, label_count=2)), actions)
        if all(a <= b for a, b in zip(wg.size_report(doc), (wg.MAX_HTML, wg.MAX_COMPRESSED))):
            return doc, warnings + [f'Chart sampled from {n} to {len(indices)} points; no averaging.']
    raise wg.WidgetError('Chart exceeds the full widget size budget after reducing labels/points; use text or a text table.')
