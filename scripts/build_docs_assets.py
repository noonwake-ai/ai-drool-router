#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Generate the README diagrams as SVG, for both languages.

The diagrams are generated rather than hand-drawn so that:

  * the numbers in them (scoring weights, cadence) stay tied to the real
    defaults instead of drifting away from the code;
  * the Chinese and English sets are laid out by one piece of code, so they
    cannot silently fall out of sync;
  * anyone can regenerate or restyle them with one command.

    python3 scripts/build_docs_assets.py
    python3 scripts/build_docs_assets.py --check   # fail if the SVGs are stale
"""
import argparse
import hashlib
import math
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / 'docs' / 'assets' / 'diagrams'

CANVAS = 1200

# Palette lifted from the dashboard so the diagrams and the product match.
BG = '#141417'
PANEL = '#1c1c21'
PANEL_2 = '#222228'
LINE = '#33333b'
TEXT = '#f0f0f2'
MUTED = '#9a9aa3'
DIM = '#6b6b76'
GOLD = '#d5b769'
GOOD = '#00c895'
BAD = '#ff4c5d'
WARN = '#ffbf28'
INFO = '#32b8df'

FONT = ('system-ui,-apple-system,Segoe UI,Roboto,PingFang SC,'
        'Hiragino Sans GB,Microsoft YaHei,sans-serif')
MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'


# --------------------------------------------------------------- primitives

def esc(value):
    return (str(value).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def tw(text, size):
    """Approximate rendered width, counting CJK as full-width."""
    total = 0.0
    for ch in str(text):
        if unicodedata.east_asian_width(ch) in ('W', 'F'):
            total += size
        elif ch == ' ':
            total += size * 0.29
        elif ch.isupper() or ch.isdigit():
            total += size * 0.60
        else:
            total += size * 0.53
    return total


def text(x, y, value, size=16, fill=TEXT, weight=400, anchor='start',
         family=None, opacity=1.0, spacing=None):
    attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'font-size="{size}"',
             f'fill="{fill}"', f'font-weight="{weight}"',
             f'font-family="{family or FONT}"']
    if anchor != 'start':
        attrs.append(f'text-anchor="{anchor}"')
    if opacity != 1.0:
        attrs.append(f'opacity="{opacity}"')
    if spacing is not None:
        attrs.append(f'letter-spacing="{spacing}"')
    return f'<text {" ".join(attrs)}>{esc(value)}</text>'


def rect(x, y, w, h, rx=0, fill=PANEL, stroke=None, opacity=1.0, width=1):
    attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'width="{w:.1f}"', f'height="{h:.1f}"',
             f'rx="{rx}"', f'fill="{fill}"']
    if stroke:
        attrs += [f'stroke="{stroke}"', f'stroke-width="{width}"']
    if opacity != 1.0:
        attrs.append(f'opacity="{opacity}"')
    return f'<rect {" ".join(attrs)}/>'


def line(x1, y1, x2, y2, stroke=LINE, width=1, dash=None, opacity=1.0,
         marker_end=None):
    attrs = [f'x1="{x1:.1f}"', f'y1="{y1:.1f}"', f'x2="{x2:.1f}"', f'y2="{y2:.1f}"',
             f'stroke="{stroke}"', f'stroke-width="{width}"', 'fill="none"']
    if dash:
        attrs.append(f'stroke-dasharray="{dash}"')
    if opacity != 1.0:
        attrs.append(f'opacity="{opacity}"')
    if marker_end:
        attrs.append(f'marker-end="url(#{marker_end})"')
    return f'<line {" ".join(attrs)}/>'


def path(d, stroke=LINE, width=2, fill='none', dash=None, marker_end=None):
    attrs = [f'd="{d}"', f'stroke="{stroke}"', f'stroke-width="{width}"',
             f'fill="{fill}"', 'stroke-linecap="round"', 'stroke-linejoin="round"']
    if dash:
        attrs.append(f'stroke-dasharray="{dash}"')
    if marker_end:
        attrs.append(f'marker-end="url(#{marker_end})"')
    return f'<path {" ".join(attrs)}/>'


def circle(cx, cy, r, fill=TEXT, opacity=1.0, stroke=None, width=1):
    attrs = [f'cx="{cx:.1f}"', f'cy="{cy:.1f}"', f'r="{r:.1f}"', f'fill="{fill}"']
    if stroke:
        attrs += [f'stroke="{stroke}"', f'stroke-width="{width}"']
    if opacity != 1.0:
        attrs.append(f'opacity="{opacity}"')
    return f'<circle {" ".join(attrs)}/>'


def pill(x, y, label, size=15, fg=MUTED, bg=PANEL_2, pad=18, height=38, stroke=LINE):
    w = tw(label, size) + pad * 2
    return [rect(x, y, w, height, height / 2, bg, stroke),
            text(x + pad, y + height / 2 + size * 0.36, label, size, fg)], x + w


def card(x, y, w, h, accent=None, radius=14):
    parts = [rect(x, y, w, h, radius, PANEL, LINE)]
    if accent:
        parts.append(rect(x, y + 16, 3, h - 32, 1.5, accent))
    return parts


def defs():
    return (
        '<defs>'
        f'<marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{MUTED}"/></marker>'
        f'<marker id="ag" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{GOOD}"/></marker>'
        f'<marker id="agold" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{GOLD}"/></marker>'
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#1a1a1f"/>'
        f'<stop offset="1" stop-color="{BG}"/></linearGradient>'
        '</defs>'
    )


def document(height, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" '
            f'height="{height}" viewBox="0 0 {CANVAS} {height}" role="img">'
            f'{defs()}{rect(0, 0, CANVAS, height, 0, "url(#bg)")}{body}</svg>\n')


# ------------------------------------------------------------------ diagrams

def hero(t):
    """Identity, plus the whole story as one sparkline."""
    parts = [circle(1030, 90, 300, GOLD, opacity=0.045)]
    parts.append(text(64, 104, t['eyebrow'], 15, GOLD, 500, spacing='0.5'))
    parts.append(text(64, 178, t['title'], 54, TEXT, 700))
    parts.append(text(64, 228, t['tagline'], 25, MUTED))

    x = 64
    for label in t['pills']:
        chunk, x = pill(x, 276, label)
        parts += chunk
        x += 12

    # --- sparkline: quality dips, the tool catches it, traffic moves ---
    px, py, pw, ph = 664, 66, 476, 268
    parts += [rect(px, py, pw, ph, 14, PANEL, LINE)]
    parts.append(text(px + 24, py + 38, t['chart_title'], 14, MUTED))

    values = [82, 84, 83, 81, 72, 64, 58, 55, 54, 66, 78, 86]
    left, right = px + 34, px + pw - 30
    top, bottom = py + 74, py + 186
    step = (right - left) / (len(values) - 1)
    pts = [(left + i * step,
            bottom - (v - 48) * (bottom - top) / (90 - 48)) for i, v in enumerate(values)]
    parts.append(line(left, bottom, right, bottom, LINE))
    parts.append(path('M ' + ' L '.join(f'{x:.1f} {y:.1f}' for x, y in pts),
                      GOLD, 2.6))
    for i, (x, y) in enumerate(pts):
        colour = BAD if 4 <= i <= 8 else GOOD
        parts.append(circle(x, y, 3.4, colour))

    dip_x, turn_x = pts[4][0], pts[9][0]
    parts.append(line(dip_x, top - 6, dip_x, bottom, BAD, 1, dash='4 4', opacity=0.7))
    parts.append(line(turn_x, top - 6, turn_x, bottom, GOOD, 1, dash='4 4', opacity=0.7))

    parts.append(circle(px + 30, py + 224, 4, BAD))
    parts.append(text(px + 44, py + 229, t['legend_dip'], 14, MUTED))
    parts.append(circle(px + 30, py + 250, 4, GOOD))
    parts.append(text(px + 44, py + 255, t['legend_turn'], 14, MUTED))
    return document(368, ''.join(parts))


def problem(t):
    """The two pain points, each as a small picture instead of a paragraph."""
    h = 372
    parts = [text(60, 54, t['title'], 30, TEXT, 650)]

    # --- left: the official model quietly degrades ---
    parts += card(60, 96, 510, 236, BAD)
    parts.append(text(92, 144, t['left_title'], 21, TEXT, 600))
    parts.append(text(92, 172, t['left_sub'], 14, DIM))
    values = [88, 86, 85, 80, 74, 71, 66, 62]
    left, right = 104, 522
    top, bottom = 202, 288
    step = (right - left) / (len(values) - 1)
    pts = [(left + i * step, bottom - (v - 55) * (bottom - top) / (90 - 55))
           for i, v in enumerate(values)]
    parts.append(line(left, bottom, right, bottom, LINE))
    parts.append(path('M ' + ' L '.join(f'{x:.1f} {y:.1f}' for x, y in pts), BAD, 2.6))
    for x, y in pts:
        parts.append(circle(x, y, 3, BAD))
    parts.append(text(92, 316, t['left_note'], 14, MUTED))

    # --- right: the relay quietly swaps the backend ---
    parts += card(630, 96, 510, 236, WARN)
    parts.append(text(662, 144, t['right_title'], 21, TEXT, 600))
    parts.append(text(662, 172, t['right_sub'], 14, DIM))
    nodes = [(662, 150, t['node_ask'], MUTED),
             (836, 104, t['node_relay'], WARN),
             (964, 150, t['node_real'], BAD)]
    for nx, nw, label, colour in nodes:
        parts.append(rect(nx, 208, nw, 46, 8, PANEL_2, LINE))
        parts.append(text(nx + nw / 2, 237, label, 14, colour, 500, anchor='middle'))
    parts.append(line(812, 231, 832, 231, MUTED, 1.8, marker_end='a'))
    parts.append(line(940, 231, 960, 231, MUTED, 1.8, marker_end='a'))
    parts.append(text(662, 316, t['right_note'], 14, MUTED))
    return document(h, ''.join(parts))


def probes(t):
    """What actually gets asked: something with a definite answer, and
    something a human can judge by eye."""
    h = 380
    parts = [text(60, 54, t['title'], 30, TEXT, 650)]

    # --- candy: candies, and one unambiguous number ---
    parts += card(60, 96, 510, 248, INFO)
    parts.append(text(92, 144, t['candy_title'], 21, TEXT, 600))
    positions = [(112, 216), (162, 196), (212, 222), (262, 200), (312, 218)]
    for i, (cx, cy) in enumerate(positions):
        colour = [BAD, WARN, INFO, GOOD, GOLD][i]
        if i % 2:
            parts.append(path(
                ' '.join(f'{"M" if k == 0 else "L"} {cx + 15 * math.sin(3.14159 * 2 * k / 5):.1f} '
                         f'{cy - 15 * math.cos(3.14159 * 2 * k / 5):.1f}'
                         for k in range(6)) + ' Z', colour, 1, colour))
        else:
            parts.append(circle(cx, cy, 15, colour, 0.85))
    parts.append(text(92, 296, t['candy_answer'], 40, GOOD, 700, family=MONO))
    parts.append(text(92, 322, t['candy_note'], 14, MUTED))

    # --- drawing: the flamingo prompt, drawn small ---
    parts += card(630, 96, 510, 248, GOOD)
    parts.append(text(662, 144, t['draw_title'], 21, TEXT, 600))
    parts.append(rect(662, 166, 446, 132, 10, '#8fd0e8'))
    parts.append(circle(1052, 198, 19, '#ffe9a3'))
    parts.append(path('M 662 270 C 790 250 980 290 1108 260 L 1108 298 L 662 298 Z',
                      '#f0d9a8', 0, '#f0d9a8'))
    parts.append(path('M 662 268 C 790 252 980 288 1108 258', '#7fc4dd', 5))

    def flamingo(cx, cy, s, neck):
        """Body, curved neck, head and beak -- reads as a flamingo at 60px."""
        out = [path(f'M {cx:.0f} {cy:.0f} '
                    f'c {-26 * s:.0f} {-6 * s:.0f} {-40 * s:.0f} {-20 * s:.0f} {-26 * s:.0f} {-22 * s:.0f} '
                    f'c {10 * s:.0f} {-2 * s:.0f} {34 * s:.0f} {4 * s:.0f} {40 * s:.0f} {16 * s:.0f} Z',
                    '#ff8fb8', 1, '#ff8fb8'),
               path(f'M {cx - 6 * s:.0f} {cy - 18 * s:.0f} '
                    f'c {neck * 4 * s:.0f} {-30 * s:.0f} {neck * 18 * s:.0f} {-34 * s:.0f} '
                    f'{neck * 22 * s:.0f} {-16 * s:.0f}',
                    '#ff8fb8', max(3, 5 * s)),
               circle(cx - 6 * s + neck * 24 * s, cy - 34 * s, 7 * s, '#ff8fb8',
                      stroke='#d95f8c', width=1.5),
               path(f'M {cx - 2 * s + neck * 28 * s:.0f} {cy - 35 * s:.0f} '
                    f'l {10 * s:.0f} {4 * s:.0f} l {-10 * s:.0f} {4 * s:.0f} Z',
                    '#ffd166', 0, '#ffd166'),
               line(cx - 4 * s, cy - 2 * s, cx - 9 * s, cy + 18 * s, '#d95f8c', max(1.6, 2.4 * s)),
               line(cx + 8 * s, cy - 2 * s, cx + 4 * s, cy + 18 * s, '#d95f8c', max(1.6, 2.4 * s))]
        return out

    # tandem bicycle first, riders on top
    for bx, by, s in ((748, 272, 1.0), (872, 272, 0.78)):
        parts.append(circle(bx - 34 * s, by, 15 * s, 'none', stroke='#2b3a4a', width=3.4 * s))
        parts.append(circle(bx + 34 * s, by, 15 * s, 'none', stroke='#2b3a4a', width=3.4 * s))
    parts.append(path('M 714 272 L 782 272 L 802 246 M 782 272 L 806 272 '
                      'M 838 272 L 868 248 L 906 272 L 878 272', '#2b3a4a', 3.4))
    for chunk in flamingo(768, 222, 1.0, 1):
        parts.append(chunk)
    for chunk in flamingo(878, 240, 0.8, -1):
        parts.append(chunk)
    parts.append(text(662, 322, t['draw_note'], 14, MUTED))
    return document(h, ''.join(parts))


def loop(t):
    """The closed loop, with Sub2API as the thing it reads from and writes to."""
    h = 400
    bw, bh, by = 245, 116, 96
    xs = [60, 338, 616, 894]
    accents = [GOLD, INFO, GOOD, WARN]

    body = []
    for (x, (title, sub)), accent in zip(zip(xs, t['steps']), accents):
        body += card(x, by, bw, bh, accent)
        body.append(text(x + 24, by + 46, title, 17, TEXT, 600))
        for i, row in enumerate(sub):
            body.append(text(x + 24, by + 74 + i * 22, row, 13, MUTED))
        if x != xs[-1]:
            body.append(line(x + bw + 8, by + bh / 2, xs[xs.index(x) + 1] - 10,
                             by + bh / 2, MUTED, 2, marker_end='a'))

    # return arc: the result feeds the next round
    arc_y = 296
    body.append(path(f'M {xs[-1] + bw / 2:.0f} {by + bh:.0f} L {xs[-1] + bw / 2:.0f} {arc_y} '
                     f'L {xs[0] + bw / 2:.0f} {arc_y} L {xs[0] + bw / 2:.0f} {by + bh + 10:.0f}',
                     GOLD, 2, dash='7 6', marker_end='agold'))
    body.append(rect(xs[0] + bw / 2, arc_y - 15, 300, 30, 15, PANEL, LINE))
    body.append(text(xs[0] + bw / 2 + 150, arc_y + 5, t['return_label'], 14, GOLD, 500,
                     anchor='middle'))
    body.append(text(60, 366, t['caption'], 14, DIM))
    return document(h, ''.join(body))


def scoring(t):
    """Four weights, one formula. No prose needed."""
    rows = t['rows']
    strip_y = 108 + len(rows) * 46 + 4
    h = int(strip_y + 58 + 40)          # strip height plus bottom margin
    parts = [text(60, 54, t['title'], 30, TEXT, 650)]
    x0, bar_x, bar_w = 60, 250, 560
    for i, (label, weight, note, colour) in enumerate(rows):
        y = 108 + i * 46
        parts.append(text(x0, y + 18, label, 17, TEXT, 550))
        parts.append(rect(bar_x, y, bar_w, 24, 6, PANEL_2))
        parts.append(rect(bar_x, y, bar_w * weight, 24, 6, colour))
        parts.append(text(bar_x + bar_w * weight + 14, y + 18, f'{weight:.0%}',
                          17, colour, 650, family=MONO))
        parts.append(text(bar_x + bar_w + 84, y + 17, note, 13, DIM))

    parts.append(rect(60, strip_y, 1040, 58, 12, PANEL, LINE))
    parts.append(text(88, strip_y + 37, t['formula'], 16, GOLD, 550, family=MONO))
    return document(h, ''.join(parts))


def architecture(t):
    """Three processes, and the credential boundary between them."""
    h = 380
    parts = [text(60, 54, t['title'], 30, TEXT, 650)]
    boxes = [(60, t['worker'], GOLD, True), (470, t['data'], INFO, None), (880, t['web'], GOOD, False)]
    for x, (title, rows), accent, has_key in boxes:
        parts += card(x, 96, 260, 210, accent)
        parts.append(text(x + 24, 140, title, 18, TEXT, 600))
        for i, row in enumerate(rows):
            parts.append(text(x + 24, 172 + i * 24, row, 13, MUTED))
        if has_key is not None:
            label = t['has_key'] if has_key else t['no_key']
            colour = GOOD if has_key else BAD
            w = tw(label, 12) + 24
            parts.append(rect(x + 24, 262, w, 26, 13, PANEL_2, colour, width=1))
            parts.append(text(x + 36, 279, label, 12, colour, 550))
    # One-way flow: worker -> data dir -> web. Nothing flows back to the worker.
    parts.append(line(328, 201, 462, 201, MUTED, 2, marker_end='a'))
    parts.append(line(738, 201, 872, 201, MUTED, 2, marker_end='a'))
    parts.append(text(60, 350, t['caption'], 14, DIM))
    return document(h, ''.join(parts))


DIAGRAMS = [
    ('hero', hero),
    ('problem', problem),
    ('probes', probes),
    ('loop', loop),
    ('scoring', scoring),
    ('architecture', architecture),
]


# ------------------------------------------------------------------ content

STRINGS = {
    'zh': {
        'hero': {
            'eyebrow': '定时探测 · 直观对比 · 自动调度',
            'title': 'AI 流口水降智调度',
            'tagline': '你的 AI 现在还在流口水吗？',
            'pills': ['糖果题 + 画图题', '盯住降智与掺水', '自动切到最强那家'],
            'chart_title': '某家供应商 · 近 24 小时综合分',
            'legend_dip': '检测到降智',
            'legend_turn': '自动切走流量',
        },
        'problem': {
            'title': '两个让你半夜睡不着的问题',
            'left_title': '官方模型自己变傻',
            'left_sub': '没人会通知你',
            'left_note': '上周还好好的，这周开始答非所问',
            'right_title': '中转站在掺水',
            'right_sub': '你以为在用 Claude',
            'node_ask': '你请求 Claude',
            'node_relay': '中转站',
            'node_real': '实际是豆包',
            'right_note': '客户端永远显示 200，答案还挺通顺',
        },
        'probes': {
            'title': '怎么测：一个看对错，一个看画工',
            'candy_title': '糖果题 · 有标准答案',
            'candy_answer': '21',
            'candy_note': '对错一清二楚，不用争',
            'draw_title': '画图题 · 火烈鸟骑双人自行车',
            'draw_note': '画得好不好，自己一眼看得出来',
        },
        'loop': {
            'steps': [
                ('① 读账号', ['账号 · 分组 · 倍率', '决定测谁']),
                ('② 直连上游', ['用账号自己的 key', '绕过分组网关']),
                ('③ 判对错 · 出画作', ['拿到真实答案', '算出综合分']),
                ('④ 写回优先级', ['分数换成优先级', '交给 Sub2API']),
            ],
            'return_label': '流量自动切给当前最强的那家',
            'caption': '每 45 分钟一轮；凌晨 04:00–08:00 降到 90 分钟。默认只算分，不改你的网关。',
        },
        'scoring': {
            'title': '四个维度，算出一个优先级',
            'rows': [
                ('智力', 0.36, '糖果题答得怎么样', GOLD),
                ('成本', 0.36, '你填的供货倍率', INFO),
                ('稳定性', 0.18, '最近三轮成功率', GOOD),
                ('速度', 0.10, '首字延迟 · 每秒 token', WARN),
            ],
            'formula': '综合分 → 优先级 = 100 + (100 − 综合分) × 1000    （数值越小越先被调用）',
        },
        'architecture': {
            'title': '三个进程，凭据各管各的',
            'worker': ('worker', ['拉账号、发探测、算分', '按需写回优先级']),
            'data': ('数据目录', ['private/ 私有库', 'public/ 脱敏投影']),
            'web': ('web', ['只读公开投影', '对浏览器提供页面']),
            'has_key': '持有 Sub2API 密钥',
            'no_key': '拿不到任何密钥',
            'caption': '浏览器永远拿不到上游密钥；web 进程在 systemd 层被挡在私有目录和密钥文件之外。',
        },
    },
    'en': {
        'hero': {
            'eyebrow': 'SCHEDULED PROBES · VISIBLE COMPARISON · AUTOMATIC ROUTING',
            'title': 'AI Drool Router',
            'tagline': 'Is your AI still drooling?',
            'pills': ['Candy puzzle + drawing task', 'Catch degradation and dilution', 'Route to whoever still delivers'],
            'chart_title': 'One supplier · composite score, last 24h',
            'legend_dip': 'degradation detected',
            'legend_turn': 'traffic moved away',
        },
        'problem': {
            'title': 'Two problems that keep you up at night',
            'left_title': 'The official model got dumber',
            'left_sub': 'nobody will tell you',
            'left_note': 'Fine last week, nonsense this week',
            'right_title': 'The relay is watering it down',
            'right_sub': 'you think you are using Claude',
            'node_ask': 'you ask for Claude',
            'node_relay': 'relay',
            'node_real': 'actually a cheaper model',
            'right_note': 'your client always says 200, the answer still reads fine',
        },
        'probes': {
            'title': 'How it measures: one has an answer, one has craft',
            'candy_title': 'Candy puzzle · definite answer',
            'candy_answer': '21',
            'candy_note': 'right or wrong, no argument',
            'draw_title': 'Drawing task · flamingos on a tandem bike',
            'draw_note': 'you can judge the drawing with your own eyes',
        },
        'loop': {
            'steps': [
                ('1. Read accounts', ['accounts, groups, rates', 'decides who gets probed']),
                ('2. Call upstream', ['with that account own key', 'bypassing the gateway']),
                ('3. Grade + render', ['the real answer, graded', 'then a composite score']),
                ('4. Write priority', ['score becomes priority', 'handed back to Sub2API']),
            ],
            'return_label': 'traffic moves to whoever is strongest right now',
            'caption': 'Every 45 minutes, slowing to 90 between 04:00 and 08:00. Scores only by default; your gateway is untouched.',
        },
        'scoring': {
            'title': 'Four factors become one priority',
            'rows': [
                ('Intelligence', 0.36, 'how it does on the candy puzzle', GOLD),
                ('Cost', 0.36, 'the rate multiplier you enter', INFO),
                ('Stability', 0.18, 'request success, last 3 rounds', GOOD),
                ('Speed', 0.10, 'first token latency, tokens/s', WARN),
            ],
            'formula': 'composite -> priority = 100 + (100 - composite) x 1000    (smaller is called first)',
        },
        'architecture': {
            'title': 'Three processes, one credential boundary',
            'worker': ('worker', ['pull accounts, probe, score', 'write priority back on demand']),
            'data': ('data dir', ['private/ the real database', 'public/ redacted projection']),
            'web': ('web', ['reads the public projection', 'serves the browser']),
            'has_key': 'holds the Sub2API key',
            'no_key': 'cannot see any key',
            'caption': 'The browser never receives an upstream key, and the web process is blocked from the private database and the credential file.',
        },
    },
}


def render(language):
    files = {}
    for name, builder in DIAGRAMS:
        files[f'{name}.svg'] = builder(STRINGS[language][name])
    return files


def write_all(root):
    for language in ('zh', 'en'):
        target = Path(root) / language
        target.mkdir(parents=True, exist_ok=True)
        for filename, content in render(language).items():
            (target / filename).write_text(content)


def digest(root):
    root = Path(root)
    parts = []
    for path in sorted(p for p in root.rglob('*.svg') if p.is_file()):
        parts.append(path.relative_to(root).as_posix())
        parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true',
                        help='regenerate elsewhere and fail if the committed SVGs differ')
    args = parser.parse_args()

    if args.check:
        if not OUT.exists():
            print('docs assets are missing: run python3 scripts/build_docs_assets.py')
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            write_all(tmp)
            if digest(tmp) == digest(OUT):
                print('committed diagrams are up to date')
                return 0
        print('committed diagrams are STALE: run python3 scripts/build_docs_assets.py')
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    write_all(OUT)
    count = sum(1 for _ in OUT.rglob('*.svg'))
    print('generated %d diagrams in %s' % (count, OUT.relative_to(REPO)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
