from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote_plus
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import ExecutionContext, OperationDefinition, Profile, S3ServiceConfig
from qa_runtime.ports.operation import ResultPayload
from qa_runtime.ports.s3 import MAX_BYTES, MUTATING_ACTIONS, S3Action, S3Port, S3Request, S3Result
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor

OBJECT_ACTIONS = frozenset({"head-object", "get-object", "put-object", "delete-object", "copy-object"})
LIST_ACTIONS = frozenset({"list-buckets", "list-objects"})
BINDING_ACTIONS = {
    "key_parameter": OBJECT_ACTIONS,
    "prefix_parameter": LIST_ACTIONS,
    "delimiter_parameter": {"list-objects"},
    "continuation_token_parameter": LIST_ACTIONS,
    "max_results_parameter": LIST_ACTIONS,
    "version_id_parameter": {"head-object", "get-object", "delete-object"},
    "range_parameter": {"get-object"},
    "if_match_parameter": {"head-object", "get-object", "put-object", "delete-object"},
    "if_none_match_parameter": {"head-object", "get-object", "put-object"},
    "body_parameter": {"put-object"},
    "content_type_parameter": {"put-object"},
    "source_key_parameter": {"copy-object"},
    "source_version_id_parameter": {"copy-object"},
}


class S3OperationConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)

    storage: str = Field(min_length=1)
    action: S3Action
    bucket_ref: str | None = Field(default=None, alias="bucketRef", min_length=1)
    key_parameter: str | None = Field(default=None, alias="keyParameter", min_length=1)
    prefix_parameter: str | None = Field(default=None, alias="prefixParameter", min_length=1)
    delimiter_parameter: str | None = Field(default=None, alias="delimiterParameter", min_length=1)
    continuation_token_parameter: str | None = Field(default=None, alias="continuationTokenParameter", min_length=1)
    max_results_parameter: str | None = Field(default=None, alias="maxResultsParameter", min_length=1)
    version_id_parameter: str | None = Field(default=None, alias="versionIdParameter", min_length=1)
    range_parameter: str | None = Field(default=None, alias="rangeParameter", min_length=1)
    if_match_parameter: str | None = Field(default=None, alias="ifMatchParameter", min_length=1)
    if_none_match_parameter: str | None = Field(default=None, alias="ifNoneMatchParameter", min_length=1)
    body_parameter: str | None = Field(default=None, alias="bodyParameter", min_length=1)
    content_type_parameter: str | None = Field(default=None, alias="contentTypeParameter", min_length=1)
    source_bucket_ref: str | None = Field(default=None, alias="sourceBucketRef", min_length=1)
    source_key_parameter: str | None = Field(default=None, alias="sourceKeyParameter", min_length=1)
    source_version_id_parameter: str | None = Field(default=None, alias="sourceVersionIdParameter", min_length=1)
    body_encoding: Literal["utf8", "json", "base64", "none"] = Field(default="utf8", alias="bodyEncoding")
    request_body_encoding: Literal["utf8", "json", "base64"] = Field(default="utf8", alias="requestBodyEncoding")
    max_results: int = Field(default=100, ge=1, le=1000, alias="maxResults", strict=True)
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=MAX_BYTES, alias="maxBytes", strict=True)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300, alias="timeoutSeconds")


@dataclass(frozen=True, slots=True)
class CompiledS3Operation:
    definition: OperationDefinition
    config: S3OperationConfig
    storage: S3ServiceConfig
    bucket: str | None
    source_bucket: str | None


class S3OperationHandler:
    kind = "s3"

    def __init__(
        self, s3: S3Port, *, tls_provider: TlsProvider | None = None, redactor: SecretRedactor | None = None
    ) -> None:
        self._s3 = s3
        self._tls_provider = tls_provider
        self._redactor = redactor or SecretRedactor()

    def compile(self, operation: OperationDefinition, profile: Profile) -> CompiledS3Operation:
        try:
            config = S3OperationConfig.model_validate(operation.config)
        except ValidationError as error:
            raise self._invalid(operation, f"invalid S3 config: {error}") from error
        storage = profile.s3.get(config.storage)
        if storage is None:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "S3_NOT_CONFIGURED",
                f"operation '{operation.id}' references missing S3 storage '{config.storage}'",
                source="compiler",
            )
        if storage.tls_ref is not None and self._tls_provider is None:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "TLS_PROVIDER_NOT_FOUND",
                f"S3 storage '{config.storage}' requires tlsRef '{storage.tls_ref}', but no TLS provider is configured",
                source="compiler",
            )
        if self._tls_provider is not None:
            self._tls_provider.ssl_context(storage.tls_ref)
        if operation.mutating != (config.action in MUTATING_ACTIONS):
            raise self._invalid(operation, "mutating must match the S3 action")
        bucket = self._bucket(operation, storage, config.bucket_ref, required=config.action != "list-buckets")
        if config.action == "list-buckets" and config.bucket_ref is not None:
            raise self._invalid(operation, "list-buckets does not accept bucketRef")
        if config.action in OBJECT_ACTIONS and config.key_parameter is None:
            raise self._invalid(operation, "object actions require keyParameter")
        if config.action == "put-object" and config.body_parameter is None:
            raise self._invalid(operation, "put-object requires bodyParameter")
        if config.action == "copy-object":
            if config.source_key_parameter is None:
                raise self._invalid(operation, "copy-object requires sourceKeyParameter")
            source_bucket = self._bucket(operation, storage, config.source_bucket_ref, required=True)
        else:
            source_bucket = None
            if config.source_bucket_ref is not None:
                raise self._invalid(operation, "sourceBucketRef requires copy-object")
        if config.action not in LIST_ACTIONS and {"maxResults", "max_results"} & operation.config.keys():
            raise self._invalid(operation, "maxResults requires a list action")
        if config.action != "put-object" and {"requestBodyEncoding", "request_body_encoding"} & operation.config.keys():
            raise self._invalid(operation, "requestBodyEncoding requires put-object")
        bound: set[str] = set()
        for field_name, actions in BINDING_ACTIONS.items():
            name = getattr(config, field_name)
            if name is None:
                continue
            if config.action not in actions:
                raise self._invalid(operation, f"{field_name} is unsupported for {config.action}")
            parameter = operation.parameters.get(name)
            if parameter is None:
                raise self._invalid(operation, f"binding references unknown parameter '{name}'")
            if parameter.location not in {None, "context"}:
                raise self._invalid(operation, f"S3 parameter '{name}' must use context location")
            if field_name == "max_results_parameter":
                if parameter.value_type != "integer":
                    raise self._invalid(operation, "maxResultsParameter must reference an integer parameter")
            elif field_name == "body_parameter" and config.request_body_encoding == "json":
                pass
            elif parameter.value_type not in {None, "string"}:
                raise self._invalid(operation, f"S3 binding '{name}' must reference a string parameter")
            if (
                field_name in {"key_parameter", "source_key_parameter", "body_parameter"}
                and not parameter.required
                and parameter.default is None
            ):
                raise self._invalid(operation, f"S3 binding '{name}' must be required or have a default")
            bound.add(name)
        unused = set(operation.parameters) - bound
        if unused:
            raise self._invalid(operation, f"unbound S3 parameters: {', '.join(sorted(unused))}")
        return CompiledS3Operation(operation, config, storage, bucket, source_bucket)

    @staticmethod
    def _bucket(
        operation: OperationDefinition, storage: S3ServiceConfig, reference: str | None, *, required: bool
    ) -> str | None:
        if reference is None and not required:
            return None
        if reference is None or reference not in storage.buckets:
            raise S3OperationHandler._invalid(operation, "bucketRef must reference a configured bucket")
        return storage.buckets[reference]

    @staticmethod
    def _invalid(operation: OperationDefinition, message: str) -> RuntimeFailure:
        return RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_INVALID",
            f"S3 operation '{operation.id}': {message}",
            source="registry",
        )

    def validate_input(self, plan: CompiledS3Operation, params: dict[str, Any]) -> None:
        self._request(plan, params)

    def _request(self, plan: CompiledS3Operation, params: dict[str, Any]) -> S3Request:
        config = plan.config

        def value(field_name: str) -> Any:
            name = getattr(config, field_name)
            return params.get(name) if name is not None else None

        for field_name in BINDING_ACTIONS:
            selected = value(field_name)
            if (
                field_name not in {"body_parameter", "max_results_parameter"}
                and selected is not None
                and not isinstance(selected, str)
            ):
                raise self._input_failure("S3 string bindings must receive string values")
        max_results = value("max_results_parameter")
        if max_results is None:
            max_results = config.max_results
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= config.max_results
        ):
            raise self._input_failure("max results must be an integer between 1 and configured maxResults")
        for field_name in ("key_parameter", "source_key_parameter"):
            key = value(field_name)
            if getattr(config, field_name) is not None and (
                not isinstance(key, str) or not key or len(key.encode("utf-8")) > 1024
            ):
                raise self._input_failure("object keys must contain 1 to 1024 UTF-8 bytes")
        byte_range = value("range_parameter")
        if byte_range is not None:
            if not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", byte_range):
                raise self._input_failure("range must be a single bytes range")
            start, end = byte_range[6:].split("-", 1)
            if (start and end and int(start) > int(end)) or (not start and int(end) == 0):
                raise self._input_failure("range bounds are invalid")
        for field_name in ("if_match_parameter", "if_none_match_parameter", "content_type_parameter"):
            header = value(field_name)
            if header is not None and (
                not isinstance(header, str) or not header.isascii() or "\r" in header or "\n" in header
            ):
                raise self._input_failure("S3 header parameters must be single-line ASCII strings")
        body = b""
        if config.body_parameter is not None:
            raw = value("body_parameter")
            try:
                if config.request_body_encoding == "json":
                    body = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
                elif not isinstance(raw, str):
                    raise ValueError("body must be a string")
                elif config.request_body_encoding == "base64":
                    body = base64.b64decode(raw, validate=True)
                else:
                    body = raw.encode("utf-8")
            except (ValueError, TypeError, binascii.Error) as error:
                raise self._input_failure("S3 request body cannot be encoded") from error
            if len(body) > config.max_bytes:
                raise self._input_failure("S3 request body exceeds maxBytes")
        return S3Request(
            storage=config.storage,
            action=config.action,
            bucket=plan.bucket,
            key=value("key_parameter"),
            prefix=value("prefix_parameter"),
            delimiter=value("delimiter_parameter"),
            continuation_token=value("continuation_token_parameter"),
            max_results=max_results,
            version_id=value("version_id_parameter"),
            byte_range=byte_range,
            if_match=value("if_match_parameter"),
            if_none_match=value("if_none_match_parameter"),
            body=body,
            content_type=value("content_type_parameter"),
            source_bucket=plan.source_bucket,
            source_key=value("source_key_parameter"),
            source_version_id=value("source_version_id_parameter"),
            timeout_seconds=config.timeout_seconds,
            max_bytes=config.max_bytes,
        )

    @staticmethod
    def _input_failure(message: str) -> RuntimeFailure:
        return RuntimeFailure(ExitCode.INVALID_INPUT, "S3_INPUT_INVALID", message, source="input")

    async def execute(
        self, plan: CompiledS3Operation, params: dict[str, Any], context: ExecutionContext
    ) -> ResultPayload:
        request = self._request(plan, params)
        response = await self._s3.send(request, context)
        headers = {name.lower(): value for name, value in response.headers.items()}
        payload: dict[str, Any] = {
            "storage": request.storage,
            "action": request.action,
            "status_code": response.status_code,
            "headers": headers,
            "bucket": request.bucket,
            "key": request.key,
        }
        for header, name in (
            ("content-length", "contentLength"),
            ("content-type", "contentType"),
            ("content-encoding", "contentEncoding"),
            ("etag", "etag"),
            ("last-modified", "lastModified"),
            ("x-amz-version-id", "versionId"),
        ):
            if header in headers:
                raw = headers[header]
                payload[name] = int(raw) if header == "content-length" and raw.isdigit() else raw
        if request.action in {"head-bucket", "head-object", "get-object"}:
            payload["found"] = (
                False if response.status_code == 404 else True if 200 <= response.status_code < 300 else None
            )
        if request.action in LIST_ACTIONS and 200 <= response.status_code < 300:
            try:
                payload.update(self._list_payload(request, response))
            except RuntimeFailure as error:
                payload.update(self._raw_response(response.body, error.code, str(error)))
        elif request.action == "copy-object" and response.body and 200 <= response.status_code < 300:
            try:
                root = self._xml(response.body)
                payload["copyResult"] = {child.tag: child.text for child in root}
                payload["responseType"] = root.tag
            except RuntimeFailure as error:
                payload.update(self._raw_response(response.body, error.code, str(error)))
        elif request.action not in {"head-bucket", "head-object", "delete-object"} or response.body:
            encoding = plan.config.body_encoding
            if not 200 <= response.status_code < 300:
                try:
                    response.body.decode("utf-8")
                    encoding = "utf8"
                except UnicodeError:
                    encoding = "base64"
            try:
                payload["bodyEncoding"] = encoding
                payload["body"] = self._decode_body(response.body, encoding)
            except (UnicodeError, ValueError):
                payload.update(
                    self._raw_response(
                        response.body,
                        "S3_BODY_DECODE_FAILED",
                        "S3 response cannot be decoded using configured bodyEncoding",
                    )
                )
        if request.action == "get-object" and 200 <= response.status_code < 300:
            payload["bytesRead"] = len(response.body)
            payload["sha256"] = hashlib.sha256(response.body).hexdigest()
        return self._redactor.redact(payload)

    @staticmethod
    def _decode_body(body: bytes, encoding: str) -> Any:
        if encoding == "none":
            return None
        if encoding == "base64":
            return {"encoding": "base64", "data": base64.b64encode(body).decode("ascii")}
        text = body.decode("utf-8")
        return json.loads(text) if encoding == "json" else text

    def _raw_response(self, body: bytes, code: str, message: str) -> dict[str, Any]:
        return {
            "bodyEncoding": "base64",
            "body": self._decode_body(body, "base64"),
            "decode_error": {"code": code, "message": message},
        }

    @staticmethod
    def _xml(body: bytes) -> Element:
        try:
            root = ElementTree.fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (ParseError, DefusedXmlException, ValueError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "S3_RESPONSE_INVALID",
                "S3 protocol response is not safe, valid XML",
                source="s3",
                execution_outcome="confirmed",
                retry_advice="do_not_retry",
            ) from error
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        return root

    @classmethod
    def _list_payload(cls, request: S3Request, response: S3Result) -> dict[str, Any]:
        root = cls._xml(response.body)
        expected = "ListAllMyBucketsResult" if request.action == "list-buckets" else "ListBucketResult"
        if root.tag != expected:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "S3_RESPONSE_INVALID",
                "S3 list response has an unexpected XML root",
                source="s3",
                execution_outcome="confirmed",
                retry_advice="do_not_retry",
            )
        if request.action == "list-buckets":
            nodes = root.findall("Buckets/Bucket")
            token = root.findtext("ContinuationToken")
            items = [{child.tag: child.text for child in node} for node in nodes[: request.max_results]]
            return {
                "buckets": items,
                "returnedCount": len(items),
                "nextContinuationToken": token,
                "isTruncated": bool(token),
                "localTruncated": len(nodes) > request.max_results,
                "complete": not token and len(nodes) <= request.max_results,
            }
        encoded = root.findtext("EncodingType") == "url"
        contents = root.findall("Contents")
        prefixes = root.findall("CommonPrefixes")
        objects = []
        for node in contents[: request.max_results]:
            item: dict[str, Any] = {child.tag: child.text for child in node if len(child) == 0}
            if encoded and "Key" in item:
                # Match the S3 SDK's EncodingType=url decoder: '+' encodes a
                # space, while a literal plus is percent-encoded as '%2B'.
                item["Key"] = unquote_plus(item["Key"] or "")
            if isinstance(item.get("Size"), str) and item["Size"].isdigit():
                item["Size"] = int(item["Size"])
            objects.append(item)
        remaining = request.max_results - len(objects)
        common_prefixes = [node.findtext("Prefix") for node in prefixes[:remaining]]
        if encoded:
            common_prefixes = [unquote_plus(value or "") for value in common_prefixes]
        token = root.findtext("NextContinuationToken")
        truncated = root.findtext("IsTruncated") == "true"
        locally_truncated = len(contents) + len(prefixes) > request.max_results
        return {
            "objects": objects,
            "commonPrefixes": common_prefixes,
            "returnedCount": len(objects),
            "nextContinuationToken": token,
            "isTruncated": truncated,
            "localTruncated": locally_truncated,
            "complete": not truncated and not token and not locally_truncated,
        }

    async def aclose(self) -> None:
        await self._s3.aclose()
