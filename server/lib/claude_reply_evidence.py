"""Attempt-local Claude provenance facts; no lifecycle or answer authority.

Provider message metadata is consumed before output. No phrase or token-count
heuristic is used. Bounded hashed identities retain context across result resets;
unknown/overflow evidence stays unknown rather than becoming a final-answer fact.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib

_LIMIT = 256


def _digest(value: str) -> bytes:
    return hashlib.blake2b(value.encode('utf-8'), digest_size=16).digest()


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        return [c['text'] for c in content if isinstance(c, dict)
                and c.get('type') == 'text' and isinstance(c.get('text'), str)]
    return []


def is_synthetic_assistant_record(record: dict) -> bool:
    message = record.get('message')
    message = message if isinstance(message, dict) else record
    assistant = (record.get('type') == 'assistant'
                 or (record.get('type') not in {'user', 'system'}
                     and message.get('role') == 'assistant'))
    return assistant and (record.get('isApiErrorMessage') is True
                          or message.get('isApiErrorMessage') is True
                          or message.get('model') == '<synthetic>')


class ClaudeReplyEvidence:
    """One exact CLI attempt, facts copied/reset for each provider result."""
    def __init__(self):
        self._messages: OrderedDict[bytes, tuple[str, int]] = OrderedDict()
        self._meta: OrderedDict[bytes, bool] = OrderedDict()
        self._suppressed_texts: OrderedDict[bytes, bool] = OrderedDict()
        self._active: tuple[str, int] = ('unknown', 0)
        self._scope = 0
        self._overflow = False
        self.current_is_genuine = False
        self._facts = self._empty()

    @staticmethod
    def _empty() -> dict[str, bool]:
        return dict(synthetic_seen=False, genuine_text_seen=False,
                    genuine_tool_seen=False, unknown_assistant_seen=False)

    @staticmethod
    def _remember(mapping, key, value):
        mapping[key] = value
        mapping.move_to_end(key)
        if len(mapping) > _LIMIT:
            mapping.popitem(last=False)

    def _message(self, event, message) -> tuple[str, int]:
        parent = event.get('parentUuid')
        if isinstance(parent, str) and _digest(parent) in self._meta:
            kind = 'meta'
        elif is_synthetic_assistant_record({'type': 'assistant', 'message': message,
                                           'isApiErrorMessage': event.get('isApiErrorMessage')}):
            kind = 'synthetic'
        elif isinstance(message.get('model'), str) and message['model']:
            kind = 'genuine'
        else:
            kind = 'unknown'
        identity = message.get('id') or event.get('uuid')
        if not isinstance(identity, str) or not identity:
            return kind, self._scope
        key = _digest(identity)
        old = self._messages.get(key)
        scope = old[1] if old else self._scope
        if old and kind == 'unknown':
            kind = old[0]
        if key not in self._messages and len(self._messages) >= _LIMIT:
            self._overflow = True
        self._remember(self._messages, key, (kind, scope))
        return kind, scope

    def suppress_event(self, event: dict) -> bool:
        self.current_is_genuine = False
        if event.get('isMeta') is True:
            identity = event.get('uuid')
            if isinstance(identity, str):
                self._remember(self._meta, _digest(identity), True)
            return True
        inner = event.get('event')
        inner = inner if isinstance(inner, dict) else {}
        message = event.get('message')
        message = message if isinstance(message, dict) else {}
        outer_assistant = event.get('type') == 'assistant' or message.get('role') == 'assistant'
        if inner.get('type') == 'message_start':
            message = inner.get('message')
            message = message if isinstance(message, dict) else {}
            context = self._message(event, message)
            self._active = context
        elif outer_assistant:
            context = self._message(event, message)
        elif event.get('type') == 'stream_event' and str(inner.get('type', '')).startswith(
                ('message_', 'content_block_')):
            context = self._active
        else:
            return False
        kind, scope = context
        self.current_is_genuine = kind == 'genuine' and scope == self._scope
        # A delayed snapshot of a completed previous message must not become
        # a new result's answer or contaminate its text accumulator.
        if scope < self._scope:
            return True
        content = message.get('content')
        text = _texts(content)
        block = inner.get('content_block')
        delta = inner.get('delta')
        if isinstance(block, dict) and block.get('type') == 'text':
            text += _texts(block.get('text'))
        if isinstance(delta, dict) and delta.get('type') == 'text_delta':
            text += _texts(delta.get('text'))
        tools = (isinstance(content, list) and any(isinstance(c, dict)
                 and c.get('type') in {'tool_use', 'server_tool_use'} for c in content))
        tools = tools or (isinstance(block, dict) and block.get('type') in {'tool_use', 'server_tool_use'})
        tools = tools or (isinstance(delta, dict) and delta.get('type') == 'input_json_delta')
        if self._overflow:
            self._facts['unknown_assistant_seen'] = True
        if kind == 'synthetic':
            self._facts['synthetic_seen'] = True
            for value in text:
                self._remember(self._suppressed_texts, _digest(value.strip()), True)
        elif kind == 'genuine' and not self._overflow:
            self._facts['genuine_text_seen'] |= any(t.strip() for t in text)
            self._facts['genuine_tool_seen'] |= bool(tools)
        elif text or tools or kind == 'meta':
            self._facts['unknown_assistant_seen'] = True
        return kind in {'synthetic', 'meta'}

    def suppresses_result_text(self, value) -> bool:
        """Exact association with this result's structurally suppressed text."""
        return (not self._facts['genuine_text_seen']
                and not self._facts['unknown_assistant_seen']
                and isinstance(value, str)
                and _digest(value.strip()) in self._suppressed_texts)

    def finish_result(self) -> dict[str, bool] | None:
        facts = self._facts.copy()
        self._facts = self._empty()
        self._suppressed_texts.clear()
        self._scope += 1
        return facts if any(facts.values()) else None


def suppresses_result_fallback(event: dict) -> bool:
    """Honor only the private, structurally associated Claude text sentinel.

    Original result/usage remains available to runtime ownership/diagnostics.
    This decides text extraction only, never turn settlement or carry.
    """
    fields = {'synthetic_seen', 'genuine_text_seen',
              'genuine_tool_seen', 'unknown_assistant_seen'}
    facts = event.get('_clarp_reply_evidence')
    return ('_assistant_text' in event and event['_assistant_text'] is None
            and isinstance(facts, dict) and set(facts) == fields
            and all(type(value) is bool for value in facts.values())
            and facts['synthetic_seen'] and not facts['genuine_text_seen']
            and not facts['unknown_assistant_seen'])
