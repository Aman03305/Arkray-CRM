"""The edge proxy's per-address limit counts an IPv6 client by its /64 (R127).

infrastructure/nginx/arkray.conf maps nginx's text of the client address ($remote_addr) to
`$arkray_client_key` with ordered regexes. This reads that map and applies it the way nginx
does (exact `default`, then the regexes in order, `~*` case-insensitive, `$1`/`$2` filled
from the match) to nginx's own formatting of each address (ngx_inet6_ntop, ported below),
and checks the key is the address's /64 written as four groups, for every way an address
can be compressed. Checked live too: nginx -t, and curl through the real_ip module with
these addresses (docs/operations.md#edge-rate-limits).
"""

from __future__ import annotations

import ipaddress
import random
import re
from pathlib import Path

import pytest

NGINX = Path(__file__).resolve().parents[3] / "infrastructure" / "nginx" / "arkray.conf"


def _map() -> tuple[str, list[tuple[re.Pattern[str], str]]]:
    text = NGINX.read_text(encoding="utf-8")
    (body,) = re.findall(r"^map \$remote_addr \$arkray_client_key \{\n(.*?)^\}", text, re.S | re.M)
    default, rules = "", []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source, value = re.fullmatch(r'("[^"]*"|\S+)\s+(\S+);', line).groups()  # type: ignore[union-attr]
        source = source.strip('"')
        if source == "default":
            default = value
            continue
        flags = re.IGNORECASE if source.startswith("~*") else 0
        rules.append((re.compile(source.lstrip("~*"), flags), value))
    return default, rules


DEFAULT, RULES = _map()


def nginx_key(remote_addr: str) -> str:
    """What nginx sets $arkray_client_key to."""
    for pattern, value in RULES:
        found = pattern.search(remote_addr)
        if found is not None:
            filled = found.expand(re.sub(r"\$(\d)", r"\\\1", value))
            return filled.replace("$remote_addr", remote_addr)
    return DEFAULT.replace("$remote_addr", remote_addr)


def nginx_text(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """$remote_addr as nginx writes it (a port of src/core/ngx_inet.c, ngx_inet6_ntop):
    lower-case hex without leading zeros; the first longest run of two or more zero groups
    as "::"; ::ffff:a.b.c.d and ::a.b.c.d with the IPv4 tail dotted."""
    if address.version == 4:
        return str(address)
    p = address.packed
    zero, last, longest, n = -1, -1, 1, 0
    for i in range(0, 16, 2):
        if p[i] or p[i + 1]:
            if longest < n:
                zero, longest = last, n
            n = 0
            continue
        if n == 0:
            last = i
        n += 1
    if longest < n:
        zero, longest = last, n
    n = 16
    out = ""
    if zero == 0:
        if (
            (longest == 5 and p[10] == 0xFF and p[11] == 0xFF)
            or longest == 6
            or (longest == 7 and p[14] != 0 and p[15] != 1)
        ):
            n = 12
        out += ":"
    i = 0
    while i < n:
        if i == zero:
            out += ":"
            i += longest * 2
            continue
        out += f"{(p[i] << 8) | p[i + 1]:x}"
        if i < 14:
            out += ":"
        i += 2
    if n == 12:
        out += ".".join(str(b) for b in p[12:])
    return out


def expected_key(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if address.version == 4:
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    groups = [int.from_bytes(address.packed[i : i + 2], "big") for i in range(0, 8, 2)]
    return ":".join(f"{g:x}" for g in groups)


EDGE_CASES = [
    "203.0.113.5",
    "::ffff:198.51.100.7",
    "::1",
    "::",
    "::1.2.3.4",
    "2001:db8::",
    "2001:db8::1",
    "2001:db8:0:5:1:2:3:4",
    "2001:db8::5:1:2:3:4",
    "2001:db8:0:0:1:0:0:0",
    "2001:db8:1:2::",
    "2001:db8:1:2:3::",
    "2001:db8:1::2",
    "2001:db8:1:0:0:2:3:4",
    "2001:0:0:1:2:3:4:5",
    "1::2:3:4:5:6",
    "::1:2:3:4:5",
    "::1:2:3:4:5:6",
    "0:0:1:2:3:4:5:6",
    "fe80::1ff:fe23:4567:890a",
    "2a00:1450:4001:82a::200e",
    "2600:1f18:0:ffff:abcd:ef01:2345:6789",
    "1:2:3:4:5:6:7:8",
    "1:2:3:4::",
    "1:2:3:4:5::",
    "1:0:0:0:0:0:0:8",
]


@pytest.mark.parametrize("text", EDGE_CASES)
def test_edge_cases(text):
    address = ipaddress.ip_address(text)
    assert nginx_key(nginx_text(address)) == expected_key(address), nginx_text(address)


def test_random_addresses_in_every_shape():
    """Random addresses with random zero groups, so every place and length of "::" occurs."""
    rng = random.Random(127)
    for _ in range(20_000):
        groups = [rng.choice([0, 0, 0, rng.randrange(1, 0x10000)]) for _ in range(8)]
        address = ipaddress.IPv6Address(b"".join(g.to_bytes(2, "big") for g in groups))
        text = nginx_text(address)
        if address.ipv4_mapped is None and "." not in text:
            # The port agrees with Python's own RFC 5952 text where both use plain hex.
            assert text == address.compressed
        assert nginx_key(text) == expected_key(address), text


def test_one_64_is_one_key_and_neighbours_differ():
    keys = {
        nginx_key(nginx_text(ipaddress.ip_address(f"2001:db8:aa:bb::{n:x}"))) for n in range(1, 50)
    }
    keys |= {nginx_key(nginx_text(ipaddress.ip_address("2001:db8:aa:bb:1:2:3:4")))}
    assert keys == {"2001:db8:aa:bb"}
    assert nginx_key("2001:db8:aa:bc::1") == "2001:db8:aa:bc"


def test_the_limit_is_keyed_by_the_map():
    text = NGINX.read_text(encoding="utf-8")
    (zone,) = re.findall(r"^limit_req_zone (\S+) zone=arkray_auth:", text, re.M)
    assert zone == "$arkray_client_key"
    assert "$binary_remote_addr" not in text.split("map $remote_addr")[0] + zone
