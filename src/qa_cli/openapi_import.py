"""Conservative local OpenAPI import: reviewable read-only registry drafts only."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import ValidationError

from qa_runtime.compiler import CatalogCompiler, HandlerRegistry
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.handlers.http import HttpOperationHandler
from qa_runtime.models import HttpServiceConfig, OperationDefinition, ParameterDefinition, Profile, RegistryDocument
from qa_runtime.registry import OperationRegistry
from qa_runtime.security import SENSITIVE_KEYS

_METHODS = {"get", "head", "options", "post", "put", "patch", "delete", "trace"}
_READ_METHODS = {"get", "head", "options"}
_SCHEMA_FIELDS = {
    "type",
    "format",
    "enum",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "title",
    "description",
    "default",
    "example",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
    "nullable",
}


class _Unsupported(ValueError):
    pass


class _OpenApiLoader(yaml.SafeLoader):
    """Retain source decimal precision until representability has been checked."""


def _decimal_value(loader: _OpenApiLoader, node: yaml.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node).replace("_", ""))


_OpenApiLoader.add_constructor("tag:yaml.org,2002:float", _decimal_value)


class _NoIO:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"OpenAPI draft compilation attempted I/O: {name}")


def _load_source(raw: bytes) -> Any:
    try:
        return json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError:
        return yaml.load(raw, Loader=_OpenApiLoader)


class _LocalResolver:
    def __init__(self, source: Path, document: dict[str, Any], raw: bytes, root: Path | None) -> None:
        self.source = source.resolve()
        self.root = root.expanduser().resolve() if root is not None else None
        if self.root is not None and (not self.root.is_dir() or not self.source.is_relative_to(self.root)):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "CONTRACT_ROOT_INVALID",
                "contract root must be a directory containing the source document",
                source="authoring",
            )
        if len(raw) > 32 * 1024 * 1024:
            raise _Unsupported("source document exceeds the 32 MiB total size limit")
        self.documents: dict[Path, Any] = {self.source: document}
        self.hashes = {self.source: hashlib.sha256(raw).hexdigest()}
        self.origins: dict[int, Path] = {}
        self.total_bytes = len(raw)
        self._register(document, self.source)

    def _register(self, document: Any, source: Path) -> None:
        pending = [(document, 0)]
        seen: set[int] = set()
        origins: dict[int, Path] = {}
        while pending:
            value, depth = pending.pop()
            if not isinstance(value, (dict, list)) or id(value) in seen:
                continue
            if depth > 128 or len(seen) >= 100_000:
                raise _Unsupported("contract document exceeds structure limits")
            seen.add(id(value))
            origins[id(value)] = source
            pending.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))
        self.origins.update(origins)

    def _document(self, source: Path) -> Any:
        if source in self.documents:
            return self.documents[source]
        if len(self.hashes) >= 64:
            raise _Unsupported("contract references exceed the 64-file limit")
        if self.total_bytes >= 32 * 1024 * 1024:
            raise _Unsupported("contract references exceed the 32 MiB total size limit")
        try:
            with source.open("rb") as stream:
                raw = stream.read(32 * 1024 * 1024 - self.total_bytes + 1)
            self.total_bytes += len(raw)
            if self.total_bytes > 32 * 1024 * 1024:
                raise _Unsupported("contract references exceed the 32 MiB total size limit")
            self.hashes[source] = hashlib.sha256(raw).hexdigest()
            document = _load_source(raw)
            self._register(document, source)
        except (OSError, ValueError, TypeError, InvalidOperation, yaml.YAMLError) as error:
            if isinstance(error, _Unsupported):
                raise
            raise _Unsupported("cannot read or parse referenced contract file") from error
        self.documents[source] = document
        return document

    def resolve(self, value: Any) -> dict[str, Any]:
        visited: set[tuple[Path, str]] = set()
        while isinstance(value, dict) and "$ref" in value:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise _Unsupported("$ref must be a string")
            if set(value) - {"$ref", "summary", "description"}:
                raise _Unsupported("$ref with structural siblings requires manual resolution")
            try:
                parsed = urlsplit(reference)
            except ValueError as error:
                raise _Unsupported("invalid reference URI") from error
            if parsed.scheme or parsed.netloc or parsed.query:
                raise _Unsupported("remote/external URI $ref requires manual resolution; network access is disabled")
            current = self.origins.get(id(value), self.source)
            if parsed.path:
                if self.root is None:
                    raise _Unsupported("external file $ref requires explicit --contract-root")
                relative = Path(unquote(parsed.path))
                if relative.is_absolute():
                    raise _Unsupported("absolute file $ref requires a relative path inside contract root")
                try:
                    target_file = (current.parent / relative).resolve()
                except (OSError, RuntimeError, ValueError) as error:
                    raise _Unsupported("cannot resolve local file $ref") from error
                if not target_file.is_relative_to(self.root):
                    raise _Unsupported("file $ref escapes contract root (including symlinks)")
            else:
                target_file = current
            fragment = unquote(parsed.fragment)
            if fragment and not fragment.startswith("/"):
                raise _Unsupported("non-pointer $ref requires manual resolution")
            key = target_file, fragment
            if key in visited or len(visited) >= 32:
                raise _Unsupported("cyclic or excessively deep $ref")
            visited.add(key)
            target = self._document(target_file)
            try:
                for part in fragment[1:].split("/") if fragment else ():
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError, IndexError) as error:
                raise _Unsupported("unresolved local $ref") from error
            value = target
        if not isinstance(value, dict):
            raise _Unsupported("expected an object")
        return value


def _parameter(resolver: _LocalResolver, value: Any) -> tuple[str, dict[str, Any]]:
    parameter = resolver.resolve(value)
    location = parameter.get("in")
    name = parameter.get("name")
    if location not in {"path", "query", "header"} or not isinstance(name, str) or not name.strip():
        raise _Unsupported("only named path/query/header parameters are supported")
    normalized = "".join(character for character in name.lower() if character.isalnum())
    if normalized in SENSITIVE_KEYS or normalized in {"xapikey", "xauthtoken"}:
        raise _Unsupported("credential parameters require explicit CredentialSource/auth composition")
    if location == "header" and name.lower() in {"x-qa-run-id", "accept", "content-type"}:
        raise _Unsupported("runtime-managed header requires manual review")
    default_style = "form" if location == "query" else "simple"
    if parameter.get("style", default_style) != default_style:
        raise _Unsupported("non-default parameter style requires manual serialization")
    if parameter.get("allowReserved") or parameter.get("allowEmptyValue") or "content" in parameter:
        raise _Unsupported("parameter content/allowReserved/allowEmptyValue requires manual review")
    if location == "path" and parameter.get("required") is not True:
        raise _Unsupported("path parameters must declare required: true")
    schema = resolver.resolve(parameter.get("schema"))
    unsupported = sorted(key for key in schema if key not in _SCHEMA_FIELDS and not key.startswith("x-"))
    if unsupported:
        raise _Unsupported(f"unsupported input schema constraints: {', '.join(unsupported)}")
    if schema.get("type") not in {"string", "integer", "number"} or schema.get("nullable"):
        raise _Unsupported("only non-nullable string/integer/number parameters are supported")
    if schema.get("format") not in {None, "uuid", "date", "date-time"}:
        raise _Unsupported("unsupported parameter format")
    result = {
        "location": location,
        "name": name,
        "required": parameter.get("required", False),
        "type": schema["type"],
    }
    for key in ("enum", "minLength", "maxLength", "minimum", "maximum"):
        if key in schema:
            result[key] = schema[key]
    if schema.get("format") in {"uuid", "date", "date-time"}:
        result["format"] = schema["format"]
    for bound in ("Minimum", "Maximum"):
        value = schema.get(f"exclusive{bound}")
        if isinstance(value, bool):
            result[f"exclusive{bound}"] = value
        elif isinstance(value, (int, float, Decimal)):
            # OpenAPI 3.1 uses a numeric exclusive bound; the runtime uses a flag.
            ordinary = result.get(bound.lower())
            if ordinary is not None:
                raise _Unsupported("combined inclusive/exclusive numeric bounds require manual review")
            result[bound.lower()] = value
            result[f"exclusive{bound}"] = True
        elif value is not None:
            raise _Unsupported("invalid exclusive numeric bound")
    validated = ParameterDefinition.model_validate(result)
    for bound_name in ("minimum", "maximum"):
        if bound_name in result:
            if isinstance(result[bound_name], bool) or not isinstance(result[bound_name], (int, float, Decimal)):
                raise _Unsupported("numeric bounds must be numbers")
            original = Decimal(str(result[bound_name]))
            effective = Decimal(str(getattr(validated, bound_name)))
            if not original.is_finite() or original != effective:
                raise _Unsupported("numeric bound loses precision in the runtime; author manually")
    if (
        location == "header"
        and name.lower() == "traceparent"
        and (result["type"] != "string" or set(result) - {"location", "name", "required", "type"})
    ):
        raise _Unsupported("constraints on runtime-managed traceparent require manual review")
    # Examples/default values are deliberately never copied from external documents.
    return name, result


def _operation(
    document: dict[str, Any],
    resolver: _LocalResolver,
    path: str,
    method: str,
    item: dict[str, Any],
    operation: dict[str, Any],
    service: str,
) -> tuple[OperationDefinition, list[str]]:
    if method not in _READ_METHODS:
        raise _Unsupported("mutating/TRACE methods are outside the read-only import subset")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise _Unsupported("path must be an absolute API path without query or fragment")
    if "requestBody" in operation or "callbacks" in operation:
        raise _Unsupported("requestBody/callbacks require manual operation authoring")
    operation_id = operation.get("operationId")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise _Unsupported("operationId is required to keep source identity explicit")
    if operation_id != operation_id.strip():
        raise _Unsupported("operationId with surrounding whitespace requires manual source correction")
    by_wire: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for group in (item.get("parameters", []), operation.get("parameters", [])):
        if not isinstance(group, list):
            raise _Unsupported("parameters must be an array")
        group_keys: set[tuple[str, str]] = set()
        for raw in group:
            name, parameter = _parameter(resolver, raw)
            key = (parameter["location"], name)
            if key in group_keys:
                raise _Unsupported("duplicate parameters in one scope")
            group_keys.add(key)
            by_wire[key] = name, parameter
    parameters: dict[str, Any] = {}
    runtime_parameters: list[str] = []
    for name, parameter in by_wire.values():
        if parameter["location"] == "header" and name.lower() == "traceparent":
            runtime_parameters.append(name)
            continue
        if name in parameters:
            raise _Unsupported("same parameter name in multiple locations requires explicit CLI naming")
        parameters[name] = parameter
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise _Unsupported("responses must declare explicit successful HTTP status codes")
    if any(str(code).upper() == "2XX" for code in responses):
        raise _Unsupported("2XX response ranges require explicit status review")
    statuses = sorted({int(str(code)) for code in responses if re.fullmatch(r"2\d\d", str(code))})
    if not statuses:
        raise _Unsupported("no explicit successful HTTP status code")
    info = document["info"]
    definition = OperationDefinition.model_validate(
        {
            "id": operation_id,
            "kind": "http",
            "purpose": operation.get("summary") or f"{method.upper()} {path}",
            "mutating": False,
            "admission": "prototype",
            "parameters": parameters,
            "contractRef": {
                "artifact": info["title"],
                "version": info["version"],
                "operationId": operation_id,
                "provenance": "unverified",
            },
            "config": {"service": service, "method": method.upper(), "path": path},
        }
    )
    return definition, runtime_parameters


def _profile(document: dict[str, Any], service: str, operations: list[dict[str, Any]]) -> tuple[dict | None, str]:
    if not operations:
        return None, "no supported operations were imported"
    if any(item["requiresAuth"] for item in operations):
        return None, "auth composition must be configured explicitly"
    if any(item["serverOverride"] for item in operations):
        return None, "path/operation servers require explicit service mapping"
    servers = document.get("servers", [])
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict):
        return None, "one unambiguous HTTPS server is required"
    server = servers[0]
    url = server.get("url")
    if not isinstance(url, str) or server.get("variables") or "{" in url:
        return None, "server variables require explicit values"
    try:
        parsed = urlsplit(url)
        HttpServiceConfig(baseUrl=url)
    except (ValueError, ValidationError):
        return None, "invalid HTTPS server URL"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None, "server must be HTTPS without credentials/query/fragment"
    return {"environment": "unconfigured", "services": {service: {"baseUrl": url}}}, "draft; verify QA endpoint and TLS"


def import_openapi(
    source: Path, destination: Path, service: str, *, contract_root: Path | None = None
) -> dict[str, Any]:
    """Write local drafts and a complete skipped-operation report into a new directory."""
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", service):
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT, "SERVICE_ID_INVALID", "service must be a lowercase identifier", source="authoring"
        )
    try:
        source = source.expanduser()
        with source.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        document = _load_source(raw)
        if not isinstance(document, dict) or not re.fullmatch(r"3\.[01]\.\d+", str(document.get("openapi", ""))):
            raise ValueError("expected OpenAPI 3.0 or 3.1")
        info = document.get("info")
        if not isinstance(info, dict) or any(
            not isinstance(info.get(key), str) or not info[key].strip() for key in ("title", "version")
        ):
            raise ValueError("info.title and info.version are required")
        if not isinstance(document.get("paths"), dict):
            raise ValueError("paths must be an object")
        resolver = _LocalResolver(source, document, raw, contract_root)
    except (OSError, ValueError, TypeError, InvalidOperation, yaml.YAMLError) as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "OPENAPI_INVALID",
            "cannot read a valid local OpenAPI 3.0/3.1 document",
            source="authoring",
        ) from error

    operations: list[OperationDefinition] = []
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    candidates: list[tuple[str, str, dict, dict]] = []
    for path, raw_item in document["paths"].items():
        if str(path).startswith("x-"):
            continue
        try:
            item = resolver.resolve(raw_item)
        except _Unsupported as error:
            skipped.append({"path": str(path), "method": "*", "reason": str(error)})
            continue
        for method in _METHODS:
            if method in item:
                candidates.append((str(path), method, item, item[method]))
    ids = Counter(str(operation.get("operationId")) for _, _, _, operation in candidates if isinstance(operation, dict))
    compile_profile = Profile.model_validate({"services": {service: {"baseUrl": "https://import.invalid"}}})
    for path, method, item, raw_operation in sorted(candidates, key=lambda item: (item[0], item[1])):
        try:
            if isinstance(raw_operation, dict) and "$ref" in raw_operation:
                raise _Unsupported("operation-level $ref is outside the supported subset")
            operation = resolver.resolve(raw_operation)
            if ids[str(operation.get("operationId"))] > 1:
                raise _Unsupported("duplicate operationId requires manual disambiguation")
            definition, runtime_parameters = _operation(document, resolver, path, method, item, operation, service)
            registry = OperationRegistry(
                RegistryDocument(apiVersion="qa.runtime/v1", kind="OperationRegistry", operations=[definition])
            )
            CatalogCompiler().compile(registry, compile_profile, HandlerRegistry([HttpOperationHandler(_NoIO())]))
        except (_Unsupported, ValidationError, RuntimeFailure) as error:
            reason = (
                str(error)
                if isinstance(error, _Unsupported)
                else "declaration/bindings fail runtime validation; author manually"
            )
            skipped.append({"path": path, "method": method.upper(), "reason": reason})
            continue
        operations.append(definition)
        imported.append(
            {
                "operationId": definition.id,
                "path": path,
                "method": method.upper(),
                "requiresAuth": bool(operation.get("security", document.get("security", []))),
                "serverOverride": "servers" in operation or "servers" in item,
                "runtimeManagedParameters": runtime_parameters,
            }
        )
    # Check the combined document too: per-operation compile cannot detect duplicate IDs.
    OperationRegistry(RegistryDocument(apiVersion="qa.runtime/v1", kind="OperationRegistry", operations=operations))
    profile, profile_reason = _profile(document, service, imported)
    destination = destination.expanduser().absolute()
    report: dict[str, Any] = {
        "source": str(source.expanduser().absolute()),
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "contractRoot": str(resolver.root) if resolver.root is not None else None,
        "sourceFiles": [{"path": str(path), "sha256": checksum} for path, checksum in sorted(resolver.hashes.items())],
        "provenance": "unverified",
        "directory": str(destination),
        "service": service,
        "imported": imported,
        "skipped": skipped,
        "importedCount": len(imported),
        "skippedCount": len(skipped),
        "profile": "profiles/imported.yaml" if profile is not None else None,
        "profileReason": profile_reason,
        "limitations": [
            "Drafts only: source authenticity, deployed version and business behavior are not verified.",
            "Only GET/HEAD/OPTIONS with supported scalar inputs; all unsupported operations are reported.",
            "Parameter examples/default values and response schemas are not imported.",
            "Review response sensitivity and configure responseBodyFields where required.",
            "Schema/compile success is not business correctness or authorization for live I/O.",
        ],
        "nextSteps": [
            "Compare every imported declaration and skipped reason with the canonical contract and requirements.",
            "Move reviewed registry YAML into the product operations directory; add guides and fake transport tests.",
            "Configure QA endpoints, auth/TLS and credentials explicitly, then run config validate.",
        ],
    }
    files = {
        "registry.yaml": yaml.safe_dump(
            RegistryDocument(apiVersion="qa.runtime/v1", kind="OperationRegistry", operations=operations).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            ),
            allow_unicode=True,
            sort_keys=False,
        ),
        "import-report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    }
    if profile is not None:
        files["profiles/imported.yaml"] = yaml.safe_dump(profile, sort_keys=False)
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT, "IMPORT_DIRECTORY_EXISTS", "import directory already exists", source="authoring"
        ) from error
    except OSError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT, "IMPORT_DIRECTORY_UNAVAILABLE", "cannot create import directory", source="authoring"
        ) from error
    try:
        for relative, contents in files.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(contents)
    except OSError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "OPENAPI_IMPORT_WRITE_FAILED",
            "could not finish writing import; inspect the partial directory",
            source="authoring",
        ) from error
    return report
