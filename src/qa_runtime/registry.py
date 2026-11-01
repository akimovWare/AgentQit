from __future__ import annotations

from collections.abc import Iterable
from difflib import get_close_matches
from pathlib import Path

import yaml
from pydantic import ValidationError

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.models import CodecDefinition, OperationDefinition, RegistryDocument


class OperationRegistry:
    def __init__(self, document: RegistryDocument) -> None:
        operations: dict[str, OperationDefinition] = {}
        for operation in document.operations:
            if operation.id in operations:
                raise RuntimeFailure(
                    ExitCode.INVALID_INPUT,
                    "DUPLICATE_OPERATION",
                    f"operation '{operation.id}' is declared more than once",
                    source="registry",
                )
            operations[operation.id] = operation
        self._operations = operations
        self._codecs = dict(document.codecs)

    @classmethod
    def load(cls, paths: Path | Iterable[Path]) -> OperationRegistry:
        files = _resolve_registry_files(paths)
        operations: list[OperationDefinition] = []
        declared_at: dict[str, Path] = {}
        codecs: dict[str, CodecDefinition] = {}
        for path in files:
            document = _load_document(path)
            for name, codec in document.codecs.items():
                if not name.strip() or name != name.strip():
                    raise RuntimeFailure(
                        ExitCode.INVALID_INPUT,
                        "REGISTRY_INVALID",
                        "codec references must be nonempty and trimmed",
                        source="registry",
                    )
                if name in codecs:
                    raise RuntimeFailure(
                        ExitCode.INVALID_INPUT,
                        "DUPLICATE_CODEC",
                        f"codec '{name}' is declared more than once",
                        source="registry",
                    )
                if codec.descriptor_path is not None:
                    descriptor = codec.descriptor_path.expanduser()
                    if not descriptor.is_absolute():
                        descriptor = path.parent / descriptor
                    codec = codec.model_copy(update={"descriptor_path": descriptor.resolve()})
                codecs[name] = codec
            for operation in document.operations:
                previous = declared_at.get(operation.id)
                if previous is not None:
                    raise RuntimeFailure(
                        ExitCode.INVALID_INPUT,
                        "DUPLICATE_OPERATION",
                        f"operation '{operation.id}' is declared in both '{previous}' and '{path}'",
                        source="registry",
                    )
                declared_at[operation.id] = path
                operations.append(operation)
        return cls(
            RegistryDocument(
                apiVersion="qa.runtime/v1",
                kind="OperationRegistry",
                operations=operations,
                codecs=codecs,
            )
        )

    def get(self, operation_id: str) -> OperationDefinition:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            suggestions = get_close_matches(operation_id, self.ids(), n=3, cutoff=0.55)
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "UNKNOWN_OPERATION",
                f"no operation registered with id '{operation_id}'",
                source="registry",
                details={"suggestions": suggestions},
                hint="Inspect 'qa registry list --search <term> --output json' to discover operations.",
            ) from error

    def ids(self) -> list[str]:
        return sorted(self._operations)

    def summaries(
        self,
        *,
        mutating: bool | None = None,
        admissions: set[str] | None = None,
        kinds: set[str] | None = None,
        search: str | None = None,
        services: set[str] | None = None,
    ) -> list[dict[str, object]]:
        available_kinds = {operation.kind for operation in self.operations()}
        invalid_kinds = sorted((kinds or set()) - available_kinds)
        if invalid_kinds:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "INVALID_KIND_FILTER",
                f"unknown operation kinds: {', '.join(invalid_kinds)}",
                source="registry",
                details={"available": sorted(available_kinds)},
            )
        query = (search or "").strip().casefold()
        return [
            {
                "id": operation.id,
                "kind": operation.kind,
                "purpose": operation.purpose,
                "mutating": operation.mutating,
                "admission": operation.admission,
                "requiredParameters": sorted(
                    name for name, parameter in operation.parameters.items() if parameter.required
                ),
                "optionalParameters": sorted(
                    name for name, parameter in operation.parameters.items() if not parameter.required
                ),
            }
            for operation in self.operations()
            if (mutating is None or operation.mutating is mutating)
            and (not admissions or operation.admission in admissions)
            and (not kinds or operation.kind in kinds)
            and (not query or query in operation.id.casefold() or query in operation.purpose.casefold())
            and (not services or (operation.kind == "http" and operation.config.get("service") in services))
        ]

    def operations(self) -> tuple[OperationDefinition, ...]:
        return tuple(self._operations[operation_id] for operation_id in self.ids())

    def codecs(self) -> dict[str, CodecDefinition]:
        return dict(self._codecs)


def _resolve_registry_files(paths: Path | Iterable[Path]) -> tuple[Path, ...]:
    requested = (paths,) if isinstance(paths, Path) else tuple(paths)
    if not requested:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_NOT_FOUND",
            "no registry files were provided",
            source="registry",
        )

    files: list[Path] = []
    for path in requested:
        if path.is_file():
            files.append(path)
            continue
        if path.is_dir():
            discovered = sorted(
                candidate for pattern in ("*.yaml", "*.yml") for candidate in path.rglob(pattern) if candidate.is_file()
            )
            if discovered:
                files.extend(discovered)
                continue
            message = f"registry directory contains no YAML files: {path}"
        else:
            message = f"registry not found at {path}"
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_NOT_FOUND",
            message,
            source="registry",
        )
    return tuple(files)


def _load_document(path: Path) -> RegistryDocument:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return RegistryDocument.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_INVALID",
            f"failed to load registry '{path}': {error}",
            source="registry",
        ) from error
