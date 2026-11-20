#!/usr/bin/env python3
"""Writes the vectors the Rust Host's choice of who a spoken message is for is tested against.

A message that was dictated hands-free, and that does not say it is for the agent it names,
goes to the agent whose name it begins with: within the first three words after "hey", "ok" and
the like, by the name as it is written or, for the first word only, by a name that sounds much
like it. Without a name it stays with the agent that is open (`server/lib/routing.py`,
`server/lib/send_service.py`). This runs those functions themselves, for agents and messages
made up here, and writes what they said to
`crates/clarp-kernel/tests/fixtures/addressed-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/addressed-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. The database it touches is a new one in that directory. No Host is touched or
started. The judgment of a spoken name by a model, which the Python Host can be given and which
is off unless its owner turns it on, is off here: this Host has none.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'addressed-vectors.json'

# The agents of a Host, in the order they were made: session and name.
MANY = [['rachel', 'Rachel'], ['mike-1a2b', 'Mike'], ['claire', 'Claire'], ['bella', 'Bella'], ['bel', 'Bel'],
        ['codex-8194', 'Codex-8194'], ['anna-maria', 'Anna Maria'], ['zoe', 'Zoë'], ['dr', 'Dr'], ['al', 'Al'],
        ['j', 'J'], ['nameless', ''], ['spaces', '   '], ['digits', '1234'], ['hey', 'Hey']]
TWINS = [['mike-1', 'Mike'], ['mike-2', 'Mike'], ['long', 'Bella-2'], ['short', 'Bella']]
RHYMES = [['bella', 'Bella'], ['della', 'Della'], ['stella', 'Stella']]

SAID = [
    'Rachel, check the branch', 'hey rachel can you check', 'Hey, um, Mike: status?', 'ok so please Claire do it',
    'did Rachel finish?', 'I think that Rachel should', "Bell, what's up", "Bel, what's up", 'Michael do this',
    'Racheal fix it', 'Clair, hello', 'Mikey hi', 'codex do x', 'Codex-8194, run', 'anna maria please',
    'Zoe come here', 'Zo come', 'Dr, come', 'Al, come', 'Also do', 'J do', '', '   ', '123 456', 'um uh hey',
    'hey hey hey Rachel', 'hey hey hey hey Rachel now', 'one two three Rachel', 'one two Rachel go', 'Rachel',
    'Rachel!!! ... -- do it', "rachel's branch is broken", 'RACHEL DO IT', 'Rachel_2 go', 'Rachel-Mike go',
    'Rachel  go', 'Rachel\x1cgo', 'Rachel\x1f\x85 go \x1e', 'Привет Rachel сделай',
    'Mi do it', 'Mik do it', 'mick do it', 'rakel do it', 'rachl do it', 'clare do it', 'please also and yo Mike',
    'hey do it', 'Hey Hey', 'so', 'Rachel Mike Claire', 'mike rachel', 'tell Mike that Rachel waits',
    'the claire of the day', 'Bellas turn', 'belladonna is a plant', 'annamaria go', 'Anna, go', 'maria go',
    'rächel go', 'Rachél go', 'Ｒachel go', 'Rachel　， go', 'rachel;:!?.,- - go', 'rachel\n\ngo\n',
    '  rachel  ', 'x' * 300 + ' rachel', 'ra' + 'chel ' * 40, 'digits go', '1234 go', 'nameless go', 'Zoë go',
    'Zoé go', 'zo-e go', 'dr. Mike', 'a b c d', 'ab cd ef', 'abc def ghi', 'Claire?', '?Claire', '...claire...',
    # A name spelled out whole, in more words than one or in letters English has not, however
    # they are composed: the address ends where the name does.
    'Zoe\u0308 go', 'ZO\u00cb, go', 'zo\u00eb go', 'Zo\u00eby go', 'Zo\u00eb-go', 'Zo\u00eb', 'hey zo\u00eb: go',
    'Anna Maria, please', 'ANNA MARIA please', 'anna  maria please', 'Anna Mariana please', 'anna maria',
    'hey anna maria: go', 'Anna Mar\u00eda please', 'anna-maria please', 'Rache\u0301l go', 'Anna Maria-go',
]
# Names of two words and a name in letters English has not, and what may stand right after one:
# the address ends with the name only where the name ends as a word.
PAIRS = [['mary-ann', 'Mary Ann'], ['jean-luc', 'Jean Luc'], ['zoe', 'Zo\u00eb'], ['anna-maria', 'Anna Maria']]
SAID_TO_PAIRS = [
    'Mary Annette go', 'Jean Lucas go', 'mary ann go', 'Jean Luc, go', 'Jean Luc', 'jean luc\u2014go', 'Mary Ann\u2026 go',
    'Zo\u00eb\U0001f642go', 'Zo\u00eb\u200dgo', 'Zo\u00eb\u2026go', 'Zo\u00eb\u2014go', 'Zo\u00eb_go', 'Zo\u00eb1 go',
    'Zo\u00eb\u3000go', 'Zo\u00eb\u00a0go', 'Zo\u00eb\x1cgo', 'Zo\u00eb\u00bfgo', 'Zo\u00eb+go', 'Zo\u00eb$go', 'Zo\u00eb\u0301 go',
    'Anna Maria\u00b7go', 'Anna Maria1 go', 'Anna Maria\u0301 go',
]
SAID_TO_TWINS = ['Mike go', 'mikey go', 'Bella go', 'Bell go', 'bella-2 go', 'short go', 'long go']
SAID_TO_RHYMES = ['Cella go', 'ella go', 'Stela go', 'della go', 'Bela go', 'tella go']

# Two words, and how alike they are.
ALIKE = [['', ''], ['', 'a'], ['a', ''], ['a', 'a'], ['Bell', 'Bella'], ['bella', 'BELL'], ['bell', 'Bel'],
         ['michael', 'Mike'], ['racheal', 'Rachel'], ['rakel', 'rachel'], ['mick', 'mike'], ['clare', 'Claire'],
         ['zoe', 'Zoë'], ['banana', 'ananas'], ['ananas', 'banana'], ['abcabcabc', 'cbacbacba'],
         ['anna', 'Anna Maria'], ['codex', 'Codex-8194'], ['straße', 'STRASSE'], ['İstanbul', 'istanbul'],
         ['ΣΣ', 'σς'], ['aaaa', 'aa'], ['abab', 'baba'], ['abcd', 'dcba'], ['xyz', 'abc'],
         ['a' * 250, 'a' * 120 + 'b' + 'a' * 129], ['ab' * 150, 'ba' * 150], ['mississippi', 'missouri'],
         ['the quick brown fox', 'the quack brown box'], ['kitten', 'sitting'], ['claude', 'Claire'],
         ['rachel', 'rachael'], ['rachael', 'rachel'], ['nova', 'Nova'], ['nóva', 'nova']]

# A message, the agent it names, the default of the Host and the agent that is open.
TARGETS = [
    {'text': 'Rachel, check the branch', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': 'claire'},
    {'text': 'check the branch', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': 'claire'},
    {'text': 'check the branch', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': ''},
    {'text': 'check the branch', 'requested': '', 'default': 'claire', 'sticky': ''},
    {'text': 'check the branch', 'requested': 'nobody', 'default': 'claire', 'sticky': ''},
    {'text': 'check the branch', 'requested': 'mike-1a2b', 'default': 'claire', 'sticky': 'gone'},
    {'text': 'check the branch', 'requested': 'nobody', 'default': 'nobody-either', 'sticky': ''},
    {'text': 'Rachel', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': ''},
    {'text': 'Rachel ...', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': 'claire'},
    {'text': 'check the branch', 'requested': '  mike-1a2b  ', 'default': 'claude', 'sticky': ''},
    {'text': 'check the branch', 'requested': '   ', 'default': 'claire', 'sticky': ''},
    {'text': 'check the branch', 'requested': 'mike-1a2b', 'default': 'claude', 'sticky': '  '},
    {'text': 'hey mikey, hello', 'requested': 'rachel', 'default': 'claude', 'sticky': 'rachel'},
    {'text': '', 'requested': 'rachel', 'default': 'claude', 'sticky': ''},
]


@tool_scratch.case('clarp-addressed-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's functions say."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import routing, send_service

    def named(agents: list, said: list) -> list:
        table = {session: {'name': name} for session, name in agents}
        rows = []
        for text in said:
            session, rest = routing.resolve_agent_by_spoken_name(text, table)
            rows.append({'text': text, 'session': session, 'rest': rest})
        return rows

    rooms = [{'agents': MANY, 'said': named(MANY, SAID)}, {'agents': PAIRS, 'said': named(PAIRS, SAID_TO_PAIRS)},
             {'agents': TWINS, 'said': named(TWINS, SAID_TO_TWINS)},
             {'agents': RHYMES, 'said': named(RHYMES, SAID_TO_RHYMES)}, {'agents': [], 'said': named([], ['Rachel go'])}]
    alike = [{'a': a, 'b': b, 'alike': routing.word_similarity(a, b)} for a, b in ALIKE]
    # The table of agents is made up: the ones of the first room, and no other.
    table = {session: {'name': name, 'session': session} for session, name in MANY}
    send_service.load_agents = lambda path=None: dict(table)
    send_service.agents_db.get_by_session = lambda session: table.get(session)
    targets = []
    for case in TARGETS:
        target = send_service.resolve_send_target(text=case['text'], requested_session=case['requested'],
                                                  default_session=case['default'], agents_path=None,
                                                  sticky_session=case['sticky'])
        targets.append({**case, 'session': target.session, 'given': target.text, 'by_name': target.routed_by_name})
    shutil.rmtree(root, ignore_errors=True)
    return {'fillers': sorted(routing.LEADING_ADDRESS_FILLERS), 'words': routing.MAX_ADDRESS_TOKENS, 'rooms': rooms,
            'alike': alike, 'targets': targets}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "fillers": ' + json.dumps(said['fillers']) + ',', ' "words": ' + json.dumps(said['words']) + ',',
           ' "rooms": [']
    for at, room in enumerate(said['rooms']):
        out.append('  {"agents": ' + json.dumps(room['agents'], ensure_ascii=True) + ', "said": [')
        rows = room['said']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['rooms']) - 1 else ''))
    out.append(' ],')
    for key in ('alike', 'targets'):
        rows = said[key]
        out.append(f' "{key}": [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append(' ]' + (',' if key == 'alike' else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    routed = sum(1 for room in said['rooms'] for row in room['said'] if row['session'])
    total = sum(len(room['said']) for room in said['rooms'])
    print(f'{OUT.relative_to(REPO)}: {total} messages ({routed} for an agent by its name), {len(said["alike"])} pairs of '
          f'words, {len(said["targets"])} targets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
