import ssl

import pytest

from qa_runtime.adapters.tls import ConfiguredTlsProvider, TlsMaterial
from qa_runtime.errors import RuntimeFailure


def test_system_tls_context_requires_certificate_validation_and_hostname_check() -> None:
    context = ConfiguredTlsProvider().ssl_context("system")

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_unknown_tls_reference_is_rejected() -> None:
    with pytest.raises(RuntimeFailure, match="tlsRef 'missing'") as raised:
        ConfiguredTlsProvider().ssl_context("missing")

    assert raised.value.code == "TLS_REF_NOT_FOUND"


def test_extra_ca_bundle_is_added_to_system_trust(tmp_path) -> None:
    bundle = tmp_path / "extra-ca.pem"
    bundle.write_text("not a certificate", encoding="utf-8")

    with pytest.raises(RuntimeFailure) as raised:
        ConfiguredTlsProvider({"qa": TlsMaterial(ca_bundle=bundle)}).ssl_context("qa")

    assert raised.value.code == "TLS_CONFIG_INVALID"
    assert "not a certificate" not in str(raised.value)
