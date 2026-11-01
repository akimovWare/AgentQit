from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from qa_runtime.bootstrap import build_runtime, scope_profile
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import HttpResult, Profile, RegistryDocument
from qa_runtime.ports.product import ProductComponents, ProductPlugin
from qa_runtime.registry import OperationRegistry

pytestmark = pytest.mark.anyio


class UnusedCredentials:
    async def get(self, reference):
        pytest.fail("compilation must not obtain credentials")


class FakeHttp:
    def __init__(self):
        self.requests = []
        self.close_count = 0

    async def send(self, request, context):
        self.requests.append(request)
        return HttpResult(status_code=200, body={"label": "scope-secret"})

    async def aclose(self):
        await anyio.lowlevel.checkpoint()
        self.close_count += 1


class FakeHandler:
    kind = "custom"

    def __init__(self, *, kind="custom", cleanup="success"):
        self.kind = kind
        self.cleanup = cleanup
        self.close_count = 0

    def compile(self, operation, profile):
        return operation

    async def execute(self, plan, params, context):
        return {}

    async def aclose(self):
        self.close_count += 1
        if self.cleanup == "hang":
            await anyio.sleep_forever()
        if self.cleanup == "error":
            raise RuntimeError("cleanup must not replace the original error")
        if self.cleanup == "cancel":
            raise anyio.get_cancelled_exc_class()()


def _registry(*extra):
    return OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {
                        "id": "sample.read",
                        "kind": "http",
                        "config": {"service": "sample", "method": "GET", "path": "/items"},
                    },
                    *extra,
                ],
            }
        )
    )


def _profile():
    return Profile.model_validate(
        {
            "environment": "qa",
            "services": {
                "sample": {"baseUrl": "https://sample.invalid"},
                "other": {"baseUrl": "https://other.invalid"},
            },
            "databases": {
                "db": {"dsn": "postgresql://db.invalid/test", "usernameRef": "db/user", "passwordRef": "db/pass"}
            },
            "kafka": {"events": {"bootstrapServers": ["kafka.invalid:9092"]}},
            "endpoints": {"login": "https://login.invalid"},
            "defaults": {"ids": ["client-1"]},
        }
    )


def _plugin(factory=None):
    return ProductPlugin("sample", Path("unused-registry-path"), runtime_factory=factory)


async def test_build_uses_loaded_selected_configuration_and_transfers_resource_ownership():
    profiles = []

    def factory(profile, credentials, redactor):
        profiles.append(profile)
        return ProductComponents()

    profile = _profile()
    port = FakeHttp()
    registry = _registry({"id": "unconfigured", "kind": "unsupported", "config": {}})
    runtime = await build_runtime(
        _plugin(factory), profile, registry, UnusedCredentials(), operation_ids=("sample.read",), http_port=port
    )
    assert list(profiles[0].services) == ["sample"]
    assert profiles[0].databases == profiles[0].kafka == {}
    assert profiles[0].endpoints == profile.endpoints
    assert port.requests == []
    assert port.close_count == 0

    result = await runtime.invoke("sample.read", {})
    await runtime.aclose()

    assert result.exit_code == 0
    assert port.requests[0].url == "https://sample.invalid/items"
    assert port.close_count == 1
    assert list(profile.services) == ["sample", "other"]


@pytest.mark.parametrize("selection", [None, ("sample.read",), ("custom.read",)])
def test_scoping_copies_shared_configuration_and_preserves_custom_dependencies(selection):
    registry = _registry({"id": "custom.read", "kind": "custom", "config": {}})
    profile = _profile()
    scoped = scope_profile(profile, registry, selection)
    if selection != ("sample.read",):
        assert scoped == profile
    scoped.defaults["ids"].append("changed")
    scoped.services["sample"].base_url = "https://changed.invalid"

    assert profile.defaults["ids"] == ["client-1"]
    assert profile.services["sample"].base_url == "https://sample.invalid"


def test_scoping_retains_database_and_kafka_resources_for_selected_operations():
    registry = _registry(
        {"id": "db.read", "kind": "db-query", "config": {"database": "db"}},
        {"id": "events.read", "kind": "kafka-consume", "config": {"cluster": "events"}},
        {"id": "events.write", "kind": "kafka-publish", "mutating": True, "config": {"cluster": "events"}},
    )
    scoped = scope_profile(_profile(), registry, ("db.read", "events.read", "events.write"))

    assert scoped.services == {}
    assert list(scoped.databases) == ["db"]
    assert list(scoped.kafka) == ["events"]


async def test_each_build_has_a_distinct_redactor_and_factory_call():
    redactors = []

    def factory(profile, credentials, redactor):
        redactors.append(redactor)
        return ProductComponents()

    plugin = _plugin(factory)
    first = await build_runtime(plugin, _profile(), _registry(), UnusedCredentials(), http_port=FakeHttp())
    second = await build_runtime(plugin, _profile(), _registry(), UnusedCredentials(), http_port=FakeHttp())
    try:
        redactors[0].register("scope-secret")
        first_result = await first.invoke("sample.read", {})
        second_result = await second.invoke("sample.read", {})
        assert first_result.result["body"]["label"] == "***"
        assert second_result.result["body"]["label"] == "scope-secret"
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("failure", ["compile", "duplicate"])
@pytest.mark.parametrize("cleanup", ["success", "error", "hang", "cancel"])
async def test_failed_build_closes_every_owned_resource_without_replacing_error(monkeypatch, failure, cleanup):
    monkeypatch.setattr("qa_runtime.bootstrap._CLEANUP_TIMEOUT_SECONDS", 0.01)
    handler = FakeHandler(kind="http" if failure == "duplicate" else "custom", cleanup=cleanup)
    provider = FakeHandler()
    port = FakeHttp()

    def factory(profile, credentials, redactor):
        return ProductComponents(handlers=[handler], auth_providers={"first": provider, "alias": provider})

    registry = _registry({"id": "missing", "kind": "unsupported", "config": {}})
    with pytest.raises(RuntimeFailure) as raised:
        await build_runtime(_plugin(factory), _profile(), registry, UnusedCredentials(), http_port=port)

    assert raised.value.code == ("DUPLICATE_HANDLER" if failure == "duplicate" else "HANDLER_NOT_FOUND")
    assert handler.close_count == provider.close_count == port.close_count == 1
    assert port.requests == []


async def test_compile_failure_cleanup_is_shielded_from_caller_cancellation():
    port = FakeHttp()
    with anyio.CancelScope() as scope:
        scope.cancel()
        with pytest.raises(RuntimeFailure, match="no operation registered"):
            await build_runtime(
                _plugin(), _profile(), _registry(), UnusedCredentials(), operation_ids=("missing",), http_port=port
            )

    assert port.close_count == 1


async def test_default_adapter_creation_failure_closes_returned_product_resources(monkeypatch):
    handler = FakeHandler()

    def failed_adapter(**kwargs):
        raise ValueError("adapter allocation failed")

    def factory(profile, credentials, redactor):
        return ProductComponents(handlers=[handler])

    monkeypatch.setattr("qa_runtime.bootstrap.HttpxAdapter", failed_adapter)
    with pytest.raises(ValueError, match="adapter allocation failed"):
        await build_runtime(_plugin(factory), _profile(), _registry(), UnusedCredentials())

    assert handler.close_count == 1
