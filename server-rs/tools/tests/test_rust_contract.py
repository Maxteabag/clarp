"""The Rust Host's client contract against its record (docs/compatibility.md).

server-rs/crates/clarp-host/src/edge.rs holds HOST_CONTRACT and FEATURES; the compatibility table records
every Rust contract number in a row of its own, newest first. These checks keep the two in step, as
tests/unit/test_client_contract.py did for the Python Host's constants.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
EDGE = (ROOT / "server-rs/crates/clarp-host/src/edge.rs").read_text()
TABLE = (ROOT / "docs/compatibility.md").read_text()
# The first Rust contract number with a row of its own (the rows before it are the Python Host's).
FIRST_RUST_ROW = 145


def host_contract():
    return int(re.search(r"pub const HOST_CONTRACT: u64 = (\d+);", EDGE)[1])


def features():
    body = re.search(r"FEATURES: &\[\(&str, u64\)\] = &\[(.*?)\n\];", EDGE, re.S)
    assert body, "FEATURES not found in edge.rs"
    return [(name, int(n)) for name, n in re.findall(r'\(\s*"([a-z0-9_]+)",\s*(\d+)\s*\)', body[1])]


def rust_rows():
    """The table's numbered Rust rows, in the order they stand: (number, the row's text)."""
    return [(int(n), line) for line in TABLE.splitlines()
            for n in re.findall(r"^\|\s*`(\d+)`\s*\(Rust only\)", line)]


def test_the_newest_rust_row_is_the_host_contract():
    rows = rust_rows()
    assert rows, "docs/compatibility.md has no Rust contract row"
    assert rows[0][0] == host_contract(), (
        f"HOST_CONTRACT is {host_contract()} but the newest Rust row says {rows[0][0]}: add its row on top")


def test_the_rust_rows_stand_newest_first_each_once():
    numbers = [n for n, _ in rust_rows()]
    assert numbers == sorted(numbers, reverse=True), f"Rust rows out of order: {numbers[:12]}"
    assert len(numbers) == len(set(numbers)), "a Rust contract number has two rows"


def test_no_feature_is_newer_than_the_host_contract():
    late = [(name, n) for name, n in features() if n > host_contract()]
    assert not late, f"features numbered past HOST_CONTRACT {host_contract()}: {late}"


def test_each_rust_feature_has_the_row_of_its_number_and_the_row_names_it():
    rows = dict(rust_rows())
    missing = []
    for name, n in features():
        if n < FIRST_RUST_ROW:
            continue
        row = rows.get(n)
        if row is None or f"`{name}`" not in row:
            missing.append((name, n))
    assert not missing, f"features without a row of their number that names them: {missing}"
