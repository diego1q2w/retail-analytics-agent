"""Trusted privacy derivations: keyed opaque references and grid age bands.

References are computed inside the warehouse, in the trusted binding, as
HMAC-SHA256 (RFC 2104) over ``<kind>:<raw key>``. BigQuery has no HMAC
function, so the construction is spelled out with ``SHA256`` and the two
padded keys, which are precomputed here and sent as secret, trusted query
parameters. Raw keys therefore never leave the warehouse and the result only
ever holds the truncated digest.

Key hierarchy: the configured master key never leaves the application. Each
executive gets ``HMAC(master, label || executive_id)`` as their reference key,
and only that derived key reaches a query job. Anyone able to read the
project's job metadata (query parameters are recorded there) could compute
that one executive's references from raw keys, but not anyone else's and not
reverse a reference; the master key is never exposed. Rotating the master key
retires every reference.

Age bands are derived from the raw age column inside the binding following
:mod:`retail_analytics.domain.privacy`; raw age is never projected.
"""

from __future__ import annotations

import hashlib
import hmac

from sqlglot import exp

from retail_analytics.adapters.sql_compiler.bindings import (
    DerivationUnavailable,
    TrustedDerivations,
)
from retail_analytics.adapters.sql_compiler.compiler import (
    CompilerLimits,
    SqlglotQueryCompiler,
)
from retail_analytics.application.contracts.query_compiler import (
    ParameterType,
    QueryParameter,
)
from retail_analytics.application.ports.query_compiler import QueryCompiler
from retail_analytics.domain.privacy import (
    AGE_BAND_WIDTH,
    AGE_TOP_CODE,
    REFERENCE_HEX_CHARS,
    REFERENCE_PREFIXES,
    format_reference,
    reference_message,
)

INNER_PAD_PARAMETER = "_policy_ref_inner"
OUTER_PAD_PARAMETER = "_policy_ref_outer"
MINIMUM_MASTER_KEY_BYTES = 32
_BLOCK = 64  # SHA-256 block size
_SCOPE_LABEL = b"retail-analytics/opaque-reference/v1/executive:"


class ReferenceKey:
    """One executive's reference key. Holds secret material; never printed."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) != hashlib.sha256().digest_size:
            raise ValueError("reference key must be a SHA-256 digest")
        self._key = key

    def __repr__(self) -> str:
        return "ReferenceKey(<redacted>)"

    def reference(self, kind: str, raw_key: int | str) -> str:
        """The reference the warehouse computes for ``raw_key`` (trusted use only).

        For tests and trusted tooling; application flows never hold raw keys.
        """
        digest = hmac.new(
            self._key, reference_message(kind, str(raw_key)), hashlib.sha256
        )
        return format_reference(kind, digest.hexdigest())

    def pads(self) -> tuple[str, str]:
        """HMAC inner/outer padded keys as hex (RFC 2104 ``K^ipad``, ``K^opad``)."""
        block = self._key.ljust(_BLOCK, b"\0")
        inner = bytes(b ^ 0x36 for b in block)
        outer = bytes(b ^ 0x5C for b in block)
        return inner.hex(), outer.hex()


class ReferenceKeyring:
    """Derives per-executive reference keys from the master key."""

    __slots__ = ("_master",)

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) < MINIMUM_MASTER_KEY_BYTES:
            raise ValueError("reference master key must be at least 32 bytes")
        self._master = master_key

    def __repr__(self) -> str:
        return "ReferenceKeyring(<redacted>)"

    def for_executive(self, executive_id: str) -> ReferenceKey:
        if not executive_id:
            raise ValueError("executive_id is required")
        label = _SCOPE_LABEL + executive_id.encode()
        return ReferenceKey(hmac.new(self._master, label, hashlib.sha256).digest())


class KeyedDerivations:
    """``TrustedDerivations`` for one executive.

    Without a reference key, references stay unavailable (fail closed) while
    age bands, which need no key, keep working.
    """

    def __init__(self, reference_key: ReferenceKey | None) -> None:
        self._key = reference_key

    def opaque_reference(self, kind: str, raw_key: exp.Expr) -> exp.Expr:
        if self._key is None or kind not in REFERENCE_PREFIXES:
            raise DerivationUnavailable(kind)
        message = exp.cast(
            exp.func(
                "CONCAT",
                exp.Literal.string(f"{kind}:"),
                exp.cast(raw_key.copy(), exp.DataType.Type.TEXT),
            ),
            exp.DataType.Type.VARBINARY,
        )
        inner = _sha256(_concat_bytes(_pad(INNER_PAD_PARAMETER), message))
        outer = _sha256(_concat_bytes(_pad(OUTER_PAD_PARAMETER), inner))
        digest = exp.Substring(
            this=exp.LowerHex(this=outer),
            start=exp.Literal.number(1),
            length=exp.Literal.number(REFERENCE_HEX_CHARS),
        )
        return exp.func(
            "CONCAT", exp.Literal.string(f"{REFERENCE_PREFIXES[kind]}_"), digest
        )

    def age_band(self, raw_age: exp.Expr) -> exp.Expr:
        low = exp.Mul(
            this=exp.IntDiv(
                this=raw_age.copy(), expression=exp.Literal.number(AGE_BAND_WIDTH)
            ),
            expression=exp.Literal.number(AGE_BAND_WIDTH),
        )
        high = exp.Add(
            this=low.copy(), expression=exp.Literal.number(AGE_BAND_WIDTH - 1)
        )
        grid = exp.func(
            "CONCAT",
            exp.cast(low, exp.DataType.Type.TEXT),
            exp.Literal.string("-"),
            exp.cast(high, exp.DataType.Type.TEXT),
        )
        # NULL and negative ages have no band rather than a misleading one.
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.Or(
                        this=exp.Is(this=raw_age.copy(), expression=exp.Null()),
                        expression=exp.LT(
                            this=raw_age.copy(), expression=exp.Literal.number(0)
                        ),
                    ),
                    true=exp.Null(),
                ),
                exp.If(
                    this=exp.GTE(
                        this=raw_age.copy(),
                        expression=exp.Literal.number(AGE_TOP_CODE),
                    ),
                    true=exp.Literal.string(f"{AGE_TOP_CODE}+"),
                ),
            ],
            default=grid,
        )

    def parameters(self) -> tuple[QueryParameter, ...]:
        if self._key is None:
            return ()
        inner, outer = self._key.pads()
        return (
            QueryParameter(
                INNER_PAD_PARAMETER,
                ParameterType.STRING,
                inner,
                trusted=True,
                secret=True,
            ),
            QueryParameter(
                OUTER_PAD_PARAMETER,
                ParameterType.STRING,
                outer,
                trusted=True,
                secret=True,
            ),
        )


class ScopedSqlglotCompilers:
    """``ScopedQueryCompilers`` over the SQLGlot compiler.

    Building a compiler is cheap (it parses four trusted templates), so one is
    built per request instead of caching key material per executive.
    """

    def __init__(
        self,
        dataset: str,
        keyring: ReferenceKeyring | None,
        *,
        limits: CompilerLimits | None = None,
    ) -> None:
        self._dataset = dataset
        self._keyring = keyring
        self._limits = limits

    @property
    def references_enabled(self) -> bool:
        return self._keyring is not None

    def for_executive(self, executive_id: str) -> QueryCompiler:
        return SqlglotQueryCompiler(
            self._dataset,
            derivations=self.derivations_for(executive_id),
            limits=self._limits,
        )

    def derivations_for(self, executive_id: str) -> TrustedDerivations:
        if not executive_id:
            raise ValueError("executive_id is required")
        key = (
            None if self._keyring is None else self._keyring.for_executive(executive_id)
        )
        return KeyedDerivations(key)


def _pad(name: str) -> exp.Expr:
    return exp.Unhex(this=exp.Parameter(this=exp.var(name)))


def _sha256(value: exp.Expr) -> exp.Expr:
    return exp.SHA2Digest(this=value, length=exp.Literal.number(256))


def _concat_bytes(left: exp.Expr, right: exp.Expr) -> exp.Expr:
    return exp.Concat(expressions=[left, right])
