#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The sound cues of a call of the Oracle: the kernel's rules.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'oracle_earcon_vectors'],
}
K = 'clarp-kernel/src/oracle/earcons.rs'
# NOT PLANTED, each with its reason:
#   where the attack ends and the release begins (`i < fade`, `count - i <= fade`): at the sample between, the fade is
#     one whole, so one sample more or less of it is the same sound.
#   that a sample is held between -1 and 1 before it is scaled: no cue has a sample outside, by its levels.
#   how a half is rounded (to the even number, as Python rounds): no sample of a cue is a half.
PLANTS = [
    (K, 'vectors', 'the attack and the release of a tone are twice as long', '    let fade = samples_in(FADE_SECONDS).max(1);', '    let fade = samples_in(FADE_SECONDS).max(1) * 2;'),
    (K, 'vectors', 'a tone of several parts is as loud as all of them together', '        let value = wave / norm;', '        let value = wave;'),
    (K, 'vectors', 'a tone begins at its loudest', '            sum + weight * (2.0 * PI * tone.freq * multiple * t).sin()', '            sum + weight * (2.0 * PI * tone.freq * multiple * t).cos()'),
    (K, 'vectors', 'every tone is an octave lower', '            sum + weight * (2.0 * PI * tone.freq * multiple * t).sin()', '            sum + weight * (PI * tone.freq * multiple * t).sin()'),
    (K, 'vectors', 'the parts of a tone are all at its pitch', '            sum + weight * (2.0 * PI * tone.freq * multiple * t).sin()', '            sum + weight * (2.0 * PI * tone.freq * t).sin() + 0.0 * multiple'),
    (K, 'vectors', 'a tone has no attack', '            envelope = 0.5 - 0.5 * (PI * real(i) / real(fade)).cos();', '            envelope = 1.0;'),
    (K, 'vectors', 'a tone has no release', '            envelope *= 0.5 - 0.5 * (PI * real(count - i) / real(fade)).cos();', '            envelope *= 1.0;'),
    (K, 'vectors', 'a tone that dies away grows louder instead', '            envelope *= (-t / decay).exp();', '            envelope *= (t / decay).exp();'),
    (K, 'vectors', 'every tone is as loud as it can be', '        out.push(value * envelope * tone.level);', '        out.push(value * envelope);'),
    (K, 'vectors', 'a sample is counted in whole seconds', '        let t = real(i) / rate();', '        let t = real(i / 24_000);'),
    (K, 'vectors', 'the silence between two tones is none', '    vec![0.0; samples_in(seconds)]', '    vec![0.0; samples_in(seconds) / 2]'),
    (K, 'vectors', 'a cue is as loud as a sound can be', '    let scale = 32767.0 * 10f64.powf(-18.0 / 20.0);', '    let scale = 32767.0;'),
    (K, 'vectors', 'a sample is cut off and not rounded', '        let whole = (sample.clamp(-1.0, 1.0) * scale).round_ties_even() as i16;', '        let whole = (sample.clamp(-1.0, 1.0) * scale) as i16;'),
    (K, 'vectors', 'the bytes of a sample are the other way round', '        bytes.extend_from_slice(&whole.to_le_bytes());', '        bytes.extend_from_slice(&whole.to_be_bytes());'),
    (K, 'vectors', "nobody's chime is the first note", '        return TINTS[2];', '        return TINTS[0];'),
    (K, 'vectors', 'a name in large letters has another chime', '    let key = python::casefold(python::strip(persona));', '    let key = python::strip(persona).to_string();'),
    (K, 'vectors', 'a name is made small as Rust makes letters small', '    let key = python::casefold(python::strip(persona));', '    let key = python::strip(persona).to_lowercase();'),
    (K, 'vectors', 'the blanks around a name are part of its chime', '    let key = python::casefold(python::strip(persona));', '    let key = python::casefold(persona);'),
    # (`left + byte` in the place of `left * 256 + byte` is NOT a wrong rule and was taken out after the first run: 256
    # leaves 1 when it is told off in fives, so with five notes the sum of the bytes is the same number.)
    (K, 'vectors', 'the note of a name is told off by the last byte of its digest', '        (left * 256 + usize::from(*byte)) % TINTS.len()', '        (left * 255 + usize::from(*byte)) % TINTS.len()'),
    (K, 'vectors', 'the chime of an agent rings an octave up', '                ..plain(root * 1.5, 0.22, BELL)', '                ..plain(root * 2.0, 0.22, BELL)'),
    (K, 'vectors', 'the chime of an agent does not die away', '                decay: Some(0.12),\n                ..plain(root * 1.5, 0.22, BELL)', '                decay: None,\n                ..plain(root * 1.5, 0.22, BELL)'),
    (K, 'vectors', "the chime of an agent begins as a plain tone", '            [tone(&plain(root, 0.09, SOFT)), tone(&rings)].concat()', '            [tone(&plain(root, 0.09, PLAIN)), tone(&rings)].concat()'),
    (K, 'vectors', 'the chime of an agent is the ring alone', '            [tone(&plain(root, 0.09, SOFT)), tone(&rings)].concat()', '            [tone(&rings)].concat()'),
    (K, 'vectors', 'the way back to the Oracle rises', '            tone(&plain(880.00, 0.12, SOFT)),\n            silence(0.03),\n            tone(&plain(659.25, 0.15, SOFT)),', '            tone(&plain(659.25, 0.12, SOFT)),\n            silence(0.03),\n            tone(&plain(880.00, 0.15, SOFT)),'),
    (K, 'vectors', 'the two notes of the way back follow at once', '            silence(0.03),\n', ''),
    (K, 'vectors', 'a handoff that failed sounds as plain tones', '            tone(&plain(220.00, 0.12, LOW)),', '            tone(&plain(220.00, 0.12, PLAIN)),'),
    (K, 'vectors', 'the second tone of a failure is as long as the first', '            tone(&plain(196.00, 0.14, LOW)),', '            tone(&plain(196.00, 0.12, LOW)),'),
    (K, 'vectors', 'the tick of work handed on is twice as loud', '            level: 0.35,', '            level: 0.7,'),
    (K, 'vectors', 'the tick of work handed on dies away slowly', '            decay: Some(0.025),', '            decay: Some(0.25),'),
    (K, 'vectors', 'the bell of a result is a soft tone', '            ..plain(1046.5, 0.40, BELL)', '            ..plain(1046.5, 0.40, SOFT)'),
    (K, 'vectors', 'the bell of a result is as loud as it can be', '            level: 0.7,\n            decay: Some(0.14),', '            level: 1.0,\n            decay: Some(0.14),'),
    (K, 'vectors', 'the bell of a result is a third of a second', '            ..plain(1046.5, 0.40, BELL)', '            ..plain(1046.5, 0.33, BELL)'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
