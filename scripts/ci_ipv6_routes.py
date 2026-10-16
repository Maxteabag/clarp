"""IPv6 route-table check for the CI offline preflight (scripts/ci_docker_offline.sh).

The preflight runs this source inside a test container, ahead of its own checks, so it is pure
standard library with no top-level effects. tests/unit/test_ci_ipv6_routes.py holds its proof.

A kernel with IPv6 always lists ::/0 "unreachable" rows on lo (RTF_REJECT); they carry no packet
anywhere, so only those are skipped. Every other ::/0 row is a usable default route, and a row that
does not parse, or carries a flag this check does not know, fails closed.
"""

RTF_REJECT = 0x00000200
# include/uapi/linux/route.h RTF_UP..RTF_REJECT and include/uapi/linux/ipv6_route.h RTF_DEFAULT..RTF_LOCAL
# (RTF_PREF occupies 0x18000000).
KNOWN_ROUTE_FLAGS = 0x000003FF | 0x07FF0000 | 0x18000000 | 0x40000000 | 0x80000000
_HEX_WIDTHS = (32, 2, 32, 2, 32, 8, 8, 8, 8)  # dest, dest_len, src, src_len, next hop, metric, refcnt, use, flags
_HEX = set("0123456789abcdefABCDEF")


def usable_ipv6_defaults(table: str) -> list[str]:
    """The ::/0 rows of a /proc/net/ipv6_route text that a packet could leave by.

    Raises ValueError for a malformed row or an unknown flag, so the caller fails closed.
    """
    usable = []
    for row in table.splitlines():
        if not row.strip():
            continue
        fields = row.split()
        if len(fields) != len(_HEX_WIDTHS) + 1:
            raise ValueError(f"malformed ipv6_route row: {row!r}")
        for value, width in zip(fields, _HEX_WIDTHS):
            if len(value) != width or not set(value) <= _HEX:
                raise ValueError(f"malformed ipv6_route row: {row!r}")
        flags = int(fields[8], 16)
        if flags & ~KNOWN_ROUTE_FLAGS:
            raise ValueError(f"unknown ipv6 route flags {fields[8]} in row: {row!r}")
        if int(fields[1], 16) == 0 and not flags & RTF_REJECT:
            usable.append(row)
    return usable
