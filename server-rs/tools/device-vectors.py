#!/usr/bin/env python3
"""Writes the vectors the Rust Host's register of push devices is tested against.

A phone that wants to be told of finished turns registers a push token with its Host. The
Python Host's rules for that register are in `server/lib/apns.py`: `register_token` (who owns
a token, what a new token of the same phone retires, what is refused), `disable_tokens`,
`disable_token`, `_clear_grant`, `_mark_pushed`, `active_tokens`, `push_token_rows`. This
calls them in sequences, in a database of its own with a fixed clock, and writes each call
with what it answered and the whole register as it then stands to
`crates/clarp-kernel/tests/fixtures/device-vectors.json`.

    python3 server-rs/tools/device-vectors.py            # write the vectors
    python3 server-rs/tools/device-vectors.py --show N   # print scenario N's calls instead

Nothing of a live Host is touched and nothing is sent: the library is pointed at a directory
under /var/tmp that is made new on every run. Which paired devices may receive is the
scenario's to say, in place of the pairing table. Every token is synthetic.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'device-vectors.json'
T0 = 1_794_900_000_000
STEP_MS = 1_000

A = 'a1' * 32            # an APNs token: 64 hex digits
B = 'b2' * 32
C = 'c3' * 32
LONG = 'd4' * 100        # 200 hex digits: the longest
TOO_LONG = 'e5' * 101
SHORT = 'f6' * 20
FCM = 'fcm_Token-01:' + 'Zz09_-' * 10
GRANT_1 = 'pg1.' + '1' * 64 + '.' + '2' * 16 + '.' + '3' * 64
GRANT_2 = 'pg1.' + '4' * 64 + '.' + '5' * 16 + '.' + '6' * 64
PHONE = 'device_phone'
TABLET = 'device_tablet'


def isolate(root) -> None:
    """Points the library at a place of its own before it is imported."""
    os.environ.update({
        'CLAUDE_PWA_DB': str(root / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(root / 'logs'),
        'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_SHARE_DIR': str(root / 'share'),
        'CLARP_CACHE_DIR': str(root / 'cache'), 'CLAUDE_PWA_CONFIG': str(root / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


class World:
    """One scenario's clock, register and paired devices."""

    def __init__(self, number: int, root: pathlib.Path):
        from lib import apns, clock, db
        self.apns = apns
        self.now = T0 + number * 86_400_000
        for module in (clock, db):
            module.now_ms = lambda: self.now
        db.reset_for_tests(root / f'scenario-{number}.sqlite')
        # The devices that are paired with full scope and not revoked. Anything that is not
        # a device (the administrator, nobody) may always receive, as on the Python Host.
        self.paired = {PHONE, TABLET}
        apns.owner_can_receive = lambda owner: (not owner or not owner.startswith('device_')
                                                or owner in self.paired)
        self.calls: list[dict] = []

    def register_rows(self) -> list[dict]:
        from lib import db
        return [dict(row) for row in db.conn().execute('SELECT * FROM device_tokens ORDER BY token')]

    def call(self, op: str, args: dict, run, note: str = '') -> None:
        self.now += STEP_MS
        apns = self.apns
        try:
            answer = {'ok': run()}
        except apns.TokenOwnedElsewhere:
            answer = {'refused': 'owned_elsewhere'}
        except apns.InvalidDeviceToken as exc:
            answer = {'refused': 'invalid', 'why': str(exc)}
        except ValueError as exc:
            answer = {'refused': 'value', 'why': str(exc)}
        from lib import db
        db.conn().commit()
        call = {'op': op, 'at': self.now, 'args': args, 'paired': sorted(self.paired), 'answer': answer,
                'register': self.register_rows(), 'active': apns.active_tokens(), 'shown': apns.push_token_rows(),
                'shown_to_phone': apns.push_token_rows(owner=PHONE)}
        if note:
            call['note'] = note
        self.calls.append(call)

    def register(self, token, note: str = '', **args) -> None:
        self.call('register', {'token': token, **args}, lambda: self.apns.register_token(token, **args), note)

    def disable(self, note: str = '', **args) -> None:
        self.call('disable', args, lambda: self.apns.disable_tokens(**args), note)

    def dead(self, token: str, reason: str = 'Unregistered') -> None:
        self.call('dead', {'token': token, 'reason': reason}, lambda: self.apns.disable_token(token, reason))

    def grant_revoked(self, token: str) -> None:
        self.call('grant_revoked', {'token': token}, lambda: self.apns._clear_grant(token))

    def pushed(self, token: str) -> None:
        self.call('pushed', {'token': token}, lambda: self.apns._mark_pushed(token))

    def revoke(self, device: str) -> None:
        """The device is no longer paired. (What a revocation does to its rows is a call of its own.)"""
        self.paired.discard(device)


def a_phone_registers_and_its_token_rotates(w: World) -> None:
    w.register(A, session='vera', environment='production', platform='ios', owner=PHONE,
               base_url='https://host.example:7682/', push_grant=GRANT_1)
    w.register(A.upper(), session='vera', owner=PHONE, note='the same token in capitals: one token')
    w.pushed(A)
    w.register(B, session='omar', owner=PHONE, note='a new token of the same phone retires the old one')
    w.register(C, session='vera', environment='sandbox', owner=PHONE, note='another environment: both stay')
    w.register(B, owner=PHONE, base_url='', push_grant='', note='no grant and no address sent: what it had stays')
    w.register(B, owner=PHONE, base_url='http://100.80.1.2:7682', push_grant=GRANT_2)
    w.dead(C)
    w.register(C, environment='sandbox', owner=PHONE, note='registered again: it lives again')
    w.grant_revoked(B)
    w.disable(token=B, only_owner=PHONE, reason='unregistered-by-device')
    w.disable(token=B, only_owner=PHONE, note='nothing left to disable')


def tokens_have_owners(w: World) -> None:
    w.register(A, session='vera', owner=PHONE)
    w.register(A, session='vera', owner=TABLET, note='refused: the phone owns it')
    w.register(A, session='vera', note='refused: nobody takes an owned token')
    w.register(A.upper(), session='vera', owner=TABLET, note='refused in capitals too')
    w.register(B, session='vera', owner=TABLET)
    w.disable(token=A, only_owner=TABLET, note='the tablet cannot unregister the phone')
    w.disable(owner=TABLET, reason='unregistered-by-administrator')
    w.revoke(PHONE)
    w.register(A, session='vera', note='still refused for nobody after the phone was revoked')
    w.register(A, session='vera', owner=TABLET, note='a device takes over what a revoked device owned')
    w.disable(note='refused: a token or an owner is needed')
    w.disable(token=A)


def registrations_without_a_device(w: World) -> None:
    w.register(A, session='vera', platform='ios')
    w.register(B, session='vera', platform='ios', note='the same session and platform: the older is retired')
    w.register(C, session='vera', platform='desktop', owner=PHONE, note='not a phone: binds no owner')
    w.register('not-hex-but-accepted', session='omar', platform='web')
    w.register(LONG, session='omar', push_grant=GRANT_1)
    w.register(A, session='zoe', push_grant=GRANT_1, note='a grant follows the phone: the other row is retired')
    w.register(A, owner=PHONE, note='an unbound row becomes the phone that registers it')
    w.register(B, session='vera', platform='ios', note='does not retire what a device owns')
    w.register(C, session=None, platform='desktop')
    w.register('  ' + SHORT + '  ', session='  spaced  ', environment='  SANDBOX  ', platform='  ', note='trimmed')


def what_is_refused(w: World) -> None:
    w.register('', session='vera', owner=PHONE, note='empty')
    w.register('   ', note='empty')
    w.register(SHORT, owner=PHONE, note='a device binds only a real token: 64 to 200 hex digits')
    w.register(TOO_LONG, owner=PHONE)
    w.register('not hex at all', owner=PHONE)
    w.register(LONG, owner=PHONE, note='the longest that is taken')
    w.register(A, owner=PHONE, provider='carrier-pigeon', note='no such push service')
    w.register('no token, this!', owner=PHONE, platform='android', note='android means FCM, and this is no FCM token')
    w.register(B, owner=PHONE, platform='android', note='hex digits are a well-formed FCM token too')
    w.register(B, owner=PHONE, platform='ios', note='refused: it is an FCM row now')
    w.register(FCM, owner=PHONE, platform='android', conversation_modes=['agent_live_v1', 'agent_live_v1'])
    w.register(FCM, owner=PHONE, platform='android', provider='apns', note='a token stays with its service')
    w.register(FCM, owner=PHONE, platform='android', note='registered again without modes: none are kept')
    w.register(A, owner=PHONE, base_url='http://8.8.8.8:7682', note='a public address without TLS is not kept')
    w.register(A, owner=PHONE, base_url='https://user:secret@host.example/', push_grant='pg1.short',
               note='no credentials in an address; a malformed grant is no grant')
    w.register(A, owner=PHONE, base_url='http://printer.local:7682/path/', note='a name on the local network')


SCENARIOS = [a_phone_registers_and_its_token_rotates, tokens_have_owners, registrations_without_a_device,
             what_is_refused]


def main(argv: list[str]) -> int:
    with tool_scratch.directory('clarp-device-vectors-') as name:
        return generate(pathlib.Path(name), argv)


def generate(root, argv):
    isolate(root)
    scenarios = []
    for number, scenario in enumerate(SCENARIOS, start=1):
        world = World(number, root)
        scenario(world)
        scenarios.append({'name': scenario.__name__, 'calls': world.calls})
    if len(argv) == 3 and argv[1] == '--show':
        for call in scenarios[int(argv[2]) - 1]['calls']:
            args = {key: (value if len(str(value)) < 30 else str(value)[:27] + '...') for key, value in call['args'].items()}
            print(f"{call['op']} {args}\n  -> {call['answer']}{'  # ' + call['note'] if call.get('note') else ''}")
            for row in call['register']:
                print('     ', {key: (value if len(str(value)) < 14 else str(value)[:11] + '...') for key, value in row.items()
                                if value not in (None, '')})
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/device-vectors.py from server/lib/apns.py. Do not edit by hand.',
               'phone': PHONE, 'scenarios': scenarios}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    calls = sum(len(s['calls']) for s in scenarios)
    refused = sum(1 for s in scenarios for call in s['calls'] if 'refused' in call['answer'])
    print(f'{OUT.relative_to(REPO)}: {len(scenarios)} scenarios, {calls} calls, {refused} refusals, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
