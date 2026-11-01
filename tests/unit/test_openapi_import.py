import hashlib
import json
from pathlib import Path

import pytest
import yaml

from qa_cli.openapi_import import import_openapi
from qa_runtime.errors import RuntimeFailure
from qa_runtime.registry import OperationRegistry


def _document(**changes):
    return {
        "openapi": "3.0.3",
        "info": {"title": "Customers", "version": "1.2.3"},
        "servers": [{"url": "https://customers.invalid/api"}],
        "paths": {
            "/customers/{customerId}": {
                "parameters": [{"$ref": "#/components/parameters/CustomerId"}],
                "get": {"operationId": "getCustomer", "responses": {"200": {"description": "Found"}}},
            }
        },
        "components": {
            "parameters": {
                "CustomerId": {
                    "name": "customerId",
                    "in": "path",
                    "required": True,
                    "schema": {"$ref": "#/components/schemas/CustomerId"},
                }
            },
            "schemas": {"CustomerId": {"type": "string", "format": "uuid"}},
        },
        **changes,
    }


def _import(tmp_path: Path, document: dict):
    source = tmp_path / "openapi.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    destination = tmp_path / "draft"
    return import_openapi(source, destination, "customers"), destination, source


def test_import_resolves_local_refs_and_produces_explicit_unverified_draft(tmp_path) -> None:
    report, destination, source = _import(tmp_path, _document())
    registry = OperationRegistry.load(destination / "registry.yaml")
    operation = registry.get("getCustomer")
    assert operation.mutating is False
    assert operation.admission == "prototype"
    assert operation.parameters["customerId"].format == "uuid"
    assert operation.parameters["customerId"].wire_name == "customerId"
    assert operation.contract_ref.provenance == "unverified"
    assert operation.contract_ref.source_revision is None
    assert operation.contract_ref.checksum_sha256 is None
    assert report["sourceSha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["importedCount"] == 1
    assert report["skippedCount"] == 0
    assert json.loads((destination / "import-report.json").read_text()) == report
    profile = yaml.safe_load((destination / "profiles/imported.yaml").read_text())
    assert profile["environment"] == "unconfigured"
    assert profile["services"]["customers"]["baseUrl"] == "https://customers.invalid/api"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"requestBody": {}}, "requestBody"),
        ({"operationId": None}, "operationId is required"),
        (
            {"parameters": [{"in": "query", "name": "ids", "schema": {"type": "array", "items": {"type": "string"}}}]},
            "unsupported input schema",
        ),
        (
            {"parameters": [{"in": "query", "name": "code", "schema": {"type": "string", "pattern": "[A-Z]+"}}]},
            "pattern",
        ),
        ({"parameters": [{"in": "header", "name": "Authorization", "schema": {"type": "string"}}]}, "CredentialSource"),
        (
            {"parameters": [{"in": "query", "name": "q", "schema": {"$ref": "https://contracts.invalid/schema"}}]},
            "external",
        ),
        ({"responses": {"2XX": {}}}, "2XX response"),
    ],
)
def test_unsupported_operation_is_reported_instead_of_silently_weakened(tmp_path, change, reason) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"]["get"].update(change)
    report, destination, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert report["skippedCount"] == 1
    assert reason in report["skipped"][0]["reason"]
    assert OperationRegistry.load(destination / "registry.yaml").ids() == []


def test_import_reports_mutations_and_preserves_supported_read_operation(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers"] = {
        "post": {"operationId": "createCustomer", "responses": {"201": {"description": "Created"}}}
    }
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == report["skippedCount"] == 1
    assert report["skipped"][0]["method"] == "POST"
    assert "read-only import subset" in report["skipped"][0]["reason"]


def test_import_uses_runtime_compiler_for_invalid_path_bindings(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"].pop("parameters")
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert "bindings fail runtime validation" in report["skipped"][0]["reason"]


def test_import_never_copies_example_or_default_values(tmp_path) -> None:
    document = _document()
    document["components"]["schemas"]["CustomerId"].update({"default": "secret-default", "example": "secret-example"})
    report, destination, _ = _import(tmp_path, document)
    assert report["importedCount"] == 1
    assert "secret-default" not in (destination / "registry.yaml").read_text()
    assert "secret-example" not in (destination / "registry.yaml").read_text()


def test_operation_parameters_override_path_scope_with_same_wire_name(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "customerId", "in": "path", "required": True, "schema": {"type": "string", "minLength": 1}}
    ]
    report, destination, _ = _import(tmp_path, document)
    parameter = OperationRegistry.load(destination / "registry.yaml").get("getCustomer").parameters["customerId"]
    assert report["importedCount"] == 1
    assert parameter.format is None
    assert parameter.min_length == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"servers": [{"url": "http://customers.invalid"}]},
        {"servers": [{"url": "https://one.invalid"}, {"url": "https://two.invalid"}]},
        {"servers": [{"url": "https://user:password@customers.invalid"}]},
        {
            "servers": [
                {"url": "https://customers.invalid/{environment}", "variables": {"environment": {"default": "qa"}}}
            ]
        },
        {"security": [{"bearerAuth": []}]},
    ],
)
def test_import_avoids_ambiguous_or_auth_incomplete_profile(tmp_path, changes) -> None:
    report, destination, _ = _import(tmp_path, _document(**changes))
    assert report["importedCount"] == 1
    assert report["profile"] is None
    assert not (destination / "profiles").exists()


def test_duplicate_operation_ids_are_all_reported_and_not_arbitrarily_chosen(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers"] = {"get": {"operationId": "getCustomer", "responses": {"200": {}}}}
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert report["skippedCount"] == 2
    assert all("duplicate operationId" in skipped["reason"] for skipped in report["skipped"])


def test_whitespace_operation_id_is_not_silently_normalized_into_duplicate(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers"] = {"get": {"operationId": " getCustomer ", "responses": {"200": {}}}}
    report, destination, _ = _import(tmp_path, document)
    assert report["importedCount"] == 1
    assert report["skippedCount"] == 1
    assert "whitespace" in report["skipped"][0]["reason"]
    assert OperationRegistry.load(destination / "registry.yaml").ids() == ["getCustomer"]


def test_operation_refs_are_reported_instead_of_bypassing_duplicate_detection(tmp_path) -> None:
    document = _document()
    document["paths"]["/copy"] = {"get": {"$ref": "#/paths/~1customers~1{customerId}/get"}}
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 1
    assert report["skippedCount"] == 1
    assert "operation-level $ref" in report["skipped"][0]["reason"]


def test_cyclic_local_ref_is_reported_without_recursion(tmp_path) -> None:
    document = _document()
    document["components"]["schemas"]["CustomerId"] = {"$ref": "#/components/schemas/CustomerId"}
    report, _, _ = _import(tmp_path, document)
    assert "cyclic" in report["skipped"][0]["reason"]


def test_numeric_exclusive_bounds_from_openapi_31_are_preserved(tmp_path) -> None:
    document = _document(openapi="3.1.0")
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "exclusiveMinimum": 0, "maximum": 100}}
    ]
    report, destination, _ = _import(tmp_path, document)
    parameter = OperationRegistry.load(destination / "registry.yaml").get("getCustomer").parameters["limit"]
    assert report["importedCount"] == 1
    assert parameter.minimum == 0
    assert parameter.exclusive_minimum is True
    assert parameter.maximum == 100


@pytest.mark.parametrize("numeric_format", ["int32", "int64", "float", "double"])
def test_numeric_formats_are_not_silently_dropped(tmp_path, numeric_format) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "format": numeric_format}}
    ]
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert "unsupported parameter format" in report["skipped"][0]["reason"]


@pytest.mark.parametrize("bound", ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"])
def test_numeric_bounds_must_be_representable_without_precision_loss(tmp_path, bound) -> None:
    document = _document(openapi="3.1.0")
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", bound: 9007199254740993}}
    ]
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert "loses precision" in report["skipped"][0]["reason"]


@pytest.mark.parametrize("source_format", ["yaml", "json"])
def test_decimal_precision_is_not_lost_during_source_parsing(tmp_path, source_format) -> None:
    document = _document(openapi="3.1.0")
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "number", "minimum": "EXACT_DECIMAL"}}
    ]
    if source_format == "json":
        raw = json.dumps(document).replace('"EXACT_DECIMAL"', "0.10000000000000001")
    else:
        raw = yaml.safe_dump(document).replace("EXACT_DECIMAL", "0.10000000000000001")
    source = tmp_path / f"openapi.{source_format}"
    source.write_text(raw, encoding="utf-8")
    report = import_openapi(source, tmp_path / "draft", "customers")
    assert report["importedCount"] == 0
    assert "loses precision" in report["skipped"][0]["reason"]


def test_import_refuses_overwrite(tmp_path) -> None:
    _, destination, source = _import(tmp_path, _document())
    content = (destination / "registry.yaml").read_bytes()
    with pytest.raises(RuntimeFailure) as raised:
        import_openapi(source, destination, "customers")
    assert raised.value.code == "IMPORT_DIRECTORY_EXISTS"
    assert (destination / "registry.yaml").read_bytes() == content


def test_invalid_source_fails_before_creating_output(tmp_path) -> None:
    source = tmp_path / "invalid.yaml"
    source.write_text("not an OpenAPI document", encoding="utf-8")
    with pytest.raises(RuntimeFailure) as raised:
        import_openapi(source, tmp_path / "draft", "customers")
    assert raised.value.code == "OPENAPI_INVALID"
    assert not (tmp_path / "draft").exists()


def _file_contract(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "contracts"
    (root / "entry").mkdir(parents=True)
    (root / "definitions/nested").mkdir(parents=True)
    document = _document()
    document["paths"]["/customers/{customerId}"]["parameters"] = [
        {"$ref": "../definitions/parameters.yaml#/CustomerId"}
    ]
    source = root / "entry/openapi.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    (root / "definitions/parameters.yaml").write_text(
        "CustomerId:\n  name: customerId\n  in: path\n  required: true\n"
        "  schema: {$ref: 'nested/schema.yaml#/Identifier'}\n",
        encoding="utf-8",
    )
    (root / "definitions/nested/schema.yaml").write_text(
        "Identifier: {$ref: '#/Uuid'}\nUuid: {type: string, format: uuid}\n", encoding="utf-8"
    )
    return root, source


def test_explicit_contract_root_resolves_nested_file_and_same_document_refs(tmp_path) -> None:
    root, source = _file_contract(tmp_path)
    destination = tmp_path / "draft"
    report = import_openapi(source, destination, "customers", contract_root=root)
    operation = OperationRegistry.load(destination / "registry.yaml").get("getCustomer")
    assert report["importedCount"] == 1
    assert report["skippedCount"] == 0
    assert operation.parameters["customerId"].format == "uuid"
    assert report["contractRoot"] == str(root.resolve())
    assert len(report["sourceFiles"]) == 3
    assert all(
        item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() for item in report["sourceFiles"]
    )
    assert report["sourceSha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_file_refs_require_explicit_root(tmp_path) -> None:
    _, source = _file_contract(tmp_path)
    report = import_openapi(source, tmp_path / "draft", "customers")
    assert report["importedCount"] == 0
    assert "explicit --contract-root" in report["skipped"][0]["reason"]
    assert len(report["sourceFiles"]) == 1


def test_constraints_in_referenced_files_are_not_silently_dropped(tmp_path) -> None:
    root, source = _file_contract(tmp_path)
    (root / "definitions/nested/schema.yaml").write_text(
        "Identifier: {type: string, pattern: '[A-Z]+'}\n", encoding="utf-8"
    )
    report = import_openapi(source, tmp_path / "draft", "customers", contract_root=root)
    assert report["importedCount"] == 0
    assert "pattern" in report["skipped"][0]["reason"]


@pytest.mark.parametrize("escape", ["traversal", "symlink", "remote", "file-uri"])
def test_refs_cannot_read_outside_explicit_root_or_use_network(tmp_path, escape) -> None:
    root, source = _file_contract(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("Identifier: {type: string, format: uuid}\n", encoding="utf-8")
    if escape == "traversal":
        reference = "../../outside.yaml#/Identifier"
    elif escape == "symlink":
        (root / "definitions/link.yaml").symlink_to(outside)
        reference = "link.yaml#/Identifier"
    elif escape == "remote":
        reference = "https://contracts.invalid/schema.yaml#/Identifier"
    else:
        reference = outside.as_uri() + "#/Identifier"
    (root / "definitions/parameters.yaml").write_text(
        f"CustomerId:\n  name: customerId\n  in: path\n  required: true\n  schema: {{$ref: '{reference}'}}\n",
        encoding="utf-8",
    )
    report = import_openapi(source, tmp_path / "draft", "customers", contract_root=root)
    assert report["importedCount"] == 0
    assert report["skippedCount"] == 1
    assert len(report["sourceFiles"]) == 2
    assert all(Path(item["path"]).is_relative_to(root) for item in report["sourceFiles"])
    assert any(
        term in report["skipped"][0]["reason"] for term in ("escapes contract root", "network access is disabled")
    )


def test_cross_file_ref_cycles_are_bounded(tmp_path) -> None:
    root, source = _file_contract(tmp_path)
    (root / "definitions/nested/schema.yaml").write_text(
        "Identifier: {$ref: '../cycle.yaml#/Identifier'}\n", encoding="utf-8"
    )
    (root / "definitions/cycle.yaml").write_text(
        "Identifier: {$ref: 'nested/schema.yaml#/Identifier'}\n", encoding="utf-8"
    )
    report = import_openapi(source, tmp_path / "draft", "customers", contract_root=root)
    assert report["importedCount"] == 0
    assert "cyclic" in report["skipped"][0]["reason"]
    assert len(report["sourceFiles"]) == 4


def test_contract_root_must_contain_source_document(tmp_path) -> None:
    root, source = _file_contract(tmp_path)
    with pytest.raises(RuntimeFailure) as raised:
        import_openapi(source, tmp_path / "draft", "customers", contract_root=root / "definitions")
    assert raised.value.code == "CONTRACT_ROOT_INVALID"
    assert not (tmp_path / "draft").exists()


def test_file_reference_chain_has_fixed_depth_limit(tmp_path) -> None:
    root, source = _file_contract(tmp_path)
    (root / "definitions/nested/schema.yaml").write_text("Identifier: {$ref: '0.yaml#/Identifier'}\n", encoding="utf-8")
    for index in range(35):
        (root / f"definitions/nested/{index}.yaml").write_text(
            f"Identifier: {{$ref: '{index + 1}.yaml#/Identifier'}}\n", encoding="utf-8"
        )
    report = import_openapi(source, tmp_path / "draft", "customers", contract_root=root)
    assert report["importedCount"] == 0
    assert "excessively deep" in report["skipped"][0]["reason"]
    assert len(report["sourceFiles"]) <= 34


def test_runtime_traceparent_is_reported_without_becoming_caller_input(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "traceparent", "in": "header", "required": True, "schema": {"type": "string"}}
    ]
    report, destination, _ = _import(tmp_path, document)
    assert report["importedCount"] == 1
    assert report["imported"][0]["runtimeManagedParameters"] == ["traceparent"]
    assert "traceparent" not in OperationRegistry.load(destination / "registry.yaml").get("getCustomer").parameters


def test_runtime_traceparent_constraints_are_not_silently_discarded(tmp_path) -> None:
    document = _document()
    document["paths"]["/customers/{customerId}"]["get"]["parameters"] = [
        {"name": "traceparent", "in": "header", "schema": {"type": "string", "enum": ["custom"]}}
    ]
    report, _, _ = _import(tmp_path, document)
    assert report["importedCount"] == 0
    assert "constraints on runtime-managed traceparent" in report["skipped"][0]["reason"]


@pytest.mark.parametrize(
    "url",
    [
        "https://@customers.invalid",
        "https://customers.invalid:bad",
        "https://customers.invalid:0",
        "https://customers.invalid:65536",
        "https://customers.invalid:",
    ],
)
def test_imported_profile_uses_runtime_url_validation(tmp_path, url):
    report, _, _ = _import(tmp_path, _document(servers=[{"url": url}]))
    assert report["importedCount"] == 1
    assert report["profile"] is None
