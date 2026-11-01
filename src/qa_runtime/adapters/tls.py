from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    ca_bundle: Path | None = None
    client_certificate: Path | None = None
    client_key: Path | None = None


class ConfiguredTlsProvider:
    def __init__(self, materials: dict[str, TlsMaterial] | None = None) -> None:
        self._materials = materials or {}
        self._contexts: dict[str | None, ssl.SSLContext] = {}

    def ssl_context(self, reference: str | None) -> ssl.SSLContext:
        if reference in self._contexts:
            return self._contexts[reference]
        try:
            context = self._create_context(reference)
        except OSError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_CONFIG_INVALID",
                "TLS trust material could not be loaded; check the CA bundle and client certificate files",
                source="tls",
            ) from error
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self._contexts[reference] = context
        return context

    def _create_context(self, reference: str | None) -> ssl.SSLContext:
        if reference is None or reference == "system":
            context = ssl.create_default_context()
        else:
            material = self._materials.get(reference)
            if material is None:
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "TLS_REF_NOT_FOUND",
                    f"TLS provider has no configuration for tlsRef '{reference}'",
                    source="tls",
                )
            context = ssl.create_default_context()
            if material.ca_bundle is not None:
                context.load_verify_locations(cafile=str(material.ca_bundle))
            if (material.client_certificate is None) != (material.client_key is None):
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "TLS_CONFIG_INVALID",
                    "client certificate and private key must be configured together",
                    source="tls",
                )
            if material.client_certificate is not None and material.client_key is not None:
                context.load_cert_chain(
                    certfile=str(material.client_certificate),
                    keyfile=str(material.client_key),
                )
        return context
