"""Named-method baseline validation must preserve both rewritten sources and mixins."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tools/refactor'))
import s3_methods as methods


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(root, message):
    _git(root, 'add', '.')
    _git(root, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', message)
    return _git(root, 'rev-parse', 'HEAD')


def _tree(root, split, two_groups=False):
    (root / 'server/lib').mkdir(parents=True)
    (root / 'server/lib/__init__.py').write_text('')
    source = 'lib/client.py' if split else 'lib/engine.py'
    text = ('VALUE = 5\n' if split else 'from .values import VALUE\n') + '''
class Engine:
    def __init__(self, bias=2):
        self.bias = bias

    def read(self):
        return VALUE + self.bias
'''
    if two_groups:
        text += '\n    def twice(self):\n        return self.read() * 2\n'
    (root / 'server/lib/engine.py').write_text(text)
    if not split:
        (root / 'server/lib/values.py').write_text('VALUE = 5\n')
    (root / 'tools/refactor/methods').mkdir(parents=True)
    (root / 'tools/refactor/methods/engine.toml').write_text(
        f'source_module = "{source}"\nsource_class = "Engine"\n'
        '[[group]]\nname = "read"\nmodule = "lib/read.py"\n'
        'class = "ReadMixin"\nmembers = ["read"]\n')
    if two_groups:
        recipe = root / 'tools/refactor/methods/engine.toml'
        recipe.write_text(recipe.read_text() + '\n[[group]]\nname = \"twice\"\nmodule = \"lib/twice.py\"\nclass = \"TwiceMixin\"\nmembers = [\"twice\"]\n')
    if split:
        (root / 'tools/refactor/splits').mkdir()
        (root / 'tools/refactor/splits/engine.toml').write_text(
            '[[split]]\nname = "engine"\nmodule = "lib/engine.py"\n'
            '[[split.part]]\nmodule = "lib/values.py"\nnames = ["VALUE"]\n'
            '[[split.part]]\nmodule = "lib/client.py"\nnames = ["Engine"]\n')
    _git(root, 'init', '-q')
    base = _commit(root, 'baseline')
    return base, source


def _apply(root, split):
    if split:
        entries, _ = methods.split_apply.load_recipes(root / 'tools/refactor/splits')
        methods.write(root, methods.split_apply.apply_split(root, entries[0]))
    found = methods.recipes(root)
    for _, groups in found:
        for count in range(1, len(groups) + 1):
            methods.write(root, methods.s3_apply.apply(root, groups[:count]))
    return _commit(root, 'generated integration')


@pytest.mark.parametrize('split', [False, True])
def test_unchanged_generated_family_and_unstarted_family_are_accepted(tmp_path, split):
    base, _ = _tree(tmp_path, split)
    found = methods.recipes(tmp_path)
    methods.validate(tmp_path, base, base, found)
    head = _apply(tmp_path, split)
    methods.validate(tmp_path, base, head, found)


@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('where', ['source', 'mixin'])
def test_integration_edits_in_source_or_mixin_stop_before_regeneration(tmp_path, split, where):
    base, source = _tree(tmp_path, split)
    _apply(tmp_path, split)
    path = tmp_path / 'server' / (source if where == 'source' else 'lib/read.py')
    path.write_text(path.read_text().replace('self.bias = bias', 'self.bias = bias + 1')
                    if where == 'source' else path.read_text().replace('VALUE + self.bias', 'VALUE - self.bias'))
    head = _commit(tmp_path, 'integration-only edit')
    with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit'):
        methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


def test_partial_split_is_refused(tmp_path):
    base, _ = _tree(tmp_path, True)
    _apply(tmp_path, True)
    (tmp_path / 'server/lib/values.py').unlink()
    head = _commit(tmp_path, 'missing module part')
    with pytest.raises(methods.s3_apply.Refusal, match='partial module split'):
        methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


def test_unstarted_unsplit_source_edit_is_not_silently_replaced(tmp_path):
    base, _ = _tree(tmp_path, False)
    path = tmp_path / 'server/lib/engine.py'
    path.write_text(path.read_text().replace('VALUE + self.bias', 'VALUE - self.bias'))
    head = _commit(tmp_path, 'edit before method move')
    with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit'):
        methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


@pytest.mark.parametrize('split', [False, True])
def test_missing_mixin_with_a_rewritten_source_is_refused(tmp_path, split):
    base, _ = _tree(tmp_path, split)
    _apply(tmp_path, split)
    (tmp_path / 'server/lib/read.py').unlink()
    head = _commit(tmp_path, 'lost generated mixin')
    with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit'):
        methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


def test_old_module_of_an_unstarted_split_is_not_a_method_source(tmp_path):
    base, _ = _tree(tmp_path, True)
    recipe = tmp_path / 'tools/refactor/methods/engine.toml'
    recipe.write_text(recipe.read_text().replace('lib/client.py', 'lib/engine.py'))
    with pytest.raises(methods.s3_apply.Refusal, match='unstarted split old module'):
        methods.validate(tmp_path, base, base, methods.recipes(tmp_path))


@pytest.mark.parametrize('split', [False, True])
def test_two_group_sequential_stage_output_validates(tmp_path, split):
    # Real stage E writes each successive prefix, rather than one all-group move.
    # The validator must accept exactly that output for original and split sources.
    base, _ = _tree(tmp_path, split, two_groups=True)
    head = _apply(tmp_path, split)
    methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


@pytest.mark.parametrize('applied', [False, True])
def test_unstarted_split_with_a_part_repoint_keeps_its_baseline_valid(tmp_path, applied):
    _tree(tmp_path, True)
    recipe = tmp_path / 'tools/refactor/splits/engine.toml'
    recipe.write_text(recipe.read_text() + (
        '\n[[repoint]]\nname = "client-values"\nfile = "lib/client.py"\n'
        'from_module = ".values"\nto_module = ".replacement"\nnames = ["VALUE"]\n'))
    (tmp_path / 'server/lib/replacement.py').write_text('from .values import VALUE\n')
    base = _commit(tmp_path, 'baseline includes split and following part repoint')
    # Nothing has moved on integration yet. D will create the part before its
    # repoint runs; preflight must preserve today's original source meanwhile.
    head = base
    if applied:
        entries, repoints = methods.split_apply.load_recipes(tmp_path / 'tools/refactor/splits')
        methods.write(tmp_path, methods.split_apply.apply_split(tmp_path, entries[0]))
        methods.write(tmp_path, methods.split_apply.apply_repoint(tmp_path, repoints[0]))
        for _, groups in methods.recipes(tmp_path):
            for count in range(1, len(groups) + 1):
                methods.write(tmp_path, methods.s3_apply.apply(tmp_path, groups[:count]))
        head = _commit(tmp_path, 'applied split then repoint then methods')
    methods.validate(tmp_path, base, head, methods.recipes(tmp_path))


def _evolve_family(root, split=True):
    """Old integration and new main have different, independently pinned recipes."""
    base, source = _tree(root, split, two_groups=True)
    integration = _apply(root, split)
    _git(root, 'switch', '-qc', 'new-main', base)
    original = root / 'server/lib/engine.py'
    original.write_text(original.read_text().replace('VALUE + self.bias', 'VALUE * self.bias')
                        + '\n    def extra(self):\n        return helper()\n\ndef helper():\n    return 8\n')
    if split:
        recipe = root / 'tools/refactor/splits/engine.toml'
        recipe.write_text(recipe.read_text().replace('["VALUE"]', '["VALUE", "helper"]'))
    else:
        # A named move must import a module helper from below its source, not
        # accidentally depend on a new name that the facade retains.
        original.write_text(original.read_text().replace('\ndef helper():\n    return 8\n', ''))
        values = root / 'server/lib/values.py'
        values.write_text(values.read_text() + '\ndef helper():\n    return 8\n')
        original.write_text(original.read_text().replace('import VALUE', 'import VALUE, helper'))
    recipe = root / 'tools/refactor/methods/engine.toml'
    extra = ('[[group]]\nname = "extra"\nmodule = "lib/extra.py"\n'
             'class = "ExtraMixin"\nmembers = ["extra"]\n\n')
    recipe.write_text(recipe.read_text().replace('[[group]]\nname = "twice"', extra + '[[group]]\nname = "twice"'))
    _commit(root, 'new source and recipe additions')
    return base, integration, source


@pytest.mark.parametrize('split', [False, True])
def test_recipe_evolution_preserves_old_family_and_generates_new_requirements(tmp_path, split):
    base, integration, source = _evolve_family(tmp_path, split)
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    # New generation still sees every new requirement and the latest old body.
    head = _apply(tmp_path, split)
    program = ('from lib.engine import Engine; obj=Engine(); '
               'assert obj.read()==10; assert obj.extra()==8; assert obj.twice()==20')
    result = subprocess.run([sys.executable, '-c', program], cwd=tmp_path / 'server',
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'return VALUE * self.bias' in (tmp_path / 'server/lib/read.py').read_text()
    assert (tmp_path / 'server/lib/extra.py').exists()
    assert head != integration


def test_split_only_recipe_evolution_uses_historical_data(tmp_path):
    _tree(tmp_path, True)
    (tmp_path / 'tools/refactor/methods/engine.toml').unlink()
    base = _commit(tmp_path, 'split only baseline')
    entries, _ = methods.split_apply.load_recipes(tmp_path / 'tools/refactor/splits')
    methods.write(tmp_path, methods.split_apply.apply_split(tmp_path, entries[0]))
    integration = _commit(tmp_path, 'old split')
    _git(tmp_path, 'switch', '-qc', 'new-main', base)
    source = tmp_path / 'server/lib/engine.py'
    source.write_text(source.read_text() + '\ndef helper():\n    return 8\n')
    recipe = tmp_path / 'tools/refactor/splits/engine.toml'
    recipe.write_text(recipe.read_text().replace('["VALUE"]', '["VALUE", "helper"]'))
    _commit(tmp_path, 'new source and split recipe')
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


@pytest.mark.parametrize('edit', ['retire_member', 'rename_output', 'reorder_groups', 'retire_recipe'])
def test_historical_obligations_cannot_disappear_from_current_accounting(tmp_path, edit):
    base, integration, _ = _evolve_family(tmp_path)
    recipe = tmp_path / 'tools/refactor/methods/engine.toml'
    text = recipe.read_text()
    if edit == 'retire_member':
        text = text.replace('members = ["read"]', 'members = ["__init__"]')
    elif edit == 'rename_output':
        text = text.replace('lib/read.py', 'lib/renamed.py')
    elif edit == 'reorder_groups':
        first, rest = text.split('[[group]]', 1)
        groups = ['[[group]]' + x for x in rest.split('[[group]]')]
        text = first + ''.join(reversed(groups))
    else:
        recipe.unlink()
    if recipe.exists():
        recipe.write_text(text)
    _commit(tmp_path, 'unsupported history migration')
    with pytest.raises(methods.s3_apply.Refusal, match='historical|topology|retir|order'):
        methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


def test_git_read_failure_is_not_proven_path_absence(tmp_path, monkeypatch):
    base, _ = _tree(tmp_path, False)
    real_run = methods.subprocess.run
    def failed_show(args, *a, **kw):
        if args[:3] == ['git', '-C', str(tmp_path)] and any(op in args for op in ('show', 'cat-file')):
            return subprocess.CompletedProcess(args, 128, '', 'synthetic object read failure')
        return real_run(args, *a, **kw)
    monkeypatch.setattr(methods.subprocess, 'run', failed_show)
    with pytest.raises(methods.s3_apply.Refusal, match='read|Git|git'):
        methods.show(tmp_path, base, 'server/lib/engine.py')


@pytest.mark.parametrize('changed', [None, 'source', 'output'])
def test_first_method_recipe_on_a_historical_split_part_needs_proven_absence(tmp_path, changed):
    _tree(tmp_path, True)
    recipe = tmp_path / 'tools/refactor/methods/engine.toml'
    text = recipe.read_text()
    recipe.unlink()
    base = _commit(tmp_path, 'module split predates first method recipe')
    entries, _ = methods.split_apply.load_recipes(tmp_path / 'tools/refactor/splits')
    methods.write(tmp_path, methods.split_apply.apply_split(tmp_path, entries[0]))
    if changed == 'source':
        source = tmp_path / 'server/lib/client.py'
        source.write_text(source.read_text().replace('VALUE + self.bias', 'VALUE - self.bias'))
    elif changed == 'output':
        (tmp_path / 'server/lib/read.py').write_text('class ReadMixin:\n    pass\n')
    integration = _commit(tmp_path, 'historical split, before methods')
    _git(tmp_path, 'switch', '-qc', 'new-main', base)
    recipe.write_text(text)
    _commit(tmp_path, 'first method recipe')
    found = methods.recipes(tmp_path)
    if changed:
        with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit'):
            methods.validate(tmp_path, base, integration, found)
    else:
        methods.validate(tmp_path, base, integration, found)


@pytest.mark.parametrize('changed', [False, True])
def test_first_recipe_without_historical_directory_preserves_the_old_source(tmp_path, changed):
    _tree(tmp_path, False)
    recipe = tmp_path / 'tools/refactor/methods/engine.toml'
    text = recipe.read_text()
    recipe.unlink()
    base = _commit(tmp_path, 'no historical recipe directory')
    if changed:
        source = tmp_path / 'server/lib/engine.py'
        source.write_text(source.read_text().replace('VALUE + self.bias', 'VALUE - self.bias'))
        integration = _commit(tmp_path, 'unrecorded original edit')
    else:
        integration = base
    _git(tmp_path, 'switch', '-qc', 'new-main', base)
    recipe.write_text(text)
    _commit(tmp_path, 'first reviewed recipe')
    if changed:
        with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit'):
            methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    else:
        methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


def test_added_part_and_group_preserve_historical_relative_order(tmp_path):
    base, integration, _ = _evolve_family(tmp_path)
    recipe = tmp_path / 'tools/refactor/splits/engine.toml'
    text = recipe.read_text().replace('["VALUE", "helper"]', '["VALUE"]')
    text = text.replace('[[split.part]]\nmodule = "lib/client.py"',
                        '[[split.part]]\nmodule = "lib/helpers.py"\nnames = ["helper"]\n\n'
                        '[[split.part]]\nmodule = "lib/client.py"')
    recipe.write_text(text)
    _commit(tmp_path, 'new helper has its own earlier part')
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    _apply(tmp_path, True)
    assert 'def helper' in (tmp_path / 'server/lib/helpers.py').read_text()
    result = subprocess.run([sys.executable, '-c',
                             'from lib.engine import Engine; assert Engine().extra()==8'],
                            cwd=tmp_path / 'server', capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_required_new_name_is_not_optional_during_current_generation(tmp_path):
    base, integration, _ = _evolve_family(tmp_path)
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    source = tmp_path / 'server/lib/engine.py'
    source.write_text(source.read_text().replace('\ndef helper():\n    return 8\n', ''))
    entries, _ = methods.split_apply.load_recipes(tmp_path / 'tools/refactor/splits')
    with pytest.raises(methods.s3_apply.Refusal, match='helper is not a single plain'):
        methods.split_apply.apply_split(tmp_path, entries[0])


def test_historical_python_tool_is_data_never_executed(tmp_path):
    _tree(tmp_path, True)
    (tmp_path / 'tools/refactor/s3_apply.py').write_text("raise RuntimeError('HISTORICAL TOOL EXECUTED')\n")
    base = _commit(tmp_path, 'historical tool execution sentinel')
    integration = _apply(tmp_path, True)
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


def test_malformed_historical_metadata_is_not_an_empty_recipe_set(tmp_path):
    base, _ = _tree(tmp_path, True)
    integration = _apply(tmp_path, True)
    _git(tmp_path, 'switch', '-qc', 'malformed-history', base)
    recipe = tmp_path / 'tools/refactor/splits/engine.toml'
    good = recipe.read_text()
    recipe.write_text('invalid TOML !!!\n')
    malformed = _commit(tmp_path, 'malformed historical recipe')
    recipe.write_text(good)
    with pytest.raises(methods.s3_apply.Refusal, match='invalid historical recipe'):
        methods.validate(tmp_path, malformed, integration, methods.recipes(tmp_path))


@pytest.mark.parametrize('failure', ['invalid-ref', 'catalogue-read'])
def test_unprovable_history_refuses_instead_of_claiming_absence(tmp_path, monkeypatch, failure):
    base, _ = _tree(tmp_path, False)
    found = methods.recipes(tmp_path)
    if failure == 'invalid-ref':
        with pytest.raises(methods.s3_apply.Refusal, match='Git data read failed'):
            methods.validate(tmp_path, 'no-such-baseline', base, found)
    else:
        real_run = methods.subprocess.run
        def fail_catalogue(args, *a, **kw):
            if args[:3] == ['git', '-C', str(tmp_path)] and 'ls-tree' in args and '-r' in args:
                return subprocess.CompletedProcess(args, 128, '', 'synthetic catalogue failure')
            return real_run(args, *a, **kw)
        monkeypatch.setattr(methods.subprocess, 'run', fail_catalogue)
        with pytest.raises(methods.s3_apply.Refusal, match='Git data read failed.*catalogue'):
            methods.validate(tmp_path, base, base, found)


@pytest.mark.parametrize('kind', ['split', 'repoint'])
def test_historical_recipe_filename_rename_requires_explicit_migration(tmp_path, kind):
    if kind == 'split':
        base, _ = _tree(tmp_path, True)
        integration = _apply(tmp_path, True)
        old_path = tmp_path / 'tools/refactor/splits/engine.toml'
    else:
        _tree(tmp_path, False)
        folder = tmp_path / 'tools/refactor/splits'
        folder.mkdir()
        old_path = folder / 'edge.toml'
        old_path.write_text('[[repoint]]\nname = "values-edge"\nfile = "lib/engine.py"\n'
                            'from_module = ".values"\nto_module = ".replacement"\nnames = ["VALUE"]\n')
        (tmp_path / 'server/lib/replacement.py').write_text('from .values import VALUE\n')
        base = _commit(tmp_path, 'historical repoint recipe')
        _, repoints = methods.split_apply.load_recipes(folder)
        methods.write(tmp_path, methods.split_apply.apply_repoint(tmp_path, repoints[0]))
        integration = _apply(tmp_path, False)
    _git(tmp_path, 'switch', '-qc', 'new-main', base)
    old_path.rename(old_path.with_name('renamed.toml'))
    _commit(tmp_path, 'unsupported recipe file rename')
    with pytest.raises(methods.s3_apply.Refusal, match='historical.*recipe|topology'):
        methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


@pytest.mark.parametrize(('split', 'rel'), [
    (False, 'lib/engine.py'),
    (False, 'lib/read.py'),
    (True, 'lib/engine.py'),
    (True, 'lib/client.py'),
    (True, 'lib/values.py'),
    (True, 'lib/read.py'),
])
def test_newline_only_committed_family_edit_is_still_byte_drift(tmp_path, split, rel):
    base, _ = _tree(tmp_path, split)
    integration = _apply(tmp_path, split)
    methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    path = tmp_path / 'server' / rel
    original = path.read_bytes()
    changed = original.replace(b'\n', b'\r\n')
    assert changed != original and changed.replace(b'\r\n', b'\n') == original
    path.write_bytes(changed)
    integration = _commit(tmp_path, 'newline-only integration byte edit')
    with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit') as error:
        methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))
    assert rel in str(error.value)


@pytest.mark.parametrize('applied', [False, True])
def test_newline_only_repoint_edit_matches_neither_exact_alternative(tmp_path, applied):
    _tree(tmp_path, False)
    folder = tmp_path / 'tools/refactor/splits'
    folder.mkdir()
    (folder / 'edge.toml').write_text(
        '[[repoint]]\nname = "aux-edge"\nfile = "lib/aux.py"\n'
        'from_module = ".values"\nto_module = ".replacement"\nnames = ["VALUE"]\n')
    aux = tmp_path / 'server/lib/aux.py'
    aux.write_text('from .values import VALUE\n')
    (tmp_path / 'server/lib/replacement.py').write_text('VALUE = 5\n')
    base = _commit(tmp_path, 'historical repoint with an unapplied alternative')
    if applied:
        _, repoints = methods.split_apply.load_recipes(folder)
        methods.write(tmp_path, methods.split_apply.apply_repoint(tmp_path, repoints[0]))
    original = aux.read_bytes()
    aux.write_bytes(original.replace(b'\n', b'\r\n'))
    assert aux.read_bytes() != original
    integration = _commit(tmp_path, 'newline-only repoint edit')
    with pytest.raises(methods.s3_apply.Refusal, match='integration-only edit.*aux.py'):
        methods.validate(tmp_path, base, integration, methods.recipes(tmp_path))


def test_unchanged_historical_crlf_source_is_not_rejected_as_a_policy(tmp_path):
    _tree(tmp_path, False)
    source = tmp_path / 'server/lib/engine.py'
    source.write_bytes(source.read_bytes().replace(b'\n', b'\r\n'))
    base = _commit(tmp_path, 'legitimate unchanged CRLF historical source')
    methods.validate(tmp_path, base, base, methods.recipes(tmp_path))
