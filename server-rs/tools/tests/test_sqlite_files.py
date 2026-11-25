"""Nothing in the Host opens SQLite's own files with a plain descriptor (issue 848).

On Linux, POSIX locks belong to a process and a file, and closing *any* descriptor of the file drops
every one of them: SQLite's own included. On 25 Nov 2026 a diagnostic opened an agent log's `-shm`
with std::fs::File, read its header and closed it. That dropped the live connection's locks;
another connection then truncated the index under its mapping, and the Host died of SIGBUS in
walIndexAppend about once a minute for 22 minutes.

So: code that names one of SQLite's files (a `.db` or `.sqlite` file, a `-wal`, `-shm` or `-journal`,
or a constant that holds such a name, such as LOG_FILE) may not, in the same function, open a file
with a plain descriptor (std::fs::File, OpenOptions, fs::read, fs::read_to_string, fs::write,
fs::copy, a memory map, libc or nix open). Go through rusqlite, which shares SQLite's lock table.
In the SQLite store crate (clarp-store-sqlite), whose paths are all SQLite's files, every such call
is a finding, whatever the function names.

What opens no descriptor is not a finding, and needs no entry: metadata, symlink_metadata, is_file,
is_dir, exists, try_exists, canonicalize, remove_file, rename. rusqlite's Connection::open* is
SQLite itself. Tests (tests/ directories, *_tests.rs, #[cfg(test)] items) are not the live Host and
are not read.

The check is static and reads one function at a time: it catches the pattern that caused the
incident and its near relatives, not a path carried into another function. ALLOWED names each
exception and why it is safe; an entry that no longer matches anything fails too, so the list
never outlives its reasons.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
CRATES = ROOT / "server-rs/crates"

SQLITE_LITERAL = re.compile(r'"[^"\n]*(\.db|\.sqlite\d*|-wal|-shm|-journal)"')
CONSTANT = re.compile(r'\bconst\s+([A-Z][A-Z0-9_]*)\s*:\s*&(?:\'static\s+)?str\s*=\s*("[^"\n]*")')
PLAIN = re.compile(
    r"\b(File::(open|create|create_new|options)\b|OpenOptions\b|fs::(read|read_to_string|write|copy)\b"
    r"|Mmap(Mut)?::map|(libc|fcntl)::open\b)")
FN = re.compile(r"^(\s*)(pub(\([^)]*\))?\s+)?((const|async|unsafe|extern\s+\"C\")\s+)*fn\s+(\w+)")

# Crates whose every plain-descriptor call is a finding: they keep SQLite's files and nothing else.
STORE_CRATES = ("clarp-store-sqlite/",)

# (file under server-rs/crates, function, a fragment of the call's line, why it is safe)
ALLOWED = [
    ("clarp-store-sqlite/src/client_events.rs", "open", "OpenOptions::new()",
     "creates the client-events database with mode 600 before the store's connection opens it; "
     "clarp-host's client_events::Desk opens its one Store lazily, under a mutex, only while it holds "
     "no connection, and nothing else in the Host opens that file, so the process holds no lock on "
     "it to lose"),
    ("clarp-host/src/diagnostics.rs", "sample_from", "procfs.join(",
     "reads the kernel's /proc files; the function names fleet.db only to take its size with "
     "metadata, which opens no descriptor"),
]


def live_lines(text):
    """The lines of a source file with test-only items blanked: (number, line) pairs."""
    lines = text.splitlines()
    out = []
    skip_until = None
    pending_test = False
    for number, line in enumerate(lines, 1):
        if skip_until is not None:
            # The item ends at the first line back at its own indentation (`}`, `);`, `];`).
            if line.startswith(skip_until) and line[len(skip_until):][:1] not in (" ", ""):
                skip_until = None
            continue
        stripped = line.strip()
        if stripped.startswith("#[cfg(test)]"):
            pending_test = True
            continue
        if pending_test:
            if stripped.startswith("#[") or not stripped:
                continue
            pending_test = False
            # One line (`mod tests;`, a field, a `use`), or a block or statement over several.
            if not stripped.endswith((";", ",", "}")):
                skip_until = line[: len(line) - len(line.lstrip())]
            continue
        out.append((number, line))
    return out


def without_comment(line):
    return re.sub(r"//.*", "", line)


def sqlite_constants(sources):
    names = set()
    for text in sources:
        for name, value in CONSTANT.findall(text):
            if SQLITE_LITERAL.fullmatch(value):
                names.add(name)
    return names


def findings_in(text, constants, every_call=False):
    """(line, function, call line) for each plain-descriptor call in a function naming SQLite's files
    (in any function, with every_call). A call in a nested function is named by the innermost one."""
    lines = live_lines(text)
    names = SQLITE_LITERAL.pattern
    if constants:
        names += r"|\b(" + "|".join(sorted(constants)) + r")\b"
    names_sqlite = re.compile(names)
    found = {}
    for index, (number, line) in enumerate(lines):
        match = FN.match(line)
        if not match:
            continue
        indent, name = match[1], match[6]
        body = [(number, line)]
        if not (line.rstrip().endswith("}") or line.rstrip().endswith(";")):
            for later in lines[index + 1:]:
                body.append(later)
                if later[1] == indent + "}":
                    break
        code = [(n, without_comment(text_line)) for n, text_line in body]
        if not every_call and not any(names_sqlite.search(text_line) for _, text_line in code):
            continue
        for n, text_line in code:
            if PLAIN.search(text_line):
                found[n] = (n, name, text_line.strip())
    return [found[n] for n in sorted(found)]


def rust_sources():
    for path in sorted(CRATES.rglob("*.rs")):
        relative = path.relative_to(CRATES).as_posix()
        if "/tests/" in f"/{relative}" or path.name == "tests.rs" or path.name.endswith("_tests.rs"):
            continue
        if "/target/" in f"/{relative}":
            continue
        yield relative, path.read_text()


def test_no_plain_descriptor_opens_sqlites_files():
    sources = list(rust_sources())
    assert sources, f"no Rust sources under {CRATES}"
    constants = sqlite_constants(text for _, text in sources)
    assert "LOG_FILE" in constants, "the agent log's file name constant was not recognised"
    used = set()
    wrong = []
    for relative, text in sources:
        for line, function, call in findings_in(text, constants, relative.startswith(STORE_CRATES)):
            entry = next((i for i, (file, fn, fragment, _) in enumerate(ALLOWED)
                          if file == relative and fn == function and fragment in call), None)
            if entry is None:
                wrong.append(f"  server-rs/crates/{relative}:{line} in fn {function}: {call}")
            else:
                used.add(entry)
    assert not wrong, (
        "code that names SQLite's files opens a file with a plain descriptor. Closing any descriptor "
        "of a file drops every POSIX lock this process holds on it, SQLite's own included (issue 848: "
        "the Host crash-looped with SIGBUS in walIndexAppend). Go through rusqlite, or use metadata; "
        "if the call is safe, add it to ALLOWED in server-rs/tools/tests/test_sqlite_files.py with "
        "the reason:\n" + "\n".join(wrong))
    stale = [ALLOWED[i][:3] for i in range(len(ALLOWED)) if i not in used]
    assert not stale, f"ALLOWED entries that match nothing any more; remove them: {stale}"


# The code that caused the incident (removed in 29bb9b444), and its harmless neighbour.
INCIDENT = '''
const LOG_FILE: &str = "log.db";

fn wal_index(log_file: &Path) -> Option<WalIndex> {
    use std::io::Read;
    let mut shm = log_file.as_os_str().to_owned();
    shm.push("-shm");
    let mut header = [0u8; 100];
    std::fs::File::open(PathBuf::from(shm))
        .and_then(|mut file| file.read_exact(&mut header))
        .ok()?;
    None
}

fn copy_log(dir: &Path, to: &Path) {
    std::fs::copy(dir.join(LOG_FILE), to).unwrap();
}

fn size(dir: &Path) -> u64 {
    std::fs::metadata(dir.join("fleet.db")).map_or(0, |m| m.len())
}

fn config(dir: &Path) -> String {
    std::fs::read_to_string(dir.join("config.toml")).unwrap()
}

#[cfg(test)]
mod tests {
    fn synthetic(dir: &Path) {
        std::fs::write(dir.join("fleet.db"), b"synthetic").unwrap();
    }
}
'''


def test_the_check_catches_the_incidents_code_and_passes_its_neighbours():
    constants = sqlite_constants([INCIDENT])
    assert constants == {"LOG_FILE"}
    found = findings_in(INCIDENT, constants)
    assert [(function, call.split("(")[0]) for _, function, call in found] == [
        ("wal_index", "std::fs::File::open"),
        ("copy_log", "std::fs::copy"),
    ], found
    assert findings_in(INCIDENT, constants, every_call=True)[-1][1] == "config"
