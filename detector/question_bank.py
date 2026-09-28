"""Original candy probe and closed-answer grading for current and historical runs."""
from copy import deepcopy
from fractions import Fraction
import json
import re

from .prompts import CANDY_PROMPT, INSTRUCTIONS

TWO_PASS_VERSION = 'original-candy-v2-two-pass-20260915'
VERSION = 'original-candy-v3-fail-fast-20260915'
CONFIRMATION = 'two_consecutive_correct_independent_requests'
ORIGINAL = {'id':'original-candy', 'template':'original-candy', 'family':'candy',
            'title':'原始糖果题', 'prompt':CANDY_PROMPT, 'instructions':INSTRUCTIONS['candy'],
            'kind':'number', 'expected':'21', 'unit':'个', 'source':'用户指定原题', 'version':VERSION}
QUESTIONS = [ORIGINAL]
TEMPLATES = tuple(dict.fromkeys(q['template'] for q in QUESTIONS))
CANDY_UNIT = r'(?:个|颗|粒)(?:\s*(?:糖果|糖))?'


def unique_json_pairs(items):
    if len({key for key, _ in items}) != len(items):
        raise ValueError('duplicate key')
    return dict(items)


def select_question(slot, exclude=None):
    # Both stages use the original question in separate fresh requests.
    return deepcopy(ORIGINAL)


def two_request_verdict(results, attempts, *, fail_fast=False):
    """Fail on a proven first wrong answer; passing still needs two fresh stages."""
    answers = [a for a in attempts if a.get('status') in ('pass','fail')]
    if fail_fast and len(results) == len(answers) == 1:
        result, answer = results[0], answers[0]
        if (result.get('question_id') == answer.get('question_id') == ORIGINAL['id'] and
                result.get('status') == answer.get('status') == 'fail' and
                answer.get('stage') == 0 and answer.get('request_id')):
            return 'fail'
    if len(results) != 2 or any(r.get('question_id') != ORIGINAL['id'] or
                              r.get('status') not in ('pass','fail') for r in results):
        return None
    if (len(answers) != 2 or [a.get('stage') for a in answers] != [0,1] or
            any(a.get('question_id') != ORIGINAL['id'] or not a.get('request_id') for a in answers) or
            answers[0]['request_id'] == answers[1]['request_id'] or
            [a['status'] for a in answers] != [r['status'] for r in results]):
        return None
    if any(r['status'] == 'fail' for r in results):
        return 'fail'
    return 'pass' if attempts[-2:] == answers else None


def candy_conclusion(text):
    """Read explicit candy conclusions, not arbitrary numbers in the proof."""
    number = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:/\d+)?'
    quantity = number + r'\s*(?:' + CANDY_UNIT + r')?'
    prefix = (r'(?:(?:最终)?答案[:：]\s*)?'
              r'(?:(?:所以|因此|故|综上(?:所述)?)[，,\s]*)?'
              r'(?:最少(?:需要)?(?:取出|摸出|拿出|抽取|取)?|'
              r'最少(?:应为|为|就是|是)|'
              r'(?:最终)?答案(?:是|为|[:：]))\s*')
    candidates = []
    # Quoted examples and code are not the model's own conclusion.
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    text = re.sub(r'[*`_#]', '', text)
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip()
        if not paragraph or paragraph.startswith(('>', '“', '「', '如果', '若', '假设', '假如')):
            continue
        paragraph = re.sub(r'\\(?:text|mathrm)\{\s*(' + CANDY_UNIT + r')\s*\}', r'\1', paragraph)
        paragraph = re.sub(r'\\[\[\]()]|\$', '', paragraph)
        paragraph = re.sub(r'\s+', ' ', paragraph).strip()
        # Explicit uncertainty about the answer still invalidates a conclusion;
        # possibilities inside a separate proof sentence do not.
        if re.search(r'(?:不确定|不能确定|无法确定)(?:最终答案|答案|[。.!！]|$)', paragraph):
            return None
        for sentence in re.split(r'[。！!；;]', paragraph):
            sentence = sentence.strip()
            # A conditional explanation is not an unconditional final answer.
            if re.search(r'如果|假设|假如|若', sentence):
                continue
            # Normalize only verified numeric addition equalities, without eval.
            # A proof's arbitrary numbers still cannot create a conclusion.
            def sum_result(match):
                try:
                    terms = re.split(r'\s*\+\s*', match['terms'])
                    if sum(Fraction(term) for term in terms)==Fraction(match['total']):
                        return match['total']
                except (ValueError, ZeroDivisionError):
                    pass
                return match[0]
            sentence = re.sub(r'(?<![A-Za-z0-9_.])(?P<terms>\d+(?:\s*\+\s*\d+){1,5})\s*=\s*'
                              r'(?P<total>\d+)(?![A-Za-z0-9_.]|\s*[+*/=−-])', sum_result, sentence)
            boxed = re.fullmatch(r'\\boxed\{\s*(' + quantity + r')\s*\}\s*[.]?', sentence)
            if boxed:
                candidates.append(boxed[1])
                continue
            # A conclusion can be followed by an explanation, but not a second
            # alternative, a negation, an expression, or a longer numeric token.
            sentence = re.sub(r'\\boxed\{\s*(' + quantity + r')\s*\}', r'\1', sentence)
            # Recognize an explicit conclusion after a comma as well as at the
            # sentence start. Do not use arbitrary numbers from the proof.
            for match in re.finditer(r'(?:^|[，,])\s*' + prefix + r'(' + quantity +
                                     r')(?=$|[，,:：（(]|\.(?!\d)|\s)', sentence):
                if re.search(r'不确定|不能确定|无法确定|未必|可能是|也许|大概|或(?:者)?\s*\d', sentence):
                    return None
                lead = sentence[:match.start()]
                if re.search(r'“|「|["\']|(?:不是|并非|否认|错误|有误|不成立|不对|不能确定)', lead):
                    return None
                tail = sentence[match.end():].strip()
                if tail.startswith(('（', '(')):
                    note = re.match(r'(?:（[^（）]*）|\([^()]*\))', tail)
                    if not note:
                        return None
                    rest = tail[note.end():].strip()
                    if rest and not rest.startswith(('，', ',', '：', ':', '.')):
                        return None
                elif tail and not tail.startswith(('，', ',', '：', ':', '.')):
                    return None
                if re.search(r'不是|并非|不对|错误|有误|不成立|不能保证|不够|但|不过|然而|而是|更正|改为|应为', tail):
                    return None
                candidates.append(match[1])
    if not candidates:
        return None
    try:
        values = {Fraction(re.match(number, value)[0]) for value in candidates}
    except (ValueError, ZeroDivisionError):
        return None
    return candidates[0] if len(values) == 1 else None


def answer_slot(text, *, candy=False):
    if candy:
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        text = re.sub(r'^\s*>[^\n]*$', '', text, flags=re.M)
    normalized = re.sub(r'[*`_#]', '', text).strip()
    slots = re.findall(r'^\s*最终答案\s*[:：]\s*(.*?)\s*$', normalized, re.M)
    if slots:
        if candy:
            values = []
            for slot in slots:
                # Some relays leak an empty tool-envelope suffix into output_text.
                # Accept only a complete, exact empty envelope, never loose suffix stripping.
                try:
                    envelope = json.loads('{"answer":"' + slot, object_pairs_hook=unique_json_pairs)
                except (ValueError, TypeError):
                    envelope = None
                if (isinstance(envelope, dict) and set(envelope)=={'answer','calls'}
                        and envelope['calls']==[] and isinstance(envelope['answer'],str)):
                    slot = envelope['answer']
                slot = re.sub(r'\\(?:text|mathrm)\{\s*(' + CANDY_UNIT + r')\s*\}', r'\1', slot)
                slot = re.sub(r'\\[\[\]()]|\$', '', slot).strip()
                slot = re.sub(r'^\\boxed\{([^{}]+)\}([。.!！]?)$', r'\1\2', slot)
                conclusion = candy_conclusion(slot)
                values.append(conclusion if conclusion is not None else slot)
            if len(set(values)) != 1:
                return None
            return values[0]
        return slots[-1].strip()
    boxed = re.fullmatch(r'\s*\\boxed\{([^{}]+)\}\s*[。.!！]?\s*', normalized)
    if boxed:
        return boxed[1]
    if candy:
        conclusion = candy_conclusion(text)
        if conclusion is not None:
            return conclusion
    # Bare conclusions are accepted only when the entire reply is one line.
    return normalized if '\n' not in normalized else None


def grade(question, text):
    """Return pass/fail/unknown, never infer correctness from a number in reasoning."""
    if question['kind'] == 'json':
        try:
            actual = json.loads(text.strip(), object_pairs_hook=unique_json_pairs)
        except (ValueError, TypeError):
            return 'unknown', None
        expected = question['expected']
        valid = isinstance(actual,dict) and actual.keys()==expected.keys() and all(
            type(actual[k]) is type(v) and actual[k]==v for k,v in expected.items())
        return ('pass' if valid else 'fail'), actual
    value = answer_slot(text, candy=question['id'] == ORIGINAL['id'])
    if value is None:
        return 'unknown', None
    if question['kind'] == 'choice':
        value = value.rstrip('。.!！').strip()
        if value not in question['choices'] or len(value)!=1:
            return 'unknown', None
        return ('pass' if value==question['expected'] else 'fail'), value
    unit = question.get('unit','')
    units = {'元':r'(?:元|人民币|RMB)?', '小时':r'(?:小时|时|h)?', '个':r'(?:'+CANDY_UNIT+r')?'}.get(unit,r'(?:只|个|次)?')
    match = re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:/\d+)?)\s*'+units+r'\s*[。.!！]?\s*', value)
    if not match:
        return 'unknown', None
    try:
        actual = Fraction(match[1])
        expected = Fraction(question['expected'])
    except (ValueError, ZeroDivisionError):
        return 'unknown', None
    return ('pass' if actual==expected else 'fail'), match[1]


def first_verdict(row):
    return row.get('first_status') or (row['status'] if row['status'] in ('pass','fail') else None)
