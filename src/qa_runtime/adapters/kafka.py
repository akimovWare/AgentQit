from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import anyio
import jks
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.errors import (
    AuthenticationFailedError,
    ClusterAuthorizationFailedError,
    KafkaError,
    TopicAuthorizationFailedError,
)

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.lifecycle import CloseAttempt, close_resources
from qa_runtime.models import KafkaClusterConfig
from qa_runtime.ports.credentials import CredentialSource
from qa_runtime.ports.decoding import ValueDecoder
from qa_runtime.ports.kafka import (
    KafkaConsumeRequest,
    KafkaConsumeResult,
    KafkaCursor,
    KafkaJsonFieldMatch,
    KafkaPublishResult,
    KafkaRecord,
    kafka_record_size,
    topic_fingerprint,
)
from qa_runtime.ports.tls import TlsProvider
from qa_runtime.security import SecretRedactor
from qa_runtime.serialization import to_jsonable


class AioKafkaAdapter:
    def __init__(
        self,
        clusters: Mapping[str, KafkaClusterConfig],
        credentials: CredentialSource,
        redactor: SecretRedactor,
        *,
        tls_provider: TlsProvider | None = None,
    ) -> None:
        self._clusters = dict(clusters)
        self._credentials = credentials
        self._redactor = redactor
        self._tls_provider = tls_provider
        self._producers: dict[str, AIOKafkaProducer] = {}
        self._producer_lock = asyncio.Lock()
        self._ssl_contexts: dict[str, ssl.SSLContext] = {}

    async def committed_offset(
        self,
        cluster: str,
        topic: str,
        group_id: str,
        partition: int,
        *,
        client_id: str,
        timeout_seconds: float,
    ) -> int | None:
        admin = None
        try:
            async with asyncio.timeout(timeout_seconds):
                config = self._cluster(cluster)
                admin = AIOKafkaAdminClient(
                    bootstrap_servers=config.bootstrap_servers,
                    client_id=client_id,
                    request_timeout_ms=max(1000, int(config.request_timeout_seconds * 1000)),
                    **await self._security_options(cluster, config),
                )
                await admin.start()
                target = TopicPartition(topic, partition)
                offsets = await admin.list_consumer_group_offsets(group_id, partitions=[target])
                committed = offsets.get(target)
                return committed.offset if committed is not None and committed.offset >= 0 else None
        except RuntimeFailure:
            raise
        except AuthenticationFailedError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED, "KAFKA_AUTH_FAILED", "Kafka rejected authentication", source="kafka"
            ) from error
        except (TimeoutError, KafkaError, OSError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_OFFSETS_UNAVAILABLE",
                "Kafka consumer group offsets could not be observed",
                source="kafka",
                retryable=True,
            ) from error
        finally:
            if admin is not None:
                with suppress(Exception, asyncio.CancelledError):
                    with anyio.CancelScope(shield=True):
                        async with asyncio.timeout(min(1.0, timeout_seconds)):
                            await admin.close()

    async def consume(self, request: KafkaConsumeRequest) -> KafkaConsumeResult:
        self._validate_cursor_source(request)
        deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        consumer = None
        try:
            async with asyncio.timeout_at(deadline):
                consumer = await self._new_consumer(request.cluster, request.client_id)
                consumer.subscribe(topics={request.topic})
                await consumer.start()
                partitions = self._partitions(consumer, request.topic)
                if request.after_cursor is not None:
                    await self._seek_cursor(consumer, partitions, request.after_cursor)
                else:
                    await self._seek(consumer, partitions, request.lookback_seconds)
            return await self._poll(consumer, request, deadline=deadline)
        except RuntimeFailure:
            raise
        except TimeoutError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_SETUP_TIMEOUT",
                "Kafka connection or cursor preparation exhausted the observation budget",
                source="kafka",
                retryable=True,
            ) from error
        except AuthenticationFailedError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "KAFKA_AUTH_FAILED",
                "Kafka rejected authentication",
                source="kafka",
                execution_outcome="not_sent",
            ) from error
        except (KafkaError, OSError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_UNAVAILABLE",
                f"Kafka cluster '{request.cluster}' request failed",
                source="kafka",
                retryable=True,
            ) from error
        finally:
            await self._close_consumer(consumer, request.timeout_seconds)

    async def capture(
        self,
        cluster: str,
        topic: str,
        *,
        client_id: str,
        timeout_seconds: float,
    ) -> KafkaCursor:
        consumer = None
        try:
            async with asyncio.timeout(timeout_seconds):
                consumer = await self._new_consumer(cluster, client_id)
                consumer.subscribe(topics={topic})
                await consumer.start()
                partitions = self._partitions(consumer, topic)
                offsets = await consumer.end_offsets(partitions)
                return KafkaCursor(
                    cluster=cluster,
                    topic_fingerprint=topic_fingerprint(topic),
                    source_fingerprint=self._source_fingerprint(cluster),
                    offsets={partition.partition: offsets[partition] for partition in partitions},
                )
        except RuntimeFailure:
            raise
        except TimeoutError as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_CHECKPOINT_TIMEOUT",
                "Kafka checkpoint exhausted its time budget",
                source="kafka",
                retryable=True,
            ) from error
        except AuthenticationFailedError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "KAFKA_AUTH_FAILED",
                "Kafka rejected authentication",
                source="kafka",
                execution_outcome="not_sent",
            ) from error
        except (KafkaError, OSError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_UNAVAILABLE",
                "Kafka checkpoint could not be captured",
                source="kafka",
                retryable=True,
            ) from error
        finally:
            await self._close_consumer(consumer, timeout_seconds)

    async def _new_consumer(self, cluster: str, client_id: str):
        config = self._cluster(cluster)
        return AIOKafkaConsumer(
            bootstrap_servers=config.bootstrap_servers,
            client_id=client_id,
            group_id=None,
            enable_auto_commit=False,
            isolation_level="read_committed",
            request_timeout_ms=max(1000, int(config.request_timeout_seconds * 1000)),
            **await self._security_options(cluster, config),
        )

    @staticmethod
    def _partitions(consumer, topic: str) -> list[TopicPartition]:
        partitions = sorted(consumer.assignment(), key=lambda item: item.partition)
        if not partitions:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KAFKA_TOPIC_NOT_FOUND",
                f"topic '{topic}' has no assigned partitions",
                source="kafka",
            )
        return partitions

    @staticmethod
    async def _close_consumer(consumer, budget: float) -> None:
        if consumer is not None:
            # Cleanup has its own documented upper bound; it must not replace a
            # completed observation or extend an unavailable broker call forever.
            with suppress(Exception, asyncio.CancelledError):
                with anyio.CancelScope(shield=True):
                    async with asyncio.timeout(min(1.0, budget)):
                        await consumer.stop()

    def _source_fingerprint(self, cluster: str) -> str:
        servers = sorted(self._cluster(cluster).bootstrap_servers)
        return hashlib.sha256(json.dumps(servers, separators=(",", ":")).encode()).hexdigest()

    def _validate_cursor_source(self, request: KafkaConsumeRequest) -> None:
        cursor = request.after_cursor
        if cursor is not None and (
            cursor.cluster != request.cluster
            or not cursor.matches_topic(request.topic)
            or cursor.source_fingerprint != self._source_fingerprint(request.cluster)
        ):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KAFKA_CURSOR_MISMATCH",
                "cursor does not belong to this configured Kafka source and topic",
                source="kafka",
            )

    @staticmethod
    async def _seek_cursor(consumer, partitions: list[TopicPartition], cursor: KafkaCursor) -> None:
        if set(cursor.offsets) != {partition.partition for partition in partitions}:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KAFKA_CURSOR_STALE",
                "Kafka partition set changed since checkpoint; capture a new boundary before a new action",
                source="kafka",
            )
        beginnings = await consumer.beginning_offsets(partitions)
        ends = await consumer.end_offsets(partitions)
        for partition in partitions:
            offset = cursor.offsets[partition.partition]
            if not beginnings[partition] <= offset <= ends[partition]:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "KAFKA_CURSOR_STALE",
                    "checkpoint is outside retained offsets; the previous observation window cannot be proven",
                    source="kafka",
                )
            consumer.seek(partition, offset)

    async def publish(
        self,
        cluster: str,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: list[tuple[str, bytes | None]],
        client_id: str,
    ) -> KafkaPublishResult:
        config = self._cluster(cluster)
        action_started = False
        try:
            async with asyncio.timeout(config.request_timeout_seconds):
                producer = await self._producer_for(cluster, client_id)
                action_started = True
                metadata = await producer.send_and_wait(topic, value=value, key=key, headers=headers)
            return KafkaPublishResult(
                topic=metadata.topic,
                partition=metadata.partition,
                offset=metadata.offset,
                timestamp_ms=metadata.timestamp,
            )
        except RuntimeFailure as error:
            if not action_started:
                error.execution_outcome = "not_sent"
            raise
        except AuthenticationFailedError as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "KAFKA_AUTH_FAILED",
                "Kafka rejected authentication",
                source="kafka",
                execution_outcome=None if action_started else "not_sent",
            ) from error
        except (TopicAuthorizationFailedError, ClusterAuthorizationFailedError) as error:
            raise RuntimeFailure(
                ExitCode.AUTHENTICATION_FAILED,
                "KAFKA_AUTHORIZATION_FAILED",
                "Kafka identity is not authorized to publish",
                source="kafka",
                execution_outcome="not_sent",
                details={"reason": type(error).__name__},
            ) from error
        except (KafkaError, OSError, TimeoutError) as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_UNAVAILABLE",
                f"Kafka cluster '{cluster}' publish failed",
                source="kafka",
                details={"reason": type(error).__name__},
                retryable=True,
                execution_outcome=None if action_started else "not_sent",
            ) from error

    async def aclose(self) -> None:
        producers = list(self._producers.items())
        self._producers.clear()
        self._ssl_contexts.clear()
        await close_resources(
            [
                CloseAttempt(producer.stop, self._cluster(cluster).request_timeout_seconds)
                for cluster, producer in producers
            ],
            source="kafka",
            code="KAFKA_CLOSE_FAILED",
        )

    async def _producer_for(self, cluster: str, client_id: str) -> AIOKafkaProducer:
        producer = self._producers.get(cluster)
        if producer is not None:
            return producer
        async with self._producer_lock:
            producer = self._producers.get(cluster)
            if producer is not None:
                return producer
            config = self._cluster(cluster)
            producer = AIOKafkaProducer(
                bootstrap_servers=config.bootstrap_servers,
                client_id=client_id,
                enable_idempotence=True,
                request_timeout_ms=max(1000, int(config.request_timeout_seconds * 1000)),
                **await self._security_options(cluster, config),
            )
            try:
                await producer.start()
            except BaseException:
                # A startup timeout cancels start(); the partially initialized
                # producer still owns driver resources and must be stopped.
                with suppress(Exception, asyncio.CancelledError):
                    with anyio.CancelScope(shield=True):
                        async with asyncio.timeout(min(1.0, config.request_timeout_seconds)):
                            await producer.stop()
                raise
            self._producers[cluster] = producer
            return producer

    async def _security_options(self, cluster: str, config: KafkaClusterConfig) -> dict[str, Any]:
        options: dict[str, Any] = {"security_protocol": config.security_protocol}
        if config.security_protocol.endswith("SSL"):
            options["ssl_context"] = await self._ssl_context(cluster, config)
        if config.security_protocol.startswith("SASL_"):
            username = await self._credentials.get(config.username_ref or "")
            self._redactor.register(username)
            password = await self._credentials.get(config.password_ref or "")
            self._redactor.register(password)
            options.update(
                {
                    "sasl_mechanism": config.sasl_mechanism,
                    "sasl_plain_username": username.reveal(),
                    "sasl_plain_password": password.reveal(),
                }
            )
        return options

    async def _ssl_context(self, cluster: str, config: KafkaClusterConfig) -> ssl.SSLContext:
        if config.truststore is None:
            return self._provider_ssl_context(config.tls_ref)
        cached = self._ssl_contexts.get(cluster)
        if cached is not None:
            return cached
        password = await self._credentials.get(config.truststore.password_ref)
        self._redactor.register(password)
        context = self._jks_ssl_context(config.truststore.location, password.reveal())
        self._ssl_contexts[cluster] = context
        return context

    def _provider_ssl_context(self, reference: str | None) -> ssl.SSLContext:
        if self._tls_provider is None:
            if reference is not None:
                raise RuntimeFailure(
                    ExitCode.ENVIRONMENT_UNAVAILABLE,
                    "TLS_PROVIDER_NOT_FOUND",
                    f"Kafka configuration requires tlsRef '{reference}'",
                    source="kafka",
                )
            return ssl.create_default_context()
        return self._tls_provider.ssl_context(reference)

    @staticmethod
    def _jks_ssl_context(location: Path, password: str) -> ssl.SSLContext:
        try:
            truststore = jks.KeyStore.load(str(location), password, try_decrypt_keys=False)
            if truststore.private_keys or truststore.secret_keys:
                raise ValueError("truststore contains key entries")
            if not truststore.certs:
                raise ValueError("truststore contains no trusted certificates")
            certificates = "\n".join(ssl.DER_cert_to_PEM_cert(entry.cert) for entry in truststore.certs.values())
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=certificates)
        except Exception as error:
            raise RuntimeFailure(
                ExitCode.ENVIRONMENT_UNAVAILABLE,
                "KAFKA_TRUSTSTORE_INVALID",
                "Kafka JKS truststore could not be loaded; check the file and password",
                source="kafka",
            ) from error
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def _cluster(self, reference: str) -> KafkaClusterConfig:
        try:
            return self._clusters[reference]
        except KeyError as error:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "KAFKA_CLUSTER_NOT_CONFIGURED",
                f"Kafka cluster '{reference}' is not configured",
                source="kafka",
            ) from error

    @staticmethod
    async def _seek(
        consumer: AIOKafkaConsumer,
        partitions: list[TopicPartition],
        lookback_seconds: float | None,
    ) -> None:
        if lookback_seconds is None:
            await consumer.seek_to_beginning(*partitions)
            return
        if lookback_seconds == 0:
            await consumer.seek_to_end(*partitions)
            return
        timestamp_ms = int((time.time() - lookback_seconds) * 1000)
        offsets = await consumer.offsets_for_times({partition: timestamp_ms for partition in partitions})
        for partition in partitions:
            offset = offsets.get(partition)
            if offset is None:
                end = await consumer.end_offsets([partition])
                consumer.seek(partition, end[partition])
            else:
                consumer.seek(partition, offset.offset)

    @staticmethod
    async def _poll(
        consumer: AIOKafkaConsumer,
        request: KafkaConsumeRequest,
        *,
        deadline: float | None = None,
    ) -> KafkaConsumeResult:
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + request.timeout_seconds
        matched: list[KafkaRecord] = []
        decode_errors: list[dict[str, Any]] = []
        diagnostic_offsets: set[tuple[int, int]] = set()
        decode_errors_truncated = False
        size_bytes = 0
        while len(matched) < request.max_records:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(remaining):
                    batches = await consumer.getmany(
                        timeout_ms=max(1, min(500, int(remaining * 1000))),
                        max_records=request.max_records,
                    )
            except TimeoutError:
                break
            for records in batches.values():
                if len(matched) >= request.max_records:
                    break
                for record in records:
                    if request.after_cursor is not None and (
                        record.partition not in request.after_cursor.offsets
                        or record.offset < request.after_cursor.offsets[record.partition]
                    ):
                        continue
                    if request.key_equals is not None and record.key != request.key_equals:
                        continue
                    if request.value_contains is not None and request.value_contains not in (record.value or b""):
                        continue
                    if request.json_fields_equal:
                        try:
                            fields_match = _matches_value_fields(
                                record.value,
                                request.json_fields_equal,
                                request.value_decoder,
                            )
                        except Exception:
                            position = (record.partition, record.offset)
                            if len(decode_errors) < request.max_records and position not in diagnostic_offsets:
                                raw_value = record.value or b""
                                preview = raw_value[:4096]
                                if size_bytes + len(preview) > request.max_bytes:
                                    return KafkaConsumeResult(
                                        matched, decode_errors, True, size_bytes, True, "max_bytes"
                                    )
                                decode_errors.append(
                                    {
                                        "partition": record.partition,
                                        "offset": record.offset,
                                        "code": "KAFKA_VALUE_DECODE_FAILED",
                                        "value": to_jsonable(preview),
                                        "value_size_bytes": len(raw_value),
                                        "value_sha256": hashlib.sha256(raw_value).hexdigest(),
                                        "value_truncated": len(raw_value) > 4096,
                                    }
                                )
                                diagnostic_offsets.add(position)
                                size_bytes += len(preview)
                            elif position not in diagnostic_offsets:
                                decode_errors_truncated = True
                            continue
                        if not fields_match:
                            continue
                    record_size = kafka_record_size(record)
                    if size_bytes + record_size > request.max_bytes:
                        return KafkaConsumeResult(
                            matched, decode_errors, decode_errors_truncated, size_bytes, True, "max_bytes"
                        )
                    size_bytes += record_size
                    matched.append(
                        KafkaRecord(
                            topic=record.topic,
                            partition=record.partition,
                            offset=record.offset,
                            timestamp_ms=record.timestamp,
                            key=record.key,
                            value=record.value,
                            headers=list(record.headers),
                        )
                    )
                    if request.stop_after_matches is not None and len(matched) >= request.stop_after_matches:
                        return KafkaConsumeResult(
                            matched, decode_errors, decode_errors_truncated, size_bytes, True, "stop_after_matches"
                        )
                    if len(matched) >= request.max_records:
                        break
        limit_reached = len(matched) >= request.max_records
        return KafkaConsumeResult(
            matched,
            decode_errors,
            decode_errors_truncated,
            size_bytes,
            limit_reached,
            "max_records" if limit_reached else None,
        )


def _matches_value_fields(
    value: bytes | None,
    matches: tuple[KafkaJsonFieldMatch, ...],
    decoder: ValueDecoder | None = None,
) -> bool:
    if value is None:
        return False
    decoded = decoder.decode(value) if decoder is not None else json.loads(value.decode("utf-8"))
    for match in matches:
        current: Any = decoded
        for component in match.path:
            if not isinstance(current, Mapping) or component not in current:
                return False
            current = current[component]
        if current != match.expected:
            return False
    return True
