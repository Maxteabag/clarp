#!/usr/bin/env python3
"""Writes the vectors the Rust Host's forms are tested against.

A form asks its user for answers and says what an answer is with a JSON Schema. The Python
Host asks a library whether a schema is one (`Draft202012Validator.check_schema`) and whether
answers fit it (`.validate`), and tells the user the library's own sentence when they do not.
The Rust Host reads a part of JSON Schema itself. This writes down, for schemas and answers
within that part and around it, what the Python Host says, so that the Rust Host says the
same where it reads a schema and says plainly where it does not:

    reprs     a value, and Python's `repr` of it: how a sentence of the library names a value
    encoded   a value, and `json.dumps(value, sort_keys=True, separators=(',', ':'))`: the
              form answers are kept, weighed and shown to their agent in
    contracts a form's payload, and what `html_forms.validate_contract` says of it
    beyond    a form's payload with a word of JSON Schema the Rust Host does not read, and the
              word: the Python Host takes each, the Rust Host refuses each and names the word
    answers   a schema and answers, and the first line of what `.validate` raises

    python3 server-rs/tools/form-vectors.py

Run it with the Python the Host runs with (it has the library). Nothing of a live Host is
touched: the library is pointed at a directory under /var/tmp that is made new on every run.
Every text is synthetic.
"""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'form-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-form-vectors')


def isolate() -> None:
    """Points the library at a place of its own before it is imported."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


# Values a form's answers and a schema's own words are made of. Whole numbers stay within 64
# bits: beyond that the Rust Host reads a number as a fraction (docs/differences.md).
TEXTS = [
    '', 'a', 'plain words', "it's", 'say "yes"', 'both \' and "', 'back\\slash', 'tab\there', 'line\nbreak',
    'return\rhere', 'nul\x00', 'bell\x07', 'escape\x1b', 'delete\x7f', 'next line\x85', 'no-break\xa0space',
    'soft\xadhyphen', 'blåbærsyltetøy', 'Ærlig talt', 'naïve café', 'Ωμέγα', 'привет', '日本語', 'عربى',
    'emoji 🎉', 'family 👨‍👩‍👧', 'heart ❤️', 'zero​width', 'line separator',
    'paragraph separator', 'bom﻿', 'thin space', 'ideographic　space', 'privateuse',
    'astral 𝒳', 'flag 🇳🇴', 'combining é', 'left‎right', 'narrow no-break', 'x' * 40,
    'unassigned͸', 'tag\U000e0041', 'replacement�', 'not a char￿', 'medium space',
    'ogham space', 'word⁠joiner', 'isolate⁦', 'annotation￹', 'last\U0010ffff',
]
NUMBERS = [
    0, 1, -1, 7, 42, 1000000, 9007199254740991, -9007199254740991, 9223372036854775807, -9223372036854775808,
    18446744073709551615, 0.0, -0.0, 1.0, -1.0, 2.5, 0.1, 0.5, 1.5, 3.14159, 100.0, 1e15, 1e16, 1.5e16, 1e17, 1e21,
    1e22, 1e100, 1.7976931348623157e308, 0.001, 0.0001, 0.00012, 0.00001, 1.234e-05, 1e-07, 5e-324, 123456789.125,
    1234567890123456.0, 12345678901234567.0, 0.30000000000000004, 2.0 ** 53, -2.5e-10, 1e-4 - 1e-12, 9999999999999998.0,
]
PLAIN = [None, True, False]


def nested() -> list:
    return [
        [], {}, [1, 'a', None, True, 2.5], {'a': 1}, {'b': [1, {'c': 'd'}], 'a': {'z': None, 'y': False}},
        [[[]]], {'': ''}, {"it's": 'say "yes"'}, {'né': 'ü', 'a': ['x', {'k': 1.0}]}, [0.1, 1e16, -0.0],
        {'n': 10, 'm': 2, 'A': 0, 'a': 1, '_': 3, '10': 4, '9': 5}, {'tab\t': 'line\n'}, ['🎉', {'🎉': '🎉'}],
        {'deep': {'deeper': {'deepest': [1, [2, [3, [4]]]]}}},
    ]


def contracts() -> list[dict]:
    """Payloads of forms: what a schema may be, and what is said of one that is not."""
    base = {'content': '<form></form>', 'version': '1'}
    schemas = [
        {'type': 'object'},
        {'type': 'object', 'properties': {}},
        {'type': 'object', 'properties': {'name': {'type': 'string'}}},
        {'type': 'object', 'properties': {'n': {'type': 'number'}, 'when': {'type': 'string', 'enum': ['now', 'later']}}},
        {'type': 'object', 'properties': {'o': {'type': 'object', 'properties': {'x': {'type': 'integer'}}}}},
        {'type': 'object', 'required': ['a'], 'additionalProperties': False},
        {'type': 'object', 'additionalProperties': {'type': 'string'}},
        {'type': 'object', 'properties': {'tags': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3}}},
        {'type': 'object', 'properties': {'s': {'type': 'string', 'minLength': 1, 'maxLength': 5}}},
        {'type': 'object', 'properties': {'n': {'type': 'integer', 'minimum': 0, 'maximum': 10}}},
        {'type': 'object', 'properties': {'c': {'const': 'yes'}}},
        {'type': 'object', 'properties': {'t': {'type': ['string', 'null']}}},
        {'type': 'object', 'title': 'Answers', 'description': 'What the user chose', 'properties': {
            'a': {'type': 'string', 'title': 'A', 'description': 'The first', 'default': 'x', 'examples': ['x', 'y']}}},
        {'type': 'object', '$schema': 'https://json-schema.org/draft/2020-12/schema', '$comment': 'kept'},
        {'type': 'object', 'properties': {'a': True, 'b': False}},
        {'type': 'object', 'properties': {'a': {}}},
        {'type': 'object', 'properties': {'a': {'enum': []}}},
        {'type': 'object', 'properties': {'a': {'enum': [1, 'a', None, [1], {'k': 1}]}}},
        {'type': 'object', 'properties': {'a': {'type': 'string', 'format': 'date'}}},
        {'type': 'object', 'properties': {'a': {'deprecated': True, 'readOnly': False, 'writeOnly': False}}},
        {'type': 'object', 'properties': {'n': {'minimum': 1.5, 'maximum': 2}}},
        {'type': 'object', 'properties': {'s': {'minLength': 2.0}}},
        # What a schema is not.
        {'type': 'object', 'properties': []},
        {'type': 'object', 'properties': {'a': 'string'}},
        {'type': 'object', 'properties': {'a': {'type': 'text'}}},
        {'type': 'object', 'properties': {'a': {'type': ['string', 'string']}}},
        {'type': 'object', 'properties': {'a': {'type': []}}},
        {'type': 'object', 'properties': {'a': {'type': 5}}},
        {'type': 'object', 'required': 'a'},
        {'type': 'object', 'required': ['a', 'a']},
        {'type': 'object', 'required': [1]},
        {'type': 'object', 'additionalProperties': 'no'},
        {'type': 'object', 'properties': {'a': {'enum': 'x'}}},
        {'type': 'object', 'properties': {'a': {'minLength': -1}}},
        {'type': 'object', 'properties': {'a': {'minLength': 1.5}}},
        {'type': 'object', 'properties': {'a': {'maxLength': '5'}}},
        {'type': 'object', 'properties': {'a': {'minimum': '0'}}},
        {'type': 'object', 'properties': {'a': {'maximum': None}}},
        {'type': 'object', 'properties': {'a': {'minItems': True}}},
        {'type': 'object', 'properties': {'a': {'items': 'string'}}},
        {'type': 'object', 'properties': {'a': {'items': [{'type': 'string'}]}}},
        {'type': 'object', 'properties': {'a': {'title': 5}}},
        {'type': 'object', 'properties': {'a': {'description': ['x']}}},
        {'type': 'object', 'properties': {'a': {'examples': 'x'}}},
        {'type': 'object', 'properties': {'a': {'deprecated': 'yes'}}},
        {'type': 'object', '$comment': 5},
        {'type': 'object', '$schema': 5},
        {'type': 'object', 'properties': {'a': {'format': 5}}},
        # References are refused before anything else is asked.
        {'type': 'object', 'properties': {'a': {'$ref': '#/$defs/a'}}, '$defs': {'a': {'type': 'string'}}},
        {'type': 'object', '$dynamicRef': '#x'},
        {'type': 'object', 'properties': {'a': [{'$recursiveRef': '#'}]}},
        # What is not an object's schema.
        {'type': 'string'}, {'type': ['object']}, {}, [], 'object', None, True,
        # Words that mean nothing to the library mean nothing here: both take the schema.
        {'type': 'object', 'x-widget': 'slider', 'properties': {'a': {'x-order': 1, 'ui': {'pattern': 5}}}},
        {'type': 'object', 'properties': {'a': {'type': 'string', 'contentMediaType': 'text/plain', 'contentEncoding': 'base64'}}},
        # A word of a schema is a word where a schema is: not the name of an answer, nor
        # anything inside a value an answer is compared with.
        {'type': 'object', 'properties': {'pattern': {'type': 'string'}, 'oneOf': {'const': {'pattern': 'x', 'oneOf': []}}}},
        {'type': 'object', 'properties': {'a': {'enum': [{'allOf': 5}], 'default': {'not': 1}, 'examples': [{'if': 2}]}}},
    ]
    deep = {'type': 'object'}
    for _ in range(31):
        deep = {'type': 'object', 'properties': {'a': deep}}
    schemas.append(deep)
    payloads = [{**base, 'answer_schema': schema} for schema in schemas]
    payloads += [
        {**base},
        {'version': '1', 'answer_schema': {'type': 'object'}},
        {'content': '   ', 'version': '1', 'answer_schema': {'type': 'object'}},
        {'content': 5, 'version': '1', 'answer_schema': {'type': 'object'}},
        {'content': 'x', 'answer_schema': {'type': 'object'}},
        {'content': 'x', 'version': 'has space', 'answer_schema': {'type': 'object'}},
        {'content': 'x', 'version': 'v' * 81, 'answer_schema': {'type': 'object'}},
        {'content': 'x', 'version': 1, 'answer_schema': {'type': 'object'}},
        {**base, 'answer_schema': {'type': 'object'}, 'read_only': 'no'},
        {**base, 'answer_schema': {'type': 'object'}, 'read_only': False},
        {**base, 'answer_schema': {'type': 'object'}, 'connect_origins': ['https://example.org']},
        {**base, 'answer_schema': {'type': 'object'}, 'connect_origins': ['http://example.org']},
        {**base, 'answer_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'read_only': True},
        {**base, 'answer_schema': {'type': 'object'}, 'read_only': True},
    ]
    return payloads


def beyond() -> list[tuple[str, dict]]:
    """Schemas with a word the library reads and the Rust Host does not, each with that word.
    The Python Host takes every one of them."""
    return [
        ('pattern', {'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': '^a+$'}}}),
        ('oneOf', {'type': 'object', 'properties': {'a': {'oneOf': [{'const': 'x', 'title': 'X'}, {'const': 'y'}]}}}),
        ('anyOf', {'type': 'object', 'properties': {'a': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}}),
        ('allOf', {'type': 'object', 'allOf': [{'required': ['a']}]}),
        ('not', {'type': 'object', 'properties': {'a': {'not': {'type': 'null'}}}}),
        ('patternProperties', {'type': 'object', 'patternProperties': {'^x': {'type': 'string'}}}),
        ('propertyNames', {'type': 'object', 'propertyNames': {'maxLength': 3}}),
        ('dependentRequired', {'type': 'object', 'dependentRequired': {'a': ['b']}}),
        ('dependentSchemas', {'type': 'object', 'dependentSchemas': {'a': {'required': ['b']}}}),
        ('if', {'type': 'object', 'if': {'required': ['a']}, 'then': {'required': ['b']}}),
        ('prefixItems', {'type': 'object', 'properties': {'a': {'type': 'array', 'prefixItems': [{'type': 'string'}]}}}),
        ('contains', {'type': 'object', 'properties': {'a': {'type': 'array', 'contains': {'type': 'string'}}}}),
        ('unevaluatedProperties', {'type': 'object', 'unevaluatedProperties': False}),
        ('unevaluatedItems', {'type': 'object', 'properties': {'a': {'unevaluatedItems': False}}}),
        ('$defs', {'type': 'object', '$defs': {'a': {'type': 'string'}}}),
        ('minProperties', {'type': 'object', 'minProperties': 1, 'maxProperties': 4}),
        ('exclusiveMinimum', {'type': 'object', 'properties': {'n': {'type': 'number', 'exclusiveMinimum': 0}}}),
        ('exclusiveMaximum', {'type': 'object', 'properties': {'n': {'exclusiveMaximum': 1}}}),
        ('multipleOf', {'type': 'object', 'properties': {'n': {'multipleOf': 0.25}}}),
        ('uniqueItems', {'type': 'object', 'properties': {'l': {'type': 'array', 'uniqueItems': True}}}),
        ('uniqueItems', {'type': 'object', 'properties': {'l': {'items': {'additionalProperties': {'uniqueItems': True}}}}}),
        ('$id', {'type': 'object', '$id': 'https://example.org/form'}),
        ('$anchor', {'type': 'object', 'properties': {'a': {'$anchor': 'a'}}}),
        ('contentSchema', {'type': 'object', 'properties': {'a': {'contentSchema': {'type': 'string'}}}}),
    ]


def cases() -> list[tuple[dict, object]]:
    """Schemas within what the Rust Host reads, each with answers that fit and that do not."""
    found: list[tuple[dict, object]] = []

    def each(schema: dict, instances: list) -> None:
        found.extend((schema, instance) for instance in instances)

    anything = [None, True, False, 0, 1, -1, 1.0, 2.5, '', 'a', 'yes', [], [1], ['a', 'b'], {}, {'a': 1}]
    for name in ('string', 'number', 'integer', 'boolean', 'null', 'object', 'array'):
        each({'type': name}, anything + [1e16, 1.5, -0.0, 9007199254740993, 'é', {'b': [1]}])
    for names in (['string', 'null'], ['number', 'string'], ['integer', 'boolean'], ['array', 'object', 'null'], ['string']):
        each({'type': names}, anything)
    each({'enum': ['now', 'later']}, anything + ['now', 'later', 'Now'])
    each({'enum': [1, 2.5, None, True, 'x', [1, 2], {'k': 1}]},
         anything + [2.5, 'x', [1, 2], [1.0, 2], [True, 2], {'k': 1}, {'k': 1.0}, {'k': True}, {'k': 2}, 1.0, True, False, 0])
    each({'enum': [0, False]}, [0, False, 0.0, -0.0, 1, True, None, ''])
    each({'enum': [True]}, [1, 1.0, True, 'true'])
    each({'enum': []}, [None, 'a'])
    each({'enum': ["it's", 'say "yes"', 'tab\t']}, ["it's", 'nope', 'tab\t'])
    for const in ('yes', 1, 1.0, True, False, None, [1, 'a'], {'k': [1]}, 0, ''):
        each({'const': const}, anything + [1.0, [1, 'a'], [1.0, 'a'], {'k': [1]}, {'k': [True]}])
    for low, high in ((0, 3), (1, 1), (2, 5), (0, 0)):
        each({'minLength': low, 'maxLength': high}, ['', 'a', 'ab', 'abc', 'abcd', 'abcdef', 'æøå', '🎉', '👨‍👩', 5, None, ['a'], {}])
        each({'minItems': low, 'maxItems': high}, [[], [1], [1, 2], [1, 2, 3], [1, 2, 3, 4], [1, 2, 3, 4, 5, 6], 'abcdefg', {}, 5, None])
    each({'minLength': 1}, ['', 'a'])
    each({'maxLength': 0}, ['', 'a'])
    each({'minItems': 1}, [[], [0]])
    each({'maxItems': 0}, [[], [0]])
    each({'minLength': 2.0, 'maxLength': 3.0}, ['a', 'ab', 'abcd'])
    for low, high in ((0, 10), (1.5, 2.5), (-5, -1), (0, 0), (1e16, 1e17)):
        each({'minimum': low, 'maximum': high},
             [-10, -5, -1, 0, 0.0, -0.0, 1, 1.5, 2, 2.5, 3, 10, 10.5, 11, 1e16, 2e17, True, False, '5', None, [5], {}])
    each({'type': 'integer', 'minimum': 1, 'maximum': 5}, [0, 1, 3, 5, 6, 2.0, 2.5, 'three', True, None])
    each({'type': 'array', 'items': {'type': 'string'}}, [[], ['a'], ['a', 1], [1, 'a', None], ['a', ['b']], 'a', None])
    each({'type': 'array', 'items': {'type': 'object', 'properties': {'n': {'type': 'number'}}, 'required': ['n']}},
         [[], [{'n': 1}], [{'n': 1}, {}], [{'n': 'x'}, {}], [{'n': 1}, 5]])
    each({'items': True}, [[1, 'a'], 'x'])
    each({'items': False}, [[], [1], [1, 2], 'x'])
    each({'items': {'enum': ['a', 'b']}, 'minItems': 1, 'maxItems': 2}, [[], ['a'], ['a', 'c'], ['c', 'd', 'e'], ['a', 'b', 'a']])

    person = {'type': 'object', 'properties': {
        'name': {'type': 'string', 'minLength': 1}, 'age': {'type': 'integer', 'minimum': 0, 'maximum': 150},
        'when': {'type': 'string', 'enum': ['now', 'later']}, 'tags': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['name', 'when'], 'additionalProperties': False}
    people = [
        {'name': 'Ada', 'when': 'now'}, {'name': 'Ada', 'when': 'now', 'age': 36, 'tags': ['a', 'b']}, {}, {'name': 'Ada'},
        {'when': 'now'}, {'name': '', 'when': 'now'}, {'name': 5, 'when': 'soon'}, {'name': 'Ada', 'when': 'soon'},
        {'name': 'Ada', 'when': 'now', 'age': -1}, {'name': 'Ada', 'when': 'now', 'age': 151}, {'name': 'Ada', 'when': 'now', 'age': 3.5},
        {'name': 'Ada', 'when': 'now', 'age': 3.0}, {'name': 'Ada', 'when': 'now', 'tags': ['a', 1]}, {'name': 'Ada', 'when': 'now', 'tags': 'a'},
        {'name': 'Ada', 'when': 'now', 'extra': 1}, {'name': 'Ada', 'when': 'now', 'z': 1, 'b': 2}, {'name': 'Ada', 'when': 'now', "it's": 1},
        {'extra': 1}, {'z': 1, 'a': 2, 'm': 3}, {'name': None, 'when': None, 'age': None, 'tags': None}, [], 'Ada', None, 5,
        {'name': 'Ada', 'when': 'now', '10': 1, '9': 2, 'B': 3, 'a': 4},
    ]
    each(person, people)
    # The order a schema's words are in is the order they are asked in.
    for order in itertools.permutations(('type', 'properties', 'required', 'additionalProperties')):
        each({key: person[key] for key in order}, [{}, {'name': 5}, {'extra': 1}, {'name': 5, 'extra': 1}, [], {'when': 'x', 'q': 1}])
    for order in itertools.permutations(('type', 'enum', 'minLength', 'maxLength')):
        words = {'type': 'string', 'enum': ['aa', 'bbb'], 'minLength': 2, 'maxLength': 3}
        each({key: words[key] for key in order}, ['a', 'cc', 'dddd', 5, 'aa'])
    for order in itertools.permutations(('type', 'minimum', 'maximum', 'const')):
        words = {'type': 'integer', 'minimum': 2, 'maximum': 4, 'const': 3}
        each({key: words[key] for key in order}, [1, 3, 5, 2.5, 'x', 4])
    each({'type': 'object', 'additionalProperties': {'type': 'string'}}, [{}, {'a': 'x'}, {'a': 1}, {'b': 1, 'a': 1}, {'a': 'x', 'b': None}])
    each({'type': 'object', 'properties': {'a': {'type': 'string'}}, 'additionalProperties': {'type': 'number'}},
         [{'a': 'x', 'b': 1}, {'a': 'x', 'b': 'x'}, {'b': 'x', 'c': 'x'}, {'a': 'x'}])
    each({'type': 'object', 'properties': {'a': True, 'b': False}}, [{}, {'a': 1}, {'b': 1}, {'a': 1, 'b': [1, 'x']}])
    each({'type': 'object', 'properties': {'a': {}}, 'additionalProperties': True}, [{'a': 1, 'b': 2}])
    each({'additionalProperties': False}, [{}, {'a': 1}, {'b': 1, 'a': 2, 'c': 3}, [], 'x'])
    each({'required': ['a', 'b']}, [{}, {'a': 1}, {'b': 1}, {'a': 1, 'b': 2}, [], 'a', None])
    each({'required': ["it's", 'say "yes"', 'né']}, [{}])
    each({'required': []}, [{}])
    each({'type': 'object', 'properties': {'o': {'type': 'object', 'properties': {'deep': {'type': 'array', 'items': {'const': 1}}},
                                                 'required': ['deep']}}, 'required': ['o']},
         [{}, {'o': {}}, {'o': {'deep': []}}, {'o': {'deep': [1, 1.0]}}, {'o': {'deep': [1, 2]}}, {'o': []}, {'o': {'deep': 'x'}}])
    # Words that say nothing of an answer.
    each({'type': 'object', 'title': 'T', 'description': 'D', 'default': {}, 'examples': [{}], '$comment': 'c',
          '$schema': 'https://json-schema.org/draft/2020-12/schema', 'deprecated': False, 'readOnly': True, 'writeOnly': False,
          'properties': {'a': {'type': 'string', 'format': 'date', 'title': 'A', 'default': 5}}},
         [{}, {'a': 'not a date'}, {'a': 5}, []])
    # How a value that does not fit is named.
    for value in [*TEXTS[:38], *NUMBERS, *nested()]:
        found.append(({'type': 'null'}, value))
        found.append(({'const': value}, 'something else'))
    each({'enum': [1e16, 0.1, -0.0, 'é', None, True, [1.5, {'k': 'v'}]]}, ['x'])
    each({'type': 'object', 'properties': {'s': {'type': 'string', 'maxLength': 2}}}, [{'s': 'x' * 400}])
    each({'type': 'string'}, [{'k': 'v' * 400}, ['é' * 200, 'x' * 200]])
    return found


def main() -> int:
    isolate()
    from jsonschema import Draft202012Validator
    from lib import html_forms
    from importlib.metadata import version

    def contract(payload):
        try:
            html_forms.validate_contract(payload)
            return None
        except ValueError as exc:
            return str(exc)

    def answered(schema, instance):
        try:
            Draft202012Validator(schema).validate(instance)
            return None
        except Exception as exc:
            return str(exc).split('\n')[0][:300]

    values = [*TEXTS, *NUMBERS, *PLAIN, *nested()]
    vectors = {
        'made_with': {'jsonschema': version('jsonschema'), 'python': sys.version.split()[0]},
        'reprs': [{'value': value, 'repr': repr(value)} for value in values],
        'encoded': [{'value': value, 'encoded': json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)}
                    for value in values],
        'contracts': [{'payload': payload, 'error': contract(payload)} for payload in contracts()],
        'beyond': [{'word': word, 'payload': {'content': '<form></form>', 'version': '1', 'answer_schema': schema},
                    'error': contract({'content': '<form></form>', 'version': '1', 'answer_schema': schema})}
                   for word, schema in beyond()],
        'answers': [{'schema': schema, 'instance': instance, 'error': answered(schema, instance)}
                    for schema, instance in cases()],
    }
    # Written so that every sign arrives as it is: a value is read back as the value it was.
    OUT.write_text(json.dumps(vectors, ensure_ascii=True, indent=1) + '\n')
    assert all(row['error'] is None for row in vectors['beyond']), 'the library refuses a schema listed as beyond'
    refused = sum(1 for row in vectors['contracts'] if row['error'])
    unfit = sum(1 for row in vectors['answers'] if row['error'])
    print(f'{len(vectors["reprs"])} values, {len(vectors["contracts"])} payloads ({refused} refused), '
          f'{len(vectors["answers"])} answers ({unfit} do not fit) -> {OUT.relative_to(REPO)}')
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
