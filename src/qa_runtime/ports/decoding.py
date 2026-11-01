from __future__ import annotations

from typing import Any, Protocol


class ValueDecoder(Protocol):
    def decode(self, value: bytes) -> Any: ...


class ValueEncoder(Protocol):
    def encode(self, value: Any) -> bytes: ...
