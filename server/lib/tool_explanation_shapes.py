"""Call shapes: the stable signature of a tool call and its dynamic values.

Pure parsing of bounded tool metadata; nothing here runs a command.

A shell command is split into parts at top-level `&&`, `||`, `;`, `&`, `|` and
newlines. Each part becomes a shape: its words with every dynamic value
replaced by a typed slot. What counts as dynamic, in this order:

    number      42, -5, 30s, 1.5         `num`
    commit id   7-64 hex chars with a digit and a letter      `sha`
    URL         scheme://...             `url` (derived `urlN_host`)
    path        has `/`, starts with `.` or `~`, or ends in an extension
                                         `path` (derived `pathN_name`)
    variable    contains `$`             `expr`
    identifier  anything else without spaces that is not a plain word,
                such as nadia-1374, MyClass or a UUID         `id`
    free text   contains spaces or punctuation                `text`

Everything else stays literal and is part of the signature: the program,
subcommands and other plain lowercase words (`git log`, `--mode list`),
upper-case words (`HEAD`, `TODO`), and flags. A flag value written as
`--name=value` is typed like any other word. A plain word stays literal
because it usually selects what a program does (`filectl list` versus
`filectl delete`); a learned explanation must never be reused across that.

Slots are numbered per kind in order of appearance (`path1`, `path2`,
`num1`). The signature is `sh:<program> #<keyed hash of the shape>`, so it
never stores a literal word, and it is stable for one Host. The program is
the first word, looking through `timeout N` (`timeout 60 dotnet test` is
`dotnet`); an exact whole command takes the program of its first real simple
command (`first_program`).

A part that cannot be shaped safely is exact: its signature hashes the whole
part, and a learned explanation for it is only reused for the identical text.
That is the case for inline code (`python -c`), flags that could carry a value
(`-pSECRET`), unusually long shapes, and for the whole command when it uses
command substitution, a subshell, a heredoc, process substitution, a shell
keyword or a pipeline into anything but a plain output filter.

`cd DIR` parts are not explained; they become the directory context of the
parts after them. A trailing `| head -n N` or `| tail -N` only shortens
output and is dropped, as are `true` and `:`.
"""
from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import tool_explanation_templates as templates

MAX_PARTS = 8
MAX_SHAPE = 400
SLOT_KINDS = ("num", "sha", "url", "path", "expr", "id", "text", "env")
# Downstream stages of a pipeline that only filter or format what they receive.
_FILTERS = frozenset({"grep", "rg", "egrep", "fgrep", "head", "tail", "sort", "uniq", "wc", "cut", "tr",
                      "jq", "column", "nl", "cat", "sed", "less", "more", "fmt", "fold", "rev", "tac"})
_KEYWORDS = frozenset({"{", "}", "(", ")", "if", "then", "else", "elif", "fi", "for", "while", "until",
                       "do", "done", "case", "esac", "function", "select", "[[", "]]"})
_NOOP = frozenset({"true", ":"})
_INLINE = frozenset({"-c", "-e", "--eval", "-E", "-p", "--print", "-r"})
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,60}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/~+-]{1,160}$")
_WORD = re.compile(r"^[a-z]+(?:[-_][a-z]+)*$")
_VERSIONED = re.compile(r"^[a-z]+\d{1,2}$")
_UPPER = re.compile(r"^[A-Z][A-Z_]*$")
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?[smhdkKMG%]?$")
_SHA = re.compile(r"^(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}$")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_EXTENSION = re.compile(r"^[^\s]+\.[A-Za-z0-9]{1,8}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+=,-]{0,160}$")
_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
_SHORT_WITH_NUMBER = re.compile(r"^(-[A-Za-z])(\d+)$")
_LONG_WITH_VALUE = re.compile(r"^(--[a-z][a-z0-9-]{1,30})=(.*)$", re.S)
# Unquoted redirection operators, marked by split_shell before shlex sees them.
_IN, _OUT = "\x01", "\x02"
_DEVNULL = re.compile("^(?:[12&]?\x02\x02?|\x02&)/dev/null$|^2\x02&1$|^\x02&2$|^1\x02&2$")
_REDIRECT = re.compile("^([12&]?\x02\x02?|\x01)(.*)$", re.S)
_PLACEHOLDER = re.compile(r"\{([a-z]+[0-9]+(?:_name|_host)?)\}")
_NUMBER_WORDS = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                           r"fifteen|twenty|thirty|fifty|hundred|thousand|dozen|single|double|twice|once)\b", re.I)


@dataclass
class Part:
    """One explainable piece of a tool call.

    `activity` is what the tiers see for this part. `signature` is the
    learned-table key for its shape; `exact_key` keys an exact-only answer
    and equals `signature` when the part is exact. `slots` holds the literal
    dynamic values by slot name. `reason` says why a part is exact.
    `legacy` pairs a key an earlier Host version learned this part under
    with the key it has now, so those rows are still found and moved.
    """
    activity: dict
    signature: str
    exact_key: str
    program: str = ""
    slots: dict = field(default_factory=dict)
    exact: bool = False
    reason: str = ""
    cwd: str = ""
    legacy: list = field(default_factory=list)

    def to_json(self):
        return {"activity": self.activity, "signature": self.signature, "exact_key": self.exact_key,
                "program": self.program, "slots": self.slots, "exact": self.exact, "reason": self.reason,
                "cwd": self.cwd, "legacy": self.legacy}

    @classmethod
    def from_json(cls, value):
        return cls(**{key: value[key] for key in ("activity", "signature", "exact_key", "program", "slots",
                                                   "exact", "reason", "cwd", "legacy") if key in value})

    def template_activity(self):
        """The part as the scripted templates classify it, with its `cd` context."""
        if self.cwd and self.activity.get("name") == "Bash":
            command = f"cd {shlex.quote(self.cwd)} && {self.activity['command']}"
            return {**self.activity, "command": command, "input": {"command": command}}
        return self.activity


def _key(kind, shape):
    # The same per-Host keyed hash the scripted templates use for identities.
    return templates._identity(kind, shape)


def _exact(activity, text, reason, program="", previous="?"):
    """An exact part. `previous` is the program its key named before this version.

    Whole commands used to be keyed `x:? #hash` with no program, and a
    `timeout` part by `timeout`; rows learned so are still found (`legacy`).
    """
    digest = _key("exact", text)
    signature = f"x:{program or '?'} #{digest}"
    old = f"x:{previous or '?'} #{digest}"
    return Part(activity=activity, signature=signature, exact_key=signature, program=program, exact=True,
                reason=reason, legacy=[[old, signature]] if old != signature else [])


def _program(words):
    """The program a simple command runs: its first word, looking through `timeout`.

    Returns (program, first word's name).
    """
    words = [w for w in words if not _ENV.fullmatch(w)]
    first = PurePosixPath(words[0]).name if words else "?"
    rest = words[1:] if first == "timeout" else []
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in {"-s", "-k", "--signal", "--kill-after"} else rest[1:]
    program = PurePosixPath(rest[1]).name if len(rest) > 1 and _NUMBER.fullmatch(rest[0]) else first
    return (program if templates._NAME.fullmatch(program) else "?"), (first if templates._NAME.fullmatch(first) else "?")


_SKIP_SEGMENT = frozenset({"for", "case", "select", "function"})
_LEADING = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "time", "exec", "nohup"})


def first_program(command):
    """The program a whole opaque or clipped command mostly runs, or "".

    A heuristic over its first simple command: leading `cd`, env assignments,
    no-ops, shell builtins and keywords are skipped. Only a program name is
    ever kept, never an argument.
    """
    text = re.sub(r"^\s*(?:/\S*/)?(?:ba|z)?sh\s+-l?c\s+['\"]?", "", command)
    for segment in re.split(r"&&|\|\||[;\n|&]", text):
        words = segment.split()
        while words and (words[0] in _LEADING or _ENV.fullmatch(words[0])):
            if _ENV.fullmatch(words[0]) and ("$(" in words[0] or "`" in words[0]):
                words = []
                break
            words = words[1:]
        if not words or words[0] in _SKIP_SEGMENT:
            continue
        name = _program([word.strip("'\"") for word in words])[0]
        if not name or name in templates._BUILTINS or name in _NOOP or name in _KEYWORDS:
            continue
        return name if _PROGRAM.fullmatch(name) else ""
    return ""


# ---- word typing ------------------------------------------------------------

def kind(word):
    """Slot kind of one word, or None when it is literal."""
    if _SHA.fullmatch(word):
        return "sha"
    if _WORD.fullmatch(word) or _VERSIONED.fullmatch(word) or _UPPER.fullmatch(word):
        return None
    if _NUMBER.fullmatch(word):
        return "num"
    if _URL.fullmatch(word):
        return "url"
    if "$" in word:
        return "expr"
    if not re.search(r"\s", word) and ("/" in word or word[:1] in ".~" or _EXTENSION.fullmatch(word)):
        return "path"
    if _ID.fullmatch(word):
        return "id"
    return "text"


class _Shaper:
    def __init__(self):
        self.shape, self.slots, self.counts = [], {}, {}

    def literal(self, word):
        self.shape.append(word)

    def slot(self, slot_kind, value, prefix=""):
        self.counts[slot_kind] = self.counts.get(slot_kind, 0) + 1
        self.slots[f"{slot_kind}{self.counts[slot_kind]}"] = value
        self.shape.append(prefix + "{" + slot_kind + "}")

    def word(self, word, prefix=""):
        slot_kind = kind(word)
        if slot_kind is None:
            self.literal(prefix + word)
        else:
            self.slot(slot_kind, word, prefix)


def _interpreter(word):
    base = PurePosixPath(word).name
    return base in templates._SCRIPT or bool(re.fullmatch(r"python3\.\d+", base))


def _shape_words(words):
    """Shape of one simple command, or the reason it must stay exact."""
    shaper = _Shaper()
    index = 0
    while index < len(words) and (match := _ENV.fullmatch(words[index])):
        shaper.slot("env", match.group(2), prefix=match.group(1) + "=")
        index += 1
    words = words[index:]
    if not words:
        return None, "malformed"
    program = words[0]
    if "/" in program:
        if not _SAFE_PATH.fullmatch(program):
            return None, "unsafe_program"
    elif not _PROGRAM.fullmatch(program):
        return None, "unsafe_program"
    shaper.literal(program)
    interpreter = _interpreter(program)
    script_named = False
    rest = words[1:]
    i = 0
    while i < len(rest):
        word = rest[i]
        if word.startswith("-") and word != "-" and not _NUMBER.fullmatch(word):
            if interpreter and not script_named and word in _INLINE:
                return None, "inline_code"
            if interpreter and not script_named and word == "-m" and i + 1 < len(rest):
                if not _MODULE.fullmatch(rest[i + 1]):
                    return None, "unsafe_program"
                shaper.literal(word)
                shaper.literal(rest[i + 1])
                script_named = True
                i += 2
                continue
            if (match := _LONG_WITH_VALUE.fullmatch(word)):
                shaper.word(match.group(2), prefix=match.group(1) + "=")
            elif (match := _SHORT_WITH_NUMBER.fullmatch(word)):
                shaper.literal(match.group(1))
                shaper.slot("num", match.group(2))
            elif templates._FLAG.fullmatch(word) or word == "--":
                shaper.literal(word)
            else:
                return None, "unsafe_flag"
        elif interpreter and not script_named:
            # The script an interpreter runs is what the call does, not a value.
            if not _SAFE_PATH.fullmatch(word):
                return None, "unsafe_program"
            shaper.literal(word)
            script_named = True
        elif len(word) > 240:
            return None, "long_value"
        elif _interpreter(word) and kind(word) is None:
            # `uv run python x.py`: the script after a wrapped interpreter is identity too.
            shaper.literal(word)
            interpreter, script_named = True, False
        else:
            shaper.word(word)
        i += 1
    if len(" ".join(shaper.shape)) > MAX_SHAPE or len(shaper.slots) > 16:
        return None, "long_shape"
    return shaper, ""


# ---- shell splitting --------------------------------------------------------

def split_shell(command):
    """Top-level parts `[(text, operator_before, redirects)]`, or None when opaque.

    Quotes are respected; operators inside them do not split. Substitution,
    subshells, heredocs and process substitution make the command opaque.
    `redirects` says whether the part has an unquoted `<` or `>`; those are
    then written as `_IN`/`_OUT` so a quoted `<b>` is never read as one.
    """
    parts, current, quote, i, before, redirects = [], [], "", 0, "", False
    n = len(command)
    while i < n:
        char = command[i]
        if quote:
            if quote == '"' and (char == "`" or command.startswith("$(", i)):
                return None
            current.append(char)
            if char == "\\" and quote == '"' and i + 1 < n:
                current.append(command[i + 1])
                i += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2
                continue
            current.extend(command[i:i + 2])
            i += 1
        elif char == "`" or char in "()" or command.startswith("$(", i) or command.startswith("<<", i):
            return None
        elif char == "&" and (command[i - 1:i] in {">", "<"} or command[i + 1:i + 2] == ">"):
            current.append(char)
        elif char in "<>":
            redirects = True
            current.append(_IN if char == "<" else _OUT)
        elif char in "|&;\n":
            operator = command[i:i + 2] if command[i:i + 2] in {"&&", "||", "|&", ";;"} else char
            if operator == ";;":
                return None
            parts.append(("".join(current).strip(), before, redirects))
            before, redirects = operator, False
            current = []
            i += len(operator)
            continue
        else:
            current.append(char)
        i += 1
    if quote:
        return None
    parts.append(("".join(current).strip(), before, redirects))
    return [part for part in parts if part[0]]


def _clean(words):
    """Drop diagnostics-only redirections; keep others as `> {path}`-style words.

    Returns (words for the templates, words for the shape) or None.
    """
    plain, shaped, i = [], [], 0
    while i < len(words):
        word = words[i]
        if _DEVNULL.fullmatch(word):
            i += 1
            continue
        match = _REDIRECT.fullmatch(word) if (_IN in word or _OUT in word) else None
        if match:
            operator, target = _unmark(match.group(1)), match.group(2)
            if not target:
                if i + 1 >= len(words):
                    return None
                target = words[i + 1]
                i += 1
            if target == "/dev/null":
                i += 1
                continue
            if target.startswith("&") or _IN in target or _OUT in target:
                return None
            plain.extend([operator, target])
            shaped.append(("redirect", operator, target))
        elif _IN in word or _OUT in word:
            return None
        else:
            plain.append(word)
            shaped.append(("word", word))
        i += 1
    return plain, shaped


def _unmark(text):
    return text.replace(_IN, "<").replace(_OUT, ">")


def _shell_parts(command):
    try:
        command = templates._strip_wrapper(command)
    except ValueError:
        return [_exact(_bash(command), command, "malformed", first_program(command))]
    whole = first_program(command)
    segments = split_shell(command)
    if segments is None:
        return [_exact(_bash(command), command, "opaque", whole)]
    parsed = []
    for text, operator, redirects in segments:
        try:
            words = shlex.split(text)
        except ValueError:
            return [_exact(_bash(command), command, "malformed", whole)]
        cleaned = _clean(words) if redirects else (words, [("word", w) for w in words])
        if cleaned is None or not cleaned[0]:
            return [_exact(_bash(command), command, "opaque", whole)]
        first = next((w for w in cleaned[0] if not _ENV.fullmatch(w)), "")
        if first in _KEYWORDS or cleaned[0][0] in _KEYWORDS:
            return [_exact(_bash(command), command, "opaque", whole)]
        if operator in {"|", "|&"} and PurePosixPath(first).name not in _FILTERS:
            return [_exact(_bash(command), command, "opaque", whole)]
        parsed.append((_unmark(text), operator, cleaned))
    parts, cwd = [], ""
    for text, operator, (plain, shaped) in parsed:
        base = PurePosixPath(plain[0]).name
        if len(parsed) > 1 and base == "cd" and len(plain) <= 2:
            cwd = plain[1] if len(plain) == 2 else ""
            continue
        if len(parsed) > 1 and base in _NOOP and len(plain) == 1:
            continue
        if operator in {"|", "|&"} and re.fullmatch(r"(head|tail)(\s+-n)?\s+-?\d+", " ".join(plain)):
            continue
        parts.append(_shell_part(text, shaped, cwd))
    if not parts:
        return [_shell_part(parsed[0][0], parsed[0][2][1], "")]
    if len(parts) > MAX_PARTS:
        return [_exact(_bash(command), command, "too_many_parts", whole)]
    return parts


def _bash(command):
    return {"name": "Bash", "command": command, "input": {"command": command}}


def _shell_part(text, shaped, cwd):
    activity = _bash(text)
    words = [entry[1] for entry in shaped if entry[0] == "word"]
    program, first = _program(words)
    shaper, reason = _shape_words(words)
    if shaper is None:
        part = _exact(activity, text, reason, program, previous=first)
        part.cwd = cwd
        return part
    for entry in shaped:
        if entry[0] == "redirect":
            shaper.slot("path", entry[2], prefix=entry[1] + " ")
    shape, exact = _key("shape", shaper.shape), _key("exact", text)
    part = Part(activity=activity, signature=f"sh:{program} #{shape}", exact_key=f"x:{program} #{exact}",
                program=program, slots=shaper.slots, cwd=cwd)
    if first != program:
        part.legacy = [[f"sh:{first} #{shape}", part.signature], [f"x:{first} #{exact}", part.exact_key]]
    return part


# ---- other tools -------------------------------------------------------------

_TOOL_FIELDS = ("file_path", "path", "pattern", "query", "title", "summary", "description")


def _tool_part(activity):
    name = str(activity.get("name") or "")
    tool_kind = str(activity.get("kind") or "")
    program = f"tool:{name or tool_kind}"[:120]
    text = repr(sorted((k, v) for k, v in activity.items() if k != "scripts"))
    if not name and not tool_kind:
        return _exact(activity, text, "malformed", "?")
    inputs = activity.get("input") if isinstance(activity.get("input"), dict) else {}
    if isinstance(activity.get("input"), str) or inputs.get("code") or inputs.get("command") or inputs.get("cmd"):
        return _exact(activity, text, "opaque_input", program, previous=program)
    shaper = _Shaper()
    shaper.literal(name)
    shaper.literal(tool_kind)
    operations = activity.get("operations") or []
    for operation in operations:
        label, _, target = operation.partition(": ")
        if not target or not _WORD.fullmatch(label.lower()):
            return _exact(activity, text, "opaque_input", program, previous=program)
        shaper.literal(label)
        shaper.slot("path", target)
    fields = {**{k: activity[k] for k in _TOOL_FIELDS if isinstance(activity.get(k), str)},
              **{k: inputs[k] for k in _TOOL_FIELDS if isinstance(inputs.get(k), str)}}
    for key in sorted(fields):
        value = fields[key]
        slot_kind = "text" if key in {"title", "summary", "description", "query", "pattern"} else kind(value)
        shaper.literal(key + "=")
        if slot_kind is None:
            shaper.literal(value)
        else:
            shaper.slot(slot_kind, value)
    return Part(activity=activity, signature=f"tool:{name or tool_kind} #{_key('tool-shape', shaper.shape)}",
                exact_key=f"x:{program} #{_key('exact', text)}", program=program, slots=shaper.slots)


def split(activity):
    """Parts of one normalized activity (see tool_explanations.normalize_activity)."""
    name = str(activity.get("name") or "").strip().lower()
    tool_kind = str(activity.get("kind") or "").strip()
    command = templates._field(activity, "command", "cmd") or (activity.get("summary", "") if tool_kind == "command" else "")
    if (name in {"bash", "shell", "exec_command", "local_shell"} or tool_kind == "command") and command:
        return _shell_parts(command)
    label = templates.shell_label(activity)
    if label:
        # A clipped label is not the whole command; it is only ever explained as itself.
        return [_exact(activity, label, "truncated", first_program(label))] if len(label) >= templates.LABEL_CLIP else _shell_parts(label)
    return [_tool_part(activity)]


def with_scripts(part, scripts):
    """Bind a part to the script evidence it was explained with.

    The script's content is part of what the call does, so a changed script
    is a new signature. Only a digest of the excerpts is kept.
    """
    if not scripts:
        return part
    digest = hashlib.sha256(repr([s.get("source_excerpt", "") for s in scripts]).encode()).hexdigest()[:12]
    part.activity = {**part.activity, "scripts": scripts}
    part.signature += f"~{digest}"
    part.exact_key = part.signature if part.exact else part.exact_key + f"~{digest}"
    part.legacy = [[old + f"~{digest}", new + f"~{digest}"] for old, new in part.legacy]
    return part


# ---- parameterised explanations -----------------------------------------------

def slot_values(slots):
    """Slot values plus derived names (`path1_name`, `url1_host`)."""
    values = dict(slots)
    for name, value in slots.items():
        if name.startswith("path"):
            base = PurePosixPath(value.rstrip("/")).name
            if base and base != value and base not in {".", "..", "~"}:
                values[f"{name}_name"] = base
        elif name.startswith("url"):
            from urllib.parse import urlsplit
            host = urlsplit(value).hostname or ""
            if host:
                values[f"{name}_host"] = host
    return values


def placeholders(template):
    return set(_PLACEHOLDER.findall(template))


def render(template, slots):
    """Fill `{slot}` placeholders; None when one has no value for this call."""
    values = slot_values(slots)
    if not placeholders(template) <= set(values):
        return None
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def parameterise(text, template, slots):
    """`(template, "")` when it is safe to reuse for other values, else `(None, why)`.

    The template must render back to exactly `text`, and must not repeat any of
    this call's values literally, or a number when the call has numeric slots,
    because either would be wrong for the next call with the same shape.
    """
    if not isinstance(template, str) or not template.strip():
        return None, "no_template"
    values = slot_values(slots)
    if not placeholders(template) <= set(values):
        return None, "unknown_slot"
    if render(template, slots) != text:
        return None, "mismatch"
    bare = _PLACEHOLDER.sub("", template).lower()
    if any(len(value) >= 2 and value.lower() in bare for value in values.values()):
        return None, "literal_value"
    if any(name.startswith("num") for name in slots) and (re.search(r"\d", bare) or _NUMBER_WORDS.search(bare)):
        return None, "literal_number"
    return template, ""


def join(texts):
    """One explanation for a call from its parts' explanations, in order."""
    unique = []
    for text in texts:
        text = text.strip()
        if text and (not unique or unique[-1] != text):
            unique.append(text)
    if not unique:
        return ""
    sentences = [unique[0]] + ["Then " + (t[:1].lower() + t[1:] if t[1:2].islower() else t) for t in unique[1:]]
    joined = " ".join(sentences)
    while len(joined) > 240 and len(sentences) > 1:
        sentences.pop()
        remaining = len(unique) - len(sentences)
        joined = " ".join(sentences) + f" Then {remaining} more step{'s' if remaining != 1 else ''}."
    return joined[:240]
