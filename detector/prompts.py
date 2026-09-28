# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""Fixed benchmark contract: prompts, per-platform models, and reasoning effort.

Everything a deployer usually wants to change lives in ``config.json``. The
prompts themselves are intentionally fixed so results stay comparable over time.
"""
from . import config

TITLE = config.get('title') or 'AI 流口水降智调度'
SUBTITLE = config.get('subtitle') or '你的 AI 现在还在流口水吗？'

# Platform -> {"model", "effort", "label"}. Only enabled platforms are benchmarked.
BENCHMARKS = {
    name: {
        'model': spec['model'],
        'effort': spec.get('effort') or 'medium',
        'label': spec.get('label') or name,
        'protocol': config.protocol_for(name),
    }
    for name, spec in config.enabled_platforms().items()
}


def platform_config(platform):
    """Return the benchmark spec for a platform, or an empty dict when disabled."""
    return BENCHMARKS.get(platform) or {}


def benchmark_spec(platform):
    """Resolve a platform's probe spec from live config.

    Unlike ``BENCHMARKS`` (an import-time snapshot used for display), this reads
    config on every call so tests and long-running workers see edits and patches.
    """
    spec = config.platform_spec(platform)
    if not spec or not spec.get('model'):
        return {}
    return {'model': spec['model'],
            'effort': spec.get('effort') or 'medium',
            'label': spec.get('label') or platform,
            'protocol': config.protocol_for(platform)}


def default_model():
    spec = BENCHMARKS.get('openai') or next(iter(BENCHMARKS.values()), {'model': ''})
    return spec['model']


def default_effort():
    spec = BENCHMARKS.get('openai') or next(iter(BENCHMARKS.values()), {'effort': 'medium'})
    return spec['effort']


# Published in the dashboard state for convenience; each account carries its own pair.
MODEL = default_model()
EFFORT = default_effort()

EXPECTED_ANSWER = 21
CANDY_PROMPT = '在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求） 苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4'
DRAWING_PROMPT = '创建一个 HTML，用 SVG 绘制一只大火烈鸟和一只小火烈鸟骑双人自行车的 2D 动画，不需要任何测试。'
PROMPTS = {'candy': CANDY_PROMPT, 'drawing': DRAWING_PROMPT}
DRAWING_PROFILE = 'visible-preface-html-v1'
LEGACY_DRAWING_INSTRUCTIONS = '请直接给出用户要求的完整、可独立打开的 HTML 源代码。不要调用工具，不要运行测试。'
INSTRUCTIONS = {
    'candy': '请独立解答用户的题目。可以先解释推理，最后一行严格使用“最终答案：N”，其中 N 为你的整数答案。',
    'drawing': '先用一小段自然语言简述实现方案，然后给出用户要求的完整、可独立打开的 HTML 源代码。不要调用工具，不要运行测试。',
}
