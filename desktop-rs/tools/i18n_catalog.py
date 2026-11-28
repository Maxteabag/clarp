"""The desktop's string catalog, shared by tools/i18n-export and tools/i18n-import.

English lives in the source: `@tr("...")` in .slint and `tr!("...")` in Rust,
with the same forms in both:

    "Text"                     plain
    "Text {}" , arg            placeholders: {} in order, {0} {1} by position
    "context" => "Text"        a context, when the English alone is ambiguous
    "{n} file" | "{n} files" % count     a plural; {n} is the count

Every string needs a note for translators: a `// i18n: ...` line right above the
call (or at the end of the call's line) saying where it appears (screen and
element: button, title, row label, a11y label, toast, placeholder), what it
does, and verb or noun when the English could be either. A string without one
is an export error. Its `screen` is the nearest `// i18n-screen: NAME` line
above it in the file, else the file's name (settings.slint and settings_view.rs
are "settings"; everything under catalogue/ is "settings"). The other languages live in
desktop-rs/i18n/<lang>/LC_MESSAGES/clarp-slint.po, which Slint bundles into
the binary and clarp-core's build script turns into the table `tr!` reads.

A key is the English text, or `context` + U+0004 + text when there is a context
(gettext's own convention).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # desktop-rs/
CATALOG = ROOT / "i18n"
DOMAIN = "clarp-slint"
# The shipped languages besides English, as folder names (the tags Slint selects).
LANGUAGES = ["nb", "zh-Hans", "hi", "es", "fr"]
# gettext plural rules; clarp-core's i18n::plural_index must agree (a test checks).
PLURAL_FORMS = {
    "nb": ("nplurals=2; plural=(n != 1);", ["one", "other"]),
    "es": ("nplurals=2; plural=(n != 1);", ["one", "other"]),
    "fr": ("nplurals=2; plural=(n > 1);", ["one", "other"]),
    "hi": ("nplurals=2; plural=(n > 1);", ["one", "other"]),
    "zh-Hans": ("nplurals=1; plural=0;", ["other"]),
}
SOURCE_DIRS = ["slint-app/ui", "slint-app/src", "core/src", "engine/src", "net/src"]
CALL = re.compile(r"(@tr|\btr!)\s*\(")
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{[^{}]*\}")


@dataclass
class Message:
    context: str
    text: str
    plural: str = ""
    comments: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    screen: str = ""

    @property
    def key(self) -> str:
        return f"{self.context}\x04{self.text}" if self.context else self.text


def placeholders(text: str) -> list[str]:
    """The placeholders in a text, in order and without repeats ({{ and }} are literal braces)."""
    out: list[str] = []
    for m in PLACEHOLDER.finditer(text):
        token = m.group(0)
        if token in ("{{", "}}") or token in out:
            continue
        out.append(token)
    return out


# ---- reading the source


class _Reader:
    def __init__(self, text: str, pos: int):
        self.text, self.pos = text, pos

    def skip(self) -> None:
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos)
                self.pos = len(self.text) if end < 0 else end + 2
            else:
                return

    def take(self, token: str) -> bool:
        self.skip()
        if self.text.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def string(self) -> str | None:
        """A string literal ("..." with Rust/Slint escapes, or a Rust raw string)."""
        self.skip()
        t, i = self.text, self.pos
        raw = re.match(r'r(#*)"', t[i:])
        if raw:
            hashes = raw.group(1)
            start = i + raw.end()
            end = t.find('"' + hashes, start)
            if end < 0:
                return None
            self.pos = end + 1 + len(hashes)
            return t[start:end]
        if i >= len(t) or t[i] != '"':
            return None
        i += 1
        out = []
        while i < len(t):
            c = t[i]
            if c == '"':
                self.pos = i + 1
                return "".join(out)
            if c == "\\":
                n = t[i + 1] if i + 1 < len(t) else ""
                if n == "u" and t[i + 2 : i + 3] == "{":
                    close = t.index("}", i)
                    out.append(chr(int(t[i + 3 : close], 16)))
                    i = close + 1
                    continue
                if n == "\n":  # Rust line continuation
                    i += 2
                    while i < len(t) and t[i] in " \t\r\n":
                        i += 1
                    continue
                if n == "{":  # Slint interpolation: not translatable text
                    return None
                out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'"}.get(n, n))
                i += 2
                continue
            out.append(c)
            i += 1
        return None


def _comment_above(lines: list[str], line_index: int) -> list[str]:
    """`// i18n: ...` on the call's line or the lines right above it."""
    found = []
    tail = re.search(r"//\s*i18n:\s*(.+)$", lines[line_index])
    if tail:
        found.append(tail.group(1).strip())
    i = line_index - 1
    above = []
    while i >= 0:
        m = re.match(r"\s*//\s*i18n:\s*(.+)$", lines[i])
        if not m:
            break
        above.insert(0, m.group(1).strip())
        i -= 1
    return above + found


SCREEN_MARK = re.compile(r"//\s*i18n-screen:\s*(.+?)\s*$", re.M)


def default_screen(rel: str) -> str:
    """The screen a file's strings belong to when it names none."""
    path = Path(rel)
    if "catalogue" in path.parts:
        return "settings"
    stem = path.stem
    for suffix in ("_view", "_checks"):
        stem = stem.removesuffix(suffix)
    return stem.replace("_", " ")


def scan_text(text: str, rel: str) -> tuple[list[Message], list[str]]:
    """The messages in one file, and the calls that could not be read."""
    messages, problems = [], []
    lines = text.split("\n")
    marks = [(m.start(), m.group(1)) for m in SCREEN_MARK.finditer(text)]
    for m in CALL.finditer(text):
        line = text.count("\n", 0, m.start())
        # Skip commented-out calls and the macro's own definition.
        prefix = lines[line][: m.start() - (text.rfind("\n", 0, m.start()) + 1)]
        if "//" in prefix or "macro_rules!" in prefix:
            continue
        r = _Reader(text, m.end())
        first = r.string()
        if first is None:
            if not text[m.end():].lstrip().startswith(("$", ")")):
                problems.append(f"{rel}:{line + 1}: the text must be a string literal")
            continue
        context, singular, plural = "", first, ""
        if r.take("=>"):
            context, singular = first, r.string()
            if singular is None:
                problems.append(f"{rel}:{line + 1}: a context needs a string literal after =>")
                continue
        if r.take("|"):
            plural = r.string()
            if plural is None:
                problems.append(f"{rel}:{line + 1}: a plural needs a string literal after |")
                continue
        screen = next((name for at, name in reversed(marks) if at < m.start()), default_screen(rel))
        notes = _comment_above(lines, line)
        if not notes:
            problems.append(f"{rel}:{line + 1}: {singular!r} needs a `// i18n:` note (where it appears, which element, what it does)")
        messages.append(Message(context, singular, plural, notes, [f"{rel}:{line + 1}"], screen))
    return messages, problems


def source_files() -> list[Path]:
    files = []
    for d in SOURCE_DIRS:
        base = ROOT / d
        files += sorted(p for p in base.rglob("*") if p.suffix in (".slint", ".rs") and p.is_file())
    return files


def scan() -> tuple[list[Message], list[str]]:
    """Every message in the source, one per key, in source order."""
    by_key: dict[str, Message] = {}
    problems: list[str] = []
    for path in source_files():
        rel = str(path.relative_to(ROOT))
        found, bad = scan_text(path.read_text(encoding="utf-8"), rel)
        problems += bad
        for msg in found:
            seen = by_key.get(msg.key)
            if seen is None:
                by_key[msg.key] = msg
                continue
            if seen.plural != msg.plural:
                problems.append(f"{msg.places[0]}: {msg.text!r} has another plural at {seen.places[0]}")
            seen.places += msg.places
            seen.comments += [c for c in msg.comments if c not in seen.comments]
    return list(by_key.values()), problems


# ---- .po files


def _unquote(s: str) -> str:
    return json.loads(s) if s.startswith('"') else s


def _po_string(s: str) -> str:
    body = s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\r", "\\r")
    if "\n" in body.rstrip("\n") or len(body) > 76:
        parts = body.split("\n")
        lines = [p + "\\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        return '""\n' + "\n".join(f'"{l}"' for l in lines)
    return '"' + body.replace("\n", "\\n") + '"'


@dataclass
class Translation:
    context: str
    text: str
    plural: str
    msgstr: str = ""
    msgstr_plural: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    fuzzy: bool = False

    @property
    def key(self) -> str:
        return f"{self.context}\x04{self.text}" if self.context else self.text


def po_path(lang: str) -> Path:
    return CATALOG / lang / "LC_MESSAGES" / f"{DOMAIN}.po"


def read_po(path: Path) -> list[Translation]:
    """The entries of a .po file (the header and obsolete entries left out)."""
    if not path.exists():
        return []
    entries: list[Translation] = []
    cur: dict = {}
    last = None

    def flush():
        nonlocal cur
        if "msgid" in cur and cur["msgid"] != "":
            plural = [cur.get(f"msgstr[{i}]", "") for i in range(10) if f"msgstr[{i}]" in cur]
            entries.append(Translation(cur.get("msgctxt", ""), cur["msgid"], cur.get("msgid_plural", ""),
                                       cur.get("msgstr", ""), plural, cur.get("#.", []), cur.get("fuzzy", False)))
        cur = {}

    for raw in path.read_text(encoding="utf-8").split("\n"):
        line = raw.strip()
        if not line:
            flush()
            last = None
            continue
        if line.startswith("#~"):
            continue
        if line.startswith("#,"):
            if "msgid" in cur:
                flush()
            cur["fuzzy"] = "fuzzy" in line
            continue
        if line.startswith("#."):
            if "msgid" in cur:
                flush()
            cur.setdefault("#.", []).append(line[2:].strip())
            continue
        if line.startswith("#"):
            continue
        if line.startswith('"'):
            if last:
                cur[last] += _unquote(line)
            continue
        word, _, rest = line.partition(" ")
        if word in ("msgctxt", "msgid") and "msgid" in cur and (word == "msgctxt" or "msgstr" in cur or "msgstr[0]" in cur):
            flush()
        cur[word] = _unquote(rest.strip())
        last = word
    flush()
    return entries


def write_po(lang: str, entries: list[Translation]) -> None:
    forms, _ = PLURAL_FORMS[lang]
    out = [
        "# Clarp desktop: " + lang + ". Edit with tools/i18n-import; see docs/i18n.md.",
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        f'"Language: {lang}\\n"',
        f'"Plural-Forms: {forms}\\n"',
        "",
    ]
    for e in sorted(entries, key=lambda e: (e.context, e.text)):
        for c in e.comments:
            out.append(f"#. {c}")
        if e.fuzzy:
            out.append("#, fuzzy")
        if e.context:
            out.append(f"msgctxt {_po_string(e.context)}")
        out.append(f"msgid {_po_string(e.text)}")
        if e.plural:
            out.append(f"msgid_plural {_po_string(e.plural)}")
            for i, s in enumerate(e.msgstr_plural):
                out.append(f"msgstr[{i}] {_po_string(s)}")
        else:
            out.append(f"msgstr {_po_string(e.msgstr)}")
        out.append("")
    path = po_path(lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")
