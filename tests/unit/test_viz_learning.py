import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'server'))
from lib import viz_library as library, viz_rule_author as author, viz_normalize


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(library, 'path', lambda: tmp_path / 'library.json')


def novel(exe='frobnicate'):
    return {'entity': {'id': 'toolchain:' + exe, 'kind': 'instrument',
                       'shape': 'diamond', 'icon': 'glyph:F'},
            'rule': {'exe': exe, 'verb': 'measure', 'target': 'toolchain:' + exe},
            'archetype': 'meter', 'spec': {'travel': .03, 'decay': .95,
                                        'persist': True, 'weight': 1, 'trail': .2}}


def test_tier_one_cannot_invent_entities():
    model = lambda p, m: json.dumps({'verdict': 'variant', 'of': 'invented',
                                     'verb': 'build', 'kind': 'toolchain', 'confidence': .99})
    with pytest.raises(ValueError, match='invented'):
        author.triage('pnpm', 'pnpm install', library.seed(), model)


def test_pnpm_triage_receives_library_and_resolves_npm():
    def model(prompt, name):
        assert name == author.TIER_ONE
        assert 'known_archetypes' in prompt and 'toolchain:npm' in prompt
        return json.dumps({'verdict': 'variant', 'of': 'toolchain:npm',
                           'verb': 'build', 'kind': 'toolchain', 'confidence': .94})
    assert author.triage('pnpm', 'pnpm install', library.seed(), model)['of'] == 'toolchain:npm'


def test_seed_aliases_are_deterministic():
    lib = library.seed()
    for command in ['npm install', 'pnpm install']:
        assert viz_normalize.classify('Bash', {'command': command}, library=lib)[1] == 'toolchain:npm'
    for command in ['python3 -V', '/usr/bin/python3 -V', 'python3.13 -V']:
        assert viz_normalize.classify('Bash', {'command': command}, library=lib)[1] == 'script:python3'


def test_novel_design_applies_itself_and_does_not_reask():
    calls = []
    def model(prompt, name):
        calls.append(name)
        return json.dumps({'verdict': 'NOVEL', 'why_novel': 'measurement'}) if name == author.TIER_ONE else json.dumps(novel())
    clusters = [{'hint': 'frobnicate', 'example': 'frobnicate x', 'count': 2}]
    assert author.learn(clusters, model)['applied'] == ['decision:1']
    assert calls == [author.TIER_ONE, author.TIER_TWO]
    assert author.learn(clusters, model)['applied'] == []
    assert len(calls) == 2
    assert library.load()['entities']['toolchain:frobnicate']['archetype'] == 'meter'


def test_decision_changes_only_when_explicitly_superseded():
    first = library.apply(novel(), 0)
    design = novel()
    design['entity']['shape'] = 'ring'
    with pytest.raises(ValueError, match='stable'):
        library.apply(design, first['revision'])
    second = library.apply(design, first['revision'], 'decision:1')
    assert second['decisions'][-1]['supersedes'] == 'decision:1'
    assert second['entities']['toolchain:frobnicate']['shape'] == 'ring'
    with pytest.raises(ValueError, match='changed'):
        library.apply(design, first['revision'], 'decision:2')


def test_merge_redirects_old_events_without_duplicating_identity():
    first = library.apply(novel(), 0)
    other = novel('meter')
    other['merge'] = ['toolchain:frobnicate']
    second = library.apply(other, first['revision'], 'decision:1')
    assert library.resolve('toolchain:frobnicate', second) == 'toolchain:meter'


def test_bad_design_leaves_last_good_snapshot():
    first = library.apply(novel(), 0)
    bad = novel('bad')
    bad['spec']['weight'] = float('nan')
    with pytest.raises(ValueError):
        library.apply(bad, 1)
    assert library.load() == first


def test_unknown_is_visible_before_any_learning():
    row = {'agent_id': 'a', 'ts': 1, 'detail': json.dumps({'tool': 'Bash', 'input': {'command': 'frobnicate x'}})}
    events = viz_normalize.normalize([row], {'a': 'Nadia'})
    assert events[0]['target'] == 'unknown:frobnicate'
    assert events[0]['provisional']


def test_model_failure_does_not_write_library():
    def fail(*args):
        raise TimeoutError('deadline')
    out = author.learn([{'hint': 'frobnicate', 'example': 'frobnicate x', 'count': 1}], fail)
    assert out['rejected'] and not library.path().exists()


def test_subcommand_only_rule_reaches_the_designed_node_and_is_not_reasked():
    design = novel('ruff')
    design['rule']['sub'] = 'check'
    lib = library.apply(design, 0)
    assert viz_normalize.classify('Bash', {'command': 'ruff check server/'}, library=lib) == ('measure', 'toolchain:ruff')
    assert viz_normalize.classify('Bash', {'command': 'ruff format server/'}, library=lib) is None
    def should_not_call(*args):
        pytest.fail('a learned subcommand must not call a model again')
    assert author.learn([{'hint':'ruff','count':1,'example':'ruff check server/'}], should_not_call)['applied'] == []


def test_explicit_file_paths_split_roles_without_guessing_historical_cwd(monkeypatch):
    monkeypatch.setattr(viz_normalize, '_is_checkout', lambda p: p == '/home/p/GIT/clarp')
    rows = [{'agent_id':'a','ts':i,'detail':json.dumps({'tool':'Read','input':{'file_path':p}})}
            for i,p in enumerate(['/home/p/GIT/clarp/tests/test_x.py', '/home/p/GIT/clarp/server/x.py', 'tests/test_x.py'])]
    assert [e['target'] for e in viz_normalize.normalize(rows,{})] == ['file:clarp/tests','file:clarp/source','file']


def test_worktrees_parent_is_not_a_repository(tmp_path, monkeypatch):
    root=tmp_path / 'GIT' / 'clarp-worktrees'
    checkout=root / 'fleet-map'
    checkout.mkdir(parents=True)
    # The live regex deliberately matches supported absolute repository roots.
    monkeypatch.setattr(viz_normalize, '_REPO_RE', __import__('re').compile(str(tmp_path) + r'/GIT/([A-Za-z0-9._-]+)'))
    assert viz_normalize.repo_of(str(checkout / 'x.py')) is None
    (checkout / '.git').write_text('gitdir: /home/peter/GIT/clarp/.git/worktrees/fleet-map')
    viz_normalize._is_checkout.cache_clear()
    assert viz_normalize.repo_of(str(checkout / 'x.py')) == 'repo:clarp'


def test_environment_assignment_is_not_a_new_executable():
    assert viz_normalize.first_known_executable('PYTHONPATH=server frobnicate x')[0] == 'frobnicate'
    assert viz_normalize.first_known_executable('task_proc=$!')[0] == ''


def test_bad_json_retains_the_last_good_live_decisions():
    first = library.apply(novel(), 0)
    library.path().write_text('{broken')
    assert library.load() == first


def test_superseding_a_specific_rule_removes_its_old_precedence():
    design=novel('ruff');design['rule']['sub']='check'
    library.apply(design,0)
    updated=novel('ruff');updated['entity']['id']='toolchain:linter';updated['rule']['target']='toolchain:linter'
    lib=library.apply(updated,1,'decision:1')
    assert 'ruff:check' not in lib['rules']
    assert viz_normalize.classify('Bash',{'command':'ruff check server/'},library=lib)[1]=='toolchain:linter'


def test_seed_rules_also_require_explicit_supersession():
    with pytest.raises(ValueError,match='seed decision'):
        library.apply(novel('git'),0)


def test_learned_native_tool_resolves_without_becoming_permanently_provisional():
    lib=library.apply(novel('custom_tool'),0)
    assert viz_normalize.classify('custom_tool',{},library=lib) == ('measure','toolchain:custom_tool')


def test_http_sends_provisional_events_before_offering_any_model_work(monkeypatch):
    import importlib.util
    spec=importlib.util.spec_from_file_location('fleet_map_http_test', pathlib.Path(__file__).resolve().parents[2] / 'server/server.py')
    server=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    from lib import viz_learning
    order=[]
    clusters=[{'hint':'frobnicate','count':1,'example':'frobnicate x'}]
    monkeypatch.setattr(viz_normalize,'build_fleet_map',lambda *a,**k:{'events':[], '_unknown_clusters':clusters})
    monkeypatch.setattr(viz_learning,'status',lambda:{'designing':'','queued':[]})
    monkeypatch.setattr(viz_learning,'offer',lambda c:order.append(('offer',c)))
    class Handler:
        path='/viz/events'
        def _send(self,status,body,content_type):
            order.append(('send',json.loads(body)))
    server.Handler._handle_viz_events(Handler())
    assert [x[0] for x in order] == ['send','offer']
    assert '_unknown_clusters' not in order[0][1]


def test_explicitly_superseding_git_changes_its_seed_behavior():
    lib=library.apply(novel('git'),0,'seed:git')
    assert viz_normalize.classify('Bash',{'command':'git push'},library=lib) == ('measure','toolchain:git')


def test_seed_selector_can_be_restructured_explicitly():
    design=novel('git');design['rule']['sub']='commit'
    lib=library.apply(design,0,'seed:git')
    assert 'git' not in lib['rules']
    assert viz_normalize.classify('Bash',{'command':'git commit -m x'},library=lib)[1]=='toolchain:git'
