import importlib.util
import pathlib

import pytest


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ci_ipv6_routes.py"
_SPEC = importlib.util.spec_from_file_location("ci_ipv6_routes_script", _SCRIPT)
assert _SPEC and _SPEC.loader
ci_ipv6_routes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_ipv6_routes)

ZERO = "0" * 32
# /proc/net/ipv6_route of the offline preflight container in docker.yml run 38941121540 (ubuntu-24.04
# 20261012.334.1): the kernel's ::/0 unreachable rows on lo, and ::1/128.
RECORDED_REJECT = f"{ZERO} 00 {ZERO} 00 {ZERO} ffffffff 00000001 00000000 00200200       lo"
RECORDED_LOOPBACK = f"{'0' * 31}1 80 {ZERO} 00 {ZERO} 00000000 00000003 00000000 80200001       lo"
RECORDED_TABLE = "\n".join([RECORDED_REJECT, RECORDED_LOOPBACK, RECORDED_REJECT, ""])
# A router-advertised default route through a link-local gateway: a real way out.
USABLE_DEFAULT = f"{ZERO} 00 {ZERO} 00 fe800000000000000000000000000001 00000400 00000001 00000000 00450003     eth0"
LINK_LOCAL = f"fe800000000000000000000000000000 40 {ZERO} 00 {ZERO} 00000100 00000001 00000000 00000001     eth0"


@pytest.mark.parametrize("table, usable", [
    (RECORDED_TABLE, []),
    (LINK_LOCAL + "\n" + RECORDED_LOOPBACK, []),
    (USABLE_DEFAULT, [USABLE_DEFAULT]),
    (RECORDED_REJECT + "\n" + USABLE_DEFAULT + "\n" + LINK_LOCAL, [USABLE_DEFAULT]),
], ids=["recorded-ci-reject-rows", "non-default-and-local", "usable-default", "reject-plus-usable"])
def test_only_explicit_reject_default_rows_are_not_routes_out(table, usable):
    assert ci_ipv6_routes.usable_ipv6_defaults(table) == usable


@pytest.mark.parametrize("row", [
    RECORDED_REJECT.replace("00200200", "zz200200"),     # flags not hex
    RECORDED_REJECT.replace("00200200", "200200"),       # flags not 8 digits
    RECORDED_REJECT.replace("00200200", "20200200"),     # reject plus a flag bit this check does not know
    RECORDED_REJECT.rsplit(maxsplit=1)[0],               # no device field
    RECORDED_REJECT.replace(" 00 ", " 0 ", 1),           # prefix length not 2 digits
], ids=["non-hex-flags", "short-flags", "unknown-flag", "missing-field", "short-prefix-length"])
def test_a_row_that_does_not_parse_fails_closed(row):
    with pytest.raises(ValueError):
        ci_ipv6_routes.usable_ipv6_defaults(RECORDED_LOOPBACK + "\n" + row)
