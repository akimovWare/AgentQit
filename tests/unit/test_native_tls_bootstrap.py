from __future__ import annotations

from pathlib import Path

import pytest

from qa_runtime.bootstrap import build_runtime
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.ports.product import ProductPlugin
from qa_runtime.registry import OperationRegistry

pytestmark = pytest.mark.anyio


class UnusedCredentials:
    async def get(self, reference):
        pytest.fail("native TLS compilation must not obtain credentials")


class OfflinePort:
    def __init__(self):
        self.close_count = 0

    async def send(self, *args, **kwargs):
        pytest.fail("native TLS compilation must not send requests")

    execute = send
    consume = send
    publish = send
    capture = send

    async def aclose(self):
        self.close_count += 1


def profile():
    return Profile.model_validate(
        {
            "environment": "qa",
            "services": {"api": {"baseUrl": "https://api.sample.invalid", "tlsRef": "system"}},
            "databases": {
                "db": {
                    "dsn": "postgresql://db.sample.invalid/sample",
                    "usernameRef": "db/user",
                    "passwordRef": "db/password",
                    "tlsRef": "system",
                }
            },
            "kafka": {
                "events": {
                    "bootstrapServers": ["events.sample.invalid:9093"],
                    "securityProtocol": "SSL",
                    "tlsRef": "system",
                    "topics": {"events": "sample-events"},
                }
            },
            "s3": {
                "files": {
                    "endpoint": "https://files.sample.invalid",
                    "region": "us-east-1",
                    "accessKeyRef": "files/access",
                    "secretKeyRef": "files/secret",
                    "tlsRef": "system",
                    "buckets": {"files": "sample-files"},
                }
            },
        }
    )


def registry():
    return OperationRegistry(
        RegistryDocument.model_validate(
            {
                "apiVersion": "qa.runtime/v1",
                "kind": "OperationRegistry",
                "operations": [
                    {"id": "http.read", "kind": "http", "config": {"service": "api", "method": "GET", "path": "/"}},
                    {
                        "id": "db.read",
                        "kind": "db-query",
                        "config": {"database": "db", "statement": "SELECT 1"},
                    },
                    {
                        "id": "kafka.read",
                        "kind": "kafka-consume",
                        "config": {"cluster": "events", "topicRef": "events", "valueEncoding": "json"},
                    },
                    {
                        "id": "s3.read",
                        "kind": "s3",
                        "config": {"storage": "files", "action": "list-objects", "bucketRef": "files"},
                    },
                ],
            }
        )
    )


def plugin():
    return ProductPlugin("sample", Path("unused-registry"))


def ports():
    return {name: OfflinePort() for name in ("http_port", "database_port", "kafka_port", "s3_port")}


async def test_factoryless_product_compiles_system_tls_for_all_native_transports_without_io():
    provided = ports()
    runtime = await build_runtime(plugin(), profile(), registry(), UnusedCredentials(), **provided)
    assert all(port.close_count == 0 for port in provided.values())
    await runtime.aclose()
    await runtime.aclose()
    assert all(port.close_count == 1 for port in provided.values())


@pytest.mark.parametrize(
    "section,resource,operation_id",
    [
        ("services", "api", "http.read"),
        ("databases", "db", "db.read"),
        ("kafka", "events", "kafka.read"),
        ("s3", "files", "s3.read"),
    ],
)
async def test_unknown_tls_reference_is_rejected_offline_and_closes_owned_ports(section, resource, operation_id):
    selected_profile = profile()
    getattr(selected_profile, section)[resource].tls_ref = "unknown-private-ca"
    provided = ports()
    with pytest.raises(RuntimeFailure) as raised:
        await build_runtime(
            plugin(), selected_profile, registry(), UnusedCredentials(), operation_ids=(operation_id,), **provided
        )
    assert raised.value.exit_code == ExitCode.ENVIRONMENT_UNAVAILABLE
    assert raised.value.code == "TLS_REF_NOT_FOUND"
    assert all(port.close_count == 1 for port in provided.values())
