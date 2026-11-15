#!/usr/bin/env python3
"""Writes the vectors the Rust Host's reading of a published file is tested against.

An agent publishes an image or a file; the Host decides what it is by its first bytes, not by
what the agent calls it, names it safely, measures an image, and answers with the row the
apps and the agent get. The Python Host does that with the functions of
`server/lib/media_store.py`. This calls them for small made-up files of every kind the store
takes and of kinds it refuses, under the names and the declared types an agent might give, and
writes what they said to `crates/clarp-kernel/tests/fixtures/media-vectors.json`.

    python3 server-rs/tools/media-vectors.py

The module asks the machine's table of file types (`mimetypes`) for an extension where a name
has none. That table differs from machine to machine, so the vectors are written against
Python's own built-in table alone: the same on every machine, and the one the Rust Host
carries the needed part of.

Run it with the Python the Host runs, not with whatever `python3` is on the path: the module
leans on `pathlib`, and what a name's extension is differs between versions (a name that ends
in a dot has none up to 3.13 and the dot itself from 3.14 on). The vectors say which Python
wrote them. On a machine with an installed Host:

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/media-vectors.py

Nothing of a Host is touched or started: functions that take bytes are called.
"""
from __future__ import annotations

import json
import mimetypes
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'media-vectors.json'


def png(width: int, height: int) -> bytes:
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00'


def jpeg(width: int, height: int, marker: int = 0xC0) -> bytes:
    app0 = b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    frame = bytes([0xFF, marker]) + struct.pack('>H', 17) + b'\x08' + struct.pack('>HH', height, width) + b'\x03' + b'\x00' * 9
    return b'\xff\xd8' + app0 + frame + b'\xff\xd9'


def webp(kind: bytes, body: bytes) -> bytes:
    chunk = kind + struct.pack('<I', len(body)) + body
    return b'RIFF' + struct.pack('<I', 4 + len(chunk)) + b'WEBP' + chunk


BLOBS = {
    'png 640x480': png(640, 480),
    'png cut short': png(640, 480)[:20],
    'jpeg 800x600': jpeg(800, 600),
    'jpeg progressive 1024x768': jpeg(1024, 768, 0xC2),
    'jpeg without a frame': b'\xff\xd8\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x00' * 9 + b'\xff\xd9',
    'jpeg with padding before the frame': b'\xff\xd8\xff\xff\xff\xc0' + struct.pack('>H', 17) + b'\x08' + struct.pack('>HH', 9, 7) + b'\x00' * 12,
    'gif 320x200': b'GIF89a' + struct.pack('<HH', 320, 200) + b'\x00' * 6,
    'gif87 2x3': b'GIF87a' + struct.pack('<HH', 2, 3) + b'\x00' * 6,
    'gif cut short': b'GIF89a\x01',
    'webp lossy 300x200': webp(b'VP8 ', b'\x00' * 6 + struct.pack('<HH', 300, 200) + b'\x00' * 8),
    'webp lossless 100x50': webp(b'VP8L', b'\x2f' + bytes([(100 - 1) & 0xFF, ((100 - 1) >> 8) | (((50 - 1) & 0x03) << 6),
                                                              ((50 - 1) >> 2) & 0xFF, ((50 - 1) >> 10) & 0x0F]) + b'\x00' * 8),
    'webp extended 4000x3000': webp(b'VP8X', b'\x00' * 4 + (4000 - 1).to_bytes(3, 'little') + (3000 - 1).to_bytes(3, 'little') + b'\x00' * 4),
    'webp of a kind nobody knows': webp(b'VP9 ', b'\x00' * 20),
    'webp cut short': b'RIFF\x04\x00\x00\x00WEBPVP8 ',
    'pdf': b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n',
    'wav': b'RIFF' + struct.pack('<I', 36) + b'WAVEfmt ' + b'\x00' * 16,
    'mp3 with a tag': b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 8,
    'mp3 frame': b'\xff\xfb\x90\x00' + b'\x00' * 12,
    'webm': b'\x1aE\xdf\xa3\x9fB\x86\x81\x01' + b'\x00' * 8,
    'mp4': b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom',
    'mov': b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ',
    'text': 'A plain note.\nWith a second line: æøå.\n'.encode(),
    'markdown': '# A title\n\nSome *text*.\n'.encode(),
    'csv': b'name,count\nfirst,1\nsecond,2\n',
    'json': b'{"ok": true, "items": [1, 2, 3]}',
    'json that is none': b'{"ok": true,',
    'bytes that are no text': b'\xff\xfe\x00\x01 not utf-8 \xc3\x28',
    'a program': b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 8,
    'html': b'<!doctype html><html><body>hello</body></html>',
    'svg': b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
    'one byte': b'\x00',
}

DECLARED = ['', 'application/octet-stream', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/heic',
            'image/svg+xml', 'application/pdf', 'audio/wav', 'audio/x-wav', 'audio/mpeg', 'audio/mp4', 'audio/x-m4a',
            'video/mp4', 'video/quicktime', 'video/webm', 'text/plain', 'text/plain; charset=utf-8', 'TEXT/CSV',
            'text/markdown', 'text/x-markdown', 'application/json', 'text/html', 'application/zip', '  image/PNG ; q=1']

NAMES = ['', 'chart.png', 'photo.JPG', 'clip.mp4', 'voice.m4a', 'notes.md', 'NOTES.MARKDOWN', 'data.csv', 'report.pdf',
         'archive.tar.gz', '.hidden', 'no extension', 'trailing.', 'a/b/c.png', 'C:\\Users\\me\\shot one.png', '../../etc/passwd',
         'weird*name?.png', 'Skjermbilde (2).png', 'æøå.txt', '日本語.webp', '   spaced   .gif', 'x' * 140 + '.png',
         'double..dots.jpeg', 'name.verylongextensionhere', 'name.p-n-g', '...', '/', 'dir/', 'mixed.PnG', 'tab\tname.txt']

ALTS = ['', 'A chart', 'With [brackets] in it', 'Two\nlines', 'ends with ]', '[', 'æøå 日本', '  spaces  ']


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    # Python's own table of file types, and nothing of this machine's.
    mimetypes.init(files=[])
    from lib import media_store as store

    names = [{'raw': raw, 'content_type': declared, 'safe': store._safe_name(raw, declared)}
             for raw in NAMES for declared in ('', 'image/png', 'video/mp4', 'text/markdown', 'application/x-unknown')]
    # Every file under every declared type without a name; and under no declared type, and
    # under the type that tells nothing, with names that do or do not say what it is.
    cases = [(label, declared, '') for label in BLOBS for declared in DECLARED]
    cases += [(label, declared, name) for label in BLOBS for declared in ('', 'application/octet-stream', 'text/x-markdown')
              for name in ('file.bin', 'notes.md', 'chart.png', 'clip.mp4', 'data.csv', 'page.html', 'voice.m4a', 'x.weird')]
    read = []
    for label, declared, name in cases:
        blob = BLOBS[label]
        safe = store._safe_name(name, declared)
        mime = store._detect_mime(blob, declared, safe)
        width, height = store._image_size(blob, mime)
        read.append({'blob': label, 'content_type': declared, 'name': name, 'safe_name': safe, 'mime': mime,
                     'taken': mime in store.SAFE_MEDIA_MIME, 'image': mime in store.SAFE_IMAGE_MIME,
                     'extension': store._extension(safe, mime), 'width': width, 'height': height})
    # The part of Python's own table of file types the store can ask for.
    known = sorted(store.SAFE_MEDIA_MIME | {'application/octet-stream', 'image/svg+xml', 'text/html', 'application/zip',
                                           'text/x-markdown', 'application/x-unknown', ''})
    by_type = {mime: mimetypes.guess_extension(mime) for mime in known}
    endings = ['.png', '.jpg', '.jpeg', '.jpe', '.gif', '.webp', '.heic', '.heif', '.mp3', '.m4a', '.wav', '.mp4', '.mov',
               '.webm', '.pdf', '.json', '.txt', '.csv', '.md', '.markdown', '.html', '.htm', '.svg', '.zip', '.bin',
               '.weird', '.PNG', '']
    by_name = {ext: mimetypes.guess_type('x' + ext)[0] for ext in endings}
    # What a name's ending alone makes of a file whose bytes say nothing and that was declared as nothing.
    by_ending = {ext: store._detect_mime(b'\x00\x01\x02', '', 'x' + ext) for ext in endings}
    extensions = [{'name': name, 'mime': mime, 'extension': store._extension(name, mime)}
                  for name in ['', 'a.png', 'a.JPEG', 'a.tar.gz', 'a', '.hidden', 'a.', 'a.p-g', 'a.verylongextensionhere', 'a.b.c.d']
                  for mime in sorted(store.SAFE_MEDIA_MIME | {'application/octet-stream', 'image/svg+xml', 'text/html', ''})]
    alts = [{'text': text, 'alt': store._markdown_alt(text)} for text in ALTS]
    rows = []
    for mime, caption, source in [('image/png', 'A chart', 'chart.png'), ('image/png', '', 'chart.png'), ('image/jpeg', '', ''),
                                  ('video/mp4', 'A clip', 'clip.mp4'), ('application/pdf', '', 'report.pdf'),
                                  ('image/webp', 'With [brackets]\nand a line', 'x.webp'), ('text/markdown', '', 'notes.md')]:
        row = {'asset_id': 'asset_made_up_0123456789ab', 'agent_id': 'a1', 'session': 'claire', 'source_name': source,
               'sha256': 'ab' * 32, 'mime_type': mime, 'bytes': 1234, 'width': 640 if mime.startswith('image/') else None,
               'height': 480 if mime.startswith('image/') else None, 'storage_path': '/somewhere/blob', 'caption': caption,
               'created_by': 'agent', 'created_at': 1_700_000_000_000, 'deleted_at': None}
        rows.append({'row': {key: value for key, value in row.items() if key not in ('storage_path', 'deleted_at')},
                     'public': store.to_response(row)})
    vectors = {
        'python': '.'.join(str(part) for part in sys.version_info[:3]),
        'max_bytes': store.MAX_MEDIA_BYTES,
        'images': sorted(store.SAFE_IMAGE_MIME), 'files': sorted(store.SAFE_FILE_MIME),
        'blobs': {label: blob.hex() for label, blob in BLOBS.items()},
        'names': names, 'read': read, 'extensions': extensions, 'alts': alts, 'rows': rows,
        'extension_of_type': by_type, 'type_of_extension': by_name, 'read_by_ending': by_ending,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    taken = sum(1 for vector in read if vector['taken'])
    print(f'{len(names)} names, {len(read)} readings ({taken} taken), {len(extensions)} extensions, {len(alts)} alts, '
          f'{len(rows)} rows -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
