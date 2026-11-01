from __future__ import annotations

import ssl
from typing import Protocol


class TlsProvider(Protocol):
    def ssl_context(self, reference: str | None) -> ssl.SSLContext: ...
