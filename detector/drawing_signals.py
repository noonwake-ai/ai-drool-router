# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Bounded observations of visible output; never a quality verdict or routing input."""
from html.parser import HTMLParser
import re

VERSION = 'visible-text-signals-v1'
MAX_SCAN = 1_000_000
MAX_EXCERPT = 180
TERMS = {
    'risk': ('内嵌 SVG', '内联 SVG', '循环'),
    'positive': ('踩踏', '沿途风景', '背景移动'),
}


def matches(text):
    return {kind: [term for term in terms
                   if re.search(re.escape(term).replace(r'\ ', r'\s*'), text, re.I)]
            for kind, terms in TERMS.items()}


class Descriptions(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.desc_depth = 0

    def handle_comment(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == 'desc':
            self.desc_depth += 1

    def handle_endtag(self, tag):
        if tag == 'desc':
            self.desc_depth = max(0, self.desc_depth - 1)

    def handle_data(self, data):
        if self.desc_depth:
            self.parts.append(data)


def analyze(text, profile):
    bounded = text[:MAX_SCAN]
    start = re.search(r'<!doctype\s+html[^>]*>|<html\b', bounded, re.I)
    prefix = bounded[:start.start()] if start else ''
    # The fence is packaging, not a model-authored explanatory paragraph.
    prefix = re.split(r'```|~~~', prefix, maxsplit=1)[0].strip()
    paragraph = re.split(r'\n\s*\n', prefix, maxsplit=1)[0].strip()
    preface = matches(paragraph)
    source = bounded[start.start():] if start else ''
    parser = Descriptions()
    # HTMLParser raises on some malformed input and silently tolerates it on
    # others depending on the Python version, so detect the shapes we care about
    # explicitly instead of relying on parser internals.
    parse_error = bool(re.search(r'<!--(?!.*?-->)', source, re.S)) or \
        bool(re.search(r'<!\[(?!CDATA\[)', source))
    try:
        parser.feed(source)
    except (ValueError, AssertionError, NotImplementedError):
        parse_error = True
    descriptions = matches('\n'.join(parser.parts))
    source_hits = matches(source)
    risk, positive = bool(preface['risk']), bool(preface['positive'])
    state = ('no_preface' if not paragraph else 'mixed' if risk and positive
             else 'risk_terms' if risk else 'positive_terms' if positive else 'no_match')
    return {
        'version': VERSION, 'prompt_profile': profile, 'state': state,
        'preface': {**preface, 'excerpt': paragraph[:MAX_EXCERPT],
                    'excerpt_truncated': len(paragraph) > MAX_EXCERPT},
        'descriptions': descriptions, 'source': source_hits,
        'scan_truncated': len(text) > MAX_SCAN, 'description_parse_error': parse_error,
    }


def from_attempts(attempts):
    return next((a['drawing_signals'] for a in reversed(attempts)
                 if a.get('drawing_signals', {}).get('version') == VERSION), None)
