#!/usr/bin/env python3
"""The conformance tool's own check of its registers: what it says of a run, tried on reports
made up for it. Starts no Host and reads no recording.

    python3 server-rs/conformance/selftest.py

What is tried: an awaited difference that the run shows is counted as awaited, never as
intended, and the run passes with it named in the verdict; one the run does not show fails the
run and is named; an id in both registers is an error; an unexplained difference beside an
awaited one fails as it always did; and the register is read as it is written. The rule for a
reference that answers in more than one way and the rules told by a shape are tried as
`conform.py selftest` tries them, here too. Exits 1 where anything does otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conform  # noqa: E402
import diff  # noqa: E402  (tests/replay, put on the path by conform)

ROUTER = [{'id': 'router'}]
BOTH = [{'id': 'router'}, {'id': 'two-turns'}]


def report(*places) -> diff.Report:
    made = diff.Report()
    for scenario, locator in places:
        made.add(scenario, 'http', locator, 'changed', 'claire', 'nova')
    return made


def register(made: diff.Report, *wanted) -> str:
    """Lines of awaited.txt for the differences of a report, as a port's owner writes them."""
    return '\n'.join(f"{d['id']}  443  rebuild/router-443  {d['scenario']} / {d['channel']} / {d['locator']}"
                     for d in made.items if d['locator'] in wanted)


def refused(call) -> str:
    try:
        call()
    except SystemExit as stop:
        return str(stop)
    return ''


def check(said: bool, what: str, failed: list) -> None:
    print(f"{'holds ' if said else 'FAILS '} {what}")
    if not said:
        failed.append(what)


def main() -> int:
    failed: list = []
    team_id, agent_id = 'team-123456789abc', '0123456789abcdef'
    team = {'team_id': team_id}
    names = conform.called([team], {})
    path = conform.by_name(f'/teams/{team_id}/members/{agent_id}', names)
    check(conform.by_name(team, names) == {'team_id': '<team:1>'}
          and conform.team_member_path(path, {agent_id: 'claire'}) == '/teams/<team:1>/members/<agent:claire>'
          and conform.team_member_path(path, {}) == path,
          'team paths keep the issued team and the named member', failed)
    made = report(('router', '[10].body.session'), ('router', '[14].body.session'))
    ids = [d['id'] for d in made.items]
    shown = set(ids)
    awaited = conform.read_awaited('# a head\n\n' + register(made, '[10].body.session', '[14].body.session') + '\n')

    # The register as it is written.
    check(sorted(awaited) == sorted(ids) and all(line['pr'] == 443 and line['scenario'] == 'router'
                                                 for line in awaited.values()),
          'the register is read: an id, the PR, the branch, the scenario', failed)
    check('line 1' in refused(lambda: conform.read_awaited('abc  rebuild/x  router / y\n')),
          'a line without its PR is refused and named', failed)
    twice = register(made, '[10].body.session')
    check('registered twice' in refused(lambda: conform.read_awaited(twice + '\n' + twice)),
          'an id registered twice is refused', failed)

    # Awaited and seen: counted as awaited, the run passes, the verdict names them.
    page = conform.rendered(made, set(), {}, 'a test', (), awaited)
    check('registered intended: 0  awaited: 2  ' in page and 'unexplained: 0' in page,
          'awaited and seen is counted as awaited, never as intended', failed)
    check(all(f'{ident}  awaited' in page for ident in ids), 'each is of the kind awaited in the report', failed)
    lines, ok = conform.verdict([], [], awaited, shown, ROUTER)
    check(ok and lines == [f'THE RUST HOST CONFORMS BUT FOR 2 AWAITED DIFFERENCE(S): PR 443 '
                           f'(rebuild/router-443 owes it): {", ".join(sorted(ids))}'],
          'awaited and seen passes, with the PR, the branch and the ids', failed)

    # Awaited and not seen: the run fails and the id is named.
    lines, ok = conform.verdict([], [], awaited, {ids[0]}, ROUTER)
    check(not ok and lines[0] == f'AWAITED AND NOT SEEN: take it out of awaited.txt: {ids[1]}'
          and 'not seen' in lines[-1], 'awaited and not seen fails, and is named', failed)
    # ... where its scenario ran: a question about another scenario says nothing of it.
    lines, ok = conform.verdict([], [], awaited, set(), [{'id': 'two-turns'}])
    check(ok and lines == ['THE RUST HOST CONFORMS'], 'an awaited difference of a scenario not run is not asked',
          failed)

    # An id in both registers is an error.
    said = refused(lambda: conform.both_registers({ids[0]}, awaited))
    check(ids[0] in said and 'intended.txt and awaited.txt' in said, 'an id in both files is an error, named', failed)

    # An unexplained difference beside an awaited one fails as always.
    more = report(('router', '[10].body.session'), ('router', '[14].body.session'), ('two-turns', '[3].body'))
    other = [d['id'] for d in more.items if d['scenario'] == 'two-turns']
    page = conform.rendered(more, set(), {}, 'a test', (), awaited)
    check('awaited: 2' in page and 'unexplained: 1' in page and f'{other[0]}  UNEXPLAINED' in page,
          'an unexplained difference beside awaited ones is unexplained', failed)
    unexplained = [d for d in more.items if d['id'] not in awaited]
    lines, ok = conform.verdict(unexplained, [], awaited, {d['id'] for d in more.items}, BOTH)
    check(not ok and lines == ['THE RUST HOST DIFFERS: 1 unexplained difference(s), 0 run error(s)'],
          'an unexplained difference beside awaited ones fails', failed)
    # A run error fails though everything else is awaited.
    lines, ok = conform.verdict([], ['router: a Host did not start'], awaited, shown, ROUTER)
    check(not ok, 'a run error fails beside awaited differences', failed)
    # Nothing awaited: as it always was.
    lines, ok = conform.verdict([], [], {}, shown, ROUTER)
    check(ok and lines == ['THE RUST HOST CONFORMS'], 'with nothing awaited the verdict is as it was', failed)
    # A line whose first word is no scenario of the corpus is an error, named by its line: it would
    # never be asked, so never stale.
    slip = conform.read_awaited('# a head\n' + register(made, '[10].body.session').replace(
        ' router / ', ' [10].body.session / router / ') + '\n')
    said = refused(lambda: conform.known_scenarios(slip, conform.scenarios.BY_ID))
    check('line 2' in said and ids[0] in said and "'[10].body.session'" in said,
          'a line that names no scenario of the corpus first is an error, named by its line', failed)
    check(not refused(lambda: conform.known_scenarios(awaited, conform.scenarios.BY_ID)),
          'a line that names a scenario of the corpus is taken', failed)

    # The register in the tree is read, names scenarios of the corpus, and shares no id with intended.txt.
    kept = conform.load_awaited(conform.HERE / 'awaited.txt')
    conform.both_registers(conform.load_intended(conform.HERE / 'intended.txt'), kept)
    tree = refused(lambda: conform.known_scenarios(kept, conform.scenarios.BY_ID))
    check(not tree and all(line['pr'] > 0 for line in kept.values()),
          f'the register in the tree is read ({len(kept)} awaited), names scenarios of the corpus and shares no '
          f'id with intended.txt{": " + tree if tree else ""}', failed)

    # The tool's other rules, as `conform.py selftest` tries them.
    for line in conform.selftest():
        print(f'holds  {line}')
    print(f'{len(failed)} failed' if failed else 'the registers hold')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
