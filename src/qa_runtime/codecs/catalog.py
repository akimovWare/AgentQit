from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from qa_runtime.codecs.protobuf import DescriptorSetProtobufDecoder, DescriptorSetProtobufEncoder
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import OperationDefinition
from qa_runtime.ports.decoding import ValueDecoder, ValueEncoder
from qa_runtime.registry import OperationRegistry

MAX_DESCRIPTOR_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeCodecs:
    decoders: dict[str, ValueDecoder]
    encoders: dict[str, ValueEncoder]


class _JsonCodec:
    def decode(self, value: bytes) -> Any:
        return json.loads(value)

    def encode(self, value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class _Utf8Codec:
    def decode(self, value: bytes) -> str:
        return value.decode("utf-8")

    def encode(self, value: Any) -> bytes:
        if not isinstance(value, str):
            raise ValueError("UTF-8 codec requires a string")
        return value.encode("utf-8")


class _Base64Codec:
    def decode(self, value: bytes) -> dict[str, str]:
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}

    def encode(self, value: Any) -> bytes:
        if isinstance(value, dict) and value.get("encoding") == "base64":
            value = value.get("data")
        if not isinstance(value, str):
            raise ValueError("base64 codec requires a string or base64 envelope")
        return base64.b64decode(value, validate=True)


def codec_references(operations: tuple[OperationDefinition, ...]) -> set[str]:
    references: set[str] = set()
    for operation in operations:
        for name in ("valueDecoderRef", "valueEncoderRef"):
            reference = operation.config.get(name)
            if isinstance(reference, str) and reference:
                references.add(reference)
        columns = operation.config.get("columnDecoders", {})
        if isinstance(columns, dict):
            references.update(value for value in columns.values() if isinstance(value, str) and value)
    return references


def load_codecs(registry: OperationRegistry, operations: tuple[OperationDefinition, ...]) -> RuntimeCodecs:
    """Load only selected, declarative data formats; never invoke a product handler."""
    definitions = registry.codecs()
    decoders: dict[str, ValueDecoder] = {}
    encoders: dict[str, ValueEncoder] = {}
    for reference in sorted(codec_references(operations)):
        definition = definitions.get(reference)
        if definition is None:
            raise _failure("CODEC_NOT_CONFIGURED", f"selected operation references unknown codec '{reference}'")
        if definition.format == "json":
            codec = _JsonCodec()
        elif definition.format == "utf8":
            codec = _Utf8Codec()
        elif definition.format == "base64":
            codec = _Base64Codec()
        else:
            path = definition.descriptor_path
            assert path is not None and definition.message_type is not None
            try:
                with path.open("rb") as descriptor_file:
                    descriptor = descriptor_file.read(MAX_DESCRIPTOR_BYTES + 1)
                if len(descriptor) > MAX_DESCRIPTOR_BYTES:
                    raise ValueError("descriptor exceeds the configured limit")
                if hashlib.sha256(descriptor).hexdigest() != definition.checksum_sha256:
                    raise ValueError("descriptor checksum mismatch")
                decoders[reference] = DescriptorSetProtobufDecoder(descriptor, definition.message_type)
                encoders[reference] = DescriptorSetProtobufEncoder(descriptor, definition.message_type)
            except (OSError, ValueError) as error:
                raise _failure("CODEC_INVALID", f"declared protobuf codec '{reference}' could not be loaded") from error
            continue
        decoders[reference] = codec
        encoders[reference] = codec
    return RuntimeCodecs(decoders, encoders)


def _failure(code: str, message: str) -> RuntimeFailure:
    return RuntimeFailure(ExitCode.INVALID_INPUT, code, message, source="codecs")
