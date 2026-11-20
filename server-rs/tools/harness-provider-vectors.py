#!/usr/bin/env python3
"""Record the Python Host's rules for a harness's model providers (OpenCode's sign-ins).

The reference's own module and route handlers run in an isolated process: OpenCode's
stores are made-up files under a scratch directory, the remembered sign-in methods live in
a dictionary, the clock is fixed, and a sign-in that would start `opencode serve` is given a
stand-in that fails at once, so nothing is ever started and nothing leaves the machine.
Every key in here is made up.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/harness-provider-vectors.json'
NOW_MS = 1789000000000
DIRECTORY = {'directory': True}
FAKE_KEY = 'sk-fixture-' + 'k' * 24


def text(value):
    return json.dumps(value)


def record(root):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(root)
    replay.loopback_only()
    os.environ['HOME'] = str(root / 'home')
    for name in [n for n in os.environ if n.startswith('FIXTURE_')]:
        del os.environ[name]
    os.environ['FIXTURE_PROVIDER_KEY'] = 'present'
    os.environ['FIXTURE_EMPTY_KEY'] = ''
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import harness_providers as hp
    from lib.routes.providers import ProviderRoutes

    settings = {}
    executable = ['/fixture/opencode']
    hp.settings_store.get_text = lambda key: settings.get(key)
    hp.settings_store.set_text = lambda key, value: settings.__setitem__(key, value)
    hp._executable = lambda: executable[0]
    hp._now_ms = lambda: NOW_MS
    hp._credentials_changed = lambda: None
    hp.refresh_methods_in_background = lambda **_: None
    hp.log = hp.log_exception = lambda *_args, **_kwargs: None
    import lib.routes.providers as provider_routes
    provider_routes.log_exception = hp.log_exception

    class NoServe:
        """What starts no OpenCode: the sign-in fails as one whose server did not start."""
        def __init__(self, _executable):
            raise hp.ProviderConflict('OpenCode did not start')

    hp._ServeProcess = NoServe

    def raised(run):
        try:
            return {'ok': run()}
        except hp.ProviderError as exc:
            return {'bad': str(exc)}
        except hp.ProviderConflict as exc:
            return {'conflict': str(exc)}
        except Exception as exc:  # noqa: BLE001 - what the reference does not survive
            return {'raises': type(exc).__name__}

    # --- the pure rules --------------------------------------------------------
    o = chr
    asked_harness = [None, '', '  ', 'opencode', 'OpenCode', ' deepseek ', 'DEEPSEEK', 'deep-seek',
                     'open-code', 'opencode-ai', 'OPENCODE-AI', 'claude', 'codex', 'muse', 'agy',
                     'antigravity', 'grok', 'nothing', 'opencode\n', o(0x1c) + 'opencode',
                     'open code', 'opencode-a' + o(0x130), o(0x212a) + 'ode', 0, 0.0, False, [], {},
                     5, True, ['opencode'], {'a': 1}, 'deepseek' + o(0x3000)]
    harnesses = [[value, raised(lambda value=value: hp.harness_id(value))] for value in asked_harness]

    asked_provider = [None, '', ' a ', 'A', 'a' * 100, 'a' * 101, '-a', 'a-', '9z', 'a.b_c-d',
                      'a/b', 'a b', 5, 0, True, False, 1.5, 1e20, -3, [], ['a'], {}, {'a': 1},
                      o(0x212a), o(0x130), o(0x1c) + 'ab', 'ab\n', 'ab' + o(0), 'Open' + o(0xdf),
                      '_a', '.a', 'OPENROUTER', ' fireworks-ai ']
    providers = [[value, raised(lambda value=value: hp._provider_id(value))]
                 for value in asked_provider]

    secrets = []
    for value, limit, what in [
            (None, 4096, 'API key'), (5, 4096, 'API key'), ('', 4096, 'API key'),
            ('   ', 4096, 'API key'), (' key ', 4096, 'API key'), ('a' * 4096, 4096, 'API key'),
            ('a' * 4097, 4096, 'API key'), (' ' + 'a' * 4096 + ' ', 4096, 'API key'),
            (o(0xe9) * 4096, 4096, 'API key'), (o(0xe9) * 4097, 4096, 'API key'),
            ('a\nb', 4096, 'API key'), ('a\rb', 4096, 'API key'), ('a' + o(0) + 'b', 4096, 'API key'),
            ('\nab\n', 4096, 'API key'), (o(0x1c) + 'ab', 4096, 'API key'), (['k'], 4096, 'API key'),
            (True, 512, 'Authorization code'), ('c' * 512, 512, 'Authorization code'),
            ('c' * 513, 512, 'Authorization code'), ('', 512, 'Authorization code'),
            ('a\tb', 512, 'Authorization code')]:
        secrets.append([value, limit, what,
                        raised(lambda v=value, n=limit, w=what: hp._clean_secret(v, limit=n, what=w))])

    def users_codes(sample):
        found = hp._USER_CODE.search(sample[:500])
        return found.group(0) if found else ''
    code_samples = ['Enter ABCD-EFGH', 'ABCDE-FGHIJ', 'ABCDEF-GHIJ', 'xABCD-EFGH', '1234-5678',
                    'ABCD-EFGHI', 'ABCD-EFGHIJ', 'abcd-efgh', 'AB-CD', 'ABCD-EFGH-IJKL',
                    o(0xc4) + 'BCD-EFGH', 'ABC-DEFG', 'code: ZZ99-8YYY.', 'a' * 497 + 'ABCD-EFGH',
                    'a' * 491 + 'ABCD-EFGH', 'ABCD--EFGH', 'ABCD-EFGH' + o(0xc4), 'ABCDE-FGHI',
                    'AAAAAAAAAA-ABCD-EFGH', '', 'ABCD-' + o(0xff11) * 4, 'xx ABCD-EFGH yy QRST-UVWX']
    user_codes = [[sample, users_codes(sample)] for sample in code_samples]

    error_samples = ['sk-abcdefgh', 'sk-abcdefg', 'SK-ABCDEFGHIJ more', 'Bearer abc def',
                     'bearer\t\tx y', 'bearer', 'auth: bearer ', '"key": "secret" end',
                     '"token" :  "x\\"y"', '"access":"a","refresh" : "b"', '"refresh": 5',
                     '"Key": "a"', 'x' * 400, 'sk-' + o(0x212a) * 8, 'bearer' + o(0x3000) + 'x',
                     'bearer' + o(0x1c) + 'x', '"key"' + o(0x1f) + ':"v"', 'sk-' + o(0x17f) * 8,
                     'error ' * 30 + 'sk-abcdefgh12345678', 'sk-ab_cd-ef01', '"secret": "kept"',
                     'POST /provider/x/oauth/authorize: HTTP 500 {"error":"bad"}', '']
    bounded = []
    for sample in error_samples:
        bounded.append([sample, hp._bounded_error(Exception(sample))])

    tasks = []
    for provider, status, started_at, label, authorization, error, submitted in [
            ('openai', 'idle', 0, '', None, '', False),
            ('openai', 'running', NOW_MS, 'ChatGPT Pro', None, '', False),
            ('openai', 'running', NOW_MS, 'Headless', {'method': 'code', 'url': 'https://example.invalid/a',
                                                      'instructions': 'Enter ABCD-EFGH'}, '', False),
            ('openai', 'running', NOW_MS, 'Headless', {'method': 'code'}, '', True),
            ('openai', 'running', NOW_MS, 'Device', {'method': 'auto', 'url': 'u' * 2100,
                                                    'instructions': 'i' * 600}, '', False),
            ('openai', 'complete', NOW_MS, 'Device', {'method': 'auto'}, '', True),
            ('openai', 'failed', NOW_MS, 'Device', {'method': 'code'}, 'e' * 600, True),
            ('github-copilot', 'running', 5, '', {'method': 5, 'url': 7, 'instructions': 3}, '', False),
            ('x', 'running', NOW_MS, '', {'method': '', 'url': '', 'instructions': ''}, '', False),
            ('x', 'running', NOW_MS, '', {'method': None, 'url': None}, '', False),
            ('x', 'running', NOW_MS, '', {'method': 'Code'}, '', False),
            ('x', 'failed', 0, '', {'instructions': 'Use ' + 'a' * 495 + 'ABCD-EFGH'}, 'x', False)]:
        tasks.append([provider, status, started_at, label, authorization, error, submitted,
                      hp._task_value(provider, status, started_at=started_at, method_label=label,
                                     authorization=authorization, error=error,
                                     code_submitted=submitted)])

    # How OpenCode's answer to the authorize call is taken (R156): a stand-in server answers
    # it, and whether the sign-in is left running with its server open is recorded. Only
    # answers that are not "auto" are given, so no thread of the reference's answers later.
    authorize = []
    for answer in [None, {}, [], 0, '', False, 'plain text', ['x'], 5, True,
                   {'method': 'code', 'url': 'https://example.invalid/c', 'instructions': 'Enter ABCD-EFGH'},
                   {'method': 'something'}, {'url': 7}]:
        hp.reset_for_tests()
        settings.clear()
        closed = []

        class AnsweringClient:
            def request(self, *_args, answer=answer, **_kwargs):
                return answer

        class AnsweringServe:
            client = AnsweringClient()

            def close(self):
                closed.append(True)

        try:
            out = {'answer': hp.start_oauth('opencode', 'openai', 1, None,
                                            serve_factory=lambda _e: AnsweringServe())}
        except Exception as exc:  # noqa: BLE001 - the reference's failure (R156)
            out = {'raises': type(exc).__name__}
        out['task'] = dict(hp._tasks.get('openai') or {})
        out['server_ended'] = bool(closed)
        authorize.append([answer, out])
        hp.reset_for_tests()

    raw_methods = [
        None, {}, {'openai': []}, {'openai': 'oauth'},
        {'openai': [{'type': 'oauth', 'label': 'ChatGPT Pro/Plus (browser)'},
                    {'type': 'oauth', 'label': 'ChatGPT Pro/Plus (headless)',
                     'prompts': [{'type': 'text', 'key': 'org'}, 'loose', 5]},
                    {'type': 'api', 'label': 'Manually enter API Key'}]},
        {'github-copilot': [{'type': 'oauth', 'label': 'Login with GitHub Copilot'}],
         'azure': [{'type': 'oauth', 'label': 'Login with Azure CLI'}],
         'google': [{'type': 'oauth', 'label': 'External BROWSER sign-in'}]},
        {'x': ['no', {'type': 'oauth'}, {'type': 'oauth', 'label': None, 'prompts': None},
               {'type': 'oauth', 'label': 5, 'prompts': 'abc'},
               {'type': 'oauth', 'label': 'Dict prompts', 'prompts': {'a': 1}},
               {'type': 'OAUTH', 'label': 'Upper type'}]},
        {'x': [{'type': 'oauth', 'label': 'Number prompts', 'prompts': 3}]},
        {'x': [{'type': 'oauth', 'label': 'True prompts', 'prompts': True}]},
        {'x': [{'type': 'oauth', 'label': 'False prompts', 'prompts': False}]},
        {'x': [{'type': 'oauth', 'label': 'Br' + o(0xf6) + 'wser'}], 'y': 7},
    ]
    methods = [[raw, raised(lambda raw=raw: hp.usable_methods(raw))] for raw in raw_methods]

    remembered_texts = [None, '', 'not json', '[]', '{}', '{"methods": []}',
                        '{"methods": {}}',
                        '{"methods": {"openai": [{"index": 1, "type": "oauth", "label": "H", "prompts": []}]}, "version": "1.2", "at": 1788999000.5}',
                        '{"methods": {}, "version": 5, "at": "7"}',
                        '{"methods": {}, "version": null, "at": "x"}',
                        '{"methods": {}, "at": true}', '{"methods": {}, "at": []}',
                        '{"methods": {}, "at": null}', '{"methods": {}, "at": 0}',
                        '{"methods": {}, "at": " 12.5 "}',
                        '{"methods": [], "version": "1.0", "at": 5}']
    remembered = []
    for value in remembered_texts:
        settings[hp._METHODS_KEY] = value
        remembered.append([value, raised(lambda: list(hp._remembered_methods()))])
    settings.clear()

    # --- the routes, with OpenCode's stores as files ------------------------------
    def setup(case):
        for path in (hp.auth_path().parent, hp.catalogue_path().parent):
            if path.exists():
                path.chmod(0o700)
                shutil.rmtree(path)
        if case.get('catalogue') is not None:
            hp.catalogue_path().parent.mkdir(parents=True, exist_ok=True)
            hp.catalogue_path().write_text(case['catalogue'], encoding='utf-8')
        auth = case.get('auth')
        if auth == DIRECTORY:
            hp.auth_path().mkdir(parents=True)
        elif auth is not None:
            hp.auth_path().parent.mkdir(parents=True, exist_ok=True)
            hp.auth_path().write_text(auth, encoding='utf-8')
        if case.get('locked'):
            hp.auth_path().parent.mkdir(parents=True, exist_ok=True)
            hp.auth_path().parent.chmod(0o500)
        settings.clear()
        if case.get('remembered') is not None:
            settings[hp._METHODS_KEY] = case['remembered']
        executable[0] = '/fixture/opencode' if case.get('installed', True) else None
        hp._tasks.clear()
        hp._tasks.update(case.get('tasks') or {})

    def call(name, body=None, query=None):
        answers = []
        handler = SimpleNamespace()
        handler._read_json = lambda: body
        handler._query = lambda: {k: [v] for k, v in (query or {}).items()}
        handler._json_error = lambda code, why: answers.append([code, {'error': why}])
        handler._json_ok = lambda result: answers.append([200, result])
        handler._json = lambda code, result: answers.append([code, result])
        handler._harness_provider_call = (
            lambda action: ProviderRoutes._harness_provider_call(handler, action))
        try:
            getattr(ProviderRoutes, name)(handler)
        except Exception as exc:  # noqa: BLE001 - the reference drops the connection
            return {'raises': type(exc).__name__}
        return answers[0]

    def after():
        path = hp.auth_path()
        if not path.is_file():
            return None
        return {'text': path.read_text(encoding='utf-8'), 'mode': path.stat().st_mode & 0o777,
                'folder': path.parent.stat().st_mode & 0o777}

    routes = []
    texts = {}

    def route(case, name, body=None, query=None):
        setup(case)
        answer = call(name, body, query)
        stored = after()
        if case.get('locked'):
            hp.auth_path().parent.chmod(0o700)
        kept = dict(case)
        for field in ('catalogue', 'auth', 'remembered'):
            value = kept.get(field)
            if isinstance(value, str):
                if value not in texts:
                    texts[value] = 'text-%d' % len(texts)
                kept[field] = {'text': texts[value]}
        routes.append(dict(kept, route=name, body=body, query=query, answer=answer, store=stored))

    n = lambda value: o(value)  # noqa: E731 - a short name for a sign by its number
    catalogue = {
        'openrouter': {'name': 'OpenRouter', 'env': ['FIXTURE_ABSENT_KEY'], 'doc': 'https://example.invalid/or',
                       'models': {'a/deepseek-r9': {'name': 'DeepSeek R9'}, 'b/other': {}}},
        'zeta': {'name': 'Zeta', 'env': ['FIXTURE_PROVIDER_KEY'], 'models': {'z': {}}},
        'zeta-lower': {'name': 'zeta', 'models': {}},
        'alpha': {'name': 'alpha', 'models': {'DeepSeek-V9': {'name': 'v9', 'variants': {'high': {}, 'max': {}, 'odd': {}}}}},
        'alpha-upper': {'name': 'Alpha', 'models': {'m': {'variants': []}}},
        'apfel': {'name': n(0xc4) + 'pfel', 'models': {}},
        'apfel-plain': {'name': 'apfel', 'models': {}},
        'omega': {'name': n(0x3a9) + 'mega', 'models': {}},
        'omega-small': {'name': n(0x3c9) + 'mega', 'models': {}},
        'inci': {'name': n(0x130) + 'nci', 'models': {}},
        'eszett': {'name': n(0xdf) + '-labs', 'models': {}},
        'double-s': {'name': 'SS-labs', 'models': {}},
        'sigma': {'name': n(0x3a3) + n(0x391) + n(0x3a3), 'models': {}},
        'sigma-small': {'name': n(0x3c3) + n(0x3b1) + n(0x3c2), 'models': {}},
        'eclair': {'name': n(0xc9) + 'clair', 'models': {}},
        'eclair-plain': {'name': 'eclair', 'models': {}},
        'dz': {'name': n(0x1c5) + 'emal', 'models': {}},
        'ligature': {'name': n(0xfb03) + 'x', 'models': {}},
        'kelvin': {'name': n(0x212a) + 'elvin', 'models': {}},
        'nameless': {'models': {'m1': 'not a dict'}},
        'empty-name': {'name': '', 'doc': None, 'env': 'FIXTURE_PROVIDER_KEY', 'models': []},
        'numbered': {'name': 7, 'doc': 8, 'env': [5, None, '', 'FIXTURE_EMPTY_KEY'], 'models': 'x'},
        'groq': {'name': 'Groq', 'models': {'llama': {}}},
        'anthropic': {'name': 'Anthropic', 'models': {'claude': {}}},
        'deepseek': {'name': 'DeepSeek', 'env': ['FIXTURE_PROVIDER_KEY'],
                     'models': {'deepseek-chat': {'name': 'DeepSeek Chat', 'variants': {'none': 1, 'low': 2, 'xhigh': 3}},
                                'deepseek-reasoner': {'name': 'deepseek reasoner'},
                                'DeepSeek-Coder': {'name': 'DeepSeek Coder', 'variants': 'high'}}},
        'fireworks-ai': {'name': 'Fireworks AI', 'models': {'accounts/fireworks/models/deepseek-v4': {'name': 'DS v4'},
                                                           'accounts/fireworks/models/kimi': {'name': 'Kimi'}}},
        'not-a-dict': 'skip me',
    }
    auth = {'zeta-lower': {'type': 'oauth', 'refresh': 'made-up', 'expires': 1789000000000},
            'groq': {'type': 'api', 'key': FAKE_KEY},
            'custom-one': {'type': 'wellknown', 'key': 'made-up', 'token': 'made-up'},
            'Bad Custom': {'type': 'api', 'key': 'made-up'},
            'typeless': {'key': 'made-up'},
            'numbered-type': {'type': 5},
            'not-an-object': 'made-up',
            'fireworks-ai': {'type': 'api', 'key': 'made-up', 'unicode': n(0xe9) + n(0x1f600)}}
    remembered_methods = text({'methods': {'openai': [{'index': 1, 'type': 'oauth', 'label': 'Headless', 'prompts': []}],
                                           'groq': [{'index': 0, 'type': 'oauth', 'label': 'Device', 'prompts': [{'key': 'a'}]}]},
                               'version': '1.0', 'at': 1788999999.0})
    full = {'catalogue': text(catalogue), 'auth': text(auth), 'remembered': remembered_methods}
    for case in [{}, {'installed': False}, {'catalogue': '[]'}, {'catalogue': 'not json'},
                 {'auth': 'not json', 'catalogue': text(catalogue)},
                 {'auth': '[1]', 'catalogue': text(catalogue)},
                 {'auth': DIRECTORY, 'catalogue': text(catalogue)},
                 full, dict(full, installed=False), dict(full, remembered='not json'),
                 dict(full, tasks={'groq': hp._task_value('groq', 'running', started_at=NOW_MS,
                                                          method_label='Device')})]:
        route(case, '_handle_harness_providers')
    for query in [{'harness': 'opencode'}, {'harness': 'DeepSeek'}, {'harness': 'claude'},
                  {'harness': ' '}, {'harness': 'deepseek'}]:
        route(full, '_handle_harness_providers', query=query)
    for query in [{}, {'provider': 'deepseek'}, {'harness': 'deepseek', 'provider': 'deepseek'},
                  {'harness': 'deepseek', 'provider': 'fireworks-ai'},
                  {'harness': 'opencode', 'provider': 'fireworks-ai'},
                  {'provider': 'alpha'}, {'harness': 'deepseek', 'provider': 'alpha'},
                  {'provider': 'nameless'}, {'provider': 'numbered'}, {'provider': 'empty-name'},
                  {'provider': 'not-a-dict'}, {'provider': 'missing'}, {'provider': 'Bad Id'},
                  {'harness': 'muse', 'provider': 'alpha'}, {'provider': ' ALPHA '},
                  {'provider': 'apfel'}, {'provider': 'openrouter'}]:
        route(full, '_handle_harness_provider_models', query=query)
    route({'catalogue': None}, '_handle_harness_provider_models', query={'provider': 'alpha'})

    login, code, logout = ('_handle_harness_provider_login', '_handle_harness_provider_login_code',
                           '_handle_harness_provider_logout')
    stores = [{'catalogue': text(catalogue)}, full, dict(full, auth='{}'),
              dict(full, auth='not json'), dict(full, auth='[]'), dict(full, auth=DIRECTORY),
              dict(full, auth=None, locked=True), dict(full, auth=text({'groq': {'type': 'api', 'key': 'x'}}), locked=True)]
    for case in stores:
        for body in [{'harness': 'opencode', 'provider': 'groq', 'key': ' ' + FAKE_KEY + ' '},
                     {'provider': 'groq', 'key': '  '},
                     {'provider': 'kelvin', 'key': 'made-up-' + n(0xe9)},
                     {'harness': 'deepseek', 'provider': 'brand-new', 'key': 'made-up'}]:
            route(case, login, body=body)
    for body in [None, {}, {'key': 'k'}, {'provider': 'groq', 'key': None},
                 {'provider': 'groq', 'key': ''}, {'provider': 'groq', 'key': 'a\nb'},
                 {'provider': 'groq', 'key': 'k' * 4097}, {'harness': 'claude', 'provider': 'groq', 'key': 'k'},
                 {'harness': 5, 'provider': 'groq', 'key': 'k'}, {'harness': 0, 'provider': 'groq', 'key': 'k'},
                 {'provider': 'Groq', 'key': 'k'}, {'provider': 'gr oq', 'key': 'k'},
                 {'provider': 'groq', 'key': 'k', 'method': 1}]:
        route(full, login, body=body)
    for case in stores:
        for body in [{'harness': 'opencode', 'provider': 'groq'}, {'provider': 'zeta-lower'},
                     {'provider': 'zeta'}, {'provider': 'missing'}, {'provider': 'custom-one'},
                     {'provider': 'not-an-object'}, {'provider': 'deepseek'}, {'provider': 'numbered'}]:
            route(case, logout, body=body)
    for body in [None, {}, {'harness': 'claude', 'provider': 'groq'}, {'provider': 7},
                 {'harness': True, 'provider': 'groq'}]:
        route(full, logout, body=body)
    oauth_bodies = [
        {'provider': 'openai', 'method': 1}, {'provider': 'openai', 'method': 0},
        {'provider': 'openai', 'method': -1}, {'provider': 'openai', 'method': True},
        {'provider': 'openai', 'method': 1.0}, {'provider': 'openai', 'method': '1'},
        {'provider': 'openai'}, {'provider': 'openai', 'method': None},
        {'provider': 'groq', 'method': 0, 'inputs': {'a': 'b'}},
        {'provider': 'groq', 'method': 0, 'inputs': {'a': 'b' * 500}},
        {'provider': 'groq', 'method': 0, 'inputs': {'a': 'b' * 501}},
        {'provider': 'groq', 'method': 0, 'inputs': {'a': n(0xe9) * 500}},
        {'provider': 'groq', 'method': 0, 'inputs': {'a': 5}},
        {'provider': 'groq', 'method': 0, 'inputs': ['a']},
        {'provider': 'groq', 'method': 0, 'inputs': {}},
        {'provider': 'groq', 'method': 0, 'inputs': None},
        {'provider': 'groq', 'method': 0, 'inputs': ''},
        {'provider': 'groq', 'method': 0, 'inputs': 0},
        {'provider': 'zeta', 'method': 0}, {'provider': 'missing', 'method': 2},
        {'provider': 'apfel', 'method': 3},
        {'harness': 'claude', 'provider': 'openai', 'method': 1},
        {'provider': 'BAD id', 'method': 1}, {'provider': 'openai', 'method': 2 ** 40}]
    for case in [full, dict(full, remembered=None), dict(full, installed=False),
                 dict(full, remembered=None, installed=False)]:
        for body in oauth_bodies:
            route(case, login, body=body)
    running = hp._task_value('openai', 'running', started_at=5, method_label='Headless')
    route(dict(full, tasks={'openai': running}), login, body={'provider': 'openai', 'method': 1})
    route(dict(full, tasks={'openai': dict(running, status='failed')}), login,
          body={'provider': 'openai', 'method': 1})
    for body in [None, {}, {'provider': 'openai'}, {'provider': 'openai', 'code': ''},
                 {'provider': 'openai', 'code': ' c '}, {'provider': 'openai', 'code': 'c' * 513},
                 {'provider': 'openai', 'code': 5}, {'harness': 'codex', 'provider': 'openai', 'code': 'c'},
                 {'provider': 'Open AI', 'code': 'c'}, {'provider': 'openai', 'code': 'a\rb'}]:
        route(full, code, body=body)
    route(dict(full, tasks={'openai': running}), code, body={'provider': 'openai', 'code': 'c'})
    setup({})

    return dict(env_set=['FIXTURE_PROVIDER_KEY'], now_ms=NOW_MS,
                constants=dict(store_harness=hp.STORE_HARNESS, timeout=hp.LOGIN_TIMEOUT_SECONDS,
                               ttl=hp.METHODS_TTL_SECONDS, max_key=hp.MAX_KEY_LENGTH,
                               max_code=hp.MAX_CODE_LENGTH, methods_key=hp._METHODS_KEY,
                               popular=list(hp.POPULAR)),
                harnesses=harnesses, providers=providers, secrets=secrets,
                user_codes=user_codes, bounded=bounded, tasks=tasks, authorize=authorize, methods=methods,
                remembered=remembered, routes=routes,
                texts={name: value for value, name in texts.items()})


if __name__ == '__main__':
    if '--record' in sys.argv:
        with scratch.directory() as area:
            print(json.dumps(record(Path(area))))
    else:
        env = scratch.child_env({'PATH': os.defpath, 'LANG': 'C.UTF-8', 'TZ': 'UTC'})
        data = json.loads(subprocess.check_output([sys.executable, '-I', __file__, '--record'], env=env))
        OUT.write_text(json.dumps(data, indent=2, ensure_ascii=True) + '\n', encoding='ascii')
        print({key: len(rows) for key, rows in data.items() if isinstance(rows, list)})
