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


def test_unstarted_split_with_a_part_repoint_keeps_its_baseline_valid(tmp_path):
    _tree(tmp_path, True)
    recipe = tmp_path / 'tools/refactor/splits/engine.toml'
    recipe.write_text(recipe.read_text() + (
        '\n[[repoint]]\nname = "client-values"\nfile = "lib/client.py"\n'
        'from_module = ".values"\nto_module = ".replacement"\nnames = ["VALUE"]\n'))
    (tmp_path / 'server/lib/replacement.py').write_text('from .values import VALUE\n')
    base = _commit(tmp_path, 'baseline includes split and following part repoint')
    # Nothing has moved on integration yet. D will create the part before its
    # repoint runs; preflight must preserve today's original source meanwhile.
    methods.validate(tmp_path, base, base, methods.recipes(tmp_path))
