from __future__ import annotations

import json

import httpx
import pytest

from qa_runtime.adapters.httpx import HttpxAdapter
from qa_runtime.compiler import CatalogCompiler, HandlerRegistry
from qa_runtime.contracts import ExitCode
from qa_runtime.engine import QaRuntime
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.database import DatabaseOperationHandler
from qa_runtime.handlers.http import HttpOperationHandler
from qa_runtime.handlers.kafka import KafkaConsumeOperationHandler
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.ports.database import DatabaseResult
from qa_runtime.ports.kafka import KafkaRecord
from qa_runtime.registry import OperationRegistry
from qa_runtime.security import SecretRedactor

pytestmark = pytest.mark.anyio


def make_runtime(handler, profile, config, *, redactor=None, mutating=False, parameters=None):
    registry = OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "audit",
                        "kind": handler.kind,
                        "mutating": mutating,
                        "config": config,
                        "parameters": parameters or {},
                    }
                ],
            }
        )
    )
    catalog = CatalogCompiler().compile(registry, profile, HandlerRegistry([handler]))
    return QaRuntime(catalog, redactor=redactor)


@pytest.mark.parametrize("kind", ["http", "db-query", "kafka-consume"])
async def test_public_result_redacts_all_transports_with_secrets_registered_during_execution(kind):
    redactor = SecretRedactor()
    registered = "credential-resolved-during-execution"
    keyed_secret = "secret-known-by-field-name-only"

    def payload():
        redactor.register(registered)
        return {"password": keyed_secret, "business": {"note": registered, "status": "DONE"}}

    class Database:
        async def execute(self, *args, **kwargs):
            return DatabaseResult(rows=[payload()], row_count=1)

    class Kafka:
        async def consume(self, request):
            return [
                KafkaRecord(
                    topic=request.topic,
                    partition=0,
                    offset=7,
                    timestamp_ms=123,
                    key=b"business-key",
                    value=json.dumps(payload()).encode(),
                    headers=[("authorization", keyed_secret.encode()), ("trace-value", registered.encode())],
                )
            ]

    async def respond(request):
        return httpx.Response(200, json=payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if kind == "http":
            profile = Profile.model_validate({"environment": "qa", "services": {"h": {"baseUrl": "https://fake"}}})
            handler = HttpOperationHandler(HttpxAdapter(client))
            config = {"service": "h", "method": "GET", "path": "/"}
        elif kind == "db-query":
            profile = Profile.model_validate(
                {
                    "environment": "qa",
                    "databases": {"db": {"dsn": "postgresql://fake/db", "usernameRef": "u", "passwordRef": "p"}},
                }
            )
            handler = DatabaseOperationHandler(Database(), profile.databases)
            config = {"database": "db", "statement": "SELECT safe_column FROM fixture", "fetch": "all"}
        else:
            profile = Profile.model_validate(
                {"environment": "qa", "kafka": {"k": {"bootstrapServers": ["fake:9092"], "topics": {"t": "fixture"}}}}
            )
            handler = KafkaConsumeOperationHandler(Kafka(), profile.kafka)
            config = {"cluster": "k", "topicRef": "t"}
        runtime = make_runtime(handler, profile, config, redactor=redactor)
        result = await runtime.invoke("audit", {})

    assert result.exit_code == 0
    serialized = result.model_dump_json()
    assert registered not in serialized
    assert keyed_secret not in serialized
    assert "DONE" in serialized
    if kind == "kafka-consume":
        record = result.result["records"][0]
        assert record["offset"] == 7
        assert record["headers"] == [
            {"name": "authorization", "value": "***"},
            {"name": "trace-value", "value": "***"},
        ]


async def test_runtime_sanitizes_every_free_form_error_field_without_losing_diagnostics():
    secret = "runtime-error-canary"
    redactor = SecretRedactor()

    class Handler:
        kind = "audit"

        def compile(self, operation, profile):
            return None

        async def execute(self, plan, params, context):
            redactor.register(secret)
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "AUDIT_UNAVAILABLE",
                f"failed: {secret}",
                source=secret,
                details={"password": "another-canary", "nested": [secret], secret: "diagnostic"},
                hint=secret,
                documentation=secret,
            )

    runtime = make_runtime(Handler(), Profile(environment="qa"), {}, redactor=redactor)
    result = await runtime.invoke("audit", {})

    assert result.error.code == "AUDIT_UNAVAILABLE"
    assert result.error.operation == "audit"
    assert result.correlation is not None
    assert secret not in result.model_dump_json()
    assert "another-canary" not in result.model_dump_json()
    assert result.error.details["***"] == "diagnostic"


@pytest.mark.parametrize("failure_kind", ["timeout", "unexpected"])
@pytest.mark.parametrize("has_idempotency_key", [False, True])
async def test_lost_mutation_response_reports_unknown_outcome_without_repeating_the_action(
    failure_kind, has_idempotency_key
):
    accepted_requests = []

    async def respond(request):
        accepted_requests.append(request)
        if failure_kind == "timeout":
            raise httpx.ReadTimeout("secret transport text", request=request)
        raise ValueError("secret transport text")

    profile = Profile.model_validate({"environment": "qa", "services": {"h": {"baseUrl": "https://fake"}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runtime = make_runtime(
            HttpOperationHandler(HttpxAdapter(client)),
            profile,
            {"service": "h", "method": "POST", "path": "/create"},
            mutating=True,
            parameters={"idempotency-key": {"type": "string", "location": "header"}},
        )
        params = {"idempotency-key": "original-action-key"} if has_idempotency_key else {}
        result = await runtime.invoke("audit", params, allow_mutation=True)

    assert len(accepted_requests) == 1
    assert result.error.execution_outcome == "unknown"
    assert result.error.retry_advice == "read_state_first"
    assert result.error.retryable is False
    assert result.error.operation == "audit"
    assert result.correlation is not None
    assert "secret transport text" not in result.model_dump_json()


async def test_transport_evidence_of_not_sent_is_preserved_for_a_retryable_mutation():
    class Handler:
        kind = "audit"

        def compile(self, operation, profile):
            return None

        async def execute(self, plan, params, context):
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "UNAVAILABLE_BEFORE_SEND",
                "transport explicitly confirmed no request was sent",
                retryable=True,
                execution_outcome="not_sent",
            )

    runtime = make_runtime(Handler(), Profile(environment="qa"), {}, mutating=True)
    result = await runtime.invoke("audit", {}, allow_mutation=True)

    assert result.error.execution_outcome == "not_sent"
    assert result.error.retry_advice == "safe"
    assert result.error.retryable is True


@pytest.mark.parametrize("status", [201, 503])
async def test_blocked_mutation_is_not_sent_and_projection_failure_preserves_response_facts(status):
    accepted_requests = []

    async def respond(request):
        accepted_requests.append(request)
        return httpx.Response(
            status,
            json="restricted-raw-body-contains-private-secret",
            headers={"retry-after": "2", "set-cookie": "session=private-secret"},
        )

    profile = Profile.model_validate({"environment": "qa", "services": {"h": {"baseUrl": "https://fake"}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runtime = make_runtime(
            HttpOperationHandler(HttpxAdapter(client)),
            profile,
            {"service": "h", "method": "POST", "path": "/create", "responseBodyFields": ["id"]},
            mutating=True,
        )
        blocked = await runtime.invoke("audit", {})
        assert not accepted_requests
        assert blocked.error.execution_outcome == "not_sent"
        assert blocked.error.retry_advice == "do_not_retry"
        result = await runtime.invoke("audit", {}, allow_mutation=True)

    assert len(accepted_requests) == 1
    assert result.error is None
    assert result.exit_code == ExitCode.SUCCESS
    assert result.result["status_code"] == status
    assert result.result["headers"]["retry-after"] == "2"
    assert result.result["headers"]["set-cookie"] == "***"
    assert result.result["body"] is None
    assert result.result["projection_error"] == {
        "code": "HTTP_RESPONSE_PROJECTION_FAILED",
        "allowed_fields": ["id"],
    }
    assert "restricted-raw-body" not in result.model_dump_json()
    assert "private-secret" not in result.model_dump_json()


async def test_validate_input_does_not_resolve_credentials_or_admit_a_mutation():
    class Handler:
        kind = "audit"

        def compile(self, operation, profile):
            return None

        async def execute(self, *args):
            pytest.fail("input validation must not execute the handler")

    runtime = make_runtime(
        Handler(),
        Profile(environment="production", defaults={"count": "2"}),
        {},
        mutating=True,
        parameters={
            "count": {"required": True, "type": "integer", "minimum": 1},
            "credential-ref": {"required": True, "type": "string", "location": "credential-body"},
        },
    )

    assert runtime.validate_input("audit", {"credential-ref": "qa/ref"}) == {"count": 2, "credential-ref": "qa/ref"}
    with pytest.raises(RuntimeFailure, match="must be greater than or equal to 1"):
        runtime.validate_input("audit", {"count": 0, "credential-ref": "qa/ref"})


@pytest.mark.parametrize("mutating, supports_capture", [(False, True), (False, False), (True, True)])
async def test_capture_uses_optional_read_only_capability_without_execution_input(mutating, supports_capture):
    captured = []

    class Handler:
        kind = "audit"

        def compile(self, operation, profile):
            return "compiled-plan"

        async def execute(self, *args):
            pytest.fail("checkpoint must not execute an operation")

    class ObservableHandler(Handler):
        async def capture(self, plan, context):
            captured.append((plan, context))
            return {"cursor": "opaque-value", "password": "must-not-leak"}

    runtime = make_runtime(
        ObservableHandler() if supports_capture else Handler(),
        Profile(environment="qa"),
        {},
        mutating=mutating,
        parameters={"required-execution-input": {"required": True}},
    )
    result = await runtime.capture("audit")

    assert result.command == "checkpoint audit"
    if not mutating and supports_capture:
        assert result.exit_code == 0
        assert result.result == {"cursor": "opaque-value", "password": "***"}
        assert captured[0][0] == "compiled-plan"
        assert captured[0][1].run_id == result.run_id
    else:
        assert result.exit_code == ExitCode.INVALID_INPUT
        assert result.error.code == "CHECKPOINT_UNSUPPORTED"
        assert result.error.execution_outcome == "not_sent"
        assert not captured
