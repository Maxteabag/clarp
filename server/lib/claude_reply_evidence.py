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


class _OutputHash:
    """Hash assembled text with strip() semantics and constant retained bytes."""
    def __init__(self):
        self._hash = hashlib.blake2b(digest_size=16)
        self._trimmed = None
        self._started = False

    def append(self, text: str) -> None:
        if not self._started:
            text = text.lstrip()
        if not text:
            return
        self._started = True
        body = text.rstrip()
        if body:
            self._hash.update(body.encode('utf-8'))
            self._trimmed = self._hash.copy()
        self._hash.update(text[len(body):].encode('utf-8'))

    def digest(self):
        return self._trimmed.digest() if self._trimmed is not None else None


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
        self._output = None
        self._output_block = None
        self._output_invalid = False
        self._output_lost = False
        self._unknown_output_seen = False
        self._active: tuple[str, int] = ('unknown', 0)
        self._active_identified = False
        self._event_identified = False
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
        self._event_identified = isinstance(identity, str) and bool(identity)
        if not self._event_identified:
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

    def _remember_output(self, digest) -> None:
        if digest is None:
            return
        if digest not in self._suppressed_texts and len(self._suppressed_texts) >= _LIMIT:
            # Losing any result-local association invalidates display proof.
            self._output_lost = True
            return
        self._suppressed_texts[digest] = True

    def _track_output(self, inner, message, *, snapshot: bool) -> None:
        """Only complete, identified message output can match a raw result.

        Text blocks concatenate in provider order, as Claude's output adapter
        does. A hash is published at message_stop (or a complete snapshot),
        never for an individual delta/prefix or across separate messages.
        """
        typ = inner.get('type')
        if snapshot:
            if self._event_identified and not self._overflow:
                output = _OutputHash()
                for text in _texts(message.get('content')):
                    output.append(text)
                self._remember_output(output.digest())
            return
        if typ == 'message_start':
            self._output = _OutputHash() if self._event_identified and not self._overflow else None
            self._output_block = None
            self._output_invalid = False
            return
        if self._output is None:
            return
        index = inner.get('index')
        if typ == 'content_block_start':
            if self._output_block is not None or type(index) is not int or index < 0:
                self._output_invalid = True
                return
            block = inner.get('content_block')
            block = block if isinstance(block, dict) else {}
            text_block = block.get('type') == 'text'
            self._output_block = (index, text_block)
            if text_block:
                text = block.get('text')
                if not isinstance(text, str):
                    self._output_invalid = True
                else:
                    self._output.append(text)
        elif typ == 'content_block_delta':
            delta = inner.get('delta')
            delta = delta if isinstance(delta, dict) else {}
            if self._output_block is None or index != self._output_block[0]:
                self._output_invalid = True
            elif delta.get('type') == 'text_delta':
                text = delta.get('text')
                if not self._output_block[1] or not isinstance(text, str):
                    self._output_invalid = True
                else:
                    self._output.append(text)
        elif typ == 'content_block_stop':
            if self._output_block is None or index != self._output_block[0]:
                self._output_invalid = True
            self._output_block = None
        elif typ == 'message_stop':
            if not self._output_invalid and self._output_block is None:
                self._remember_output(self._output.digest())
            self._output = None

    def suppress_event(self, event: dict) -> bool:
        self.current_is_genuine = False
        self._event_identified = False
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
            self._active_identified = self._event_identified
        elif outer_assistant:
            context = self._message(event, message)
        elif event.get('type') == 'stream_event' and str(inner.get('type', '')).startswith(
                ('message_', 'content_block_')):
            context = self._active
            self._event_identified = self._active_identified
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
        if kind in {'synthetic', 'meta'}:
            self._track_output(inner, message, snapshot=outer_assistant)
            if kind == 'synthetic':
                self._facts['synthetic_seen'] = True
            else:
                # Meta provenance is not a synthetic/final-answer fact.
                self._facts['unknown_assistant_seen'] = True
        elif kind == 'genuine' and self._event_identified and not self._overflow:
            self._facts['genuine_text_seen'] |= any(t.strip() for t in text)
            self._facts['genuine_tool_seen'] |= bool(tools)
        elif text or tools:
            self._facts['unknown_assistant_seen'] = True
            self._unknown_output_seen = True
        return kind in {'synthetic', 'meta'}

    def suppresses_result_text(self, value) -> bool:
        """Exact association with this result's structurally suppressed text."""
        return (not self._facts['genuine_text_seen']
                and not self._unknown_output_seen
                and not self._overflow and not self._output_lost
                and isinstance(value, str)
                and _digest(value.strip()) in self._suppressed_texts)

    def finish_result(self) -> dict[str, bool] | None:
        facts = self._facts.copy()
        self._facts = self._empty()
        self._suppressed_texts.clear()
        self._output = None
        self._output_block = None
        self._output_invalid = False
        self._output_lost = False
        self._unknown_output_seen = False
        self._scope += 1
        if not self._active_identified:
            # Metadata-less legacy chunks cannot be assigned to the prior
            # message after a result. Preserve them as unknown, never stale.
            self._active = ('unknown', self._scope)
        return facts if any(facts.values()) else None


def suppresses_result_fallback(event: dict) -> bool:
    """Honor only the private, structurally associated Claude text sentinel.

    Original result/usage remains available to runtime ownership/diagnostics.
    This decides text extraction only, never turn settlement or carry.
    """
    fields = {'synthetic_seen', 'genuine_text_seen',
              'genuine_tool_seen', 'unknown_assistant_seen'}
    facts = event.get('_clarp_reply_evidence')
    return (event.get('_clarp_reply_provider') == 'claude'
            and event.get('_clarp_suppressed_reply') is True
            and '_assistant_text' in event and event['_assistant_text'] is None
            and not event.get('last_agent_message') and not event.get('message')
            and isinstance(facts, dict) and set(facts) == fields
            and all(type(value) is bool for value in facts.values())
            and (facts['synthetic_seen'] or facts['unknown_assistant_seen'])
            and not facts['genuine_text_seen'])
