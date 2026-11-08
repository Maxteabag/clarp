"""Independent fixed-size refusal channel, SOURCE ONLY/UNEXECUTED.

This is not a success certificate or actual process/export/drain proof. The
owner must bind storage to a unique admitted producer, preserve/read it after
positive quiescence or report UNKNOWN on loss. A local bytearray is NOT durable
or cross-process storage. No shared-memory/backend availability is assumed.
"""
import hashlib
import struct
import threading

SIZE = 128
MAGIC = b'C5937RF1'
HEADER = struct.Struct('<8sBB6xQQQQ')
# Header occupies48bytes; body has80bytes, then a32byte SHA256 and16zero bytes.
BODY_SIZE = 80
CODES = {'APPEND_INCOMPLETE':1,'DECODE_INCOMPLETE':2,'EXPORT_INCOMPLETE':3,
         'UNEXPECTED_WRITER':4,'NATIVE_FAILURE':5,'ACK_UNKNOWN':6,
         'TIMEOUT':7,'PRODUCER_MISSING':8}


class Terminal:
    def __init__(self, buffer):
        view = memoryview(buffer)
        if view.readonly or not view.c_contiguous or view.nbytes != SIZE:
            raise ValueError('exact independent writable terminal storage required')
        self.buffer = view.cast('B')
        if any(self.buffer):
            raise ValueError('terminal storage not fresh; no reset/reuse')
        self.lock = threading.Lock()
        self.latched = False

    def mark(self, code, *, bank, published, triggering_event_missing=True,
             prefix_integrity_unknown=False):
        """First condition only; any torn write reads UNKNOWN, never complete.

        Three published counts are rows/dictionary entries/dictionary bytes.
        No general native history completeness follows from this watermark.
        The suffix is ALWAYS unqualified after a refusal, not silently omitted.
        """
        with self.lock:
            if self.latched:
                return
            self.latched = True
            if (code not in CODES or type(bank) is not int or not 0 <= bank < (1 << 64)
                    or type(published) is not tuple or len(published) != 3
                    or any(type(v) is not int or not 0 <= v < (1 << 64) for v in published)
                    or type(triggering_event_missing) is not bool or type(prefix_integrity_unknown) is not bool):
                raise ValueError('typed refusal code/watermark')
            flags = int(triggering_event_missing) | (int(prefix_integrity_unknown) << 1)
            body = bytearray(BODY_SIZE)
            HEADER.pack_into(body,0,MAGIC,CODES[code],flags,bank,*published)
            # Commit the digest only after the body. If interrupted or lost,
            # the owner refuses qualification; it does not retry product work.
            self.buffer[:BODY_SIZE] = body
            self.buffer[BODY_SIZE:BODY_SIZE+32] = hashlib.sha256(body).digest()
            # The final16reserved bytes remain0. No main-bank space is used.

    def read(self):
        """Read ONLY at an owner-qualified stable boundary; no lifetime claim."""
        raw = bytes(self.buffer)
        if not any(raw):
            return {'state':'UNRECORDED','qualified':False,
                    'scope':'absence of a recorded refusal is NOT producer success'}
        body = raw[:BODY_SIZE]
        if (hashlib.sha256(body).digest() != raw[BODY_SIZE:BODY_SIZE+32]
                or any(raw[BODY_SIZE+32:]) or any(body[HEADER.size:])):
            raise ValueError('UNKNOWN torn/corrupt terminal channel')
        magic,code,flags,bank,rows,entries,nbytes = HEADER.unpack_from(body)
        if (magic != MAGIC or code not in CODES.values() or flags & ~3
                or any(body[10:16])):
            raise ValueError('UNKNOWN terminal version/code/flags/padding')
        return {'state':'PARTIAL','qualified':False,
                'code':next(name for name,value in CODES.items() if value==code),
                'bank':bank,'last_published':{'rows':rows,'dictionary_entries':entries,'dictionary_bytes':nbytes},
                'triggering_event_missing':bool(flags & 1),
                'prefix_integrity_unknown':bool(flags & 2),'suffix_qualified':False}
