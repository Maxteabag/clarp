"""Qualified-source proposal: direct Git index checkout, no archive packaging.

UNEXECUTED until exact B route/roster review and captain handoff. The retained
identity index is evidence of actual source files, never an archive/capsule seal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


def path_name(name):
    if (type(name) is not str or not name or '\x00' in name
            or str(PurePosixPath(name)) != name or name.startswith('/')
            or any(p in ('.', '..', '.git', '.gitattributes') for p in name.split('/'))):
        raise ValueError('unsupported source path/attribute closure')
    return name


def validate(arm, pin):
    if arm['pin'] != pin or type(arm['member_count']) is not int:
        raise ValueError('source roster pin/count type')
    members = arm['members']
    if type(members) is not dict or arm['member_count'] != len(members):
        raise ValueError('source roster count')
    parents = set()
    for name, item in members.items():
        path_name(name)
        if (item['git_mode'] not in ('100644', '100755', '120000')
                or type(item['size']) is not int or item['size'] < 0
                or type(item['git_blob_oid']) is not str
                or not re.fullmatch('[0-9a-f]{40}', item['git_blob_oid'])
                or type(item['sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('unsupported source member metadata')
        if item['git_mode'] == '120000' and (name not in ('CLAUDE.md', 'GEMINI.md')
                                            or item.get('symlink_target') != 'AGENTS.md'):
            raise ValueError('unsupported source symlink')
        parents.update(str(p) for p in PurePosixPath(name).parents if str(p) != '.')
    dirs = arm['directories']
    if type(dirs) is not list or any(type(n) is not str for n in dirs):
        raise ValueError('directory roster type')
    for name in dirs:
        path_name(name)
    if dirs != sorted(parents):
        raise ValueError('directory closure/duplicate/unsupported empty tree')


def git(root, args, env, **kwargs):
    return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                                   '-c', 'core.symlinks=true', '-c', 'core.sparseCheckout=false',
                                   '-c', 'core.attributesFile=/dev/null', *args],
                                  cwd=root, env=env, timeout=120, **kwargs)


def verify_objects(root, declared, env):
    tree = git(root, ['rev-parse', declared['pin'] + '^{tree}'], env, text=True).strip()
    if tree != declared['git_tree_oid']:
        raise ValueError('pinned tree identity mismatch')
    files, dirs = {}, []
    for record in git(root, ['ls-tree', '-r', '-t', '-z', declared['pin']], env).split(b'\0'):
        if not record:
            continue
        header, raw_name = record.split(b'\t', 1)
        mode, kind, oid = header.decode('ascii').split()
        name = path_name(os.fsdecode(raw_name))
        if kind == 'tree':
            dirs.append(name)
        elif kind == 'blob' and mode in ('100644', '100755', '120000'):
            files[name] = (mode, oid)
        else:
            raise ValueError('submodule/unsupported Git source entry')
    expected = {name: (item['git_mode'], item['git_blob_oid'])
                for name, item in declared['members'].items()}
    if files != expected or sorted(dirs) != declared['directories']:
        raise ValueError('actual Git object closure differs from reviewed roster')


def verify_checkout(dest, declared):
    names, dirs, observed = set(), set(), {}
    for parent, children, files in os.walk(dest, followlinks=False):
        for name in children + files:
            p = Path(parent) / name
            relative = p.relative_to(dest).as_posix()
            info = p.lstat()
            if stat.S_ISDIR(info.st_mode):
                dirs.add(relative)
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                names.add(relative)
            else:
                raise ValueError('unsupported actual source node')
    if names != set(declared['members']) or sorted(dirs) != declared['directories']:
        raise ValueError('actual source name/count/directory closure mismatch')
    for name, item in declared['members'].items():
        p = dest / name
        before = p.lstat()
        if before.st_nlink != 1:
            raise ValueError('unexpected source inode alias')
        blob = hashlib.sha1(b'blob ' + str(item['size']).encode() + b'\0')
        sha = hashlib.sha256()
        size = 0
        if item['git_mode'] == '120000':
            if not stat.S_ISLNK(before.st_mode):
                raise ValueError('source link replaced')
            payload = os.fsencode(os.readlink(p))
            if payload != b'AGENTS.md':
                raise ValueError('source link target changed')
            blob.update(payload)
            sha.update(payload)
            size = len(payload)
        else:
            if (not stat.S_ISREG(before.st_mode) or before.st_size != item['size']
                    or bool(before.st_mode & 0o111) != (item['git_mode'] == '100755')):
                raise ValueError('actual source kind/size/executable mode')
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('source file changed before read')
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > item['size']:
                        raise ValueError('source grew while reading')
                    blob.update(chunk)
                    sha.update(chunk)
        after = p.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('source changed while reading')
        if size != item['size'] or blob.hexdigest() != item['git_blob_oid'] or sha.hexdigest() != item['sha256']:
            raise ValueError('actual per-member source bytes mismatch')
        observed[name] = {'git_mode': item['git_mode'], 'git_blob_oid': blob.hexdigest(),
                          'size': size, 'sha256': sha.hexdigest(),
                          'actual_mode': oct(stat.S_IMODE(after.st_mode))}
    return observed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    root = Path(parser.parse_args().workspace).resolve(strict=True)
    home = Path(__file__).parent
    plan = json.loads((home / 'plan.json').read_bytes())
    roster_raw = (home / 'source-roster.json').read_bytes()
    roster = json.loads(roster_raw)
    if set(roster['arms']) != {'A', 'B'}:
        raise ValueError('exact two-arm source roster required')
    for arm in ('A', 'B'):
        if type(plan[arm]) is not str or not re.fullmatch('[0-9a-f]{40}', plan[arm]):
            raise ValueError('exact40 pins required')
        validate(roster['arms'][arm], plan[arm])
    env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    driver = git(root, ['rev-parse', 'HEAD'], env, text=True).strip()
    if os.environ.get('GITHUB_REF_NAME') != 'ci/criterion5-937-' + driver[:12]:
        raise ValueError('immutable branch must encode exact reviewed driver')
    if (root / '.git/info/attributes').exists():
        raise ValueError('unqualified local Git attributes')
    local_filters = subprocess.run(['git', 'config', '--local', '--name-only',
                                    '--get-regexp', r'^filter\.'], cwd=root, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if local_filters.returncode != 1 or local_filters.stdout or local_filters.stderr:
        raise ValueError('unqualified local Git filter configuration')
    # No .gitattributes is part of either declared source tree. Global/system
    # attributes/config are disabled; actual bytes are still verified after checkout.
    remote = git(root, ['ls-remote', 'https://github.com/Maxteabag/clarp.git', 'refs/heads/main'],
                 env, text=True).split()[0]
    if remote != plan['B']:
        raise ValueError('reviewed B moved; explicit rebind/review required')
    (root / 'measurement-source.json').write_text(json.dumps({
        'driver_sha': driver, 'A': plan['A'], 'B': plan['B'], 'main_at_setup': remote,
        'run_id': os.environ.get('GITHUB_RUN_ID'), 'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'source_roster_sha256': hashlib.sha256(roster_raw).hexdigest(),
        'staging': 'direct_git_index_checkout_no_archive',
        'git_version': git(root, ['--version'], env, text=True).strip()}, indent=2) + '\n')
    arms = root / 'measurement-arms'
    arms.mkdir()
    for arm in ('A', 'B'):
        git(root, ['fetch', '--no-tags', '--depth=1', 'https://github.com/Maxteabag/clarp.git', plan[arm]], env)
        verify_objects(root, roster['arms'][arm], env)
    for arm in ('A', 'B'):
        dest = arms / arm
        dest.mkdir()
        index = root / ('measurement-' + arm + '.index')
        if index.exists() or index.is_symlink():
            raise ValueError('source index must be fresh')
        index_env = {**env, 'GIT_INDEX_FILE': str(index)}
        git(root, ['read-tree', plan[arm]], index_env)
        git(root, ['checkout-index', '--all', '--prefix=' + str(dest) + '/'], index_env)
        observed = verify_checkout(dest, roster['arms'][arm])
        (root / ('measurement-source-' + arm + '.json')).write_text(json.dumps({
            'pin': plan[arm], 'tree_oid': roster['arms'][arm]['git_tree_oid'],
            'source_roster_sha256': hashlib.sha256(roster_raw).hexdigest(),
            'member_count': len(observed), 'directories': roster['arms'][arm]['directories'],
            'members': observed, 'staging': 'direct_git_index_checkout_no_archive',
            'qualification': 'source identity observation; NOT an archive/capsule seal'},
            indent=2, sort_keys=True) + '\n')
        index.unlink()


if __name__ == '__main__':
    main()
