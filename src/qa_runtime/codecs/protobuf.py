from __future__ import annotations

from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from google.protobuf.message import DecodeError, Message


class DescriptorSetProtobufDecoder:
    def __init__(self, descriptor_set: bytes, message_type: str) -> None:
        self._message_class = _resolve_message_class(descriptor_set, message_type)

    @classmethod
    def from_file(cls, path: Path, message_type: str) -> DescriptorSetProtobufDecoder:
        return cls(_read_descriptor_set(path), message_type)

    def decode(self, value: bytes) -> Any:
        message = self._message_class()
        try:
            message.ParseFromString(value)
        except DecodeError as error:
            raise ValueError("protobuf payload does not match the configured message type") from error
        return json_format.MessageToDict(
            message,
            preserving_proto_field_name=True,
            use_integers_for_enums=False,
            always_print_fields_with_no_presence=True,
        )


class DescriptorSetProtobufEncoder:
    def __init__(self, descriptor_set: bytes, message_type: str) -> None:
        self._message_class = _resolve_message_class(descriptor_set, message_type)

    @classmethod
    def from_file(cls, path: Path, message_type: str) -> DescriptorSetProtobufEncoder:
        return cls(_read_descriptor_set(path), message_type)

    def encode(self, value: Any) -> bytes:
        message = self._message_class()
        try:
            json_format.ParseDict(value, message)
        except Exception as error:
            raise ValueError("protobuf value does not match the configured message type") from error
        return message.SerializeToString()


def _read_descriptor_set(path: Path) -> bytes:
    try:
        descriptor_set = path.read_bytes()
    except OSError as error:
        raise ValueError(f"protobuf descriptor set could not be read: {path}") from error
    return descriptor_set


def _resolve_message_class(descriptor_set: bytes, message_type: str) -> type[Message]:
    files = descriptor_pb2.FileDescriptorSet()
    try:
        files.ParseFromString(descriptor_set)
    except DecodeError as error:
        raise ValueError("protobuf descriptor set is invalid") from error
    if not files.file:
        raise ValueError("protobuf descriptor set contains no files")

    pool = descriptor_pool.DescriptorPool()
    pending = {file.name: file for file in files.file}
    while pending:
        added: list[str] = []
        for name, file in pending.items():
            try:
                pool.AddSerializedFile(file.SerializeToString())
            except TypeError:
                continue
            added.append(name)
        for name in added:
            del pending[name]
        if not added:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"protobuf descriptor dependencies could not be resolved: {unresolved}")

    normalized_message_type = message_type.removeprefix(".").strip()
    if not normalized_message_type:
        raise ValueError("protobuf message type must not be empty")
    try:
        descriptor = pool.FindMessageTypeByName(normalized_message_type)
    except KeyError as error:
        raise ValueError(f"protobuf message type '{normalized_message_type}' was not found") from error
    return message_factory.GetMessageClass(descriptor)
