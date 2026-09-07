import importlib.util
from pathlib import Path
import subprocess

spec = importlib.util.spec_from_file_location('qa_tool', Path(__file__).resolve().parents[2] / 'scripts/qa.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


def test_identity_includes_staged_content_and_deletions(tmp_path):
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    source = tmp_path / 'code.txt'
    source.write_text('base')
    git('add', 'code.txt')
    git('-c', 'user.name=QA', '-c', 'user.email=qa@example.invalid', 'commit', '-m', 'fixture')
    assert qa.source_identity(tmp_path)['changes'] == {}
    source.write_text('staged change')
    git('add', 'code.txt')
    staged = qa.source_identity(tmp_path)
    assert 'code.txt' in staged['changes']
    source.write_text('another staged change')
    git('add', 'code.txt')
    assert qa.source_identity(tmp_path) != staged
    source.unlink()
    git('add', '-u')
    assert qa.source_identity(tmp_path)['changes']['code.txt'] == 'deleted'
