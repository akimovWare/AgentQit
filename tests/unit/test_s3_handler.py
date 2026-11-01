from __future__ import annotations

import base64
import hashlib

import pytest

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.s3 import S3OperationHandler
from qa_runtime.models import ExecutionContext, OperationDefinition, Profile
from qa_runtime.ports.s3 import S3Result
from qa_runtime.security import SecretRedactor

pytestmark = pytest.mark.anyio


class FakeS3:
    def __init__(self, result=None):
        self.result = result or S3Result(200, {"Content-Type": "application/octet-stream"}, b"payload")
        self.calls = []
        self.closed = False

    async def send(self, request, context):
        self.calls.append((request, context))
        return self.result

    async def aclose(self):
        self.closed = True


def profile():
    return Profile.model_validate(
        {
            "environment": "qa",
            "s3": {
                "store": {
                    "endpoint": "https://storage.example",
                    "region": "r1",
                    "accessKeyRef": "access",
                    "secretKeyRef": "secret",
                    "buckets": {"files": "files-bucket", "source": "source-bucket"},
                }
            },
        }
    )


def context():
    return ExecutionContext(run_id="test", environment="qa", trace_id="trace", traceparent="00-trace-span-01")


def operation(action="get-object", **config):
    parameters = {}
    values = {"storage": "store", "action": action}
    if action != "list-buckets":
        values["bucketRef"] = "files"
    if action.endswith("object"):
        values["keyParameter"] = "key"
        parameters["key"] = {"type": "string", "required": True}
    if action == "put-object":
        values["bodyParameter"] = "data"
        parameters["data"] = {"type": "string", "required": True}
    if action == "copy-object":
        values["sourceBucketRef"] = "source"
        values["sourceKeyParameter"] = "source-key"
        parameters["source-key"] = {"type": "string", "required": True}
    values.update(config)
    return OperationDefinition.model_validate(
        {
            "id": "file-action",
            "kind": "s3",
            "mutating": action in {"put-object", "delete-object", "copy-object"},
            "parameters": parameters,
            "config": values,
        }
    )


def bind(selected, config_field, name, *, value_type="string", required=False):
    document = selected.model_dump(by_alias=True)
    document["config"][config_field] = name
    document["parameters"][name] = {"type": value_type, "required": required}
    return OperationDefinition.model_validate(document)


async def test_compile_is_offline_and_get_returns_raw_content_metadata():
    transport = FakeS3(
        S3Result(
            206,
            {"Content-Length": "7", "ETag": '"etag"', "Last-Modified": "date", "x-amz-version-id": "v1"},
            b"payload",
        )
    )
    handler = S3OperationHandler(transport)
    selected = bind(operation(), "rangeParameter", "range")
    selected = bind(selected, "versionIdParameter", "version")
    plan = handler.compile(selected, profile())
    assert transport.calls == []
    result = await handler.execute(plan, {"key": "folder/file", "range": "bytes=0-6", "version": "v1"}, context())
    request, execution = transport.calls[0]
    assert request.bucket == "files-bucket"
    assert request.key == "folder/file"
    assert request.byte_range == "bytes=0-6"
    assert request.version_id == "v1"
    assert execution == context()
    assert result["status_code"] == 206
    assert result["found"] is True
    assert result["body"] == "payload"
    assert result["contentLength"] == 7
    assert result["etag"] == '"etag"'
    assert result["lastModified"] == "date"
    assert result["versionId"] == "v1"
    assert result["sha256"] == hashlib.sha256(b"payload").hexdigest()
    await handler.aclose()
    assert transport.closed


@pytest.mark.parametrize("status,found", [(200, True), (403, None), (404, False), (500, None), (304, None)])
async def test_protocol_statuses_are_facts_not_assumptions_or_assertions(status, found):
    handler = S3OperationHandler(FakeS3(S3Result(status, {}, b"<Error>access</Error>")))
    result = await handler.execute(handler.compile(operation(bodyEncoding="json"), profile()), {"key": "k"}, context())
    assert result["status_code"] == status
    assert result["found"] is found
    if status == 200:
        assert result["decode_error"]["code"] == "S3_BODY_DECODE_FAILED"
    else:
        assert result["body"] == "<Error>access</Error>"
        assert "decode_error" not in result


@pytest.mark.parametrize(
    "encoding,body,expected",
    [
        ("json", b'{"items":[1]}', {"items": [1]}),
        ("base64", b"\x00\xff", {"encoding": "base64", "data": "AP8="}),
        ("none", b"\x00\xff", None),
    ],
)
async def test_content_encodings_are_generic(encoding, body, expected):
    handler = S3OperationHandler(FakeS3(S3Result(200, {"content-encoding": "gzip"}, body)))
    result = await handler.execute(
        handler.compile(operation(bodyEncoding=encoding), profile()), {"key": "k"}, context()
    )
    assert result["body"] == expected
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert result["contentEncoding"] == "gzip"


@pytest.mark.parametrize("encoding,body", [("utf8", b"\xff"), ("json", b"not JSON")])
async def test_decode_failure_preserves_received_bytes(encoding, body):
    handler = S3OperationHandler(FakeS3(S3Result(200, {}, body)))
    result = await handler.execute(
        handler.compile(operation(bodyEncoding=encoding), profile()), {"key": "k"}, context()
    )
    assert result["status_code"] == 200
    assert result["body"] == {"encoding": "base64", "data": base64.b64encode(body).decode()}
    assert result["decode_error"]["code"] == "S3_BODY_DECODE_FAILED"


async def test_redaction_applies_to_binary_and_json_without_changing_original_hash():
    redactor = SecretRedactor()
    redactor.register("synthetic-secret")
    body = b"\xffsynthetic-secret\x00"
    handler = S3OperationHandler(FakeS3(S3Result(200, {"Authorization": "auth-secret"}, body)), redactor=redactor)
    result = await handler.execute(
        handler.compile(operation(bodyEncoding="base64"), profile()), {"key": "k"}, context()
    )
    assert base64.b64decode(result["body"]["data"]) == b"\xff***\x00"
    assert result["body"]["redacted"] is True
    assert result["headers"]["authorization"] == "***"
    assert result["sha256"] == hashlib.sha256(body).hexdigest()


async def test_list_objects_parses_wire_xml_and_returns_explicit_cursor():
    xml = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <EncodingType>url</EncodingType><IsTruncated>true</IsTruncated><NextContinuationToken>next+=</NextContinuationToken>
      <Contents><Key>a%20%2B%25</Key><Size>7</Size><ETag>etag</ETag><StorageClass>STANDARD</StorageClass></Contents>
      <CommonPrefixes><Prefix>a%2F</Prefix></CommonPrefixes></ListBucketResult>"""
    transport = FakeS3(S3Result(200, {}, xml))
    handler = S3OperationHandler(transport)
    selected = bind(operation("list-objects", maxResults=10), "prefixParameter", "prefix")
    selected = bind(selected, "continuationTokenParameter", "cursor")
    selected = bind(selected, "maxResultsParameter", "limit", value_type="integer")
    result = await handler.execute(
        handler.compile(selected, profile()), {"prefix": "a", "cursor": "prior", "limit": 2}, context()
    )
    assert result["objects"] == [{"Key": "a +%", "Size": 7, "ETag": "etag", "StorageClass": "STANDARD"}]
    assert result["commonPrefixes"] == ["a/"]
    assert result["nextContinuationToken"] == "next+="
    assert not result["complete"]
    assert transport.calls[0][0].continuation_token == "prior"
    assert transport.calls[0][0].max_results == 2
    assert len(transport.calls) == 1


async def test_list_buckets_parses_paginated_protocol_and_bounds_output():
    xml = b"""<ListAllMyBucketsResult><Buckets>
      <Bucket><Name>a</Name><CreationDate>d</CreationDate><BucketRegion>r</BucketRegion></Bucket>
      <Bucket><Name>b</Name></Bucket></Buckets><ContinuationToken>more</ContinuationToken></ListAllMyBucketsResult>"""
    handler = S3OperationHandler(FakeS3(S3Result(200, {}, xml)))
    result = await handler.execute(handler.compile(operation("list-buckets", maxResults=1), profile()), {}, context())
    assert result["buckets"] == [{"Name": "a", "CreationDate": "d", "BucketRegion": "r"}]
    assert result["localTruncated"]
    assert result["nextContinuationToken"] == "more"
    assert not result["complete"]


@pytest.mark.parametrize(
    "body",
    [b"invalid", b"<Unexpected/>", b'<!DOCTYPE x [<!ENTITY y "expand">]><ListBucketResult>&y;</ListBucketResult>'],
)
async def test_malformed_or_unsafe_list_xml_preserves_facts_and_raw_bytes(body):
    handler = S3OperationHandler(FakeS3(S3Result(200, {}, body)))
    result = await handler.execute(handler.compile(operation("list-objects"), profile()), {}, context())
    assert result["status_code"] == 200
    assert base64.b64decode(result["body"]["data"]) == body
    assert result["decode_error"]["code"] == "S3_RESPONSE_INVALID"
    assert "complete" not in result


@pytest.mark.parametrize("action", ["head-bucket", "head-object", "delete-object"])
async def test_metadata_only_operations(action):
    handler = S3OperationHandler(FakeS3(S3Result(204, {"x-amz-version-id": "v2"})))
    result = await handler.execute(handler.compile(operation(action), profile()), {"key": "k"}, context())
    assert result["status_code"] == 204
    assert result["versionId"] == "v2"
    assert "body" not in result


@pytest.mark.parametrize(
    "encoding,data,expected",
    [
        ("utf8", "текст", "текст".encode()),
        ("base64", "AP8=", b"\x00\xff"),
        ("json", {"items": [1]}, b'{"items":[1]}'),
    ],
)
async def test_put_encodes_supplied_data_only(encoding, data, expected):
    transport = FakeS3(S3Result(200, {"etag": "uploaded"}))
    handler = S3OperationHandler(transport)
    selected = operation("put-object", requestBodyEncoding=encoding)
    if encoding == "json":
        document = selected.model_dump(by_alias=True)
        document["parameters"]["data"]["type"] = "object"
        selected = OperationDefinition.model_validate(document)
    result = await handler.execute(handler.compile(selected, profile()), {"key": "k", "data": data}, context())
    assert transport.calls[0][0].body == expected
    assert result["etag"] == "uploaded"


@pytest.mark.parametrize("root", ["CopyObjectResult", "Error"])
async def test_copy_preserves_s3_embedded_error_even_in_http200(root):
    body = f"<{root}><Code>code</Code><ETag>etag</ETag></{root}>".encode()
    transport = FakeS3(S3Result(200, {}, body))
    handler = S3OperationHandler(transport)
    result = await handler.execute(
        handler.compile(operation("copy-object"), profile()), {"key": "dest", "source-key": "src"}, context()
    )
    assert result["responseType"] == root
    assert result["copyResult"] == {"Code": "code", "ETag": "etag"}
    assert transport.calls[0][0].source_bucket == "source-bucket"


@pytest.mark.parametrize(
    "action",
    [
        "list-buckets",
        "head-bucket",
        "list-objects",
        "head-object",
        "get-object",
        "put-object",
        "delete-object",
        "copy-object",
    ],
)
async def test_compile_rejects_incorrect_mutation_marker_before_io(action):
    transport = FakeS3()
    handler = S3OperationHandler(transport)
    selected = operation(action)
    selected.mutating = not selected.mutating
    with pytest.raises(RuntimeFailure) as raised:
        handler.compile(selected, profile())
    assert raised.value.code == "REGISTRY_INVALID"
    assert transport.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"storage": "absent"},
        {"bucketRef": "absent"},
        {"keyParameter": None},
        {"keyParameter": "undeclared"},
        {"sourceBucketRef": "source"},
        {"bodyParameter": "data"},
        {"maxResults": 0},
        {"timeoutSeconds": float("nan")},
        {"timeoutSeconds": float("inf")},
        {"expectedStatus": [200]},
        {"maxBytes": True},
    ],
)
async def test_invalid_config_is_rejected_offline(change):
    transport = FakeS3()
    with pytest.raises(RuntimeFailure) as raised:
        S3OperationHandler(transport).compile(operation(**change), profile())
    assert raised.value.exit_code == ExitCode.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.parametrize(
    "field,parameter", [("rangeParameter", "range"), ("bodyParameter", "body"), ("versionIdParameter", "version")]
)
async def test_unsupported_action_bindings_are_rejected(field, parameter):
    selected = bind(operation("list-buckets"), field, parameter)
    with pytest.raises(RuntimeFailure, match="unsupported"):
        S3OperationHandler(FakeS3()).compile(selected, profile())


@pytest.mark.parametrize("key", ["", "ю" * 513])
async def test_invalid_key_is_rejected_before_io(key):
    transport = FakeS3()
    handler = S3OperationHandler(transport)
    with pytest.raises(RuntimeFailure) as raised:
        await handler.execute(handler.compile(operation(), profile()), {"key": key}, context())
    assert raised.value.code == "S3_INPUT_INVALID"
    assert not transport.calls


@pytest.mark.parametrize("byte_range", ["bytes=2-1", "bytes=-0", "bytes=0-1,2-3", "bytes=", "bytes=a-"])
async def test_invalid_range_is_rejected_before_io(byte_range):
    transport = FakeS3()
    handler = S3OperationHandler(transport)
    selected = bind(operation(), "rangeParameter", "range")
    with pytest.raises(RuntimeFailure):
        await handler.execute(handler.compile(selected, profile()), {"key": "k", "range": byte_range}, context())
    assert not transport.calls


@pytest.mark.parametrize("limit", [0, 101, True, "2"])
async def test_runtime_page_limit_is_bounded_by_registry(limit):
    transport = FakeS3()
    handler = S3OperationHandler(transport)
    selected = bind(operation("list-objects"), "maxResultsParameter", "limit", value_type="integer")
    with pytest.raises(RuntimeFailure):
        await handler.execute(handler.compile(selected, profile()), {"limit": limit}, context())
    assert not transport.calls


async def test_binary_body_invalid_input_and_oversize_do_not_send():
    transport = FakeS3()
    handler = S3OperationHandler(transport)
    plan = handler.compile(operation("put-object", requestBodyEncoding="base64", maxBytes=1), profile())
    for value in ("not base64", "AQI="):
        with pytest.raises(RuntimeFailure):
            await handler.execute(plan, {"key": "k", "data": value}, context())
    assert not transport.calls


async def test_compile_rejects_missing_tls_provider_without_io():
    selected_profile = profile()
    selected_profile.s3["store"].tls_ref = "private-ca"
    transport = FakeS3()
    with pytest.raises(RuntimeFailure) as raised:
        S3OperationHandler(transport).compile(operation(), selected_profile)
    assert raised.value.code == "TLS_PROVIDER_NOT_FOUND"
    assert not transport.calls


async def test_compile_checks_unknown_tls_reference_without_io():
    class MissingTls:
        def ssl_context(self, reference):
            raise RuntimeFailure(ExitCode.ENVIRONMENT_UNAVAILABLE, "TLS_NOT_CONFIGURED", "TLS reference is unknown")

    selected_profile = profile()
    selected_profile.s3["store"].tls_ref = "unknown-ca"
    transport = FakeS3()
    with pytest.raises(RuntimeFailure) as raised:
        S3OperationHandler(transport, tls_provider=MissingTls()).compile(operation(), selected_profile)
    assert raised.value.code == "TLS_NOT_CONFIGURED"
    assert not transport.calls


async def test_compile_checks_valid_tls_reference_without_io():
    class FakeTls:
        def __init__(self):
            self.references = []

        def ssl_context(self, reference):
            self.references.append(reference)
            return None

    selected_profile = profile()
    selected_profile.s3["store"].tls_ref = "private-ca"
    tls = FakeTls()
    transport = FakeS3()
    S3OperationHandler(transport, tls_provider=tls).compile(operation(), selected_profile)
    assert tls.references == ["private-ca"]
    assert not transport.calls


@pytest.mark.parametrize(
    "encoding,wire_key,expected",
    [
        ("url", "folder%2B+space/%D0%A4%D0%B0%D0%B9%D0%BB+%25.bin", "folder+ space/Файл %.bin"),
        ("url", "folder%2B%20space/%D0%A4%D0%B0%D0%B9%D0%BB%20%25.bin", "folder+ space/Файл %.bin"),
        ("url", "literal%2B%252B", "literal+%2B"),
        (None, "literal+ space/%2B", "literal+ space/%2B"),
    ],
)
async def test_list_url_decoding_matches_sdk_preserves_literal_plus_and_opaque_cursor(encoding, wire_key, expected):
    marker = f"<EncodingType>{encoding}</EncodingType>" if encoding is not None else ""
    xml = (
        f"<ListBucketResult>{marker}<IsTruncated>true</IsTruncated>"
        f"<NextContinuationToken>opaque+%2B==</NextContinuationToken>"
        f"<Contents><Key>{wire_key}</Key><Size>1</Size></Contents>"
        f"<CommonPrefixes><Prefix>{wire_key}</Prefix></CommonPrefixes></ListBucketResult>"
    ).encode()
    handler = S3OperationHandler(FakeS3(S3Result(200, {}, xml)))
    result = await handler.execute(handler.compile(operation("list-objects"), profile()), {}, context())
    assert result["objects"][0]["Key"] == expected
    assert result["commonPrefixes"] == [expected]
    assert result["nextContinuationToken"] == "opaque+%2B=="
