"""Upstream protocol adapters plus the Sub2API admin client. POS: worker-only."""
import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from urllib3.exceptions import ReadTimeoutError

from . import config
from .prompts import BENCHMARKS, INSTRUCTIONS, PROMPTS, benchmark_spec
from .costs import normalize_usage

MAX_OUTPUT_BYTES = 8_000_000
MAX_EVENT_BYTES = 16_000_000
MAX_STREAM_BYTES = 32_000_000
REQUEST_TOTAL_SECONDS = int(config.get('budgets.default_seconds', 900))
# Some high-effort models spend 12+ minutes in reasoning before the first visible body
# token on the drawing prompt, so the wall is configurable per platform and task.
DRAWING_TOTAL_SECONDS = {name: int(value) for name, value in
                         (config.get('budgets.drawing_platform_seconds') or {}).items()}
STREAM_IDLE_SECONDS = int(config.get('budgets.idle_seconds', 120))
GEMINI_HIGH_IDLE_SECONDS = int(config.get('budgets.gemini_high_idle_seconds', 300))
# Gemini's generation cap includes thinking, even for a short final answer.
GEMINI_MAX_OUTPUT_TOKENS = 32768
# Official @openai/codex stable dist-tag verified on 2026-09-13. This is not a CLI process.
CODEX_VERSION = '0.154.0'
CLIENT_INFO = {'transport':'direct-http', 'codex_protocol_version':CODEX_VERSION}
# Official https://x.ai/cli/stable read on 2026-09-13; HTTP compatibility, not a CLI process.
GROK_VERSION = '1.0.30'
# Matches the deployed Sub2API 0.2.4 account-test profile (commit 5de5e2bed035).
# Protocol compatibility only; this adapter does not launch Claude Code.
CLAUDE_VERSION = '2.1.258'
CLAUDE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
CLAUDE_HEADERS = {
    'User-Agent': f'claude-cli/{CLAUDE_VERSION} (external, cli)',
    'X-Stainless-Lang': 'js', 'X-Stainless-Package-Version': '0.94.0',
    'X-Stainless-OS': 'Linux', 'X-Stainless-Arch': 'arm64',
    'X-Stainless-Runtime': 'node', 'X-Stainless-Runtime-Version': 'v24.3.0',
    'X-Stainless-Retry-Count': '0', 'X-Stainless-Timeout': str(REQUEST_TOTAL_SECONDS),
    'X-App': 'cli', 'Anthropic-Dangerous-Direct-Browser-Access': 'true',
    'anthropic-beta': 'claude-code-20250219,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14',
}


PROTOCOL_LABELS = {
    'openai_responses': 'openai-responses',
    'openai_chat': 'openai-chat-completions',
    'anthropic_messages': 'anthropic-messages-2023-06-01',
    'gemini_generate': 'gemini-generateContent-v1beta',
    'xai_responses': 'xai-responses',
}


def client_info(platform):
    """Describe the client identity used for a platform, for the audit trail."""
    if platform == 'openai':
        return CLIENT_INFO
    protocol = config.protocol_for(platform)
    info = {'transport': 'direct-http', 'protocol': PROTOCOL_LABELS.get(protocol, protocol)}
    if protocol == 'xai_responses':
        info['grok_protocol_version'] = GROK_VERSION
    if protocol == 'anthropic_messages':
        info['claude_protocol_version'] = CLAUDE_VERSION
    return info


class ProbeError(Exception):
    def __init__(self, code, message, timings=None, tokens=None):
        self.code = code
        self.message = message
        self.timings = timings or {}
        self.tokens = tokens or {}
        super().__init__(code)


def request_budget(platform, kind, fallback=REQUEST_TOTAL_SECONDS):
    """Per-platform generation budget; drawing gets a wider wall only where evidence requires it."""
    if kind == 'drawing':
        return DRAWING_TOTAL_SECONDS.get(platform, fallback)
    return fallback


def _megabytes(value):
    return ('%.2f MB' % (value/1048576)) if value >= 1048576 else ('%.1f KB' % (value/1024))


def budget_exceeded_message(elapsed, timeout, timings, progress):
    """Explain a client wall that cut an actively streaming generation, without leaking content."""
    head = '首个数据 ' + str(timings.get('first_data_seconds')) + ' 秒'
    if 'first_text_seconds' in timings:
        body = '，首个正文字符 ' + str(timings['first_text_seconds']) + ' 秒才出现'
    else:
        body = '，预算内未收到任何正文字符'
    return ('上游持续有响应（' + head + body + '，累计接收 ' + _megabytes(progress.get('received_bytes', 0)) +
            '、正文 ' + str(progress.get('output_chars', 0)) + ' 个字符），但 ' + str(round(timeout)) +
            ' 秒生成预算已用完，未完整结束。属于客户端预算截断，不是上游无响应，未将部分输出判为成功。')


def redact(text, secrets=(), errors=False):
    text = str(text)
    for value in secrets:
        if isinstance(value, str) and len(value) >= 6:
            text = text.replace(value, '[已隐藏]')
    text = re.sub(r'(?i)\b(?:sk-|admin-)[a-z0-9_-]{12,}', '[凭据已隐藏]', text)
    text = re.sub(r'\beyJ[\w-]{15,}\.[\w-]+\.[\w-]+', '[令牌已隐藏]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[邮箱已隐藏]', text)
    if errors:
        text = re.sub(r'https?://[^\s<>"\']+', '[上游地址]', text)
        text = re.sub(r'(?i)(authorization|api[_-]?key|access_token|refresh_token)\s*[:=]\s*[^\s,;]+', r'\1=[已隐藏]', text)
    return text


def secret_values(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if any(t in k.lower() for t in ('key', 'token', 'password', 'secret', 'email')) and isinstance(v, str):
                yield v
            elif isinstance(v, (dict, list)):
                yield from secret_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from secret_values(v)


OAUTH_QUOTA_ERROR = 'OAUTH_QUOTA_EXHAUSTED'


def _group_labels(account):
    """Collect the group names a gateway actually exposed for this account.

    Sub2API normally returns only numeric ``group_ids``. A deployment may enrich
    the payload with ``group_names`` or a ``groups`` list of objects; if it does
    not, name-based scoping cannot be evaluated and must not be guessed.
    """
    labels = []
    for key in ('group_names', 'groups'):
        value = account.get(key)
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                labels.append(entry.strip().lower())
            elif isinstance(entry, dict):
                for field in ('name', 'label', 'title'):
                    text = entry.get(field)
                    if isinstance(text, str) and text.strip():
                        labels.append(text.strip().lower())
    return set(labels)


def scope_state(account):
    """Return ``in``, ``out``, or ``unknown`` for one gateway account.

    ``group_ids`` / ``group_names`` in ``config.json`` narrow the benchmark set;
    leaving both empty benchmarks every account of that platform. ``exclude_names``
    always wins. ``unknown`` means name scoping was configured but the gateway did
    not expose group names, so the caller must refuse to guess rather than probe
    an account the deployer did not intend.
    """
    platform = account.get('platform')
    spec = config.platform_spec(platform)
    if not spec:
        return 'out'
    lowered = str(account.get('name') or '').lower()
    for keyword in spec.get('exclude_names') or []:
        if keyword and str(keyword).lower() in lowered:
            return 'out'
    allowed_ids = set(config.evaluation_scope_ids(platform))
    allowed_names = {str(value).strip().lower() for value in (spec.get('group_names') or []) if str(value).strip()}
    if not allowed_ids and not allowed_names:
        return 'in'
    groups = account.get('group_ids')
    if isinstance(groups, list):
        for group_id in groups:
            if type(group_id) is int and group_id in allowed_ids:
                return 'in'
    if not allowed_names:
        return 'out' if isinstance(groups, list) else 'unknown'
    labels = _group_labels(account)
    if labels and labels & allowed_names:
        return 'in'
    if isinstance(groups, list) and groups and not labels:
        # Numeric ids exist but no names were published; a name-based rule cannot
        # be evaluated for this account. Say so instead of silently excluding or
        # silently including it.
        return 'unknown'
    if labels and not (labels & allowed_names):
        return 'out'
    return 'in' if not groups and not labels else 'unknown'


def in_evaluation_scope(account):
    return scope_state(account) == 'in'


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float('inf'), float('-inf')) else None


def _timestamp(value):
    number = _number(value)
    if number is not None and number > 0:
        return number / 1000 if number > 10_000_000_000 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _future_timestamp(value, now):
    timestamp = _timestamp(value)
    return timestamp if timestamp is not None and timestamp > now else None


def _reset_timestamp(extra, window, now):
    absolute = _timestamp(extra.get('codex_'+window+'_reset_at'))
    if absolute is not None:
        return absolute
    after = _number(extra.get('codex_'+window+'_reset_after_seconds'))
    if after is None or after <= 0:
        return None
    updated = _timestamp(extra.get('codex_usage_updated_at')) or now
    reset = updated + after
    return reset


def _quota_snapshot_trusted(credentials, extra):
    def first(values, keys):
        return next((str(values[k]).strip().casefold() for k in keys if values.get(k)), '')
    for left_keys, right_keys in (
            (('email',), ('email', 'email_address')),
            (('chatgpt_account_id',), ('chatgpt_account_id', 'account_id')),
            (('workspace_id', 'chatgpt_workspace_id', 'organization_id', 'org_id'),
             ('workspace_id', 'chatgpt_workspace_id', 'organization_id', 'org_id'))):
        left, right = first(credentials, left_keys), first(extra, right_keys)
        if left and right and left != right:
            return False
    return True


def _quota_disabled(value):
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1')
    return value is True or (_number(value) or 0) != 0


def _scheduling_threshold(value):
    number = _number(value)
    if number is None:
        return None
    rounded = int(number + 0.5)
    return rounded if 1 <= rounded <= 100 else None


def oauth_quota_state(account, now=None):
    """Return the current OAuth quota pause without changing Sub2API state.

    Sub2API can auto-pause an OpenAI OAuth account at an account-specific
    threshold such as 96% or 97%, so raw 100% usage is not the only signal.
    The account endpoint is read immediately before each attempt; expired
    windows are therefore allowed to recover on the next scheduled cycle.
    """
    result = {'limited': False, 'code': None, 'window': None, 'used_percent': None,
              'threshold_percent': None, 'reset_at': None, 'reason': None}
    if not isinstance(account, dict) or account.get('type') != 'oauth':
        return result
    now = time.time() if now is None else float(now)
    extra = account.get('extra') if isinstance(account.get('extra'), dict) else {}
    credentials = account.get('credentials') if isinstance(account.get('credentials'), dict) else {}

    raw_reason = account.get('temp_unschedulable_reason')
    try:
        reason = json.loads(raw_reason) if isinstance(raw_reason, str) else {}
    except (TypeError, ValueError):
        reason = {}
    if not isinstance(reason, dict):
        reason = {}
    source = reason.get('source')
    # The live account expiry wins over a retained diagnostic reason.
    pause_until = _timestamp(account.get('temp_unschedulable_until'))
    if pause_until is None:
        pause_until = _timestamp(reason.get('until_unix'))
    if source == 'account_scheduling_threshold':
        if pause_until is not None and pause_until > now:
            result.update(limited=True, code=OAUTH_QUOTA_ERROR,
                          window=reason.get('window'), used_percent=_number(reason.get('used_percent')),
                          threshold_percent=_number(reason.get('threshold_percent')),
                          reset_at=pause_until,
                          reason='account_scheduling_threshold')
            return result

    rate_limit_reset = _future_timestamp(account.get('rate_limit_reset_at'), now)
    if rate_limit_reset is not None:
        result.update(limited=True, code=OAUTH_QUOTA_ERROR, reset_at=rate_limit_reset,
                      reason='rate_limit_reset_at')
        return result

    if account.get('platform') != 'openai' or not _quota_snapshot_trusted(credentials, extra):
        return result
    policy = account.get('_quota_policy') or {}
    threshold = _scheduling_threshold(credentials.get('account_scheduling_threshold'))
    if threshold is None:
        threshold = _scheduling_threshold(policy.get('account_scheduling_threshold'))
    candidates = []
    for window in ('5h', '7d'):
        used = _number(extra.get('codex_'+window+'_used_percent'))
        reset_at = _reset_timestamp(extra, window, now)
        if used is None or reset_at is None or reset_at <= now:
            continue
        # Platform thresholds and per-window auto-pause are separate policies.
        # 100 disables the former; per-window disabled flags only disable the latter.
        thresholds = [100.0]
        if threshold is not None and threshold < 100 and reset_at is not None:
            thresholds.append(threshold)
        if not _quota_disabled(extra.get('auto_pause_'+window+'_disabled')):
            auto_threshold = _number(extra.get('auto_pause_'+window+'_threshold'))
            if auto_threshold is None or auto_threshold <= 0:
                auto_threshold = _number(policy.get('default_threshold_'+window))
            if auto_threshold is not None and auto_threshold > 0:
                thresholds.append(min(auto_threshold, 1) * 100)
        configured = min(thresholds)
        # A cached high percentage is not proof of recovery just because it aged.
        # A lower fresh percentage or an elapsed reset window releases the skip.
        if used >= configured:
            candidates.append((reset_at, window, used, configured, reset_at))
    if candidates:
        _, window, used, configured, reset_at = max(candidates)
        result.update(limited=True, code=OAUTH_QUOTA_ERROR, window=window,
                      used_percent=used, threshold_percent=configured, reset_at=reset_at,
                      reason='usage_threshold_snapshot')
    return result


class Sub2API:
    """Credential reads plus one narrow, worker-only account scheduling write."""
    def __init__(self, base_url, admin_key):
        self.base = base_url.rstrip('/')
        self.key = admin_key

    def get(self, path, params=None):
        try:
            with requests.Session() as s:
                s.trust_env = False
                r = s.get(self.base + '/api/v1/admin' + path, params=params,
                          headers={'x-api-key': self.key}, timeout=(12, 45))
                if r.status_code != 200:
                    raise ProbeError('ACCOUNT_SYNC_HTTP_'+str(r.status_code), '账户配置读取失败（HTTP '+str(r.status_code)+'）')
                data = r.json()
                if data.get('code', 0) != 0:
                    raise ProbeError('ACCOUNT_SYNC_REJECTED', 'Sub2API 拒绝读取账户配置')
                return data.get('data', data)
        except (requests.RequestException, ValueError):
            raise ProbeError('ACCOUNT_SYNC_NETWORK', '无法读取 Sub2API 账户配置') from None

    def accounts(self):
        rows = []
        seen = set()
        expected_total = None
        page = 1
        while page <= 50:
            data = self.get('/accounts', {'page':page, 'page_size':100})
            entries = data if isinstance(data, list) else data.get('items', data.get('accounts'))
            if not isinstance(entries,list):
                raise ProbeError('ACCOUNT_SYNC_INCOMPLETE','账户列表结构不完整，未覆盖现有记录')
            total = data.get('total', len(entries)) if isinstance(data, dict) else len(entries)
            if type(total) is not int or total<0 or (expected_total is not None and expected_total!=total):
                raise ProbeError('ACCOUNT_SYNC_CHANGED','同步期间账户总数发生变化，等待重新同步')
            expected_total=total
            for a in entries:
                if type(a.get('id')) is not int or a['id'] in seen or not isinstance(a.get('name'),str):
                    raise ProbeError('ACCOUNT_SYNC_INCOMPLETE','账户标识重复或不完整，未覆盖现有记录')
                seen.add(a['id'])
                if scope_state(a) == 'unknown':
                    raise ProbeError('ACCOUNT_SCOPE_UNKNOWN', '分组信息不完整，未覆盖现有记录')
                if in_evaluation_scope(a):
                    rows.append(a)
            if len(seen)==total:
                return sorted(rows, key=lambda a: a['id'])
            if not entries or len(seen)>total or len(entries)<100:
                raise ProbeError('ACCOUNT_SYNC_INCOMPLETE','账户分页未读取完整，未覆盖现有记录')
            page += 1
        raise ProbeError('ACCOUNT_SYNC_LIMIT', '账户列表超过安全读取上限')

    def account_state(self, account_id):
        account = self.get('/accounts/' + str(int(account_id)))
        if account.get('platform') == 'openai' and account.get('type') == 'oauth':
            settings = self.get('/settings')
            advanced = self.get('/ops/advanced-settings')
            thresholds = settings.get('account_scheduling_thresholds')
            defaults = advanced.get('openai_account_quota_auto_pause')
            if not isinstance(thresholds, dict) or not isinstance(defaults, dict):
                raise ProbeError('ACCOUNT_QUOTA_UNKNOWN', '无法确认 OAuth 限额配置，本次未调用模型')
            account['_quota_policy'] = {
                'account_scheduling_threshold': thresholds.get('openai'),
                **{key: defaults.get(key) for key in ('default_threshold_5h', 'default_threshold_7d')}}
        return account

    def set_callable(self, account_id, enabled):
        if type(account_id) is not int or account_id <= 0 or type(enabled) is not bool:
            raise ValueError('INVALID_SCHEDULING_CONTROL')
        before = self.account_state(account_id)
        if not in_evaluation_scope(before) or before.get('status') not in ('active','inactive','error'):
            raise ProbeError('ROUTING_ACCOUNT_CHANGED', '账户已退出自动调度范围，未修改')
        if oauth_quota_state(before)['limited']:
            raise ProbeError(OAUTH_QUOTA_ERROR, 'OAuth 账号达到限额，保留 Sub2API 调度状态')
        changes = []
        if enabled and before['status']!='active':
            changes.append(('PUT','',{'status':'active'}))
        if before.get('schedulable') is not enabled:
            changes.append(('POST','/schedulable',{'schedulable':enabled}))
        try:
            with requests.Session() as session:
                session.trust_env = False
                for method, suffix, payload in changes:
                    response = session.request(method,self.base+'/api/v1/admin/accounts/'+str(account_id)+suffix,
                        headers={'x-api-key':self.key},json=payload,timeout=(12,30),allow_redirects=False)
                    if response.status_code != 200:
                        raise ProbeError('ROUTING_HTTP_'+str(response.status_code), 'Sub2API 调度更新失败')
                    body = response.json()
                    if body.get('code',0) != 0:
                        raise ProbeError('ROUTING_REJECTED', 'Sub2API 拒绝调度更新')
        except (requests.RequestException, ValueError):
            raise ProbeError('ROUTING_UNCONFIRMED', '调度更新结果未确认，等待核对') from None
        after = self.account_state(account_id)
        if after.get('schedulable') is not enabled or after.get('platform') != before['platform'] or (enabled and after.get('status')!='active'):
            raise ProbeError('ROUTING_READBACK_FAILED', '调度更新回读不一致')
        return after

    def account(self, account_id):
        a = self.account_state(account_id)
        if not in_evaluation_scope(a):
            raise ProbeError('ACCOUNT_EXCLUDED', '账户已不符合检测范围')
        bundle = self.get('/accounts/data', {'ids':str(int(account_id)), 'include_proxies':'true'})
        entries = bundle.get('accounts', [])
        if len(entries) != 1 or entries[0].get('name') != a['name'] or entries[0].get('platform') != a['platform']:
            raise ProbeError('ACCOUNT_IDENTITY_MISMATCH', '账户配置身份校验失败')
        entry = entries[0]
        a['credentials'] = entry['credentials']
        if entry.get('proxy_key'):
            matches = [p for p in bundle.get('proxies',[]) if p.get('proxy_key')==entry['proxy_key']]
            if len(matches)!=1:
                raise ProbeError('PROXY_IDENTITY_MISMATCH','账户代理配置校验失败')
            a['proxy'] = matches[0]
        return a

    def business_stability(self):
        """Optional gateway-native stability summary; disabled unless configured."""
        endpoint = (config.get('routing.stability_endpoint') or '').strip()
        if not endpoint:
            raise ProbeError('STABILITY_UNAVAILABLE', '未配置网关稳定性接口，保留原有优先级')
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(self.base + endpoint,headers={'x-api-key':self.key},
                    timeout=(12,15),allow_redirects=False)
                if response.status_code != 200:
                    raise ProbeError('STABILITY_HTTP_'+str(response.status_code),'业务稳定性汇总暂不可用，保留原优先级')
                if len(response.content)>256000:
                    raise ValueError('STABILITY_TOO_LARGE')
                return response.json()
        except (requests.RequestException,ValueError):
            raise ProbeError('STABILITY_UNAVAILABLE','业务稳定性汇总暂不可用，保留原优先级') from None

    def set_priority(self, account_id, priority):
        if type(account_id) is not int or account_id<=0 or type(priority) is not int or not 100<=priority<=100100:
            raise ValueError('INVALID_PRIORITY')
        before = self.account_state(account_id)
        if not in_evaluation_scope(before):
            raise ProbeError('PRIORITY_EXCLUDED','账户已退出管控范围，未修改')
        if oauth_quota_state(before)['limited']:
            raise ProbeError(OAUTH_QUOTA_ERROR,'OAuth 限额保护，保留优先级')
        if before.get('priority') == priority:
            return before
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.put(self.base+'/api/v1/admin/accounts/'+str(account_id),
                    headers={'x-api-key':self.key},json={'priority':priority},timeout=(12,30),allow_redirects=False)
                if response.status_code != 200 or response.json().get('code',0) != 0:
                    raise ProbeError('PRIORITY_REJECTED','优先级更新未确认，等待回读')
        except (requests.RequestException,ValueError):
            raise ProbeError('PRIORITY_UNCONFIRMED','优先级更新未确认，等待回读') from None
        after = self.account_state(account_id)
        if after.get('priority') != priority:
            raise ProbeError('PRIORITY_READBACK_FAILED','优先级回读不一致')
        return after


def proxy_url(proxy):
    if not proxy:
        return None
    proto = proxy.get('protocol', 'http')
    if proto == 'socks5':
        proto = 'socks5h'
    if proto not in ('http', 'https', 'socks5h'):
        raise ProbeError('PROXY_UNSUPPORTED', '账户代理协议不受支持')
    auth = ''
    if proxy.get('username'):
        auth = quote(proxy['username'], safe='') + ':' + quote(proxy.get('password', ''), safe='') + '@'
    return f"{proto}://{auth}{proxy['host']}:{int(proxy['port'])}"


def endpoint(base, suffix, version='v1'):
    parts = urlsplit(base)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ProbeError('UNSAFE_UPSTREAM_URL', '账户上游地址不符合 HTTPS 安全要求')
    path = parts.path.rstrip('/')
    if not path.endswith('/'+version):
        path += '/'+version
    return urlunsplit((parts.scheme, parts.netloc, path+'/'+suffix, '', ''))


def build_request(account, kind, *, prompt=None, instructions=None, request_id=None):
    """Build (url, headers, payload) for one probe request.

    Dispatch is by wire protocol rather than platform name, so any supplier that
    speaks one of the supported protocols works without touching this file.
    """
    prompt = PROMPTS[kind] if prompt is None else prompt
    instructions = INSTRUCTIONS[kind] if instructions is None else instructions
    request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
    instructions = request_instructions(instructions, request_id)
    creds = account.get('credentials') or {}
    extra = account.get('extra') or {}
    platform = account.get('platform', 'openai')
    spec = benchmark_spec(platform)
    model = spec.get('model')
    if not model:
        raise ProbeError('MODEL_NOT_CONFIGURED', '此平台尚未指定检测模型，本轮未调用')
    effort = spec.get('effort') or 'medium'
    protocol = spec.get('protocol') or config.protocol_for(platform)
    # Sub2API records whether an OpenAI-compatible upstream speaks the Responses
    # API. Honour that account flag so a chat-only relay still works.
    if protocol == 'openai_responses' and extra.get('openai_responses_supported') is False:
        protocol = 'openai_chat'
    mapped = (creds.get('model_mapping') or {}).get(model, model)
    if mapped != model:
        raise ProbeError('MODEL_MAPPING_MISMATCH', '此账户将指定模型映射为其他模型，本轮未调用')
    if platform != 'openai' and account.get('status') != 'active':
        raise ProbeError('ACCOUNT_INACTIVE', '此账户当前未启用，本轮未调用')
    auth_type = account.get('type')
    if protocol in ('openai_responses', 'xai_responses') and platform == 'openai' and auth_type == 'oauth':
        url, headers, payload = _openai_oauth_request(
            account, request_id, model, effort, prompt, instructions, extra, creds)
    elif protocol == 'anthropic_messages':
        url, headers, payload = _anthropic_request(
            account, kind, model, effort, prompt, instructions, creds, extra)
    elif protocol == 'gemini_generate':
        url, headers, payload = _gemini_request(
            account, model, effort, prompt, instructions, creds, extra)
    elif protocol == 'xai_responses':
        url, headers, payload = _xai_request(
            account, model, effort, prompt, instructions, creds, extra)
    elif protocol == 'openai_responses':
        url, headers, payload = _openai_responses_request(
            account, model, effort, prompt, instructions, creds, extra)
    elif protocol == 'openai_chat':
        url, headers, payload = _openai_chat_request(
            account, model, effort, prompt, instructions, creds, extra)
    else:
        raise ProbeError('PROTOCOL_UNSUPPORTED', '此账户的协议尚不支持独立检测：' + str(protocol))
    return url, {**headers, **fresh_headers(request_id)}, payload


def _bearer_or_key(account, creds):
    """Return the credential a supplier expects, honouring both auth styles."""
    auth_type = account.get('type')
    token = creds.get('access_token' if auth_type == 'oauth' else 'api_key')
    if auth_type not in ('apikey', 'oauth') or not token:
        raise ProbeError('CREDENTIAL_UNAVAILABLE', '账户没有可用的请求凭据')
    return auth_type, token


def _apply_header_overrides(headers, account, creds, allowed):
    overrides = (account.get('extra') or {}).get('header_overrides') or creds.get('header_overrides') or {}
    if not isinstance(overrides, dict):
        return headers
    for key, value in overrides.items():
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value:
            continue
        if key.lower() in allowed:
            headers[key] = value
    return headers


def _openai_oauth_request(account, request_id, model, effort, prompt, instructions, extra, creds):
    token = creds.get('access_token')
    if not token:
        raise ProbeError('MISSING_ACCESS_TOKEN', '账户没有可用的访问令牌')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'User-Agent': f'codex-tui/{CODEX_VERSION} (Ubuntu 22.4.0; x86_64) xterm-256color',
               'Originator': 'codex-tui', 'Version': CODEX_VERSION,
               'Authorization': 'Bearer ' + token,
               'OpenAI-Beta': 'responses=experimental',
               'session_id': request_id}
    if creds.get('chatgpt_account_id'):
        headers['ChatGPT-Account-Id'] = creds['chatgpt_account_id']
    payload = {'model': model, 'stream': True, 'store': False, 'instructions': instructions,
               'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}],
               'reasoning': {'effort': effort}}
    _apply_header_overrides(headers, account, creds, ('user-agent', 'originator', 'openai-beta'))
    return 'https://chatgpt.com/backend-api/codex/responses', headers, payload


def _openai_responses_request(account, model, effort, prompt, instructions, creds, extra):
    _auth_type, token = _bearer_or_key(account, creds)
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'Authorization': 'Bearer ' + token}
    url = endpoint(creds.get('base_url') or 'https://api.openai.com', 'responses')
    payload = {'model': model, 'stream': True, 'store': False, 'instructions': instructions,
               'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}],
               'reasoning': {'effort': effort}}
    _apply_header_overrides(headers, account, creds, ('user-agent', 'originator', 'openai-beta'))
    return url, headers, payload


def _openai_chat_request(account, model, effort, prompt, instructions, creds, extra):
    """Generic OpenAI-compatible Chat Completions call.

    This is the adapter that makes mainstream relays work unchanged: Moonshot,
    DeepSeek, Qwen, Volcengine, SiliconFlow, OpenRouter and anything else that
    exposes `/v1/chat/completions` against a base URL plus an API key.
    """
    _auth_type, token = _bearer_or_key(account, creds)
    # Official OpenAI stays the default so a stock account works without a
    # base_url, but any relay that sets one is used instead.
    base = creds.get('base_url') or 'https://api.openai.com'
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'Authorization': 'Bearer ' + token}
    payload = {'model': model, 'stream': True,
               'messages': [{'role': 'system', 'content': instructions},
                            {'role': 'user', 'content': prompt}]}
    if effort and effort != 'default':
        # Widely understood alias; suppliers that do not know it ignore it.
        payload['reasoning_effort'] = effort
    _apply_header_overrides(headers, account, creds, ('user-agent', 'originator', 'openai-beta'))
    return endpoint(base, 'chat/completions'), headers, payload


def _anthropic_request(account, kind, model, effort, prompt, instructions, creds, extra):
    auth_type, token = _bearer_or_key(account, creds)
    if auth_type != 'apikey':
        raise ProbeError('ACCOUNT_TYPE_UNSUPPORTED', '此平台的 OAuth 请求协议尚未配置，本轮未调用')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    headers.update(CLAUDE_HEADERS)
    headers.update({'x-api-key': token, 'anthropic-version': '2023-06-01'})
    if (extra or {}).get('anthropic_apikey_auth_scheme') == 'authorization_bearer':
        del headers['x-api-key']
        headers['Authorization'] = 'Bearer ' + token
    metadata = {'device_id': uuid.uuid4().hex + uuid.uuid4().hex,
                'account_uuid': '', 'session_id': str(uuid.uuid4())}
    payload = {'model': model, 'stream': True,
               'max_tokens': 32768 if kind == 'drawing' else 8192,
               'output_config': {'effort': effort},
               'metadata': {'user_id': json.dumps(metadata, separators=(',', ':'))},
               'system': [{'type': 'text', 'text': CLAUDE_IDENTITY},
                          {'type': 'text', 'text': instructions}],
               'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}]}
    url = endpoint(creds.get('base_url') or 'https://api.anthropic.com', 'messages') + '?beta=true'
    return url, headers, payload


def _gemini_request(account, model, effort, prompt, instructions, creds, extra):
    _auth_type, token = _bearer_or_key(account, creds)
    if not re.fullmatch(r'[a-zA-Z0-9._-]+', model):
        raise ProbeError('MODEL_NOT_CONFIGURED', 'Gemini 模型配置无效')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'x-goog-api-key': token}
    base = creds.get('base_url') or 'https://generativelanguage.googleapis.com'
    url = endpoint(base, 'models/' + model + ':streamGenerateContent', version='v1beta') + '?alt=sse'
    payload = {'systemInstruction': {'parts': [{'text': instructions}]},
               'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
               'generationConfig': {'maxOutputTokens': GEMINI_MAX_OUTPUT_TOKENS,
                                    'thinkingConfig': {'thinkingLevel': effort.upper()}}}
    return url, headers, payload


def _xai_request(account, model, effort, prompt, instructions, creds, extra):
    auth_type, token = _bearer_or_key(account, creds)
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'Authorization': 'Bearer ' + token}
    if auth_type == 'oauth':
        base = creds.get('base_url') or 'https://cli-chat-proxy.grok.com'
        url = endpoint(base, 'responses')
        if urlsplit(url).hostname != 'cli-chat-proxy.grok.com':
            raise ProbeError('UNSAFE_OAUTH_HOST', 'Grok OAuth 只允许已核实的官方网关')
        headers.update({'X-XAI-Token-Auth': 'xai-grok-cli', 'x-grok-client-version': GROK_VERSION,
                        'x-grok-client-identifier': 'grok-shell', 'X-Grok-Client-Mode': 'interactive',
                        'User-Agent': 'xai-grok-workspace/' + GROK_VERSION})
    else:
        base = creds.get('base_url') or 'https://api.x.ai'
        url = endpoint(base, 'responses')
    payload = {'model': model, 'stream': True, 'store': False, 'instructions': instructions,
               'input': [{'role': 'user', 'content': prompt}], 'reasoning': {'effort': effort}}
    return url, headers, payload


def request_instructions(instructions, request_id):
    """Prefix every request with a unique id so cached history cannot help."""
    return ('独立请求标识：' + request_id + '。此标识不是题目，不要在回答中复述。'
            '请独立完成本次请求，不引用其他对话。\n' + instructions)


def fresh_headers(request_id):
    """Headers that keep each probe out of any client or edge cache."""
    return {'X-Client-Request-Id': request_id,
            'Cache-Control': 'no-cache, no-store',
            'Pragma': 'no-cache'}


def extract_response(response):
    return ''.join(str(part.get('text','')) for item in response.get('output', [])
                   if item.get('type') == 'message' for part in item.get('content', [])
                   if part.get('type') == 'output_text')


def sse_lines(response, deadline, on_chunk=None, progress=None):
    """Bound bytes before buffering an SSE event, including a stream with no newline."""
    total = event_size = 0
    fragments = []
    for chunk in response.iter_content(1024):
        if chunk and on_chunk:on_chunk()
        total += len(chunk)
        if progress is not None:progress['received_bytes'] = total
        if time.monotonic() > deadline:
            raise ProbeError('REQUEST_TIMEOUT', '整次生成达到总时间预算，未将部分输出判为成功')
        if total > MAX_STREAM_BYTES:
            raise ProbeError('STREAM_TOO_LARGE', '流式响应累计超过 32 MB 安全上限')
        parts = chunk.split(b'\n')
        for index, part in enumerate(parts):
            event_size += len(part)
            if event_size > MAX_EVENT_BYTES:
                raise ProbeError('EVENT_TOO_LARGE', '单个流式事件超过 16 MB 安全上限')
            fragments.append(part)
            if index < len(parts)-1:
                yield b''.join(fragments).decode('utf-8', errors='replace').rstrip('\r')
                fragments = []
                event_size = 0
    if fragments:
        yield b''.join(fragments).decode('utf-8', errors='replace').rstrip('\r')


def check_output_size(text):
    if len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise ProbeError('RESPONSE_TOO_LARGE', '模型正文超过 8 MB 安全上限')
    return text


def parse_events(lines, deadline, on_text=None, on_usage=None, on_finish=None, progress=None):
    text = []
    completed = False
    finished_chat = False
    finished_anthropic = False
    actual_model = None
    usage = {}
    usage_platform = 'openai'
    size = output_size = 0
    def append_text(value):
        nonlocal output_size
        if value and on_text:on_text()
        output_size += len(value.encode('utf-8'))
        if progress is not None and value:progress['output_chars'] = progress.get('output_chars', 0)+len(value)
        text.append(value)
    for line in lines:
        if time.monotonic() > deadline:
            raise ProbeError('REQUEST_TIMEOUT', '整次生成达到总时间预算，未将部分输出判为成功')
        line_size = len(line.encode('utf-8'))
        size += line_size
        if line_size > MAX_EVENT_BYTES:
            raise ProbeError('EVENT_TOO_LARGE', '单个流式事件超过 16 MB 安全上限')
        if size > MAX_STREAM_BYTES:
            raise ProbeError('STREAM_TOO_LARGE', '流式响应累计超过 32 MB 安全上限')
        if not line.startswith('data:'):
            continue
        raw = line[5:].strip()
        if raw == '[DONE]':
            if finished_chat:
                completed = True
            break
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            raise ProbeError('INVALID_RESPONSE', '流式响应事件结构无法解析')
        if progress is not None and str(event.get('type', '')).startswith('response.reasoning'):
            progress['reasoning_events'] = progress.get('reasoning_events', 0)+1
        if event.get('type') in ('message_start','message_delta'):
            usage_platform = 'anthropic'
            usage.update((event.get('message') or {}).get('usage') or {})
        if event.get('usageMetadata'):
            usage_platform = 'gemini'
            usage.update(event['usageMetadata'])
        else:
            usage.update(event.get('usage') or (event.get('response') or {}).get('usage') or {})
        if on_usage and usage:
            on_usage(normalize_usage(usage, usage_platform))
        if event.get('error') or event.get('type') in ('error','response.failed','response.incomplete'):
            err = event.get('error') or event.get('response',{}).get('error') or {}
            raise ProbeError('UPSTREAM_RESPONSE_ERROR', str(err.get('message') if isinstance(err,dict) else err) or '上游返回失败或不完整响应')
        if event.get('type') == 'response.output_text.delta':
            append_text(event.get('delta',''))
        if event.get('type') == 'response.completed':
            response = event.get('response', {})
            if response.get('status') not in ('completed', None):
                raise ProbeError('INCOMPLETE_RESPONSE', '响应未完整生成')
            canonical = extract_response(response)
            if canonical:
                if on_text:on_text()
                text = [check_output_size(canonical)]
            actual_model = response.get('model')
            usage = response.get('usage') or {}
            completed = True
            break
        if event.get('type') == 'message_start':
            message = event.get('message') or {}
            actual_model = message.get('model')
            usage.update(message.get('usage') or {})
        if event.get('type') == 'content_block_delta':
            delta = event.get('delta') or {}
            if delta.get('type') == 'text_delta':
                append_text(delta.get('text', ''))
        if event.get('type') == 'message_delta':
            reason = (event.get('delta') or {}).get('stop_reason')
            if reason and reason != 'end_turn':
                raise ProbeError('INCOMPLETE_RESPONSE', '上游提前结束：'+str(reason))
            finished_anthropic |= reason == 'end_turn'
            usage.update(event.get('usage') or {})
        if event.get('type') == 'message_stop':
            completed = finished_anthropic
            break
        if event.get('promptFeedback', {}).get('blockReason'):
            raise ProbeError('UPSTREAM_RESPONSE_ERROR', '上游拦截了此次请求')
        if 'candidates' in event:
            actual_model = event.get('modelVersion') or actual_model
            for candidate in event['candidates'][:1]:
                for part in candidate.get('content', {}).get('parts', []):
                    if not part.get('thought'):
                        append_text(part.get('text', ''))
                reason = candidate.get('finishReason')
                if reason and on_finish:on_finish(reason)
                if reason == 'MAX_TOKENS':
                    raise ProbeError('INCOMPLETE_RESPONSE', '生成达到本次 token 上限（MAX_TOKENS），回答未完整结束')
                if reason and reason != 'STOP':
                    raise ProbeError('INCOMPLETE_RESPONSE', '上游提前结束：'+str(reason))
                completed |= reason == 'STOP'
        if 'choices' in event:
            actual_model = event.get('model') or actual_model
            for choice in event['choices']:
                append_text((choice.get('delta') or {}).get('content') or '')
                reason = choice.get('finish_reason')
                if reason and reason != 'stop':
                    raise ProbeError('INCOMPLETE_RESPONSE', '上游提前结束：'+str(reason))
                finished_chat |= reason == 'stop'
            usage = event.get('usage') or usage
        if output_size > MAX_OUTPUT_BYTES:
            raise ProbeError('RESPONSE_TOO_LARGE', '模型正文超过 8 MB 安全上限')
    if not completed:
        raise ProbeError('STREAM_INTERRUPTED', '响应流在完成标记之前中断')
    result = check_output_size(''.join(text).strip())
    if not result:
        raise ProbeError('EMPTY_RESPONSE', '上游返回空回答')
    return {'text':result, 'actual_model':actual_model, 'tokens':normalize_usage(usage, usage_platform)}


def probe(account, kind, timeout=REQUEST_TOTAL_SECONDS, *, prompt=None, instructions=None, request_id=None):
    url, headers, payload = build_request(account, kind, prompt=prompt, instructions=instructions,request_id=request_id)
    secrets = list(secret_values(account))
    proxy = proxy_url(account.get('proxy'))
    start = time.monotonic()
    deadline = start + timeout
    gemini_config = payload.get('generationConfig', {}) if account.get('platform') == 'gemini' else {}
    thinking_level = gemini_config.get('thinkingConfig', {}).get('thinkingLevel')
    idle = min(GEMINI_HIGH_IDLE_SECONDS if thinking_level == 'HIGH' else STREAM_IDLE_SECONDS, timeout)
    timings = {'total_budget_seconds':round(timeout,2),'idle_timeout_seconds':idle,
               'request_id':headers['X-Client-Request-Id'],'fresh_context':True,'explicit_cache':False}
    if gemini_config:
        timings.update(max_output_tokens=gemini_config['maxOutputTokens'],thinking_level=thinking_level)
    tokens = {}
    # Counters persist on every receipt so a cut-off stream can be told apart from silence.
    progress = {'received_bytes':0,'output_chars':0,'reasoning_events':0}
    def remember_usage(value):
        nonlocal tokens
        tokens = value
    def mark(name):
        if name not in timings:timings[name]=round(time.monotonic()-start,3)
    def remember_finish(reason):
        timings['finish_reason'] = redact(str(reason), secrets, True)[:80]
    def receipt():
        return {**timings,**progress,'elapsed_seconds':round(time.monotonic()-start,3)}
    try:
        with requests.Session() as s:
            s.trust_env = False
            if proxy:
                s.proxies = {'http':proxy, 'https':proxy}
            # Idle timeout resets on incoming data; the generation budget is separate.
            timings['request_sent'] = True
            with s.post(url, headers=headers, json=payload, stream=True, timeout=(min(12,timeout),idle), allow_redirects=False) as response:
                mark('headers_seconds')
                response.encoding = 'utf-8'
                if response.status_code != 200:
                    raw = next(response.iter_content(8192), b'').decode(errors='replace')
                    try:
                        body = json.loads(raw)
                        err = body.get('error', body)
                        message = err.get('message', str(err)) if isinstance(err,dict) else str(err)
                    except (ValueError, TypeError):
                        message = '上游 HTTP 请求失败' if '<html' in raw.lower() else raw[:1600]
                    raise ProbeError('HTTP_'+str(response.status_code), 'HTTP '+str(response.status_code)+'：'+redact(message, secrets, True))
                if 'application/json' in response.headers.get('Content-Type', ''):
                    chunks = []
                    count = 0
                    for chunk in response.iter_content(8192):
                        if chunk:mark('first_data_seconds')
                        count += len(chunk)
                        if count > MAX_STREAM_BYTES:
                            raise ProbeError('STREAM_TOO_LARGE', '响应封装超过 32 MB 安全上限')
                        if time.monotonic() > deadline:
                            raise ProbeError('REQUEST_TIMEOUT', '整次生成达到总时间预算，未将部分输出判为成功')
                        chunks.append(chunk)
                    body = json.loads(b''.join(chunks))
                    if not isinstance(body, dict):
                        raise ProbeError('INVALID_RESPONSE', '上游响应结构无法解析')
                    remember_usage(normalize_usage(body.get('usageMetadata') or body.get('usage') or {}, account.get('platform','openai')))
                    if body.get('error') or body.get('status') not in (None,'completed'):
                        raise ProbeError('UPSTREAM_RESPONSE_ERROR', '上游返回失败或不完整响应')
                    text = extract_response(body)
                    if not text and body.get('choices'):
                        choice = body['choices'][0]
                        if choice.get('finish_reason') != 'stop':
                            raise ProbeError('INCOMPLETE_RESPONSE', '上游提前结束')
                        text = choice.get('message',{}).get('content','')
                    if not text and body.get('type') == 'message':
                        if body.get('stop_reason') != 'end_turn':
                            raise ProbeError('INCOMPLETE_RESPONSE', '上游提前结束')
                        text = ''.join(p.get('text', '') for p in body.get('content', []) if p.get('type')=='text')
                    if not text and body.get('candidates'):
                        native = parse_events(['data: '+json.dumps(body)], deadline,
                                              lambda:mark('first_text_seconds'),remember_usage,remember_finish)
                        text = native['text']
                    if not text:
                        raise ProbeError('EMPTY_RESPONSE','上游返回空回答')
                    mark('first_text_seconds')
                    result = {'text':check_output_size(text),'actual_model':body.get('model') or body.get('modelVersion'),'tokens':tokens}
                else:
                    result = parse_events(sse_lines(response, deadline, lambda:mark('first_data_seconds'), progress),
                                          deadline, lambda:mark('first_text_seconds'), remember_usage, remember_finish, progress)
                result['text'] = redact(result['text'], secrets)
                result['actual_model'] = redact(result['actual_model'] or '')[:100]
                result['timings'] = receipt()
                return result
    except ProbeError as exc:
        code, message = exc.code, exc.message
        if gemini_config and timings.get('finish_reason') == 'MAX_TOKENS':
            message = '生成达到本次 token 上限（MAX_TOKENS）：预算 '+str(timings['max_output_tokens'])
            if 'output_tokens' in tokens:
                message += '，已生成 '+str(tokens['output_tokens'])
            if 'reasoning_tokens' in tokens:
                message += '（含思考 '+str(tokens['reasoning_tokens'])+'）'
            message += '；回答未完整结束，未判为答错或通过'
        elif code == 'REQUEST_TIMEOUT' and 'first_data_seconds' in timings:
            # The stream was alive and the client wall ended it; that is not an upstream outage.
            code = 'GENERATION_BUDGET_EXCEEDED'
            message = budget_exceeded_message(time.monotonic()-start, timeout, timings, progress)
        raise ProbeError(code, redact(message, secrets, True), receipt(), tokens) from None
    except requests.ConnectTimeout:
        raise ProbeError('CONNECT_TIMEOUT','建立上游连接超时，连接上限 '+str(min(12,timeout))+' 秒',receipt(),tokens) from None
    except requests.RequestException as exc:
        if isinstance(exc,requests.Timeout) or any(isinstance(arg,ReadTimeoutError) for arg in exc.args):
            started = 'first_data_seconds' in timings
            code = 'STREAM_IDLE_TIMEOUT' if started else 'FIRST_RESPONSE_TIMEOUT'
            message = ('响应流连续无数据超过 ' if started else '等待首批响应数据超时，读取上限 ')+str(idle)+' 秒'
            raise ProbeError(code,message,receipt(),tokens) from None
        raise ProbeError('NETWORK_ERROR','账户上游连接失败或响应流中断',receipt(),tokens) from None
    except (ValueError, TypeError, KeyError):
        raise ProbeError('INVALID_RESPONSE','上游响应格式无法解析',receipt(),tokens) from None
