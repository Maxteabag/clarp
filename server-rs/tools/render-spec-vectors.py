#!/usr/bin/env python3
"""Writes down what the Python Host's rules of render specs say, case by case.

A render spec is a small form an agent declares and the app draws from a closed catalog (Host contract 102). Its
rules are `server/lib/render_spec.py`, run here as they are: how a body of `/artifacts` is read (`json.loads` of the
bytes with `JsonObject`), the canonical bytes and where they are refused (`canonical`), whether a payload is a spec
(`validate`), whether answers are taken (`admit`), what the agent is told (`message_text`), which client may see a
spec (`server_identity.render_spec_capable`), and which catalog files are loaded (`catalog`, on files of this
program's own). The cases of the reference's own vectors (`tests/fixtures/render_spec/scenarios.py`) are all here,
played through the rules the way `render_spec_store` plays them.

    <the Python Host's interpreter> server-rs/tools/render-spec-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'render-spec-vectors.json'
FIXTURES = REPO / 'tests' / 'fixtures' / 'render_spec'
CATALOGS = REPO / 'contract' / 'catalogs'
BASE = json.loads((FIXTURES / 'build-decision.json').read_text())
LONE = chr(0xd800)


def dumps(value) -> str:
    """A payload as a client writes it, in ASCII."""
    return json.dumps(value)


KEEP = object()


class Written(str):
    """A JSON text a case writes itself, where `json.dumps` would not write it so."""


def form():
    return copy.deepcopy(BASE)


def nodes(payload):
    return payload['spec']['elements']


# ---- reading a body ----------------------------------------------------------------------------------------------

def bodies():
    """Bytes a client may send, and how Python's reader takes them."""
    plain = json.dumps({'a': 1, 'b': ['x', 2.5, None, True, False], 'c': {'d': 'e'}})
    yield 'plain', plain.encode()
    yield 'blanks around', b' \t\r\n' + plain.encode() + b'\n '
    yield 'utf-8 mark', b'\xef\xbb\xbf' + plain.encode()
    yield 'two utf-8 marks', b'\xef\xbb\xbf\xef\xbb\xbf' + plain.encode()
    yield 'utf-16', plain.encode('utf-16')
    yield 'utf-16-le', plain.encode('utf-16-le')
    yield 'utf-16-be', plain.encode('utf-16-be')
    yield 'utf-32', plain.encode('utf-32')
    yield 'utf-32-le', plain.encode('utf-32-le')
    yield 'utf-32-be', plain.encode('utf-32-be')
    yield 'utf-32 with the mark of the big end', b'\x00\x00\xfe\xff' + plain.encode('utf-32-be')
    yield 'utf-16 with the mark of the big end', b'\xfe\xff' + plain.encode('utf-16-be')
    yield 'utf-16 with a byte more', plain.encode('utf-16-le') + b'\x00'
    yield 'utf-32 with a byte more', plain.encode('utf-32-le') + b'\x00'
    yield 'utf-16 of non-ascii', json.dumps({'k': 'ø' + chr(0x1f600)}, ensure_ascii=False).encode('utf-16-le')
    yield 'utf-16 odd length', plain.encode('utf-16-le')[:-1]
    yield 'utf-16 lone half', b'{\x00"\x00k\x00"\x00:\x00"\x00' + b'\x00\xd8' + b'"\x00}\x00'
    yield 'utf-32 beyond unicode', b'\x00\x00\x11\x00' * 2
    yield 'utf-32 beyond unicode inside a text', (b'\xff\xfe\x00\x00' + b'"\x00\x00\x00' + b'\x00\x00\x11\x00'
                                                  + b'"\x00\x00\x00')
    yield 'two bytes', b'1\x00'
    yield 'two bytes big', b'\x001'
    yield 'non-ascii utf-8', json.dumps({'k': 'ø東' + chr(0x1f600)}, ensure_ascii=False).encode()
    yield 'escaped pair', b'{"k": "\\ud83d\\ude00"}'
    yield 'escaped lone high', b'{"k": "\\ud800"}'
    yield 'escaped lone low', b'{"k": "\\udc00"}'
    yield 'escaped high then letter', b'{"k": "\\ud800x"}'
    yield 'escaped high then high', b'{"k": "\\ud800\\ud800"}'
    yield 'escaped high then bad escape', b'{"k": "\\ud800\\uZZZZ"}'
    yield 'escaped high at the end', b'{"k": "\\ud800\\udc00'
    yield 'raw half in utf-8', b'{"k": "\xed\xa0\x80"}'
    yield 'raw pair in utf-8 halves', b'{"k": "\xed\xa0\x80\xed\xb0\x80"}'
    yield 'raw half in a key', b'{"\xed\xa0\x80": 1}'
    yield 'invalid utf-8', b'{"k": "\xff"}'
    yield 'overlong utf-8', b'{"k": "\xc0\xaf"}'
    yield 'overlong three', b'{"k": "\xe0\x80\xaf"}'
    yield 'overlong four', b'{"k": "\xf0\x80\x80\xaf"}'
    yield 'beyond unicode utf-8', b'{"k": "\xf4\x90\x80\x80"}'
    yield 'truncated utf-8', b'{"k": "\xe6\x9d"}'
    yield 'a lead byte above f4', b'"\xf5\x80\x80\x80"'
    yield 'a lead byte where a continuation must be', b'"\xc3\xc3"'
    yield 'control in text', b'{"k": "a\x01b"}'
    yield 'tab in text', b'{"k": "a\tb"}'
    yield 'delete in text', b'{"k": "a\x7fb"}'
    yield 'every escape', b'{"k": "\\"\\\\\\/\\b\\f\\n\\r\\t\\u0041"}'
    yield 'bad escape', b'{"k": "\\x"}'
    yield 'short unicode escape', b'{"k": "\\u12"}'
    yield 'upper hex escape', b'{"k": "\\u00E9"}'
    yield 'nan', b'{"k": NaN}'
    yield 'infinity', b'{"k": Infinity}'
    yield 'minus infinity', b'{"k": -Infinity}'
    yield 'minus nan', b'{"k": -NaN}'
    yield 'minus zero', b'{"k": -0}'
    yield 'minus zero fraction', b'{"k": -0.0}'
    yield 'fraction', b'{"k": 1.5}'
    yield 'exponent', b'{"k": 1e3}'
    yield 'exponent upper signed', b'{"k": 1E+3}'
    yield 'exponent minus', b'{"k": 25e-1}'
    yield 'huge exponent', b'{"k": 1e400}'
    yield 'point without digit', b'{"k": 1.}'
    yield 'exponent without digit', b'{"k": 1e}'
    yield 'leading zero', b'{"k": 01}'
    yield 'leading plus', b'{"k": +1}'
    yield 'lone minus', b'{"k": -}'
    yield 'largest safe', b'{"k": 9007199254740991}'
    yield 'one beyond safe', b'{"k": 9007199254740992}'
    yield 'beyond 64 bits', b'{"k": 18446744073709551616}'
    yield 'beyond 128 bits', b'{"k": -' + b'9' * 60 + b'}'
    yield '4300 digits', b'{"k": ' + b'9' * 4300 + b'}'
    yield '4301 digits', b'{"k": ' + b'9' * 4301 + b'}'
    yield 'minus 4300 digits', b'{"k": -' + b'9' * 4300 + b'}'
    yield 'duplicate key', b'{"a": 1, "b": 2, "a": 3}'
    yield 'duplicate nested', b'{"a": {"x": 1, "x": 1}}'
    yield 'trailing comma object', b'{"a": 1,}'
    yield 'trailing comma array', b'[1,]'
    yield 'empty object', b'{}'
    yield 'empty array', b'[ ]'
    yield 'key not text', b'{1: 2}'
    yield 'no colon', b'{"a" 1}'
    yield 'two values', b'{} {}'
    yield 'nothing', b''
    yield 'only blanks', b'   '
    yield 'bare text', b'"x"'
    yield 'bare number', b'7'
    yield 'true false null', b'[true,false,null]'
    yield 'misspelled true', b'[tru]'
    yield 'form feed blank', b'\x0c{}'
    yield 'nbsp blank', ' {}'.encode()
    yield 'nested 64', b'[' * 64 + b']' * 64
    yield 'nested 66', b'[' * 66 + b']' * 66
    yield 'nested 300', b'[' * 300 + b']' * 300
    yield 'nested 512', b'[' * 512 + b']' * 512
    yield 'nested 513', b'[' * 513 + b']' * 513
    yield 'an object nested 513', b'{"a":' * 513 + b'0' + b'}' * 513
    yield 'nested 512 and 600 brackets in a text', b'[' * 511 + b'"' + b'[' * 600 + b'"' + b']' * 511
    yield 'nested 512 and an escaped quote before brackets in a text', (b'[' * 511 + b'"\\"' + b'[' * 600 + b'"'
                                                                        + b']' * 511)


def describe(value):
    """A value as Python's reader made it, in JSON a reader of plain JSON reads."""
    if value is None:
        return ['null']
    if value is True or value is False:
        return ['bool', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        return ['float']
    if isinstance(value, str):
        if any(0xd800 <= ord(c) < 0xe000 for c in value):
            return ['lone', [ord(c) for c in value]]
        return ['text', value]
    if isinstance(value, list):
        # A chain of lists, each holding only the next, is said as its length: a reader of plain JSON
        # stops 128 levels down.
        depth = 0
        while isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
            value, depth = value[0], depth + 1
        if depth:
            return ['nest', depth, describe(value)]
        return ['array', [describe(v) for v in value]]
    return ['object', [[describe(k), describe(v)] for k, v in value.items()], bool(getattr(value, 'duplicate', False))]


# ---- payloads ----------------------------------------------------------------------------------------------------

def payload_cases():
    """(name, payload or JSON text): every stage of the validator, every bound from both sides."""
    out = []

    def case(name, change=None, text=None, whole=KEEP):
        """`change` changes the form in place (what it answers is passed over); `whole` is another payload
        altogether."""
        payload = form() if whole is KEEP else whole(form())
        if change is not None:
            change(payload)
        out.append((name, Written(text(payload)) if text else payload))

    def put(path, value):
        def change(p):
            target = p
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return change

    def drop(path):
        def change(p):
            target = p
            for key in path[:-1]:
                target = target[key]
            del target[path[-1]]
        return change

    def replace(old, new):
        return lambda p: dumps(p).replace(old, new, 1)

    case('the base form')
    # The payload's own fields.
    case('a list', whole=lambda p: [p])
    case('a text', whole=lambda p: 'x')
    case('null', whole=lambda p: None)
    case('a number', whole=lambda p: 3)
    case('a key twice', text=replace('"version": "build-1"', '"version": "build-1", "version": "build-1"'))
    case('an unknown key', put(['extra'], 1))
    case('two unknown keys, the first by UTF-16', lambda p: p.update({'zeta': 1, 'alpha': 2}))
    case('an unknown key that is no identifier', put(['Bad-Key'], 1))
    case('an unknown key the contract names', put(['title'], 'x'))
    case('an unknown key with half a pair', put([LONE], 1))
    case('an unknown key beyond the basic plane', put([chr(0x1f600)], 1))
    for field in ('version', 'catalog', 'catalog_version', 'spec', 'submit', 'answer_schema'):
        case(f'without {field}', drop([field]))
    case('without spec and version', lambda p: (p.pop('spec'), p.pop('version')) and None)
    case('an unknown key and a missing one', lambda p: (p.pop('spec'), p.update({'extra': 1})) and None)
    # Numbers and texts that have no canonical form, in the order they are met.
    for token in ('1.0', '1e0', '-0.0', '9007199254740992', '-9007199254740992', 'NaN', 'Infinity', '-Infinity',
                  '1E400', '-0', '9007199254740991', '-9007199254740991', '0'):
        case(f'catalog_version written {token}', text=replace('"catalog_version": 1', f'"catalog_version": {token}'))
    case('a fraction in the state', put(['spec', 'state', 'notify'], 0.5))
    case('a fraction in the schema', put(['answer_schema', 'properties', 'answer', 'maxLength'], 2.0))
    case('a fraction in submit', put(['submit', 'label'], 1.5))
    case('half a pair in a text', put(['spec', 'elements', 'topic', 'props', 'text'], 'a' + LONE))
    case('half a pair in a key deep down', put(['spec', 'elements', 'topic', 'props', LONE], 1))
    case('a key twice deep down', text=replace('"level": 1', '"level": 1, "level": 1'))
    case('a key twice in the schema', text=replace('"type": "boolean"', '"type": "boolean", "type": "boolean"'))
    case('two faults: spec before submit', lambda p: (p['submit'].update(label=1.5),
                                                      p['spec']['state'].update(x=0.5)) and None)
    case('two faults: answer_schema before catalog', lambda p: (p['answer_schema'].update(x=0.5),
                                                                p.update(catalog_version=1.5)) and None)

    def deep(levels):
        def change(p):
            value = 1
            for _ in range(levels):
                value = [value]
            p['spec']['state']['deep'] = value
        return change
    case('nested 62 below the payload', deep(60))
    case('nested 64 below the payload', deep(62))
    case('nested 65 below the payload', deep(63))
    case('nested 70 below the payload', deep(68))
    # The catalog and its version.
    case('another catalog', put(['catalog'], 'other'))
    case('a catalog that is a number', put(['catalog'], 1))
    case('a catalog that is a list', put(['catalog'], ['clarp.form']))
    case('catalog version 2', put(['catalog_version'], 2))
    case('catalog version as text', put(['catalog_version'], '1'))
    case('catalog version true', put(['catalog_version'], True))
    case('catalog version null', put(['catalog_version'], None))
    # The version.
    case('an empty version', put(['version'], ''))
    case('a version of 80', put(['version'], 'v' * 80))
    case('a version of 81', put(['version'], 'v' * 81))
    case('a version of 80 signs beyond ASCII', put(['version'], chr(0xf8) * 80))
    case('a version of 81 signs beyond ASCII', put(['version'], chr(0xf8) * 81))
    case('a version of 80 signs beyond the basic plane', put(['version'], chr(0x1f600) * 80))
    case('a version that is a number', put(['version'], 1))
    case('a version that is null', put(['version'], None))
    # The spec as a whole.
    case('a spec that is a list', put(['spec'], []))
    case('a spec that is text', put(['spec'], 'x'))
    case('an unknown key in the spec', put(['spec', 'extra'], 1))
    case('a spec without state', drop(['spec', 'state']))
    case('a spec without root and state', lambda p: (p['spec'].pop('root'), p['spec'].pop('state')) and None)
    case('on in the props', put(['spec', 'elements', 'topic', 'props', 'on'], {'tap': 'x'}))
    case('on in the state', put(['spec', 'state', 'on'], True))
    case('on in the spec', put(['spec', 'on'], 1))
    case('on in an option', lambda p: p['spec']['elements']['selection']['props']['options'][0].update(on=1))
    case('on as an element', lambda p: (nodes(p).update(on={'type': 'divider', 'props': {}}),
                                        nodes(p)['root']['children'].append('on')) and None)
    case('two ons, the first in sorted order', lambda p: (p['spec']['state'].update(on=1),
                                                          nodes(p)['topic']['props'].update(on=1)) and None)
    case('an initial state that is a list', put(['spec', 'state'], []))
    # The answer schema.
    case('a schema that is a list', put(['answer_schema'], []))
    case('a schema with additionalProperties', put(['answer_schema', 'additionalProperties'], False))
    case('a schema with two unknown keywords', lambda p: p['answer_schema'].update({'zz': 1, '$id': 'x'}))
    case('a schema without type', drop(['answer_schema', 'type']))
    case('a schema without properties', drop(['answer_schema', 'properties']))
    case('a schema of type array', put(['answer_schema', 'type'], 'array'))
    case('a schema whose properties are a list', put(['answer_schema', 'properties'], []))
    case('required that is text', put(['answer_schema', 'required'], 'choice'))
    case('required null', put(['answer_schema', 'required'], None))
    case('required empty', put(['answer_schema', 'required'], []))
    case('required with no identifier', put(['answer_schema', 'required'], ['Choice']))
    case('required with a number', put(['answer_schema', 'required'], [1]))
    case('required of no property', put(['answer_schema', 'required'], ['nothing']))
    case('required twice', put(['answer_schema', 'required'], ['choice', 'choice']))
    case('without required', drop(['answer_schema', 'required']))
    case('a property that is no identifier', put(['answer_schema', 'properties', 'Bad-Name'], {'type': 'string'}))
    case('a property named on', put(['answer_schema', 'properties', 'on'], {'type': 'string'}))
    case('a property of 40 signs', put(['answer_schema', 'properties', 'a' * 40], {'type': 'string'}))
    case('a property of 41 signs', put(['answer_schema', 'properties', 'a' * 41], {'type': 'string'}))
    case('a property that is no object', put(['answer_schema', 'properties', 'answer'], 'string'))
    case('a property with pattern', put(['answer_schema', 'properties', 'answer', 'pattern'], 'x'))
    case('a property of type number', put(['answer_schema', 'properties', 'answer', 'type'], 'number'))
    case('a property of type null', put(['answer_schema', 'properties', 'answer', 'type'], None))
    case('a property of type object', put(['answer_schema', 'properties', 'answer', 'type'], 'object'))
    case('a property of no type and no enum', put(['answer_schema', 'properties', 'extra'], {}))
    case('a property of no type and enum null', put(['answer_schema', 'properties', 'extra'], {'enum': None}))
    case('a property of a type and enum null', put(['answer_schema', 'properties', 'extra'],
                                                   {'type': 'string', 'enum': None}))
    case('a property of an empty type and enum', put(['answer_schema', 'properties', 'extra'],
                                                     {'type': '', 'enum': ['a']}))
    case('a property of enum alone', put(['answer_schema', 'properties', 'extra'], {'enum': ['a', 1, True]}))
    case('minLength on a truth', put(['answer_schema', 'properties', 'notify', 'minLength'], 1))
    case('maxLength on an enum alone', put(['answer_schema', 'properties', 'extra'], {'enum': ['a'], 'maxLength': 3}))
    case('minLength below zero', put(['answer_schema', 'properties', 'answer', 'minLength'], -1))
    case('minLength zero', put(['answer_schema', 'properties', 'answer', 'minLength'], 0))
    case('minLength true', put(['answer_schema', 'properties', 'answer', 'minLength'], True))
    case('maxLength as text', put(['answer_schema', 'properties', 'answer', 'maxLength'], '5'))
    case('minLength and maxLength both wrong', lambda p: p['answer_schema']['properties']['answer'].update(
        minLength=-1, maxLength=-1))
    case('minLength above maxLength', put(['answer_schema', 'properties', 'answer', 'minLength'], 2001))
    case('minLength at maxLength', put(['answer_schema', 'properties', 'answer', 'minLength'], 2000))
    case('minLength without maxLength', lambda p: (p['answer_schema']['properties']['answer'].pop('maxLength'),
                                                   p['answer_schema']['properties']['answer'].update(minLength=5))
         and None)
    case('an enum that is text', put(['answer_schema', 'properties', 'choice', 'enum'], 'send'))
    case('an empty enum', put(['answer_schema', 'properties', 'choice', 'enum'], []))
    case('an enum with an object', put(['answer_schema', 'properties', 'extra'], {'enum': [{}]}))
    case('an enum with null', put(['answer_schema', 'properties', 'extra'], {'enum': [None]}))
    case('an enum twice the same', put(['answer_schema', 'properties', 'extra'], {'enum': ['a', 'a']}))
    case('an enum of one and true', put(['answer_schema', 'properties', 'extra'], {'enum': [1, True]}))
    case('an enum of the wrong type', put(['answer_schema', 'properties', 'extra'], {'type': 'string', 'enum': [1]}))
    case('an enum too long for maxLength', put(['answer_schema', 'properties', 'extra'],
                                               {'type': 'string', 'maxLength': 2, 'enum': ['abc']}))
    case('an enum too short for minLength', put(['answer_schema', 'properties', 'extra'],
                                                {'type': 'string', 'minLength': 2, 'maxLength': 3, 'enum': ['a']}))
    case('an enum of whole numbers', put(['answer_schema', 'properties', 'extra'], {'type': 'integer', 'enum': [1, 2]}))
    case('properties sorted: the first fault counts', lambda p: p['answer_schema']['properties'].update(
        {'zz': {'type': 'x'}, 'aa': {'type': 'y'}}))
    # The elements.
    case('elements that are a list', put(['spec', 'elements'], []))
    case('no elements', put(['spec', 'elements'], {}))
    case('a root that is no identifier', put(['spec', 'root'], 'Root'))
    case('a root that is a number', put(['spec', 'root'], 1))
    case('a root that is no element', put(['spec', 'root'], 'nothing'))
    case('an element that is no identifier', lambda p: (nodes(p).update({'Bad': {'type': 'divider', 'props': {}}}),
                                                        nodes(p)['root']['children'].append('Bad')) and None)
    case('an element of 41 signs', lambda p: (nodes(p).update({'d' * 41: {'type': 'divider', 'props': {}}}),
                                              nodes(p)['root']['children'].append('d' * 41)) and None)
    case('an element of 40 signs', lambda p: (nodes(p).update({'d' * 40: {'type': 'divider', 'props': {}}}),
                                              nodes(p)['root']['children'].append('d' * 40)) and None)
    case('an element beginning with a digit', lambda p: (nodes(p).update({'1d': {'type': 'divider', 'props': {}}}),
                                                         nodes(p)['root']['children'].append('1d')) and None)
    case('an element that is no object', put(['spec', 'elements', 'topic'], 'heading'))
    case('an element with an unknown key', put(['spec', 'elements', 'topic', 'on_tap'], 1))
    case('an element without props', drop(['spec', 'elements', 'topic', 'props']))
    case('an element without type', drop(['spec', 'elements', 'topic', 'type']))
    case('an element of no component', put(['spec', 'elements', 'topic', 'type'], 'button'))
    case('an element whose type is a number', put(['spec', 'elements', 'topic', 'type'], 1))
    case('children of a text', put(['spec', 'elements', 'instructions', 'children'], ['topic']))
    case('empty children of a text', put(['spec', 'elements', 'instructions', 'children'], []))
    case('children that are text', put(['spec', 'elements', 'root', 'children'], 'topic'))
    case('children null', put(['spec', 'elements', 'root', 'children'], None))
    case('a child that is no identifier', lambda p: nodes(p)['root']['children'].append('Bad'))
    case('a child that is a number', lambda p: nodes(p)['root']['children'].append(3))
    case('a child that is no element', lambda p: nodes(p)['root']['children'].append('nothing'))
    case('a prop that is unknown', put(['spec', 'elements', 'topic', 'props', 'color'], 'red'))
    case('props that are a list', put(['spec', 'elements', 'topic', 'props'], []))
    case('a heading without text', drop(['spec', 'elements', 'topic', 'props', 'text']))
    case('a heading of level 3', put(['spec', 'elements', 'topic', 'props', 'level'], 3))
    case('a heading of level 4', put(['spec', 'elements', 'topic', 'props', 'level'], 4))
    case('a heading of level 0', put(['spec', 'elements', 'topic', 'props', 'level'], 0))
    case('a heading of level as text', put(['spec', 'elements', 'topic', 'props', 'level'], '1'))
    case('a heading of level true', put(['spec', 'elements', 'topic', 'props', 'level'], True))
    case('a heading of 200 signs', put(['spec', 'elements', 'topic', 'props', 'text'], 'h' * 200))
    case('a heading of 201 signs', put(['spec', 'elements', 'topic', 'props', 'text'], 'h' * 201))
    case('a heading that is empty', put(['spec', 'elements', 'topic', 'props', 'text'], ''))
    case('a heading with a hidden block', put(['spec', 'elements', 'topic', 'props', 'text'],
                                              'a<environment_context>b</environment_context>'))
    case('a heading with a hidden block left open', put(['spec', 'elements', 'topic', 'props', 'text'],
                                                        'a <oai-mem-citation id="1">b'))
    case('a heading with markup that is no hidden block', put(['spec', 'elements', 'topic', 'props', 'text'],
                                                              'a<speak>b</speak>'))
    case('a heading that reads the state', put(['spec', 'elements', 'topic', 'props', 'text'], {'$state': '/answer'}))
    case('a heading beyond ASCII', put(['spec', 'elements', 'topic', 'props', 'text'], 'Bygg ' + chr(0xf8) + chr(0x1f600)))
    case('a text of 4000', put(['spec', 'elements', 'instructions', 'props', 'text'], 't' * 4000))
    case('a text of 4001', put(['spec', 'elements', 'instructions', 'props', 'text'], 't' * 4001))
    case('a text that reads the state', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                            {'$state': '/answer'}))
    case('a text that reads a key not in the schema', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                                          {'$state': '/nothing'}))
    case('a text that reads without a slash', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                                  {'$state': 'answer'}))
    case('a text that reads two segments', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                               {'$state': '/answer/x'}))
    case('a text that reads with the wrong operator', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                                          {'$bindState': '/answer'}))
    case('a text that reads with more', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                            {'$state': '/answer', 'x': 1}))
    case('a text that reads a number', put(['spec', 'elements', 'instructions', 'props', 'text'], {'$state': 1}))
    case('a text that reads the shortest pointer', put(['spec', 'elements', 'instructions', 'props', 'text'],
                                                       {'$state': '/'}))
    case('a text that is a list', put(['spec', 'elements', 'instructions', 'props', 'text'], ['x']))
    case('a text read twice is no binding', lambda p: (nodes(p).update(again={'type': 'text', 'props': {
        'text': {'$state': '/answer'}}}), nodes(p)['root']['children'].append('again'),
        nodes(p)['instructions']['props'].update(text={'$state': '/answer'})) and None)
    case('a field without label', drop(['spec', 'elements', 'comment', 'props', 'label']))
    case('a field with an empty label', put(['spec', 'elements', 'comment', 'props', 'label'], ''))
    case('a field with a label of 120', put(['spec', 'elements', 'comment', 'props', 'label'], 'l' * 120))
    case('a field with a label of 121', put(['spec', 'elements', 'comment', 'props', 'label'], 'l' * 121))
    case('a field with a placeholder of 121', put(['spec', 'elements', 'comment', 'props', 'placeholder'], 'p' * 121))
    case('a field that is multiline', put(['spec', 'elements', 'comment', 'props', 'multiline'], True))
    case('a field multiline as text', put(['spec', 'elements', 'comment', 'props', 'multiline'], 'yes'))
    case('a field without value', drop(['spec', 'elements', 'comment', 'props', 'value']))
    case('a field bound by text', put(['spec', 'elements', 'comment', 'props', 'value'], '/answer'))
    case('a field bound to no key', put(['spec', 'elements', 'comment', 'props', 'value'], {'$bindState': '/nothing'}))
    case('a field bound without a slash', put(['spec', 'elements', 'comment', 'props', 'value'],
                                              {'$bindState': 'answer'}))
    case('a field bound to no identifier', put(['spec', 'elements', 'comment', 'props', 'value'],
                                               {'$bindState': '/Answer'}))
    case('a field bound with the wrong operator', put(['spec', 'elements', 'comment', 'props', 'value'],
                                                      {'$state': '/answer'}))
    case('a field bound with more', put(['spec', 'elements', 'comment', 'props', 'value'],
                                        {'$bindState': '/answer', 'x': 1}))
    case('a field bound to a pointer of 42', lambda p: (p['answer_schema']['properties'].update({'a' * 41: {
        'type': 'string'}}), nodes(p)['comment']['props'].update(value={'$bindState': '/' + 'a' * 41})) and None)
    case('a field bound to a truth', put(['spec', 'elements', 'comment', 'props', 'value'], {'$bindState': '/notify'}))
    case('a field bound to a choice', put(['spec', 'elements', 'comment', 'props', 'value'], {'$bindState': '/choice'}))
    case('a field bound to a whole number', lambda p: (p['answer_schema']['properties'].update(count={
        'type': 'integer'}), nodes(p)['comment']['props'].update(value={'$bindState': '/count'})) and None)
    case('a field bound to an enum without type', lambda p: (p['answer_schema']['properties'].update(pick={
        'enum': ['a']}), nodes(p)['comment']['props'].update(value={'$bindState': '/pick'})) and None)
    case('a key bound twice', lambda p: (nodes(p).update(again={'type': 'text_field', 'props': {
        'label': 'Again', 'value': {'$bindState': '/answer'}}}), nodes(p)['root']['children'].append('again'))
         and None)
    case('a key bound twice, the second in sorted order', lambda p: (nodes(p).update(zz={'type': 'text_field', 'props': {
        'label': 'Again', 'value': {'$bindState': '/answer'}}}), nodes(p)['root']['children'].append('zz')) and None)
    case('a toggle bound to text', put(['spec', 'elements', 'notify', 'props', 'value'], {'$bindState': '/answer'}))
    case('a toggle without label', drop(['spec', 'elements', 'notify', 'props', 'label']))
    case('a choice of one option', lambda p: nodes(p)['selection']['props']['options'].pop())
    case('a choice of twelve options', lambda p: (p['answer_schema']['properties']['choice'].update(
        enum=[f'o{i}' for i in range(12)]), nodes(p)['selection']['props'].update(
        options=[{'value': f'o{i}', 'label': f'O{i}'} for i in range(12)])) and None)
    case('a choice of thirteen options', lambda p: (p['answer_schema']['properties']['choice'].update(
        enum=[f'o{i}' for i in range(13)]), nodes(p)['selection']['props'].update(
        options=[{'value': f'o{i}', 'label': f'O{i}'} for i in range(13)])) and None)
    case('options that are an object', put(['spec', 'elements', 'selection', 'props', 'options'], {'a': 1}))
    case('an option that is text', lambda p: nodes(p)['selection']['props']['options'].__setitem__(0, 'send'))
    case('an option without label', lambda p: nodes(p)['selection']['props']['options'][0].pop('label'))
    case('an option with more', lambda p: nodes(p)['selection']['props']['options'][0].update(icon='x'))
    case('an option with an empty value', lambda p: nodes(p)['selection']['props']['options'][0].update(value=''))
    case('an option value of 121', lambda p: nodes(p)['selection']['props']['options'][1].update(value='v' * 121))
    case('options in another order', lambda p: nodes(p)['selection']['props']['options'].reverse())
    case('options fewer than the enum', lambda p: p['answer_schema']['properties']['choice']['enum'].append('later'))
    case('a choice bound to text without enum', put(['spec', 'elements', 'selection', 'props', 'value'],
                                                    {'$bindState': '/answer'}))
    case('a choice bound to an enum of numbers', lambda p: (p['answer_schema']['properties'].update(pick={
        'type': 'integer', 'enum': [1, 2]}), nodes(p)['selection']['props'].update(value={'$bindState': '/pick'}))
         and None)
    case('a choice bound to an enum without type', lambda p: (p['answer_schema']['properties']['choice'].pop('type'))
         and None)
    case('a choice without options', drop(['spec', 'elements', 'selection', 'props', 'options']))
    case('a stack across', put(['spec', 'elements', 'root', 'props', 'axis'], 'horizontal'))
    case('a stack aslant', put(['spec', 'elements', 'root', 'props', 'axis'], 'diagonal'))
    case('a stack whose axis is an object', put(['spec', 'elements', 'root', 'props', 'axis'], {'$state': '/answer'}))
    case('a card with a title of 120', lambda p: (nodes(p).update(box={'type': 'card', 'props': {'title': 'c' * 120},
                                                                       'children': ['topic']}),
                                                  nodes(p)['root']['children'].remove('topic'),
                                                  nodes(p)['root']['children'].append('box')) and None)
    case('a card with a title of 121', lambda p: (nodes(p).update(box={'type': 'card', 'props': {'title': 'c' * 121},
                                                                       'children': ['topic']}),
                                                  nodes(p)['root']['children'].remove('topic'),
                                                  nodes(p)['root']['children'].append('box')) and None)
    case('a card in a card', lambda p: (nodes(p).update(outer={'type': 'card', 'props': {}, 'children': ['inner']},
                                                        inner={'type': 'card', 'props': {}, 'children': ['topic']}),
                                        nodes(p)['root']['children'].remove('topic'),
                                        nodes(p)['root']['children'].append('outer')) and None)
    case('a card in a stack in a card', lambda p: (nodes(p).update(
        outer={'type': 'card', 'props': {}, 'children': ['middle']},
        middle={'type': 'stack', 'props': {}, 'children': ['inner']},
        inner={'type': 'card', 'props': {}, 'children': ['topic']}),
        nodes(p)['root']['children'].remove('topic'), nodes(p)['root']['children'].append('outer')) and None)
    case('two cards side by side', lambda p: (nodes(p).update(
        one={'type': 'card', 'props': {}, 'children': ['topic']},
        two={'type': 'card', 'props': {}, 'children': ['instructions']}),
        nodes(p)['root'].update(children=['one', 'two', 'selection', 'comment', 'notify'])) and None)
    case('a divider with a prop', lambda p: (nodes(p).update(line={'type': 'divider', 'props': {'x': 1}}),
                                             nodes(p)['root']['children'].append('line')) and None)
    case('an element nobody holds', lambda p: nodes(p).update(stray={'type': 'divider', 'props': {}}))
    case('an element held twice', lambda p: nodes(p)['root']['children'].append('topic'))
    case('a root that is an element of no identifier', lambda p: (nodes(p).update(Bad=nodes(p).pop('root')),
                                                                  p['spec'].update(root='Bad')) and None)
    case('a child of no identifier held by an element sorted before it', lambda p: (
        nodes(p).update(a={'type': 'stack', 'props': {}, 'children': ['zZ']}, zZ={'type': 'divider', 'props': {}}),
        nodes(p)['root']['children'].append('a')) and None)
    case('two faults in the tree, the first child first', lambda p: (nodes(p).update(
        one={'type': 'card', 'props': {}, 'children': ['inner1']}, inner1={'type': 'card', 'props': {}},
        two={'type': 'card', 'props': {}, 'children': ['inner2']}, inner2={'type': 'card', 'props': {}}),
        nodes(p)['root'].update(children=['one', 'two', 'topic', 'instructions', 'selection', 'comment', 'notify']))
        and None)
    case('a root held by its child', lambda p: (nodes(p).update(inner={'type': 'stack', 'props': {},
                                                                        'children': ['root']}),
                                                nodes(p)['root']['children'].append('inner')) and None)
    case('two faults in the elements, sorted', lambda p: (nodes(p)['topic']['props'].update(level=9),
                                                          nodes(p)['comment']['props'].update(label='')) and None)
    case('a fault in props before one in children', lambda p: (nodes(p)['topic']['props'].update(level=9),
                                                               nodes(p)['root']['children'].append('nothing'))
         and None)

    def chain(levels):
        def change(p):
            for i in range(levels):
                key = f'stack_{i}'
                nodes(p)[key] = {'type': 'stack', 'props': {}, 'children': [p['spec']['root']]}
                p['spec']['root'] = key
        return change
    case('a tree of 12 levels', chain(10))
    case('a tree of 13 levels', chain(11))
    case('a tree of 14 levels', chain(12))
    # The initial state.
    case('a state with a key not in the schema', put(['spec', 'state', 'extra'], 1))
    case('a state with a key that is no identifier', put(['spec', 'state', 'Extra'], 1))
    case('a state with a text for a truth', put(['spec', 'state', 'notify'], 'yes'))
    case('a state with a truth', put(['spec', 'state', 'notify'], True))
    case('a state with a choice', put(['spec', 'state', 'choice'], 'review'))
    case('a state with no choice of the enum', put(['spec', 'state', 'choice'], 'later'))
    case('a state with a text too long', lambda p: (p['answer_schema']['properties']['answer'].update(maxLength=3),
                                                    p['spec']['state'].update(answer='abcd')) and None)
    case('a state with a text too short', lambda p: (p['answer_schema']['properties']['answer'].update(minLength=3),
                                                     p['spec']['state'].update(answer='ab')) and None)
    case('a state with a whole number for a text', put(['spec', 'state', 'answer'], 1))
    case('a state with true for a whole number', lambda p: (p['answer_schema']['properties'].update(count={
        'type': 'integer'}), p['spec']['state'].update(count=True)) and None)
    case('a state with a whole number', lambda p: (p['answer_schema']['properties'].update(count={
        'type': 'integer'}), p['spec']['state'].update(count=-9007199254740991)) and None)
    case('a state of an enum without type', lambda p: (p['answer_schema']['properties'].update(pick={
        'enum': ['a', 1]}), p['spec']['state'].update(pick=1)) and None)
    case('a state of an enum without type, typed otherwise', lambda p: (p['answer_schema']['properties'].update(pick={
        'enum': ['a', 1]}), p['spec']['state'].update(pick=True)) and None)
    case('a state without the required key')
    case('two faults in the state, sorted', lambda p: p['spec']['state'].update(notify='x', choice=1))
    # Submit.
    case('submit that is a list', put(['submit'], []))
    case('submit with more', put(['submit', 'icon'], 'x'))
    case('submit without label', drop(['submit', 'label']))
    case('submit without intent', drop(['submit', 'intent']))
    case('an intent that is no identifier', put(['submit', 'intent'], 'Submit'))
    case('an intent that is a number', put(['submit', 'intent'], 1))
    case('an intent of 40 signs', put(['submit', 'intent'], 'i' * 40))
    case('an intent of 41 signs', put(['submit', 'intent'], 'i' * 41))
    case('an empty label', put(['submit', 'label'], ''))
    case('a label of 40', put(['submit', 'label'], 'l' * 40))
    case('a label of 41', put(['submit', 'label'], 'l' * 41))
    case('a label of 40 signs beyond ASCII', put(['submit', 'label'], chr(0x6771) * 40))
    case('a label that is a number', put(['submit', 'label'], 4))
    case('a label with a hidden block is taken', put(['submit', 'label'], '<environment_context>x</environment_context>'[:40]))
    return out


# ---- answers -----------------------------------------------------------------------------------------------------

SID = 'vector-answers-0001'


def answers_cases():
    """(name, status, data or JSON text, previous change): every branch of `admit`."""
    good = {'submission_id': SID, 'version': 'build-1',
            'answers': {'intent': 'submit_decision', 'state': {'choice': 'send', 'notify': True}}}
    out = []

    def case(name, change=None, *, status='ready', text=None, previous=None, payload=None, whole=KEEP):
        data = copy.deepcopy(good) if whole is KEEP else whole(copy.deepcopy(good))
        if change is not None:
            change(data)
        out.append((name, status, Written(text(data)) if text else data, previous, payload))

    def put(path, value):
        def change(d):
            target = d
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        return change

    def drop(path):
        def change(d):
            target = d
            for key in path[:-1]:
                target = target[key]
            del target[path[-1]]
        return change

    def replace(old, new):
        return lambda d: dumps(d).replace(old, new, 1)

    case('accepted')
    case('accepted while active', status='active')
    case('a draft is not open', status='draft')
    case('a failed one is not open', status='failed')
    case('answered already', status='completed')
    case('a list', whole=lambda d: [d])
    case('a key twice', text=replace('"version": "build-1"', '"version": "build-1", "version": "build-1"'))
    case('an unknown key', put(['extra'], 1))
    case('an unknown key that is no identifier', put(['Extra'], 1))
    case('an unknown key with half a pair', put([LONE], 1))
    case('without answers', drop(['answers']))
    case('without anything', whole=lambda d: {})
    case('without version', drop(['version']))
    case('a submission id of 15', put(['submission_id'], 's' * 15))
    case('a submission id of 16', put(['submission_id'], 's' * 16))
    case('a submission id of 80', put(['submission_id'], 's' * 80))
    case('a submission id of 81', put(['submission_id'], 's' * 81))
    case('a submission id with an underscore', put(['submission_id'], 'vector_answers_0001'))
    case('a submission id beyond ASCII', put(['submission_id'], 'vector-answers-' + chr(0xea) * 4))
    case('a submission id that is a number', put(['submission_id'], 1234567890123456))
    case('a submission id with half a pair', put(['submission_id'], SID + LONE))
    case('a version that is a number', put(['version'], 1))
    case('a version that is null', put(['version'], None))
    case('a version with half a pair', put(['version'], 'build-1' + LONE))
    case('a version that is empty', put(['version'], ''))
    case('answers with a fraction', put(['answers', 'state', 'notify'], 1.0))
    case('answers with a number too large', put(['answers', 'state', 'notify'], 9007199254740992))
    case('answers with a key twice', text=replace('"choice": "send"', '"choice": "send", "choice": "send"'))
    case('answers with half a pair', put(['answers', 'state', 'answer'], LONE))
    case('answers that are a list', put(['answers'], []))
    case('answers that are text', put(['answers'], 'x'))
    case('answers with more', put(['answers', 'extra'], True))
    case('answers without state', drop(['answers', 'state']))
    case('answers without intent', drop(['answers', 'intent']))
    case('a state that is a list', put(['answers', 'state'], []))
    case('audio false', put(['synthesize_audio'], False))
    case('audio true', put(['synthesize_audio'], True))
    case('audio as text', put(['synthesize_audio'], 'yes'))
    case('audio null', put(['synthesize_audio'], None))
    case('audio as one', put(['synthesize_audio'], 1))
    case('another version', put(['version'], 'build-0'))
    case('another intent', put(['answers', 'intent'], 'other'))
    case('an intent that is a number', put(['answers', 'intent'], 1))
    case('without the required choice', drop(['answers', 'state', 'choice']))
    case('a choice not in the enum', put(['answers', 'state', 'choice'], 'later'))
    case('a choice that is a number', put(['answers', 'state', 'choice'], 1))
    case('a truth that is text', put(['answers', 'state', 'notify'], 'true'))
    case('an unknown key in the state', put(['answers', 'state', 'extra'], 1))
    case('an unknown key in the state that is no identifier', put(['answers', 'state', 'Extra'], 1))
    case('a text of 2000', put(['answers', 'state', 'answer'], 'a' * 2000))
    case('a text of 2001', put(['answers', 'state', 'answer'], 'a' * 2001))
    case('an empty text', put(['answers', 'state', 'answer'], ''))
    case('two faults in the state, sorted', lambda d: d['answers']['state'].update(notify='x', choice=1))
    case('the order: version before intent', lambda d: (d.update(version='old'),
                                                        d['answers'].update(intent='other')) and None)
    case('the order: closed before version', lambda d: d.update(version='old'), status='completed')
    case('the order: size before the envelope', lambda d: d['answers'].update(
        extra='x' * 17000))
    case('the order: audio before a receipt', put(['synthesize_audio'], 'x'), previous='same')
    # A submission id that came before.
    case('the same answers again', previous='same')
    case('the same answers again on a closed spec', previous='same', status='completed')
    case('other answers under the same id', put(['answers', 'state', 'choice'], 'review'), previous='same')
    case('the same answers to another version', put(['version'], 'build-0'), previous='same')
    case('the same answers, another audio said', put(['synthesize_audio'], False), previous='same')
    case('the same answers, the same audio said', put(['synthesize_audio'], True), previous='same')
    case('the same answers, audio not said, kept false', previous='same, audio false')
    case('the same answers, audio said false, kept false', put(['synthesize_audio'], False),
         previous='same, audio false')
    case('answers under an id of another form', previous='other')
    # A payload with a minimum length and a whole number.
    with_count = form()
    with_count['answer_schema']['properties']['count'] = {'type': 'integer'}
    with_count['answer_schema']['properties']['answer']['minLength'] = 2
    with_count['answer_schema']['required'].append('count')
    case('a whole number answered', lambda d: d['answers']['state'].update(count=-5, answer='ab'),
         payload=with_count)
    case('a whole number missing', lambda d: d['answers']['state'].update(answer='ab'), payload=with_count)
    case('true for a whole number', lambda d: d['answers']['state'].update(count=True, answer='ab'),
         payload=with_count)
    case('a text too short', lambda d: d['answers']['state'].update(count=1, answer='a'), payload=with_count)
    case('two required missing, sorted', lambda d: d['answers'].update(state={}), payload=with_count)
    return out


# ---- clients ------------------------------------------------------------------------------------------------------

def clients():
    headers = [None, '', 'ios/12 contract=73', 'ios/12 contract=74', 'ios/12 contract=75', 'ios/ contract=99',
               'ios/12 contract=99 garbage', 'ios/12 contract=0', 'ios/12 contract=999999999',
               'ios/12 contract=1000000000', 'ios/12 contract=000000074', 'a/b contract=74', '1os/12 contract=74',
               'ios-x_Y9/1.2_3-4 contract=74', 'ios/12  contract=74', ' ios/12 contract=74', 'ios/12 contract=74 ',
               'ios/12 contract=+74', 'ios/12 contract=', 'ios/12', 'ios', 'ios/12 Contract=74',
               'ios/12 contract=74 contract=75', 'ios/1/2 contract=74', 'ios/12\tcontract=74',
               'ios/' + 'b' * 80 + ' contract=74', 'ios/' + 'b' * 81 + ' contract=74',
               'p' * 32 + '/1 contract=74', 'p' * 33 + '/1 contract=74', 'ios/' + chr(0xf8) + ' contract=74',
               'ios/12 contract=7' + chr(0x664), '_os/12 contract=74', 'i.s/12 contract=74', 'ios/1 2 contract=74']
    minimums = [None, -1, 0, 1, 74, 75, 999999999, 1000000000]
    return headers, minimums


# ---- the messages --------------------------------------------------------------------------------------------------

def messages():
    titles = ['Decide', 'Say "go" \\ ' + chr(0xf8) + ', ' + chr(0x6771) + chr(0x4eac), 'a\nb\tc\r\x08\x0c\x01\x1f\x7f',
              chr(0x1f600) + ' ' + chr(0x2028), '']
    answers = [{'intent': 'submit_decision', 'state': {'choice': 'send', 'notify': True}},
               {'state': {'z': 1, 'a': [1, -2, 'x'], chr(0xe000): None, chr(0x1f600): 'y'}, 'intent': 'i'}]
    out = []
    for title in titles:
        for given in answers:
            out.append(({'title': title, 'artifact_id': 'vector-message'},
                        {'version': 'v' + chr(0xf8), 'submission_id': SID, 'answers': given}))
    return out


# ---- broken catalogs -----------------------------------------------------------------------------------------------

def catalogs():
    raw = (CATALOGS / 'clarp.form.v1.json').read_bytes()
    reasons = (CATALOGS / 'clarp.form.v1.refusals.json').read_bytes()
    data = json.loads(raw)
    out = [('the catalog as it is', raw, reasons)]

    def variant(name, change, *, refusals=None):
        changed = copy.deepcopy(data)
        change(changed)
        out.append((name, json.dumps(changed, indent=2).encode(), refusals or reasons))

    variant('the same catalog written otherwise', lambda d: None)
    variant('without schema_keywords', lambda d: d.pop('schema_keywords'))
    variant('a keyword of no schema language', lambda d: d['schema_keywords'].append('pattern'))
    variant('a schema keyword it does not name', lambda d: d['schema_keywords'].remove('minItems'))
    variant('a shape that requires two', lambda d: d['expression_shapes']['$state']['required'].append('x'))
    variant('a shape that requires none', lambda d: d['expression_shapes']['$state'].pop('required'))
    variant('a shape that is no object', lambda d: d['expression_shapes'].update(x=[]))
    variant('children that are no truth', lambda d: d['components']['stack'].update(children=1))
    variant('without children', lambda d: d['components']['stack'].pop('children'))
    variant('a descendant of no component', lambda d: d['components']['card'].update(forbid_descendants=['box']))
    variant('a binding of no property', lambda d: d['components']['toggle']['bindings'].update(other='boolean'))
    variant('a binding of no kind', lambda d: d['components']['toggle']['bindings'].update(value='number'))
    variant('a read of no shape', lambda d: d['components']['text']['reads'].update(text='$other'))
    variant('a read of no property', lambda d: d['components']['text']['reads'].update(title='$state'))
    variant('a bound property that requires two', lambda d: d['components']['toggle']['props']['properties'][
        'value']['required'].append('x'))
    variant('a bound property of another shape', lambda d: d['components']['toggle']['props']['properties'][
        'value']['properties']['$bindState'].update(maxLength=40))
    variant('a bound property written in another order', lambda d: d['components']['choice']['props'][
        'properties'].update(value=dict(reversed(list(d['components']['choice']['props']['properties'][
            'value'].items())))))
    variant('a bound property whose length is written as a fraction', lambda d: d['components']['toggle'][
        'props']['properties']['value']['properties']['$bindState'].update(maxLength=41.0))
    variant('a bound property whose length is written as a truth', lambda d: d['components']['toggle'][
        'props']['properties']['value']['properties']['$bindState'].update(minLength=True))
    variant('without the shape of a binding', lambda d: d['expression_shapes'].pop('$bindState'))

    def truthful(d):
        d['expression_shapes']['$bindState']['properties']['$bindState']['minLength'] = 1
        for name in ('text_field', 'toggle', 'choice'):
            d['components'][name]['props']['properties']['value']['properties']['$bindState']['minLength'] = True
    variant('bound properties whose least length is a truth where the shape says one', truthful)
    variant('a type of no schema language', lambda d: d['components']['heading']['props']['properties'][
        'level'].update(type='number'))
    variant('a keyword deep in a schema', lambda d: d['components']['choice']['props']['properties']['options'][
        'items']['properties']['value'].update(pattern='x'))
    variant('items that are no schema', lambda d: d['components']['choice']['props']['properties']['options'].update(
        items=[]))
    variant('props without properties', lambda d: d['components']['divider']['props'].pop('properties'))
    variant('identifiers of length 0', lambda d: d['identifiers'].update(minLength=0))
    variant('identifiers shorter at most than at least', lambda d: d['identifiers'].update(minLength=5, maxLength=4))
    variant('identifiers of no first sign', lambda d: d['identifiers'].update(first=''))
    variant('identifiers of no other sign', lambda d: d['identifiers'].update(rest=''))
    variant('identifiers of a narrower first sign', lambda d: d['identifiers'].update(first='abc'))
    variant('identifiers of signs as a list', lambda d: d['identifiers'].update(first=['ab', 'c'], rest=['a', 'b_']))
    variant('identifiers of length 1 to 3', lambda d: d['identifiers'].update(maxLength=3))
    variant('without identifiers', lambda d: d.pop('identifiers'))
    variant('identifiers whose least length is true', lambda d: d['identifiers'].update(minLength=True))
    variant('options whose items have no type', lambda d: d['components']['choice']['props']['properties'][
        'options']['items'].pop('type'))
    variant('an axis that is a constant', lambda d: d['components']['stack']['props']['properties'].update(
        axis={'const': 'vertical'}))
    variant('a level with bounds and no list', lambda d: d['components']['heading']['props']['properties'].update(
        level={'type': 'integer', 'minimum': 1, 'maximum': 2}))
    variant('a heading of length at most true', lambda d: d['components']['heading']['props']['properties'][
        'text'].update(maxLength=True))
    variant('heading props that are open', lambda d: d['components']['heading']['props'].update(
        additionalProperties=True))
    variant('heading props without a closing word', lambda d: d['components']['heading']['props'].pop(
        'additionalProperties'))
    variant('without components', lambda d: d.pop('components'))
    refusals = json.loads(reasons)
    refusals.pop('number_form')
    out.append(('refusals without a code', raw, json.dumps(refusals).encode()))
    refusals = json.loads(reasons)
    refusals['extra'] = 'More.'
    out.append(('refusals with more', raw, json.dumps(refusals).encode()))
    out.append(('a catalog that is no JSON', raw[:-10], reasons))
    out.append(('refusals that are no JSON', raw, b'{'))
    out.append(('a catalog that is a list', b'[]', reasons))
    return out


def probes():
    """Payloads every catalog that loads is asked about."""
    out = [form()]
    payload = form()
    nodes(payload)['selection']['props']['options'][0]['icon'] = 'x'
    out.append(payload)
    payload = form()
    nodes(payload)['root']['props']['axis'] = 'horizontal'
    out.append(payload)
    for level in (0, 2, 3, True):
        payload = form()
        nodes(payload)['topic']['props']['level'] = level
        out.append(payload)
    payload = form()
    nodes(payload)['topic']['props']['color'] = 'red'
    out.append(payload)
    payload = form()
    nodes(payload)['topic']['props']['text'] = 'x'
    out.append(payload)
    return out


def envelope_cases():
    """(name, JSON text): the texts of a publication or an update, as given."""
    half = chr(0xd800)
    out = [('all of them text', json.dumps({key: 'text' for key in
                                            ('session', 'title', 'summary', 'status', 'reference_id', 'artifact_id')})),
           ('none given', '{}'), ('another key is not looked at', json.dumps({'type': 7}))]
    for key in ('session', 'title', 'summary', 'status', 'reference_id', 'artifact_id'):
        out.append((f'half a pair in {key}', json.dumps({key: 'text ' + half})))
    for key in ('title', 'summary', 'status'):
        for name, value in (('a number', 7), ('null', None), ('a list', ['ready']), ('a truth', True),
                            ('an object', {'a': 1}), ('a fraction', 1.5), ('empty', '')):
            out.append((f'{key} that is {name}', json.dumps({key: value})))
    out.append(('the first fault in the contract\'s order', json.dumps({'status': 7, 'title': None})))
    out.append(('the first fault in the contract\'s order, given the other way', json.dumps({'summary': None,
                                                                                           'title': 7})))
    out.append(('a pair joined is text', json.dumps({'title': chr(0x1f600)})))
    return out


# ---- running the rules ---------------------------------------------------------------------------------------------

def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-render-spec-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import json_depth, render_spec as rs, server_identity
    scen_spec = importlib.util.spec_from_file_location('render_spec_scenarios', FIXTURES / 'scenarios.py')
    scenarios = importlib.util.module_from_spec(scen_spec)
    scen_spec.loader.exec_module(scenarios)

    def refusal(error):
        return error.triple()

    def read(raw):
        # As the Host's reader: a body nested deeper than its bound is refused before it is parsed.
        if json_depth.too_deep(raw):
            raise ValueError('too deep')
        return json.loads(raw, object_pairs_hook=rs.JsonObject)

    try:
        # Reading bodies, and their canonical form.
        reads = []
        for name, raw in bodies():
            one = {'name': name, 'bytes': base64.b64encode(raw).decode()}
            try:
                value = read(raw)
            except (ValueError, RecursionError):
                one['read'] = None
            else:
                one['read'] = describe(value)
                try:
                    one['canonical'] = rs.canonical(value).hex()
                except rs.Refusal as error:
                    one['refused'] = refusal(error)
            reads.append(one)

        # Payloads.
        payloads = []
        for name, given in payload_cases():
            text = str(given) if isinstance(given, Written) else dumps(given)
            one = {'name': name, 'text': text}
            payload = read(text.encode())
            try:
                rs.validate(payload)
            except rs.Refusal as error:
                one['refused'] = refusal(error)
            else:
                identity = rs.content_identity(payload['spec'], payload['version'])
                one['stored'] = {**payload, 'content_identity': identity,
                                 'element_count': len(payload['spec']['elements']),
                                 'catalog_sha256': rs.CATALOG_SHA256}
            payloads.append(one)

        # Answers.
        answers = []
        stored_base = None
        for name, status, given, previous, with_payload in answers_cases():
            text = str(given) if isinstance(given, Written) else dumps(given)
            payload = copy.deepcopy(with_payload or BASE)
            rs.validate(payload)
            stored = {**payload, 'content_identity': rs.content_identity(payload['spec'], payload['version']),
                      'element_count': len(payload['spec']['elements']), 'catalog_sha256': rs.CATALOG_SHA256}
            stored_base = stored_base or stored
            ident = 'vector-answers'
            before = None
            if previous:
                base = {'submission_id': SID, 'version': 'build-1',
                        'answers': {'intent': 'submit_decision', 'state': {'choice': 'send', 'notify': True}}}
                if previous == 'other':
                    digest = hashlib.sha256(rs.canonical(['another-form', 'build-1', base['answers']])).hexdigest()
                else:
                    digest = hashlib.sha256(rs.canonical([ident, 'build-1', base['answers']])).hexdigest()
                before = {'payload_hash': digest, 'synthesize_audio': 0 if previous.endswith('audio false') else 1}
            one = {'name': name, 'artifact_id': ident, 'payload': stored, 'status': status, 'text': text,
                   'previous': before}
            data = read(text.encode())
            try:
                result = rs.admit(ident, json.loads(json.dumps(stored)), status, data, before)
            except rs.Refusal as error:
                one['refused'] = refusal(error)
            else:
                one['admitted'] = result
            answers.append(one)

        # The texts besides the payload (render_spec.envelope).
        envelopes = []
        for name, texts in envelope_cases():
            one = {'name': name, 'text': texts}
            try:
                rs.envelope(read(texts.encode()))
                one['refused'] = None
            except rs.Refusal as error:
                one['refused'] = refusal(error)
            envelopes.append(one)

        # The reference's own vectors, played through the rules as its store plays them.
        flows = []
        for scenario in scenarios.scenarios():
            ident = scenario['artifact_id']
            title = scenario.get('title', ident)
            stored, status, kept = None, None, {}
            steps = []
            for step in scenario['steps']:
                done = {'action': step['action'], 'http': step['http']}
                if 'envelope_text' in step or 'envelope' in step:
                    # The texts besides the payload, checked first (R71, R72): a publication's as its
                    # route makes them (str() of a value that is no text), an update's as given.
                    texts = (read(step['envelope_text'].encode()) if 'envelope_text' in step
                             else step['envelope'])
                    if step['action'] == 'publish':
                        texts = {key: value if isinstance(value, str) else str(value or '')
                                 for key, value in texts.items()}
                    done['envelope'] = json.dumps(texts)
                    try:
                        rs.envelope(texts)
                    except rs.Refusal as error:
                        done['refused'] = refusal(error)
                        done['expected_error'] = step.get('error')
                        done['stored_status'] = step['stored_status']
                        steps.append(done)
                        continue
                if step['action'] == 'read':
                    raw = step['body_text'].encode()
                    done['text'] = step['body_text']
                    try:
                        read(raw)
                        done['read'] = True
                    except ValueError:
                        done['read'] = False
                    done['stored_status'] = step['stored_status']
                    steps.append(done)
                    continue
                if step['action'] in ('publish', 'validate', 'update'):
                    text = step.get('payload_text') or dumps(step['payload'])
                    done['text'] = text
                    payload = read(text.encode())
                    try:
                        rs.validate(payload)
                    except rs.Refusal as error:
                        done['refused'] = refusal(error)
                    else:
                        identity = rs.content_identity(payload['spec'], payload['version'])
                        done['identity'] = identity
                        if step['action'] == 'publish':
                            stored = {**payload, 'content_identity': identity,
                                      'element_count': len(payload['spec']['elements']),
                                      'catalog_sha256': rs.CATALOG_SHA256}
                            stored = json.loads(json.dumps(stored))
                            status = 'ready'
                else:
                    text = step.get('body_text') or dumps(step['body'])
                    done['text'] = text
                    data = read(text.encode())
                    sid = data.get('submission_id')
                    before = kept.get(sid)
                    try:
                        result = rs.admit(ident, stored, status, data, before)
                    except rs.Refusal as error:
                        done['refused'] = refusal(error)
                    else:
                        done['admitted'] = result
                        if not result['repeat']:
                            kept[sid] = {'payload_hash': result['payload_hash'],
                                         'synthesize_audio': int(data.get('synthesize_audio', True))}
                            status = 'completed'
                            done['message'] = rs.message_text({'title': title, 'artifact_id': ident}, data)
                    for key in ('payload_hash', 'prompt', 'receipt', 'error'):
                        if key in step:
                            done['expected_' + key] = step[key]
                if 'error' in step and step['action'] != 'submit':
                    done['expected_error'] = step['error']
                if 'identity' in step:
                    done['expected_identity'] = step['identity']
                done['stored_status'] = step['stored_status']
                steps.append(done)
            flows.append({'name': scenario['name'], 'artifact_id': ident, 'title': title, 'steps': steps})

        # What the agent is told.
        told = []
        for artifact, submission in messages():
            told.append({'artifact': artifact, 'submission': submission,
                         'text': rs.message_text(artifact, submission)})

        # Which client may see a spec.
        headers, minimums = clients()
        seen = []
        for header in headers:
            for minimum in minimums:
                seen.append({'header': header, 'minimum': minimum,
                             'capable': server_identity.render_spec_capable(header, minimum)})
        wire = scenarios.WIRE
        for client in wire['clients']:
            seen.append({'header': client['header'], 'minimum': wire['development_minimum'],
                         'capable': server_identity.render_spec_capable(client['header'], wire['development_minimum']),
                         'wire': client['capable']})

        # Catalogs of this program's own.
        loaded = []
        place = root / 'catalogs'
        place.mkdir()
        real = rs._catalog_dir
        idents = ['a', 'abc', 'z9_', 'd', 'b' * 40, 'b' * 41, 'Abc', '1a', 'ab-c', '', 'abcd']
        for name, raw, reasons in catalogs():
            (place / 'clarp.form.v1.json').write_bytes(raw)
            (place / 'clarp.form.v1.refusals.json').write_bytes(reasons)
            rs._catalog_dir = lambda: place
            rs._loaded.cache_clear()
            one = {'name': name, 'catalog': base64.b64encode(raw).decode(),
                   'refusals': base64.b64encode(reasons).decode()}
            try:
                got = rs.catalog()
            except rs.CatalogUnavailable:
                one['loaded'] = None
            else:
                one['loaded'] = {'sha256': got.sha256,
                                 'identifiers': [bool(got.ident.fullmatch(text)) for text in idents]}
                verdicts = []
                for probe in probes():
                    try:
                        rs.validate(probe)
                        verdicts.append('valid')
                    except rs.Refusal as error:
                        verdicts.append(refusal(error))
                one['loaded']['probes'] = verdicts
            loaded.append(one)
        rs._catalog_dir = real
        rs._loaded.cache_clear()
        unavailable = rs.Refusal('not_open', '')
        rs._catalog_dir = lambda: place / 'missing'
        rs._loaded.cache_clear()
        without = unavailable.triple()
        rs._catalog_dir = real
        rs._loaded.cache_clear()
        canonical = [{'value': describe(item['value']), 'utf8_hex': rs.canonical(item['value']).hex(),
                      'sha256': item['sha256']} for item in wire['canonical']]
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {'what': 'server/lib/render_spec.py: canonical, validate, admit, message_text; '
                    'server_identity.render_spec_capable; the catalog files',
            'catalog_sha256': 'c1:' + wire['catalog_sha256'], 'identifiers': idents,
            'probes': [dumps(probe) for probe in probes()],
            'without_a_catalog': without, 'canonical': canonical, 'reads': reads, 'payloads': payloads,
            'answers': answers, 'flows': flows, 'messages': told, 'clients': seen, 'catalogs': loaded,
            'envelopes': envelopes}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        sys.stdout.write(json.dumps(run()) + '\n')
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines = ['{']
    for name in ('what', 'catalog_sha256', 'identifiers', 'probes', 'without_a_catalog', 'canonical'):
        lines.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    sections = ('reads', 'payloads', 'answers', 'flows', 'messages', 'clients', 'catalogs', 'envelopes')
    for number, name in enumerate(sections):
        lines.append(f' {json.dumps(name)}: [')
        items = said[name]
        lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(items) - 1 else '')
                     for n, one in enumerate(items))
        lines.append(' ]' + (',' if number < len(sections) - 1 else ''))
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    counts = ', '.join(f'{len(said[name])} {name}' for name in sections)
    refused = sum(1 for one in said['payloads'] if 'refused' in one)
    print(f'{OUT.relative_to(REPO)}: {counts}; {refused} payloads refused; {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
