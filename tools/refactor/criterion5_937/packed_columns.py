"""Fixed-byte evidence storage primitive, SOURCE PREPARATION/UNEXECUTED.

NOT an observer, event schema, transaction oracle, domain merger or lifecycle
certificate. Column indices mean APPEND order only, never native write order.
The complete adapter must source-qualify event fields/partial order, capacities,
all producers and actual process/export completeness before measured use.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import threading

VERSION = 1
ENCODING = 'utf-8/surrogatepass'
WIDTH = {'u64': 8, 'i64': 8, 'optional_u64': 9, 'optional_i64': 9,
         'native_state': 1, 'text': 4, 'key': 4, 'ids': 4}
MAX_COLUMNS = 32
MAX_SCHEMA_BYTES = 8192
MAX_IDENTITY_BYTES = 8192
MAX_RECORD_TRANSIENT_BYTES = 65536
MAX_EXPORT_CHUNK = 65536
MAX_BANK_STORAGE_BYTES = 64 * 1024 * 1024  # tooling safety ceiling, NOT a measured-bank allocation


def integer(value, signed=False):
    if type(value) is not int or not (-(1 << 63) if signed else 0) <= value < (1 << (63 if signed else 64)):
        raise ValueError('typed integer width/overflow')
    return value


def identity(kind, value):
    """Reversible typed bytes; never digest-only, casefolded or path-normalized."""
    if kind == 'text':
        if type(value) is not str:
            raise ValueError('text type')
        result = b'T' + value.encode('utf-8', 'surrogatepass')
    elif kind == 'ids':
        if type(value) is not tuple or len(value) > 16:
            raise ValueError('bounded evidence-ID tuple type/length')
        result = b'I' + bytes([len(value)]) + b''.join(struct.pack('<Q', integer(v)) for v in value)
    elif kind == 'key':
        if type(value) is not tuple or not 1 <= len(value) <= 3:
            raise ValueError('bounded exact PK tuple type/length')
        parts = []
        for entry in value:
            if type(entry) is not str:
                raise ValueError('PK entry type')
            raw = entry.encode('utf-8', 'surrogatepass')
            if len(raw) > MAX_IDENTITY_BYTES:
                raise ValueError('PK entry byte cap')
            parts.append(struct.pack('<I', len(raw)) + raw)
        result = b'K' + bytes([len(value)]) + b''.join(parts)
    else:
        raise ValueError('identity kind')
    if len(result) > MAX_IDENTITY_BYTES:
        raise ValueError('identity byte cap')
    return result


def decode_identity(kind, raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_IDENTITY_BYTES:
        raise ValueError('invalid identity bytes')
    if kind == 'text' and raw[:1] == b'T':
        value = raw[1:].decode('utf-8', 'surrogatepass')
    elif kind == 'ids' and raw[:1] == b'I' and len(raw) >= 2:
        count = raw[1]
        if count > 16 or len(raw) != 2 + count * 8:
            raise ValueError('ID tuple wire length')
        value = tuple(struct.unpack_from('<Q', raw, 2 + 8 * i)[0] for i in range(count))
    elif kind == 'key' and raw[:1] == b'K' and len(raw) >= 2:
        count, offset, values = raw[1], 2, []
        if not 1 <= count <= 3:
            raise ValueError('PK tuple wire length')
        for _ in range(count):
            if offset + 4 > len(raw):
                raise ValueError('truncated PK length')
            size = struct.unpack_from('<I', raw, offset)[0]
            offset += 4
            if size > MAX_IDENTITY_BYTES or offset + size > len(raw):
                raise ValueError('truncated PK bytes')
            values.append(raw[offset:offset + size].decode('utf-8', 'surrogatepass'))
            offset += size
        if offset != len(raw):
            raise ValueError('trailing PK bytes')
        value = tuple(values)
    else:
        raise ValueError('dictionary type/tag mismatch')
    if identity(kind, value) != raw:
        raise ValueError('noncanonical identity bytes')
    return value


class Bank:
    """One source-qualified table of fixed-width columns, no row eviction.

    Capacities are explicit inputs, NOT approved defaults or a corpus-fit proof.
    The owner must count Python lookup/dictionary objects and ALL banks,
    producers, exports and consumers in its peak calculation. Initialization
    uses eager bytearray allocation; resident cost/page touch is NOT certified.
    """
    def __init__(self, *, schema, rows, dictionary_entries, dictionary_bytes, storage_limit_bytes, domain):
        if (type(schema) is not tuple or not 1 <= len(schema) <= MAX_COLUMNS
                or any(type(item) is not tuple or len(item) != 2 for item in schema)):
            raise ValueError('column schema type/count')
        names = [name for name, kind in schema]
        if (any(type(name) is not str or not name.isascii() or not name.isidentifier()
                or len(name) > 64 or kind not in WIDTH for name, kind in schema)
                or len(set(names)) != len(names)):
            raise ValueError('column schema name/kind/duplicate')
        if (type(rows) is not int or not 1 <= rows < (1 << 32)
                or type(dictionary_entries) is not int or not 0 <= dictionary_entries < (1 << 32)
                or type(dictionary_bytes) is not int or not 0 <= dictionary_bytes < (1 << 32)
                or type(domain) is not bytes or not domain or len(domain) > MAX_SCHEMA_BYTES):
            raise ValueError('explicit bounded capacities/domain required')
        total_storage = rows * sum(WIDTH[kind] for _, kind in schema) + dictionary_bytes + (dictionary_entries + 1) * 4
        if (type(storage_limit_bytes) is not int or not 0 < storage_limit_bytes <= MAX_BANK_STORAGE_BYTES
                or total_storage > storage_limit_bytes):
            raise ValueError('unqualified fixed-buffer allocation budget')
        self.fixed_buffer_bytes = total_storage
        self.storage_limit_bytes = storage_limit_bytes
        # Lookup objects/transients/export/consumer peaks are ADDITIONAL, not
        # hidden inside this fixed-buffer figure or certified by this ceiling.
        # Caller supplies the frozen exact producer/DB-incarnation identity bytes.
        # These bytes are NOT attestation that such a producer actually ran.
        self.schema, self.domain = schema, domain
        self.capacity, self.entry_capacity = rows, dictionary_entries
        self.columns = {name: bytearray(rows * WIDTH[kind]) for name, kind in schema}
        self.dictionary = bytearray(dictionary_bytes)
        self.offsets = bytearray((dictionary_entries + 1) * 4)
        self.lookup = {}  # bounded by BOTH entries and encoded bytes, accounted separately
        self.used = self.entries = self.bytes_used = 0
        self.lock = threading.RLock()
        self.state = 'OPEN'
        self.refusal = None

    def refuse(self, code):
        if self.refusal is None:
            self.refusal = code
        self.state = 'REFUSED'

    def append(self, record):
        """All-participating-capacity reservation before used-index publication.

        The owner observes a refusal and preserves the native call result; this
        storage primitive does not catch/replace a native primary exception.
        """
        with self.lock:
            if self.state != 'OPEN' or self.refusal is not None:
                raise ValueError('bank not open/complete')
            self.state = 'APPENDING'  # interruption after this point is never a green prefix
            try:
                if type(record) is not dict or set(record) != {name for name, _ in self.schema}:
                    raise ValueError('record fields differ from schema')
                if self.used >= self.capacity:
                    raise ValueError('row capacity')
                planned, encoded, transient = {}, {}, 0
                for name, kind in self.schema:
                    value = record[name]
                    if kind in ('text', 'key', 'ids'):
                        raw = identity(kind, value)
                        number = self.lookup.get(raw)
                        if number is None:
                            number = planned.get(raw)
                            if number is None:
                                number = self.entries + len(planned)
                                planned[raw] = number
                        data = struct.pack('<I', number)
                        transient += len(raw)
                    elif kind in ('u64', 'i64'):
                        data = struct.pack('<q' if kind == 'i64' else '<Q', integer(value, kind == 'i64'))
                    elif kind in ('optional_u64', 'optional_i64'):
                        # Tag0 has canonical zero padding; tag1 can represent actual0.
                        data = b'\x00' + bytes(8) if value is None else b'\x01' + struct.pack(
                            '<q' if kind == 'optional_i64' else '<Q', integer(value, kind == 'optional_i64'))
                    else:
                        if value is not None and type(value) is not bool:
                            raise ValueError('native state must be None/actual bool')
                        data = bytes([0 if value is None else 2 if value else 1])
                    transient += len(data)
                    if transient > MAX_RECORD_TRANSIENT_BYTES:
                        raise ValueError('record transient byte cap')
                    encoded[name] = data
                total = sum(len(raw) for raw in planned)
                if self.entries + len(planned) > self.entry_capacity or self.bytes_used + total > len(self.dictionary):
                    raise ValueError('dictionary capacity')
                # From here, ANY failure keeps REFUSED/APPENDING, not a valid prefix.
                # All buffers are already allocated. No native work is retried.
                cursor = self.bytes_used
                for raw, number in planned.items():
                    self.dictionary[cursor:cursor + len(raw)] = raw
                    struct.pack_into('<I', self.offsets, number * 4, cursor)
                    cursor += len(raw)
                    struct.pack_into('<I', self.offsets, (number + 1) * 4, cursor)
                    self.lookup[raw] = number
                for name, kind in self.schema:
                    offset = self.used * WIDTH[kind]
                    self.columns[name][offset:offset + WIDTH[kind]] = encoded[name]
                # One lock and state guard protect the complete publication.
                index = self.used
                self.entries += len(planned)
                self.bytes_used = cursor
                self.used += 1
                self.state = 'OPEN'
                return index
            except BaseException:
                self.refuse('APPEND_INCOMPLETE')
                raise

    def seal(self):
        """Structural seal ONLY; not native quiescence, exit or drain evidence."""
        with self.lock:
            if self.state != 'OPEN' or self.refusal is not None:
                raise ValueError('incomplete/refused bank')
            self.state = 'SEALED'

    def row(self, index):
        with self.lock:
            if self.state != 'SEALED' or self.refusal is not None or type(index) is not int or not 0 <= index < self.used:
                raise ValueError('unsealed/refused/invalid row')
            try:
                result = {}
                for name, kind in self.schema:
                    data = memoryview(self.columns[name])[index * WIDTH[kind]:(index + 1) * WIDTH[kind]]
                    if kind in ('text', 'key', 'ids'):
                        number = struct.unpack('<I', data)[0]
                        if number >= self.entries:
                            raise ValueError('dictionary reference outside used range')
                        left, right = struct.unpack_from('<II', self.offsets, number * 4)
                        if not 0 <= left <= right <= self.bytes_used or right - left > MAX_IDENTITY_BYTES:
                            raise ValueError('dictionary offsets outside complete range')
                        result[name] = decode_identity(kind, bytes(self.dictionary[left:right]))
                    elif kind in ('u64', 'i64'):
                        result[name] = struct.unpack('<q' if kind == 'i64' else '<Q', data)[0]
                    elif kind in ('optional_u64', 'optional_i64'):
                        if data[0] not in (0, 1) or (data[0] == 0 and bytes(data[1:]) != bytes(8)):
                            raise ValueError('optional tag/padding invalid')
                        result[name] = None if data[0] == 0 else struct.unpack(
                            '<q' if kind == 'optional_i64' else '<Q', data[1:])[0]
                    else:
                        if data[0] not in (0, 1, 2):
                            raise ValueError('native-state wire tag invalid')
                        result[name] = None if data[0] == 0 else data[0] == 2
                return result
            except BaseException:
                self.refuse('DECODE_INCOMPLETE')
                raise

    def export(self, directory):
        """Bounded bulk write; final manifest is published LAST. No retry/cleanup.

        Requires fresh exclusively owned destination; preserves partial artifacts
        on any failure and refuses bank qualification. An outer source-qualified
        owner still proves export source/domain, actual exit and transport/drain.
        """
        with self.lock:
            if self.state != 'SEALED' or self.refusal is not None:
                raise ValueError('export requires complete sealed bank')
            try:
                destination = Path(directory)
                if destination.is_symlink() or destination.resolve(strict=True) != destination or any(destination.iterdir()):
                    raise ValueError('export destination not physical/fresh')
                files = {}
                buffers = [(f'column-{i}.bin', memoryview(self.columns[name])[:self.used * WIDTH[kind]])
                           for i, (name, kind) in enumerate(self.schema)]
                buffers += [('dictionary.bin', memoryview(self.dictionary)[:self.bytes_used]),
                            ('offsets.bin', memoryview(self.offsets)[:(self.entries + 1) * 4])]
                for name, data in buffers:
                    digest = hashlib.sha256()
                    with (destination / name).open('xb', buffering=0) as stream:
                        for offset in range(0, len(data), MAX_EXPORT_CHUNK):
                            block = data[offset:offset + MAX_EXPORT_CHUNK]
                            written = stream.write(block)
                            if written != len(block):
                                raise OSError('short bank export; no retry')
                            digest.update(block)
                        os.fsync(stream.fileno())
                    files[name] = {'bytes': len(data), 'sha256': digest.hexdigest()}
                manifest = {'version': VERSION, 'byteorder': 'little', 'encoding': ENCODING,
                            'schema': self.schema, 'domain_hex': self.domain.hex(),
                            'used_rows': self.used, 'row_capacity': self.capacity,
                            'dictionary_entries': self.entries, 'entry_capacity': self.entry_capacity,
                            'dictionary_bytes_used': self.bytes_used, 'dictionary_byte_capacity': len(self.dictionary),
                            'fixed_buffer_bytes': self.fixed_buffer_bytes, 'storage_limit_bytes': self.storage_limit_bytes,
                            'files': files, 'structurally_sealed': True,
                            'scope': 'structural bank only; no native order/commit/process completeness certificate'}
                encoded = json.dumps(manifest, sort_keys=True).encode('ascii')
                if len(encoded) > 65536:
                    raise ValueError('manifest byte cap')
                with (destination / 'manifest.pending').open('xb', buffering=0) as stream:
                    if stream.write(encoded) != len(encoded):
                        raise OSError('short manifest write')
                    os.fsync(stream.fileno())
                os.rename(destination / 'manifest.pending', destination / 'manifest.json')
                descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return manifest
            except BaseException:
                self.refuse('EXPORT_INCOMPLETE')
                raise
