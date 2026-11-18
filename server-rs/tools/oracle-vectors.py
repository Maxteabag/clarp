#!/usr/bin/env python3
"""Writes down what the Python Host answers about the Oracle's contact and the voice it speaks with.

The Oracle hands what it is asked to look into to one agent, its contact, which the Host keeps and holds
against the agents it has: a contact that names no agent any more is let go and said to be stale. And an
Oracle call runs on one of two voice models, the owner's choice where the Host can run it. Both are small
rules with many corners (who counts as an agent, how a name is compared, what a phone that still sends its
own contact is given), written down here from `server/lib/oracle_contact.py` and
`server/lib/oracle_voice_provider.py` themselves.

    <the Python Host's interpreter> server-rs/tools/oracle-vectors.py

The same file at every run. Nothing is kept anywhere but in the lists of this program, and nothing is logged.
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-vectors.json'
sys.path.insert(0, str(REPO / 'server'))

from lib import oracle_contact, oracle_voice_provider  # noqa: E402

SHARP_S, LONG_S, FI = chr(0xdf), chr(0x17f), chr(0xfb01)
SIGMA, FINAL_SIGMA, BIG_SIGMA = chr(0x3c3), chr(0x3c2), chr(0x3a3)
A_RING = chr(0xc5)


def agent(session, persona, **more):
    return {'session': session, 'persona': persona, 'archived_at': None, 'is_janitor': False, 'role': 'agent',
            'parent_agent_id': None, **more}


ROSTERS = {
    'the usual': [agent('theo-97e5', 'Theo'), agent('mike-86db', 'Mike'), agent('nadia-11aa', 'Nadia'),
                  agent('old-0001', 'Olga', archived_at=1_700_000_000_000), agent('sweep-1', 'Sweeper', is_janitor=True),
                  agent('mike-helper-1', 'Mike', role='helper', parent_agent_id='a-mike'),
                  agent('nadia-22bb', 'Nadia', archived_at=5)],
    'two of a name': [agent('bella-1', 'Bella'), agent('bella-2', 'Bella'), agent('theo-97e5', 'Theo')],
    'a name that is another one\'s session': [agent('theo', 'Mike'), agent('mike-86db', 'theo')],
    'names of other alphabets': [agent('strasse-1', 'Stra' + SHARP_S + 'e'), agent('ase-1', A_RING + 'se'),
                                 agent('sas-1', BIG_SIGMA + 'A' + BIG_SIGMA), agent('fish-1', FI + 'sh')],
    'a helper that is the only one of its name': [agent('scout-1', 'Scout', role='helper', parent_agent_id='a-theo'),
                                                    agent('child-1', 'Kid', parent_agent_id='a-theo'),
                                                    agent('free-1', 'Free', role='helper')],
    'nobody': [],
    'only what is put away': [agent('old-0001', 'Olga', archived_at=7), agent('sweep-1', 'Sweeper', is_janitor=True)],
    'names that are numbers': [agent('7', 'Seven'), agent('eight-8', '8'), agent('true', 'True'), agent('none-1', 'None')],
    'a session written large, and an agent with no name': [agent('Nora-AB12', 'Nora'), agent('blank-1', ''),
                                                           agent('STRASSE-2', 'Gasse')],
}
NAMES = ['theo-97e5', 'THEO-97E5', 'Theo', 'theo', ' theo ', 'mike', 'Mike', 'mike-helper-1', 'nadia', 'nadia-22bb', 'olga',
         'old-0001', 'sweeper', 'sweep-1', 'bella', 'bella-2', 'nobody', '', '   ', None, 5, 7, '7', 8, '8', 'strasse', 'STRASSE',
         'Stra' + SHARP_S + 'e', 'stra' + LONG_S + LONG_S + 'e', 'se', A_RING.lower() + 'se', 'a' + chr(0x30a) + 'se',
         SIGMA + 'a' + FINAL_SIGMA, SIGMA + 'a' + SIGMA, 'fish', FI + 'SH', 'scout', 'scout-1', 'kid', 'free', 'x' * 160,
         'theo-97e5' + ' ' * 200, ' ' * 160 + 'theo', True, False, 0, 0.0, 1.5, [], ['theo'], {}, {'session': 'theo'}, 'True',
         'None', 'a' * 100 + ' ' * 100 + 'b', 'theo' + ' ' * 156 + 'x', "it's", 'say "theo"', "it's \"theo\"", 'tab\there',
         'back\\slash', chr(0x7f) + 'theo', chr(0xa0) + 'theo' + chr(0x2003), 'nora-ab12', 'NORA-AB12', 'Nora-AB12', 'nora',
         'blank-1', 'stra' + SHARP_S + 'e-2', 'strasse-2']


class Kept:
    """What the Host keeps of the Oracle, for one story: a list of its own."""

    def __init__(self):
        self.held = {}

    def get_text(self, key):
        return self.held.get(key, '')

    def get(self, key):
        return self.held.get(key)

    def set_text(self, key, value):
        self.held[key] = value

    def delete(self, key):
        self.held.pop(key, None)


def tried(act):
    try:
        return {'answer': act()}
    except ValueError as error:
        return {'refused': str(error)}


def contacts() -> dict:
    kept = Kept()
    oracle_contact.settings_store = kept
    oracle_contact.log = lambda *said, **more: None
    found, visible, read, taken, used = [], [], [], [], []
    for roster_name, agents in ROSTERS.items():
        oracle_contact.agents_db.list_agents = lambda agents=agents: [dict(row) for row in agents]
        live = [row['session'] for row in oracle_contact.roster()]
        for name in NAMES:
            one = oracle_contact.resolve(name)
            found.append({'roster': roster_name, 'name': name, 'found': None if one is None else one['session']})
            seen = tried(lambda: oracle_contact.resolve_visible(name)['session'])
            visible.append({'roster': roster_name, 'name': name, **seen})
        # What is kept, read: each thing that could be kept, and what is kept after the reading.
        for stored in ['', 'theo-97e5', 'Theo', 'THEO-97E5', 'old-0001', 'sweep-1', 'gone-1', 'bella', 'bella-1', ' theo-97e5 ',
                       'mike', 'strasse-1', 'STRASSE-1', 'Stra' + SHARP_S + 'e', '   ', ' gone-1 ', 'nora-ab12']:
            kept.held = {oracle_contact.KEY: stored} if stored else {}
            answer = oracle_contact.get()
            read.append({'roster': roster_name, 'stored': stored, 'answer': answer, 'kept': kept.held.get(oracle_contact.KEY)})
        # A contact is set: over nothing, and over one that was there.
        for before in ['', 'theo-97e5']:
            for value in NAMES:
                kept.held = {oracle_contact.KEY: before} if before else {}
                answer = tried(lambda: oracle_contact.set(value))
                taken.append({'roster': roster_name, 'before': before, 'value': value, **answer,
                              'kept': kept.held.get(oracle_contact.KEY)})
        # The contact a call uses: the Host's own, else what the phone still sends, where that is an agent.
        for stored in ['', 'theo-97e5', 'gone-1', 'bella']:
            for requested in ['', None, 'mike', 'Mike', 'mike-86db', 'gone-2', 'bella', 'bella-2', 'old-0001', 5, 'x' * 161,
                              ' nadia ', 'nadia-11aa' + ' ' * 200, 'nadia' + ' ' * 156 + 'x', 'NORA-AB12']:
                kept.held = {oracle_contact.KEY: stored} if stored else {}
                answer = oracle_contact.effective(requested, source='a-test')
                used.append({'roster': roster_name, 'stored': stored, 'requested': requested, 'uses': answer,
                             'kept': kept.held.get(oracle_contact.KEY)})
        ROSTERS[roster_name] = {'agents': agents, 'live': live}
    return {'key': oracle_contact.KEY, 'longest': oracle_contact.MAX_LENGTH, 'rosters': ROSTERS, 'found': found,
            'visible': visible, 'read': read, 'set': taken, 'used': used}


class Config:
    """What of the owner's settings the choice of a voice reads."""

    def __init__(self, provider='openai', key='', model='', voice='', of_oracle=None, usual=''):
        self.oracle_voice_provider = provider
        self.oracle_gemini_model = model
        self.oracle_gemini_voice = voice
        self.gemini_voice = usual
        self._key, self._of_oracle = key, of_oracle

    def gemini_key(self):
        return self._key

    def gemini_voice_for(self, persona):
        assert persona == 'Oracle'
        return self._of_oracle


def voices() -> dict:
    kept = Kept()
    oracle_voice_provider.settings_store = kept
    cases = []
    settings = [dict(), dict(provider='gemini'), dict(provider='Gemini'), dict(provider=' gemini'), dict(provider='other'),
                dict(provider=''), dict(provider=None), dict(provider='gemini', key='a-key'), dict(key='a-key'),
                dict(provider='openai', key='a-key', model='gemini-x', voice='Puck'),
                dict(key='a-key', of_oracle='Aoede', usual='Charon'), dict(key='a-key', usual='Charon'),
                dict(key='a-key', voice='', of_oracle='', usual=''),
                dict(key='a-key', voice='Puck', of_oracle='Aoede', usual='Charon')]
    for setting in settings:
        for stored in [None, '', 'gemini', 'openai', 'GEMINI', ' Gemini ', 'other', 'gemini\n']:
            kept.held = {} if stored is None else {oracle_voice_provider.KEY: stored}
            cfg = Config(**setting)
            cases.append({'settings': {'provider': setting.get('provider', 'openai'), 'has_key': bool(setting.get('key')),
                                       'model': setting.get('model', ''), 'voice': setting.get('voice', ''),
                                       'of_oracle': setting.get('of_oracle'), 'usual': setting.get('usual', '')},
                          'stored': stored, 'status': oracle_voice_provider.status(cfg),
                          'effective': oracle_voice_provider.effective(cfg)})
    chosen = []
    for value in ['gemini', 'openai', 'Gemini', ' GEMINI ', 'other', '', None, 5, 'gemini live']:
        kept.held = {}
        answer = tried(lambda: oracle_voice_provider.set_provider(value))
        chosen.append({'value': value, 'refused': answer.get('refused'), 'kept': kept.held.get(oracle_voice_provider.KEY)})
    return {'key': oracle_voice_provider.KEY, 'providers': list(oracle_voice_provider.PROVIDERS),
            'usual_model': oracle_voice_provider.DEFAULT_MODEL, 'cases': cases, 'chosen': chosen}


def main() -> int:
    answered = {'what': 'server/lib/oracle_contact.py and oracle_voice_provider.py', 'contact': contacts(), 'voice': voices()}
    OUT.write_text(json.dumps(answered, indent=1, ensure_ascii=True) + '\n', encoding='ascii')
    contact, voice = answered['contact'], answered['voice']
    print(f'{OUT.relative_to(REPO)}: {len(contact["rosters"])} rosters, {len(contact["found"])} names looked for, '
          f'{len(contact["visible"])} among what an app shows, {len(contact["read"])} readings, {len(contact["set"])} settings, '
          f'{len(contact["used"])} calls; {len(voice["cases"])} choices of a voice, {len(voice["chosen"])} choosings; '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
