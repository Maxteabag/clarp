#!/usr/bin/env python3
"""Writes the vectors the Rust port of the voice markup is tested against.

The Python Host's `server/lib/voice_markup.py` is the reference. This runs each of its
functions over a corpus of inputs and writes what came out to
`crates/clarp-speech/tests/fixtures/markup-vectors.json`; the Rust tests must give the same.
Run it again when the Python module changes, and commit the result:

    python3 server-rs/tools/markup-vectors.py

Every text is synthetic or comes from the repository's own fixtures and tests.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / 'server'))

from lib import voice_markup as vm  # noqa: E402

OUT = HERE.parent / 'crates' / 'clarp-speech' / 'tests' / 'fixtures' / 'markup-vectors.json'


def corpus() -> list[str]:
    fixture = json.loads((REPO / 'contract' / 'fixtures' / 'voice-display.json').read_text())
    texts = [case['input'] for case in fixture['cases']]
    texts += [
        '', ' ', '\n', 'plain words', '<speak>hello there</speak>',
        'So <vox>um,</vox> it\'s done',
        'All done. <team>refactored the parser</team> Cheers.',
        'Visible reply.\n<oai-mem-citation><citation_entries>x</citation_entries></oai-mem-citation>',
        'Visible.\n<oai-mem-citation><citation_entries>partial',
        'Visible. <environment_context>private </oai-mem-citation>still private</environment_context>',
        'Visible. <ENVIRONMENT_CONTEXT a="1">x</environment_context> after',
        '<vox>um</vox> ready <break time="300ms"/><speed ratio="0.9">now</speed>',
        'one<break time="300ms"/>two',
        'ready <break time="350ms"/> now, <speed ratio="0.9">go</speed><volume value="+2dB">loud</volume><emotion name="calm"/>',
        '<speak>Oh <vox>[laughing]</vox> that worked. [sigh] Finally.</speak>\nSee [the docs](https://x.y) and [x] done [1].',
        'Oh, <vox>[laughing]</vox> that worked.',
        'Done [sigh]. <break time="300ms"/> Next',
        'keep [x] and [1]', '[short pause]: not a tag', '[whispering](link) stays',
        '<speak>First block.</speak> between <speak>Second block!</speak> after',
        '<speak lang="en">With an attribute.</speak> and <SPEAK>upper</SPEAK>',
        '<speak>unclosed block [sigh] to the end',
        '<speak>unclosed block [sigh] to the end\n',
        'Text with  two  spaces ,and space before comma .',
        'Line one\n\n  - <vox>so</vox>, a list item\n  1. <vox>well</vox> another\n> <vox>okay</vox>, quoted',
        '**<vox>right</vox>, bold start** and _<vox>uh</vox> italic_',
        'A - <vox>um</vox> - B', 'A, <vox>um</vox>, B', 'A <vox>um</vox>. B', 'A <vox>um</vox>! b',
        'end with filler <vox>you know</vox>', 'end with filler, <vox>you know</vox>.',
        '<vox>um</vox><vox>uh</vox> twice', '<vox>um</vox>, <vox>uh</vox>; thrice <vox>, </vox> empty core',
        'iOS <vox>um</vox> npm. <vox>so</vox> iOS stays. <vox>and</vox> `code` too. <vox>hm</vox> straße ok.',
        '<vox>hm</vox> ärlig talat. <vox>eh</vox> ça va? <vox>so</vox> 123 go',
        'Ellipsis… <vox>um</vox> next… and — <vox>uh</vox> — dash',
        'tabs\t<vox>um</vox>\tbetween\t\twords',
        'a<break/>b <BREAK time="1s" /> c<break time="200ms">d',
        'Sentence one. Sentence two! Sentence three? Sentence four… Five.',
        'No punctuation at all just a very long run of words ' * 12,
        'A' * 75 + ' <break time="350ms"/> ' + 'B' * 75 + ' tail words here.',
        'x' * 450, ('word ' * 90).strip() + '. ' + ('more ' * 90).strip() + '!',
        'Mixed <speak>one <vox>um</vox> two <break time="300ms"/> three</speak> shown <speak>four [laughing] five</speak>',
        '<team a="b">only team</team>', 'before<team>x</team>after <TEAM>y</TEAM>',
        'unicode “quotes” — dashes – and … ellipsis <vox>so</vox>, ok',
        'colon: <vox>um</vox>: again; <vox>uh</vox>; semi',
        '(<vox>well</vox>, in parens) [<vox>hm</vox> in brackets]',
        'multi\nline <vox>so</vox>\nnext line\n<vox>and</vox> start\n\n<vox>finally</vox>, done',
        '<speak></speak>', '<speak> </speak>', '<vox></vox>', '<vox> </vox> x', '< speak>not a tag</ speak>',
        'trailing spaces   \n  leading', 'comma ,, double ; : mix', 'a [sigh] , b [laughing] ; c',
    ]
    fragments = ['Hello', 'so', '<vox>um</vox>', '<vox>well,</vox>', '<speak>', '</speak>', ',', '.', '!', ' ', '\n',
                 '<break time="300ms"/>', '[sigh]', '<speed ratio="1.1"/>', '- ', '**', 'world', '…', '—', '<team>t</team>',
                 '<oai-mem-citation>c</oai-mem-citation>', '(', ')', ':', 'iOS', 'straße']
    rng = random.Random(20261115)
    for _ in range(360):
        texts.append(''.join(rng.choice(fragments) for _ in range(rng.randint(2, 14))))
    for a, b, c in itertools.product(['', ' ', ', ', '. ', '\n'], ['<vox>um</vox>', '<vox>um,</vox>', '<vox>, um.</vox>'], ['', ' ', ', ', '. ', ' then', ', then', '.\n']):
        texts.append(f'Start{a}{b}{c}end')
    seen, unique = set(), []
    for text in texts:
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def main() -> int:
    texts = corpus()
    vectors = {
        'about': 'Written by server-rs/tools/markup-vectors.py from server/lib/voice_markup.py. Do not edit by hand.',
        'clean_for_display': [[t, vm.clean_for_display(t)] for t in texts],
        'clean_for_display_oneline': [[t, vm.clean_for_display(t, oneline=True)] for t in texts],
        'strip_hidden_blocks': [[t, vm.strip_hidden_blocks(t)] for t in texts],
        'spoken_for_tts': [[t, vm.spoken_for_tts(t)] for t in texts],
        'strip_emotion_tags': [[t, vm.strip_emotion_tags(t)] for t in texts],
        'strip_ssml_for_plain_tts': [[t, vm.strip_ssml_for_plain_tts(t)] for t in texts],
        'speak_blocks_bare': [[t, vm.extract_speak_blocks(t, attributes=False)] for t in texts],
        'speak_blocks_attributes': [[t, vm.extract_speak_blocks(t, attributes=True)] for t in texts],
        'spoken_chunks': [[t, limit, vm.spoken_chunks_for_tts(t, max_chars=limit)] for t in texts for limit in (100, 500, 1800)],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=0) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(texts)} texts, {sum(len(v) for k, v in vectors.items() if k != "about")} vectors, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
