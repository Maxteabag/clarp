"""Scripted explanation templates. Parses tool metadata; never runs it.

A versioned library of typed templates explains common tool activity without a
model. `classify()` either returns a validated match or abstains with a reason;
only an unknown program or tool may be offered a template by Jev, as an extra
beside the learned explanations it chooses from (see
`tool_explanations._select_templates`). A compound, mutating, privileged,
uploading or script call never gets a template; it gets a learned
explanation or the configured language model, which sees script evidence.
Parameters are literal values from the tool input and are validated
by type before a template renders them.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import string
from urllib.parse import urlsplit

LIBRARY = json.loads(Path(__file__).with_name("tool_explanation_templates.json").read_text())
VERSION = LIBRARY["version"]
TEMPLATES = LIBRARY["templates"]
REGRESSION = LIBRARY["regression"]

# Only these abstentions may be offered to Jev. Everything else is either
# structurally uncertain or could change or send data, where a wrong template
# would understate the effect.
JEV_REASONS = frozenset({"unknown_program", "unknown_tool"})
# Jev never learns a rule that describes a change, a network call or execution.
LEARNABLE_ACTIONS = frozenset({"read", "list", "search"})

_SEARCH = {"rg", "grep", "egrep", "fgrep", "ag", "ack"}
_READ = {"cat", "head", "tail", "nl", "less", "more", "bat"}
_SCRIPT = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "deno", "bun", "tsx", "ts-node"}
_PRIVILEGED = {"sudo", "doas", "su", "pkexec"}
_MUTATING = {"shred", "dd", "mkfs", "truncate", "chmod", "chown", "chgrp", "mv", "cp", "ln", "kill", "pkill",
             "killall", "systemctl", "docker", "podman", "kubectl", "reboot", "shutdown", "tee", "xargs",
             "install", "rsync", "scp", "ssh", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "make",
             "apt", "pacman", "brew", "eval", "exec", "env", "nohup", "watch", "crontab", "git-lfs"}
_VALUE_FLAGS = {"-g", "--glob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C", "-m", "--max-count",
                "--max-depth", "-d", "--include", "--exclude", "--exclude-dir", "-M", "--max-columns"}
_PARAMETER_TYPES = {"directory", "path", "pattern", "host", "name", "text"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f`$|;&<>]")
_IDENTITY_KEY = "tool_explanations.identity_key"
# Shell builtins set shell state, test a condition or print text. None of them
# reads, lists or searches files, so Jev has no template to offer for one.
_BUILTINS = {"export", "unset", "set", "source", ".", "[", "test", "echo", "printf", "read", "sleep", "wait",
             "trap", "exit", "return", "local", "declare", "typeset", "readonly", "alias", "unalias", "shift",
             "umask", "ulimit", "type", "command", "hash", "jobs", "fg", "bg", "disown", "builtin", "let",
             "cd", "pushd", "popd", "dirs"}
# A tool name that is really a shell invocation, as Codex rows arrive when the
# client has only the display label: `/usr/bin/bash -lc "..."`.
_SHELL_LABEL = re.compile(r"^(?:/\S*/)?(?:ba|z)?sh\s+-l?c\s")
# Clients clip that label to 80 characters, so one this long is incomplete.
LABEL_CLIP = 80


class Route(dict):
    """Classification of one activity. A plain dict so it rides in queue JSON.

    Keys: `action`, `signature`, `reason` (abstention) or `template_id` and
    `parameters` (match), and `candidates` (literal arguments Jev may pick).
    """


def _abstain(reason, *, action="unknown", signature="", candidates=()):
    return Route(reason=reason, action=action, signature=signature, candidates=list(candidates)[:8])


def _match(template_id, parameters, *, signature):
    return Route(template_id=template_id, parameters={k: v for k, v in parameters.items() if v},
                 action=TEMPLATES[template_id]["action"], signature=signature)


# ---- shell ----------------------------------------------------------------

def _segments(command):
    """Split at top-level shell operators, respecting quotes.

    Returns [(text, operator_after)] or None when the command substitutes,
    redirects or uses a heredoc. `2>&1` and `2>/dev/null` only merge or drop
    diagnostics, so they are removed rather than treated as redirection.
    """
    segments, current, quote, i = [], [], "", 0
    while i < len(command):
        char = command[i]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "\\" and i + 1 < len(command):
            current.extend(command[i:i + 2])
            i += 1
        elif char == "`" or command.startswith("$(", i) or char in "()":
            return None
        elif (command.startswith("2>&1", i) or command.startswith("2>/dev/null", i)) and (i == 0 or command[i - 1].isspace()):
            i += 4 if command.startswith("2>&1", i) else 11
            continue
        elif char in "<>":
            return None
        elif char in "|&;\n":
            operator = command[i:i + 2] if command[i:i + 2] in {"&&", "||"} else char
            segments.append(("".join(current).strip(), operator))
            current = []
            i += len(operator)
            continue
        else:
            current.append(char)
        i += 1
    if quote:
        raise ValueError("unterminated quote")
    segments.append(("".join(current).strip(), ""))
    return segments


def _strip_wrapper(command):
    # Only a command that starts as `bash -c` is split whole: a heredoc body
    # elsewhere may hold text (`don't`) no shell word splitter reads.
    if not _SHELL_LABEL.match(command.strip()):
        return command
    words = shlex.split(command)
    if len(words) == 3 and PurePosixPath(words[0]).name in {"bash", "sh", "zsh"} and words[1] in {"-c", "-lc"}:
        return words[2]
    return command


# ---- heredocs and wrappers ------------------------------------------------

# A lifted heredoc in parsed text; its body is gone.
HEREDOC = "\x03"
# How a heredoc reads once its body is withheld: valid shell, the same for every body.
REDACTED_HEREDOC = "<<'EOF'"
REDACTED_BODY = "\n…\nEOF"
_HEREDOC_START = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|(\\?)([A-Za-z0-9_.-]+))")


def lift_heredocs(command):
    """Take the heredoc bodies out of a command, and drop its comments.

    Returns `(text, redacted, bodies)`: `text` has each heredoc operator as
    ` HEREDOC ` and no body; `redacted` is the command with every heredoc
    written `<<'EOF'` and its body `…`; `bodies` pairs each body with
    whether its delimiter was quoted (so nothing in it expands). None when a
    heredoc is unterminated, or for a here-string (`<<<`).
    """
    text, redacted, bodies, pending, quote, i, n = [], [], [], [], "", 0, len(command)

    def emit(value, shown=None):
        text.append(value)
        redacted.append(value if shown is None else shown)
    while i < n:
        char = command[i]
        if quote:
            emit(char)
            if char == "\\" and quote == '"' and i + 1 < n:
                emit(command[i + 1])
                i += 1
            elif char == quote:
                quote = ""
            i += 1
        elif char in "'\"":
            quote = char
            emit(char)
            i += 1
        elif char == "\\" and i + 1 < n:
            emit(command[i:i + 2])
            i += 2
        elif char == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            end = command.find("\n", i)
            i = n if end < 0 else end
        elif command.startswith("<<", i):
            match = _HEREDOC_START.match(command, i)
            if command.startswith("<<<", i) or not match:
                return None
            pending.append((match.group(2) or match.group(3) or match.group(5), bool(match.group(1)),
                            not match.group(5) or bool(match.group(4))))
            spaced = not redacted or redacted[-1][-1:].isspace()
            emit(f" {HEREDOC} ", ("" if spaced else " ") + REDACTED_HEREDOC)
            i = match.end()
        elif char == "\n" and pending:
            redacted.append(REDACTED_BODY * len(pending))
            text.append("\n")
            i += 1
            for delimiter, tabs, quoted in pending:
                lines = []
                while True:
                    if i >= n:
                        return None
                    end = command.find("\n", i)
                    line = command[i:n if end < 0 else end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if tabs else line) == delimiter:
                        break
                    lines.append(line)
                bodies.append(("\n".join(lines), quoted))
            pending = []
            if i < n:
                redacted.append("\n")
        else:
            emit(char)
            i += 1
    if quote or pending:
        return None
    return "".join(text), "".join(redacted), bodies


def expanding(body, quoted):
    """Whether a heredoc body runs commands itself (`$(...)` in an unquoted one)."""
    return not quoted and ("$(" in body or "`" in body)


_PATH_WORD = re.compile(r"^[A-Za-z0-9._/~+-]{1,240}$")
_WRAPPERS = {"env", "nice", "ionice", "nohup", "time", "timeout", "stdbuf", "sudo"}
_WRAPPER_VALUE_FLAGS = {"timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n", "--adjustment"},
                        "ionice": {"-c", "-n", "-p", "--class", "--classdata"}, "env": {"-u", "-C", "--unset", "--chdir"},
                        "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user", "--group"},
                        "stdbuf": set(), "time": {"-f", "-o", "--format", "--output"}, "nohup": set()}


def peel(words, *, sudo=True):
    """`(wrapper words, command words)`: leading env assignments and wrappers
    such as `timeout 60`, `env A=1`, `nice -n 5` or `sudo -u x` apart from the
    command they run. With `sudo=False` a `sudo` stays part of the command.
    Nothing is peeled when a wrapper's options cannot be read or nothing runs.
    """
    i = 0
    while i < len(words):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i], re.S):
            i += 1
            continue
        base = PurePosixPath(words[i]).name
        if base not in _WRAPPERS or (base == "sudo" and not sudo):
            break
        i += 1
        while i < len(words) and words[i].startswith("-") and words[i] != "-":
            if words[i] == "--":
                i += 1
                break
            if not _FLAG.fullmatch(words[i].split("=", 1)[0]) and not re.fullmatch(r"-o[LN0]|-[ei][LN0]|-n?-?\d+", words[i]):
                return [], words
            i += 2 if words[i] in _WRAPPER_VALUE_FLAGS[base] else 1
        if base == "timeout":
            if i >= len(words) or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[i]):
                return [], words
            i += 1
    if i >= len(words):
        return [], words
    return words[:i], words[i:]


def heredoc_consumer(words):
    """What a simple command does with the one heredoc it reads, or "".

    `script`: an interpreter runs it (`python3 - <<EOF`, `bash <<EOF`);
    `write`/`append`: `cat > PATH`, `cat >> PATH`, `tee [-a] PATH` store it.
    Anything else reading a heredoc (a database shell, `ssh`) is "".
    """
    if words.count(HEREDOC) != 1:
        return ""
    _, rest = peel([word for word in words if word != HEREDOC])
    if not rest:
        return ""
    base = PurePosixPath(rest[0]).name
    if base == "cat":
        return {">": "write", ">>": "append"}.get(rest[1], "") if len(rest) == 3 and _PATH_WORD.fullmatch(rest[2]) else ""
    if base == "tee":
        append = rest[1:2] == ["-a"]
        paths = rest[2:] if append else rest[1:]
        return ("append" if append else "write") if len(paths) == 1 and _PATH_WORD.fullmatch(paths[0]) else ""
    if base in _SCRIPT or re.fullmatch(r"python3\.\d+", base):
        args = rest[1:]
        while args and args[0].startswith("-") and args[0] != "-":
            if args[0] in {"-c", "-e", "-m", "-E", "-p", "-r", "--eval", "--print", "--require", "--import"} or not _FLAG.fullmatch(args[0]):
                return ""
            if args[0] == "-s":
                return "script"
            args = args[1:]
        return "script" if not args or args[0] == "-" else ""
    return ""


def _classify_heredoc(text):
    """Route one lifted command that reads a heredoc, shaped as its consumer."""
    match = re.fullmatch(r"\s*(?:cd\s+([^\s;&|'\"]+)\s*&&\s*)?([^;&|\n'\"]*)\s*", text)
    if not match or text.count(HEREDOC) != 1:
        return _abstain("compound", action="compound", signature="shell")
    words = re.sub(r"(>>?)", r" \1 ", re.sub(r"(?:^|\s)[12]?>\s*/dev/null\b", " ", match.group(2))).split()
    consumer = heredoc_consumer(words)
    if consumer == "write":
        rest = peel([word for word in words if word != HEREDOC])[1]
        return _match("write_file", {"file": rest[rest.index(">") + 1] if ">" in rest else rest[-1]},
                      signature=_signature(rest))
    if consumer == "append":
        return _abstain("uncertain", action="write", signature="shell")
    if consumer == "script":
        return _abstain("inline_script", action="execute", signature="shell")
    return _abstain("compound", action="compound", signature="shell")


def _flags_and_args(words, value_flags=_VALUE_FLAGS):
    flags, args, skip = [], [], False
    for word in words:
        if skip:
            skip = False
        elif word.startswith("-") and word != "-":
            flags.append(word.split("=", 1)[0])
            skip = word in value_flags
        else:
            args.append(word)
    return flags, args


_FLAG = re.compile(r"^(?:-[A-Za-z]{1,3}|--[a-z][a-z0-9-]{1,30})$")


def _identity(kind, shape):
    """Keyed hash of an exact invocation shape.

    Learned mappings and cache invalidation key on this, so two invocations
    share an identity only when every executable path, subcommand, flag and flag
    value matches. Raw values are never stored, and the per-Host key keeps a
    guessable secret from being confirmed against a stored hash.
    """
    from . import settings_store
    key = settings_store.get_text(_IDENTITY_KEY)
    if not key:
        key = secrets.token_hex(32)
        settings_store.set_text(_IDENTITY_KEY, key)
    return hmac.new(key.encode(), json.dumps([kind, shape]).encode(), "sha256").hexdigest()[:24]


def _signature(words):
    """Program and flag set. A flag that could carry a value (`-pSECRET`) is masked."""
    flags = sorted({(w.split("=", 1)[0] if _FLAG.fullmatch(w.split("=", 1)[0]) else "-?")
                    for w in words[1:] if w.startswith("-") and not w.lstrip("-").isdigit()})
    program = PurePosixPath(words[0]).name if words else ""
    return " ".join([program if _NAME.fullmatch(program) else "?", *flags])[:120] if words else ""


def classify_shell(command):
    try:
        command = _strip_wrapper(command)
    except ValueError:
        return _abstain("malformed", signature="shell")
    if "<<" in command or "#" in command:
        lifted = lift_heredocs(command)
        if lifted is None or (lifted[2] and any(expanding(*body) for body in lifted[2])):
            return _abstain("compound", action="compound", signature="shell")
        if lifted[2]:
            return _classify_heredoc(lifted[0])
        command = lifted[0]
    try:
        segments = _segments(command)
    except ValueError:
        return _abstain("malformed", signature="shell")
    if segments is None:
        return _abstain("compound", action="compound", signature="shell")
    cwd = ""
    if len(segments) >= 2 and segments[0][1] == "&&":
        try:
            first = shlex.split(segments[0][0])
        except ValueError:
            return _abstain("malformed", signature="shell")
        if len(first) == 2 and first[0] == "cd":
            cwd = first[1]
            segments = segments[1:]
    # A trailing `| head -n N` or `| tail -N` only shortens displayed output.
    if len(segments) == 2 and segments[0][1] == "|" and re.fullmatch(r"(head|tail)(\s+-n)?\s+-?\d+", segments[1][0]):
        segments = [(segments[0][0], "")]
    if len(segments) != 1 or not segments[0][0]:
        return _abstain("compound", action="compound", signature="shell")
    try:
        words = shlex.split(segments[0][0])
    except ValueError:
        return _abstain("malformed", signature="shell")
    # Env assignments and wrappers (`timeout 60`, `nice`) only change how the
    # command runs; an assignment's value is never a parameter. `sudo` stays.
    words = peel(words, sudo=False)[1]
    if not words:
        return _abstain("malformed", signature="shell")
    route = _classify_words(words, cwd)
    route.setdefault("signature", _signature(words))
    return route


def _classify_words(words, cwd):
    base = PurePosixPath(words[0]).name
    signature = _signature(words)
    flags, args = _flags_and_args(words[1:])
    if base in _PRIVILEGED:
        return _abstain("privileged", action="privileged", signature=signature)
    if base in {"ls", "tree", "dir"}:
        if len(args) > 1:
            return _abstain("multiple_targets", action="list", signature=signature)
        return _match("list_directory", {"directory": args[0] if args else cwd}, signature=signature)
    if base == "pwd":
        return _match("show_current_directory", {}, signature=signature)
    if base in _READ:
        files = [a for a in args if not a.isdigit()]
        if len(files) == 1:
            return _match("read_file", {"file": files[0]}, signature=signature)
        return _abstain("multiple_targets", action="read", signature=signature)
    if base == "sed":
        if any(f.startswith("-i") or f == "--in-place" for f in flags):
            return _match("edit_file", {"file": args[-1]}, signature=signature) if len(args) == 2 else _abstain("uncertain", action="edit", signature=signature)
        if "-n" in flags and len(args) == 2 and re.fullmatch(r"\d+(,\d+)?p", args[0]):
            return _match("read_file", {"file": args[1]}, signature=signature)
        return _abstain("uncertain", signature=signature)
    if base in _SEARCH:
        if "-f" in flags or "--file" in flags:
            return _abstain("uncertain", action="search", signature=signature)
        if "--files" in flags:
            return _match("find_files", {"directory": args[0] if len(args) == 1 else cwd}, signature=signature) if len(args) <= 1 else _abstain("multiple_targets", action="list", signature=signature)
        pattern = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in {"-e", "--regexp"}), "")
        if pattern:
            flags, args = _flags_and_args([w for w in words[1:] if w != pattern], _VALUE_FLAGS - {"-e", "--regexp"})
            paths = args
        elif args:
            pattern, paths = args[0], args[1:]
        else:
            return _abstain("malformed", action="search", signature=signature)
        return _match("search_text", {"pattern": pattern, "directory": paths[0] if len(paths) == 1 else (cwd if not paths else "")}, signature=signature)
    if base == "find":
        if any(w in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"} for w in words):
            return _abstain("find_action", action="compound", signature=signature)
        directory = words[1] if len(words) > 1 and not words[1].startswith("-") else cwd
        pattern = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in {"-name", "-iname", "-path", "-ipath"}), "")
        return _match("find_files", {"pattern": pattern, "directory": directory}, signature=signature)
    if base in {"fd", "fdfind"}:
        if any(f in {"-x", "--exec", "-X", "--exec-batch"} for f in flags):
            return _abstain("find_action", action="compound", signature=signature)
        if len(args) > 2:
            return _abstain("multiple_targets", action="list", signature=signature)
        return _match("find_files", {"pattern": args[0] if args else "", "directory": args[1] if len(args) > 1 else cwd}, signature=signature)
    if base in {"rm", "rmdir", "unlink"}:
        if not args:
            return _abstain("malformed", action="delete", signature=signature)
        recursive = base == "rm" and any(f == "--recursive" or (not f.startswith("--") and set(f[1:]) & {"r", "R"}) for f in flags)
        return _match("delete_recursive" if recursive else "delete_path", {"path": args[0] if len(args) == 1 else ""}, signature=signature)
    if base == "mkdir":
        return _match("create_directory", {"directory": args[0]}, signature=signature) if len(args) == 1 else _abstain("multiple_targets", action="write", signature=signature)
    if base in {"curl", "wget"}:
        uploads = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form", "-T",
                   "--upload-file", "--json", "--post-data", "--post-file", "--body-data", "--body-file"}
        method = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in {"-X", "--request", "--method"}), "GET")
        if uploads & set(flags) or method.upper() not in {"GET", "HEAD"}:
            return _abstain("network_upload", action="network", signature=signature)
        urls = [w for w in words[1:] if re.match(r"https?://", w)]
        if len(urls) != 1:
            return _abstain("uncertain", action="network", signature=signature)
        return _match("fetch_url", {"host": urlsplit(urls[0]).hostname or ""}, signature=signature)
    if base == "git":
        return _classify_git(words, cwd, signature)
    if base in {"pytest", "py.test"} or words[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        start = 1 if base in {"pytest", "py.test"} else 3
        _, targets = _flags_and_args(words[start:], {"-k", "-m", "-n", "-p", "-c", "--rootdir", "--maxfail", "-o"})
        return _match("run_tests", {"target": targets[0] if len(targets) == 1 else ""}, signature=signature)
    if base in _SCRIPT or re.search(r"\.(py|js|mjs|cjs|ts|sh|bash|rb)$", base):
        return _abstain("script_run", action="execute", signature=signature)
    if base in _MUTATING:
        return _abstain("mutating_program", action="execute", signature=signature)
    if base in _BUILTINS:
        return _abstain("shell_builtin", signature=signature)
    candidates = [a for a in args if 0 < len(a) <= 240]
    # Only a lone positional argument may vary between reuses; anything else,
    # including option values such as `--mode=list` versus `--mode=delete`,
    # is part of what the program is being asked to do.
    shape = [words[0], *("{arg}" if len(candidates) == 1 and w == candidates[0] else w for w in words[1:])]
    display = base if _NAME.fullmatch(base) else "?"
    return _abstain("unknown_program", signature=f"{display} #{_identity('shell', shape)}", candidates=candidates)


def _classify_git(words, cwd, signature):
    rest = words[1:]
    while rest and rest[0] in {"-C", "-c", "--no-pager"}:
        rest = rest[1:] if rest[0] == "--no-pager" else rest[2:]
    if not rest:
        return _abstain("malformed", signature=signature)
    command, flags, args = rest[0], *_flags_and_args(rest[1:])
    signature = f"git {command}"
    simple = {"status": "git_status", "diff": "git_diff", "log": "git_log", "pull": "git_pull", "commit": "git_commit"}
    if command in simple and not (command == "pull" and args):
        return _match(simple[command], {}, signature=signature)
    if command == "push":
        if any(f in {"-f", "--force", "--force-with-lease", "--delete", "-d", "--mirror", "--prune"} for f in flags) or len(args) > 2 or any(":" in a or a.startswith("+") for a in args):
            return _abstain("unmapped_git", action="network", signature=signature)
        return _match("git_push", {"remote": args[0] if args else "", "branch": args[1] if len(args) > 1 else ""}, signature=signature)
    if command == "grep" and args:
        return _match("search_text", {"pattern": args[0], "directory": args[1] if len(args) == 2 else ""}, signature=signature)
    if command == "ls-files":
        return _match("find_files", {"directory": args[0] if len(args) == 1 else ""}, signature=signature)
    return _abstain("unmapped_git", signature=signature)


# ---- tools ----------------------------------------------------------------

def _field(activity, *keys):
    inputs = activity.get("input") if isinstance(activity.get("input"), dict) else {}
    for key in keys:
        for source in (activity, inputs):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def classify(activity):
    """Route one normalized activity (see tool_explanations.normalize_activity)."""
    name = str(activity.get("name") or "").strip()
    kind = str(activity.get("kind") or "").strip()
    tool = name.lower()
    signature = f"tool:{name or kind}"[:120]
    command = _field(activity, "command", "cmd") or (activity.get("summary", "") if kind == "command" else "")
    if tool in {"bash", "shell", "exec_command", "local_shell"} or kind == "command":
        return classify_shell(command) if command else _abstain("malformed", signature=signature)
    label = shell_label(activity)
    if label:
        return _abstain("truncated", action="compound", signature="shell") if len(label) >= LABEL_CLIP else classify_shell(label)
    file_path = _field(activity, "file_path", "path")
    if tool == "read":
        return _match("read_file", {"file": file_path}, signature=signature) if file_path else _abstain("malformed", signature=signature)
    if tool in {"edit", "multiedit", "notebookedit"}:
        return _match("edit_file", {"file": file_path}, signature=signature) if file_path else _abstain("malformed", signature=signature)
    if tool == "write":
        return _match("write_file", {"file": file_path}, signature=signature) if file_path else _abstain("malformed", signature=signature)
    if tool == "grep":
        pattern = _field(activity, "pattern")
        return _match("search_text", {"pattern": pattern, "directory": _field(activity, "path")}, signature=signature) if pattern else _abstain("malformed", signature=signature)
    if tool == "glob":
        return _match("find_files", {"pattern": _field(activity, "pattern"), "directory": _field(activity, "path")}, signature=signature)
    if tool == "ls":
        return _match("list_directory", {"directory": _field(activity, "path")}, signature=signature)
    if tool == "websearch":
        query = _field(activity, "query")
        return _match("web_search", {"query": query}, signature=signature) if query else _abstain("malformed", signature=signature)
    if kind == "exploration":
        return _classify_exploration(activity.get("operations") or [])
    if kind == "patch":
        operations = activity.get("operations") or []
        if len(operations) == 1 and ": " in operations[0]:
            label, path = operations[0].split(": ", 1)
            template = {"Edit": "edit_file", "Add": "write_file", "Delete": "delete_path"}.get(label)
            if template:
                return _match(template, {"file" if template != "delete_path" else "path": path}, signature=f"patch:{label}")
        return _abstain("multiple_targets", action="edit", signature="patch")
    if not name and not kind:
        return _abstain("malformed", signature=signature)
    inputs = activity.get("input") if isinstance(activity.get("input"), dict) else {}
    shape = [name, kind, sorted((k, v) for k, v in {**{k: v for k, v in activity.items() if isinstance(v, str)}, **inputs}.items()
                                if isinstance(v, str) and v != file_path)]
    return _abstain("unknown_tool", signature=f"{signature} #{_identity('tool', shape)}", candidates=[file_path] if file_path else [])


def shell_label(activity):
    """The shell command a tool's display name spells out, or ""."""
    name = str(activity.get("name") or "").strip()
    if activity.get("kind") or _field(activity, "command", "cmd") or not _SHELL_LABEL.match(name):
        return ""
    return name


def _classify_exploration(operations):
    """A grouped read-only exploration row. Only a single `Read: path` names its target.

    `List` and `Search` lines carry display text that may be a pattern or a
    whole command, so they, and several operations, are left to the model.
    """
    operations = list(dict.fromkeys(operations))
    if len(operations) == 1:
        label, _, target = operations[0].partition(": ")
        if label == "Read" and target and "," not in target and not re.search(r"\s", target) and (
                "/" in target or target[:1] in ".~" or re.fullmatch(r"[^\s]+\.[A-Za-z0-9]{1,8}", target)):
            return _match("read_file", {"file": target}, signature="exploration:Read")
    return _abstain("multiple_targets" if len(operations) > 1 else "exploration", action="read", signature="exploration")


# ---- parameters and rendering --------------------------------------------

def _evidence(activity):
    values = [v for v in activity.values() if isinstance(v, str)]
    inputs = activity.get("input")
    if isinstance(inputs, dict):
        values += [v for v in inputs.values() if isinstance(v, str)]
    elif isinstance(inputs, str):
        values.append(inputs)
    values += [v for v in activity.get("operations") or [] if isinstance(v, str)]
    return "\n".join(values)


def valid_parameter(kind, value):
    if kind not in _PARAMETER_TYPES or not isinstance(value, str) or not value or value != value.strip():
        return False
    if "[redacted]" in value:
        return False
    if kind in {"directory", "path"}:
        return len(value) <= 240 and not value.startswith("-") and not _UNSAFE.search(value) and "://" not in value
    if kind == "pattern":
        return len(value) <= 120 and not re.search(r"[\x00-\x1f\x7f`]", value)
    if kind == "host":
        return bool(_HOST.fullmatch(value)) and "." in value
    if kind == "name":
        return bool(_NAME.fullmatch(value))
    return len(value) <= 160 and not re.search(r"[\x00-\x1f\x7f`]", value)


def validate(template_id, parameters, activity):
    """Typed parameters that literally appear in the activity, or None."""
    template = TEMPLATES.get(template_id)
    if template is None or not isinstance(parameters, dict) or set(parameters) - set(template["params"]):
        return None
    evidence = _evidence(activity)
    for key, value in parameters.items():
        if not valid_parameter(template["params"][key], value):
            return None
        if value not in evidence:
            return None
    return dict(parameters)


def _derived(parameters):
    values = dict(parameters)
    for key in ("directory", "file", "path", "target"):
        if key in parameters:
            name = PurePosixPath(parameters[key].rstrip("/")).name
            if name and name not in {".", "..", "~"} and not name.startswith("~"):
                values.setdefault(f"{key}_name" if key == "directory" else "file_name", name)
                if key == "directory" and _PROJECT.fullmatch(name):
                    values["project"] = name
    return values


def render(template_id, parameters, level):
    """First variant whose placeholders are all available, or None."""
    template = TEMPLATES.get(template_id)
    if template is None or str(level) not in template["text"]:
        return None
    values = _derived(parameters)
    for variant in template["text"][str(level)]:
        needed = {field for _, field, _, _ in string.Formatter().parse(variant) if field}
        if needed <= set(values):
            text = variant.format(**{k: values[k] for k in needed})
            if len(text) <= 240:
                return text
    return None


def lookup(activity, level, mappings=None):
    """Scripted result `(text, route)`; text is None when the route abstains.

    `mappings` maps identities of explicitly vetted rules to template IDs; they
    only ever apply to an otherwise unknown program or tool whose exact
    invocation shape (see `_identity`) was approved.
    """
    route = classify(activity)
    if "template_id" not in route and route.get("reason") in JEV_REASONS and mappings:
        template_id = mappings.get(route.get("signature"))
        if template_id in TEMPLATES and TEMPLATES[template_id]["action"] in LEARNABLE_ACTIONS:
            parameters = learned_parameters(template_id, route.get("candidates", []))
            if parameters is not None:
                route = Route(route, template_id=template_id, parameters=parameters, learned=True,
                              action=TEMPLATES[template_id]["action"])
                route.pop("reason", None)
    if "template_id" not in route:
        return None, route
    parameters = validate(route["template_id"], route["parameters"], activity)
    if parameters is None:
        return None, Route(reason="invalid_parameters", action=route["action"], signature=route.get("signature", ""))
    return render(route["template_id"], parameters, level), route


def hedge(text):
    """A model-selected template states what the call most likely does, not a fact."""
    hedged = "Likely " + text[:1].lower() + text[1:]
    return hedged if len(hedged) <= 240 else None


def learned_parameters(template_id, candidates):
    """Fill the single primary parameter of a learned rule, or None if ambiguous."""
    params = TEMPLATES[template_id]["params"]
    primary = next((key for key, kind in params.items() if kind in {"directory", "path"}), None)
    if not candidates or primary is None:
        return {}
    if len(candidates) != 1:
        return None
    return {primary: candidates[0]}


def primary(template_id):
    """The one path or directory parameter a template acts on, or None."""
    return next((name for name, kind in TEMPLATES[template_id]["params"].items() if kind in {"directory", "path"}), None)


def jev_candidate(value):
    """Whether a literal argument could be the file, folder or pattern a call acts on.

    Shell punctuation (`]`), `$` expressions, flags, bare numbers (usually a
    flag's value, as in `--depth 2`) and free text with spaces (a message, not
    a file) are never offered.
    """
    return (valid_parameter("path", value) and not re.search(r"\s", value)
            and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?[smhdkKMG%]?", value)
            and bool(re.search(r"[A-Za-z0-9._~/]", value)) and value not in {"[", "]", "[[", "]]", "{", "}"})


def jev_offer(route, level):
    """What Jev may choose from for one abstaining route, or why it is not asked.

    Returns `(template_ids, candidates)`, where candidates are `(index, value)`
    pairs into `route["candidates"]` so learned rows keep their indices, or a
    `str` reason. A template that acts on a path is offered only when the call
    names a usable one; without any argument only templates that are true of
    the current directory, or need no parameter, are offered.
    """
    raw = route.get("candidates") or []
    candidates = [(index, value) for index, value in enumerate(raw) if jev_candidate(value)]
    if route.get("reason") == "unknown_tool" and not raw:
        # A status row ("done", "idle") or a delegation: nothing a read, list
        # or search template could name.
        return "jev_no_target"
    if raw and not candidates:
        return "jev_unsafe_arguments"
    offered = []
    for template_id, template in TEMPLATES.items():
        name = primary(template_id)
        if template["action"] not in LEARNABLE_ACTIONS:
            continue
        # Only what can render from this call: a pick that needs a value the
        # call does not have (a search pattern, a file name) is never valid.
        if name and candidates:
            renders = any(render(template_id, {name: value}, level) for _, value in candidates)
        else:
            renders = (name is None or template["params"][name] == "directory") and render(template_id, {}, level)
        if renders:
            offered.append(template_id)
    return offered, candidates
