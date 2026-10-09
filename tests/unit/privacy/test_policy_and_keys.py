"""Pure policy helpers, key derivation and configuration of reference keys."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from sqlglot import exp

from retail_analytics.adapters.sql_compiler import (
    DerivationUnavailable,
    KeyedDerivations,
    ReferenceKey,
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.contracts.query_compiler import (
    ParameterType,
    QueryParameter,
)
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.query import build_query_compilers
from retail_analytics.domain.privacy import (
    age_band_label,
    is_age_band,
    is_reference,
    reference_message,
)
from tests.unit.privacy.support import KEYRING, MASTER_KEY


@pytest.mark.parametrize(
    ("age", "band"),
    [(0, "0-4"), (4, "0-4"), (27, "25-29"), (29, "25-29"), (30, "30-34"),
     (89, "85-89"), (90, "90+"), (120, "90+"), (None, None)],
)  # fmt: skip
def test_age_band_grid(age: int | None, band: str | None) -> None:
    assert age_band_label(age) == band
    if band is not None:
        assert is_age_band(band)


@pytest.mark.parametrize(
    "label", ["27", "25-28", "26-30", "25-30", "90-94", "85+", "95+", "", "25 - 29"]
)
def test_off_grid_labels_are_not_bands(label: str) -> None:
    assert not is_age_band(label)


def test_negative_age_is_invalid() -> None:
    with pytest.raises(ValueError):
        age_band_label(-1)


def test_reference_shapes() -> None:
    assert is_reference("cus_" + "0" * 24, "customer_ref")
    assert not is_reference("cus_" + "0" * 24, "order_ref")
    assert not is_reference("cus_" + "0" * 23)
    assert not is_reference("CUS_" + "0" * 24)
    assert not is_reference(10)
    with pytest.raises(ValueError):
        reference_message("email", "x")


def test_reference_is_hmac_of_kind_and_raw_key_under_executive_key() -> None:
    executive_key = hmac.new(
        MASTER_KEY,
        b"retail-analytics/opaque-reference/v1/executive:demo-a",
        hashlib.sha256,
    ).digest()
    expected = hmac.new(executive_key, b"customer_ref:10", hashlib.sha256)
    assert KEYRING.for_executive("demo-a").reference("customer_ref", 10) == (
        "cus_" + expected.hexdigest()[:24]
    )


def test_executive_keys_are_independent() -> None:
    a = KEYRING.for_executive("demo-a").reference("customer_ref", 10)
    b = KEYRING.for_executive("demo-b").reference("customer_ref", 10)
    assert a != b
    with pytest.raises(ValueError):
        KEYRING.for_executive("")


def test_key_objects_never_print_material() -> None:
    key = KEYRING.for_executive("demo-a")
    inner, outer = key.pads()
    for text in (repr(KEYRING), repr(key), str(key)):
        assert MASTER_KEY.decode() not in text
        assert inner not in text and outer not in text
    with pytest.raises(ValueError):
        ReferenceKeyring(b"too-short")
    with pytest.raises(ValueError):
        ReferenceKey(b"x" * 16)


def test_derivation_parameters_are_secret_trusted_policy_parameters() -> None:
    params = KeyedDerivations(KEYRING.for_executive("demo-a")).parameters()
    assert [(p.name, p.trusted, p.secret) for p in params] == [
        ("_policy_ref_inner", True, True),
        ("_policy_ref_outer", True, True),
    ]
    with pytest.raises(ValueError):
        QueryParameter("x", ParameterType.STRING, "v", secret=True)


def test_unknown_reference_kind_fails_closed() -> None:
    derivations = KeyedDerivations(KEYRING.for_executive("demo-a"))
    with pytest.raises(DerivationUnavailable):
        derivations.opaque_reference("email_ref", exp.column("id"))
    with pytest.raises(DerivationUnavailable):
        KeyedDerivations(None).opaque_reference("customer_ref", exp.column("id"))


def test_compilers_require_an_executive() -> None:
    with pytest.raises(ValueError):
        ScopedSqlglotCompilers("p-1.d", KEYRING).for_executive("")


def test_reference_key_setting_is_secret_and_long_enough() -> None:
    key = "r" * 40
    settings = load_backend_settings(
        environ={"APP_MODE": "fixture", "REFERENCE_KEY": key}, env_file=None
    )
    assert settings.reference_key is not None
    assert key not in repr(settings)
    assert settings.redacted_summary()["REFERENCE_KEY"] == "<set>"
    assert build_query_compilers(settings).references_enabled
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"APP_MODE": "fixture", "REFERENCE_KEY": "short"}, env_file=None
        )
    assert "REFERENCE_KEY must be at least 32 bytes" in str(caught.value)
    assert "short" not in str(caught.value)


def test_without_reference_key_references_are_disabled() -> None:
    settings = load_backend_settings(environ={"APP_MODE": "fixture"}, env_file=None)
    assert not build_query_compilers(settings).references_enabled
