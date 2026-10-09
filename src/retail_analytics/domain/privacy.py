"""Customer-data privacy policy: opaque references and age bands.

Policy: direct identifiers (names, contact details, addresses, raw
customer/order/item keys) never reach the model; customer purchase behaviour is
explored through opaque references; demographics (country, state, age band) are
available ONLY as group-level statistics, never for one customer, order or item
(confirmed client requirement; pseudonymous references do not make an
individual demographic profile acceptable). The compiler's grain check
(``adapters.sql_compiler.grain``) and the result boundary enforce it. There is
no minimum group size: a naturally small group, even of one customer, is a
group statistic, while selecting people by reference or rank is refused. This
is not an anonymity claim: fine group-by combinations can still describe very
few people.

Opaque references
    Keyed HMAC-SHA256 of ``<kind>:<raw key>``, under a key derived per
    executive. The same customer has the same reference in every relation and
    every session of one executive (joins and follow-ups work) and an unrelated
    reference for another executive, so references cannot be correlated across
    executives or recovered without the key. Rotating the master key retires
    every existing reference.

Age bands
    A fixed grid of :data:`AGE_BAND_WIDTH`-year cells anchored at multiples of
    the width, top-coded at :data:`AGE_TOP_CODE`. Analysis may merge cells into
    any coarser band, but never sees a finer one: because the grid is fixed and
    raw age never enters model SQL, no combination of queries (shifted
    boundaries, differencing) can resolve an age below the grid.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

# Logical reference kind -> public prefix. A kind not listed here has no
# reference derivation and fails closed.
REFERENCE_PREFIXES: Mapping[str, str] = MappingProxyType(
    {"customer_ref": "cus", "order_ref": "ord", "item_ref": "itm"}
)
# 24 hex characters = 96 bits of the HMAC: collision-free at dataset scale.
REFERENCE_HEX_CHARS = 24
_REFERENCE = re.compile(r"(cus|ord|itm)_[0-9a-f]{24}")

AGE_BAND_WIDTH = 5
AGE_TOP_CODE = 90
_BAND = re.compile(r"(\d{1,2})-(\d{1,2})|(\d{2})\+")


def reference_message(kind: str, raw_key: str) -> bytes:
    """The HMAC input for a raw key; kinds are domain-separated."""
    if kind not in REFERENCE_PREFIXES:
        raise ValueError("unknown reference kind")
    return f"{kind}:{raw_key}".encode()


def format_reference(kind: str, digest_hex: str) -> str:
    return f"{REFERENCE_PREFIXES[kind]}_{digest_hex[:REFERENCE_HEX_CHARS]}"


def is_reference(value: object, kind: str | None = None) -> bool:
    """Whether ``value`` has the shape of an opaque reference (of ``kind``)."""
    if not isinstance(value, str):
        return False
    match = _REFERENCE.fullmatch(value)
    if match is None:
        return False
    return kind is None or REFERENCE_PREFIXES.get(kind) == match.group(1)


def age_band_label(age: int | None) -> str | None:
    """Reference semantics of the trusted age-band derivation."""
    if age is None:
        return None
    if age < 0:
        raise ValueError("age must be non-negative")
    if age >= AGE_TOP_CODE:
        return f"{AGE_TOP_CODE}+"
    low = age // AGE_BAND_WIDTH * AGE_BAND_WIDTH
    return f"{low}-{low + AGE_BAND_WIDTH - 1}"


def is_age_band(value: object) -> bool:
    """Whether ``value`` is exactly one cell of the policy grid."""
    if not isinstance(value, str):
        return False
    match = _BAND.fullmatch(value)
    if match is None:
        return False
    if match.group(3) is not None:
        return int(match.group(3)) == AGE_TOP_CODE
    low, high = int(match.group(1)), int(match.group(2))
    return (
        low % AGE_BAND_WIDTH == 0
        and high == low + AGE_BAND_WIDTH - 1
        and high < AGE_TOP_CODE
    )
