from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from qa_runtime.codecs.protobuf import DescriptorSetProtobufDecoder, DescriptorSetProtobufEncoder


def descriptor_set_bytes() -> bytes:
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    file = descriptor_set.file.add()
    file.name = "events.proto"
    file.package = "example.events"
    file.syntax = "proto3"

    status = file.enum_type.add()
    status.name = "Status"
    unknown = status.value.add()
    unknown.name = "STATUS_UNKNOWN"
    unknown.number = 0
    done = status.value.add()
    done.name = "STATUS_DONE"
    done.number = 1

    event = file.message_type.add()
    event.name = "Event"
    event_id = event.field.add()
    event_id.name = "event_id"
    event_id.number = 1
    event_id.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    event_id.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    event_status = event.field.add()
    event_status.name = "status"
    event_status.number = 2
    event_status.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    event_status.type = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
    event_status.type_name = ".example.events.Status"
    return descriptor_set.SerializeToString()


def test_descriptor_set_protobuf_decoder_returns_stable_json_projection(tmp_path: Path) -> None:
    descriptor_path = tmp_path / "events.pb"
    descriptor_path.write_bytes(descriptor_set_bytes())
    decoder = DescriptorSetProtobufDecoder.from_file(descriptor_path, ".example.events.Event")

    decoded = decoder.decode(b"\x0a\x07event-1\x10\x01")

    assert decoded == {"event_id": "event-1", "status": "STATUS_DONE"}


def test_descriptor_set_protobuf_decoder_preserves_default_scalar_and_enum_values() -> None:
    decoder = DescriptorSetProtobufDecoder(descriptor_set_bytes(), "example.events.Event")

    decoded = decoder.decode(b"")

    assert decoded == {"event_id": "", "status": "STATUS_UNKNOWN"}


@pytest.mark.parametrize(
    ("descriptor_set", "message_type", "message"),
    [
        (b"", "example.events.Event", "contains no files"),
        (descriptor_set_bytes(), "example.events.Missing", "was not found"),
        (descriptor_set_bytes(), " ", "must not be empty"),
    ],
)
def test_descriptor_set_protobuf_decoder_rejects_invalid_configuration(
    descriptor_set: bytes,
    message_type: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DescriptorSetProtobufDecoder(descriptor_set, message_type)


def test_descriptor_set_protobuf_encoder_roundtrips_through_decoder(tmp_path: Path) -> None:
    descriptor_path = tmp_path / "events.pb"
    descriptor_path.write_bytes(descriptor_set_bytes())
    encoder = DescriptorSetProtobufEncoder.from_file(descriptor_path, ".example.events.Event")
    decoder = DescriptorSetProtobufDecoder.from_file(descriptor_path, ".example.events.Event")

    encoded = encoder.encode({"event_id": "event-1", "status": "STATUS_DONE"})

    assert decoder.decode(encoded) == {"event_id": "event-1", "status": "STATUS_DONE"}


def test_descriptor_set_protobuf_encoder_accepts_enum_by_number(tmp_path: Path) -> None:
    descriptor_path = tmp_path / "events.pb"
    descriptor_path.write_bytes(descriptor_set_bytes())
    encoder = DescriptorSetProtobufEncoder.from_file(descriptor_path, ".example.events.Event")
    decoder = DescriptorSetProtobufDecoder.from_file(descriptor_path, ".example.events.Event")

    encoded = encoder.encode({"event_id": "event-2", "status": 1})

    assert decoder.decode(encoded) == {"event_id": "event-2", "status": "STATUS_DONE"}


@pytest.mark.parametrize(
    "payload",
    [
        {"event_id": "event-1", "status": "STATUS_MISSING"},
        {"unknown_field": "x"},
        {"event_id": 123},
    ],
)
def test_descriptor_set_protobuf_encoder_rejects_invalid_value(tmp_path: Path, payload: dict) -> None:
    descriptor_path = tmp_path / "events.pb"
    descriptor_path.write_bytes(descriptor_set_bytes())
    encoder = DescriptorSetProtobufEncoder.from_file(descriptor_path, ".example.events.Event")

    with pytest.raises(ValueError, match="does not match the configured message type"):
        encoder.encode(payload)
