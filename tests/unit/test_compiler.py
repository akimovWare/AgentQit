from __future__ import annotations

import pytest

from qa_runtime.compiler import CatalogCompiler, HandlerRegistry
from qa_runtime.engine import QaRuntime
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.registry import OperationRegistry

pytestmark = pytest.mark.anyio


class FakeDbHandler:
    kind = "fake-db"

    def __init__(self) -> None:
        self.closed = False

    def compile(self, operation, profile):
        return {"query": operation.config["query"], "environment": profile.environment}

    async def execute(self, plan, params, context):
        return {
            "rows": [{"client_id": params["client-id"]}],
            "query": plan["query"],
            "environment": context.environment,
        }

    async def aclose(self):
        self.closed = True


def registry() -> OperationRegistry:
    return OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "client.inspect",
                        "kind": "fake-db",
                        "parameters": {"client-id": {"required": True}},
                        "config": {"query": "client-by-id"},
                    }
                ],
            }
        )
    )


async def test_non_http_handler_runs_through_common_runtime_lifecycle() -> None:
    handler = FakeDbHandler()
    catalog = CatalogCompiler().compile(
        registry(),
        Profile(environment="qa"),
        HandlerRegistry([handler]),
    )
    runtime = QaRuntime(catalog)

    result = await runtime.invoke("client.inspect", {"client-id": "client-1"})
    await runtime.aclose()

    assert result.exit_code == 0
    assert result.result == {
        "rows": [{"client_id": "client-1"}],
        "query": "client-by-id",
        "environment": "qa",
    }
    assert handler.closed is True


async def test_compiler_rejects_operation_without_registered_handler() -> None:
    with pytest.raises(RuntimeFailure, match="fake-db") as raised:
        CatalogCompiler().compile(registry(), Profile(environment="qa"), HandlerRegistry([]))

    assert raised.value.code == "HANDLER_NOT_FOUND"


async def test_compiler_can_compile_only_selected_operation() -> None:
    handler = FakeDbHandler()
    document = RegistryDocument.model_validate(
        {
            "apiVersion": "qa.runtime/v1",
            "kind": "OperationRegistry",
            "operations": [
                {
                    "id": "client.inspect",
                    "kind": "fake-db",
                    "parameters": {"client-id": {"required": True}},
                    "config": {"query": "client-by-id"},
                },
                {
                    "id": "unrelated.operation",
                    "kind": "missing-handler",
                    "config": {},
                },
            ],
        }
    )

    catalog = CatalogCompiler().compile(
        OperationRegistry(document),
        Profile(environment="qa"),
        HandlerRegistry([handler]),
        operation_ids=("client.inspect",),
    )

    assert catalog.ids() == ["client.inspect"]
