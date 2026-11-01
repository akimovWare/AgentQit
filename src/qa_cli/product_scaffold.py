"""Generate a product package with catalog, schemas, and offline tests."""

from __future__ import annotations

import json
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from qa_runtime.authoring import registry_json_schema
from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure

_PRODUCT_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

_AUTHORING_RULES = """# Product authoring instructions

This package declares QA capabilities. The agent selects checks from requirements;
the runtime executes individual operations and returns facts.

1. Read requirements and canonical HTTP/AsyncAPI/protobuf contracts at known revisions.
   Service code explains implementation; independently supplied requirements define
   expected behavior. Do not invent paths, wire names, enums, topics, or payloads.
2. Add one operation with purpose, typed and bounded parameters, exact bindings,
   mutating, and admission. Keep unverified declarations at admission: prototype.
   Verified HTTP/Kafka declarations need audited-checkout/sourceRevision or
   pinned-artifact/checksumSha256 metadata; metadata alone does not prove correctness.
3. Kafka publish and HTTP DELETE/PUT/PATCH are mutating. Review POST effects explicitly.
   Supply secrets through CredentialSource references, never ordinary input, profiles,
   catalog YAML, fixtures, logs, or results.
4. Write guides describing effects, preconditions, outcomes, observations, limitations,
   and versioned sources. Link operation_ids and test their references. Guides are
   descriptive: do not add executable steps, automatic scenarios, or business evaluators.
5. Reuse generic handlers. Python is for composition, auth/TLS, or a new handler kind.
   Compile is synchronous with no network or credential access; local trust-file checks
   are allowed. Execution/ports are asynchronous; close resources with aclose().
6. Fill tests/profiles/compile.yaml with nonsecret fixtures for each resource reference,
   using reserved addresses such as https://service.invalid. Run tests/test_product.py;
   extend fake composition for custom handlers/auth/TLS/codecs instead of weakening checks.
7. Test exact requests and responses, invalid input, and mutation admission before I/O.
   JSON Schema helps authoring; CatalogCompiler and behavior tests verify the operation.
   Update README when behavior changes.

Live operations require an explicit user task naming the target and scope. Mutations
also require explicit authorization, --allow-mutation, and environment: qa. Retain
created QA records and report their IDs/effects; delete only when requested and restore
temporary faults. Authoring a plugin alone does not authorize live execution.
"""


_COMPILE_TEST = '''from pathlib import Path

from {package} import product_plugin
from qa_runtime.authoring import validate_product_authoring
from qa_runtime.compiler import CatalogCompiler, HandlerRegistry
from qa_runtime.handlers.database import DatabaseOperationHandler
from qa_runtime.handlers.http import HttpOperationHandler
from qa_runtime.handlers.kafka import KafkaConsumeOperationHandler, KafkaPublishOperationHandler
from qa_runtime.handlers.kafka_offsets import KafkaOffsetsOperationHandler
from qa_runtime.handlers.s3 import S3OperationHandler
from qa_runtime.handlers.kubernetes import KubernetesOperationHandler
from qa_runtime.codecs.catalog import load_codecs
from qa_runtime.profile import ProfileLoader


class NoIO:
    """Any transport access during compilation is an error."""

    def __getattr__(self, name):
        raise AssertionError(f"offline compilation accessed transport: {{name}}")


def test_registry_authoring_and_compilation():
    registry = validate_product_authoring(product_plugin())
    profile = ProfileLoader(Path(__file__).with_name("profiles"), env={{}}).load("compile")
    port = NoIO()
    codecs = load_codecs(registry, registry.operations())
    # Extend with fake auth/TLS/decoders or custom handlers when the product needs them.
    handlers = HandlerRegistry(
        [
            HttpOperationHandler(port),
            DatabaseOperationHandler(port, profile.databases, value_decoders=codecs.decoders),
            KafkaConsumeOperationHandler(port, profile.kafka, value_decoders=codecs.decoders),
            KafkaPublishOperationHandler(port, profile.kafka, value_encoders=codecs.encoders),
            KafkaOffsetsOperationHandler(port, profile.kafka),
            S3OperationHandler(port),
            KubernetesOperationHandler(port),
        ]
    )
    catalog = CatalogCompiler().compile(registry, profile, handlers)
    assert catalog.ids() == registry.ids()
'''


def _runtime_requirement() -> str:
    providers = [
        provider for provider in entry_points(group="console_scripts", name="qa") if provider.value == "qa_cli.app:main"
    ]
    if len(providers) != 1:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "RUNTIME_DISTRIBUTION_AMBIGUOUS" if providers else "RUNTIME_DISTRIBUTION_NOT_FOUND",
            "scaffolding requires exactly one installed distribution providing qa_cli.app:main as the qa command",
            source="authoring",
            hint="Install the intended runtime in this environment and remove duplicate qa command providers.",
        )
    distribution = providers[0].dist
    try:
        name = distribution.metadata["Name"] if distribution is not None else None
        version = distribution.version if distribution is not None else None
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name) is None
            or not isinstance(version, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version) is None
        ):
            raise ValueError("invalid runtime distribution metadata")
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "RUNTIME_DISTRIBUTION_INVALID",
            "the installed qa command provider has no usable distribution name/version metadata",
            source="authoring",
            hint="Reinstall the intended runtime with valid distribution metadata.",
        ) from error
    return f"{name}=={version}"


def create_product_scaffold(destination: Path, product_id: str) -> dict[str, Any]:
    """Create a new directory exclusively; never update or overwrite an existing one."""
    if len(product_id) > 64 or _PRODUCT_ID.fullmatch(product_id) is None:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_ID_INVALID",
            "product id must use lowercase ASCII letters, digits and single hyphens, starting with a letter",
            source="authoring",
        )
    package = f"qa_product_{product_id.replace('-', '_')}"
    distribution = f"qa-product-{product_id}"
    runtime_requirement = _runtime_requirement()
    destination = destination.expanduser().absolute()
    source = f"src/{package}"
    files = {
        "pyproject.toml": f'''[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "{distribution}"
version = "0.1.0"
description = "QA capabilities for {product_id}"
readme = "README.md"
requires-python = ">=3.12"
# Compatible runtime version.
dependencies = [{json.dumps(runtime_requirement)}]

[project.entry-points."qa_runtime.products"]
{product_id} = "{package}:product_plugin"

[dependency-groups]
dev = ["pytest>=8.3,<9"]

[tool.hatch.build.targets.wheel]
packages = ["{source}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
''',
        f"{source}/__init__.py": f'''from pathlib import Path

from {package}.guides import product_guides
from qa_runtime.ports.product import ProductPlugin


def product_plugin() -> ProductPlugin:
    return ProductPlugin(
        product_id="{product_id}",
        registry_path=Path(__file__).with_name("operations"),
        guides=product_guides(),
    )
''',
        f"{source}/guides.py": """from qa_runtime.ports.product import ProductGuide


def product_guides() -> tuple[ProductGuide, ...]:
    # Add source-backed effects, outcomes and observations with operation_ids.
    return ()
""",
        f"{source}/operations/registry.yaml": (
            "# yaml-language-server: $schema=../../../schemas/operation-registry.schema.json\n"
            "apiVersion: qa.runtime/v1\nkind: OperationRegistry\noperations: []\n"
        ),
        "schemas/operation-registry.schema.json": json.dumps(registry_json_schema(), ensure_ascii=False, indent=2)
        + "\n",
        "tests/test_product.py": _COMPILE_TEST.format(package=package),
        "tests/profiles/compile.yaml": "environment: offline\nservices: {}\ndatabases: {}\nkafka: {}\n",
        "AGENTS.md": _AUTHORING_RULES,
        ".gitignore": ".venv/\n__pycache__/\n.pytest_cache/\ndist/\n*.egg-info/\n.env\n/profiles/\n",
        "README.md": f"""# {distribution}

Product capabilities for `{product_id}`. Define reviewed operations in
`{source}/operations/registry.yaml` using AGENTS.md and canonical contracts.

Runtime dependency: `{runtime_requirement}`. Keep the dependency and schema
snapshot aligned when upgrading.

From the runtime checkout, install this plugin into the same environment:

```sh
uv pip install --python .venv/bin/python --no-deps -e /absolute/path/to/{distribution}
uv run pytest /absolute/path/to/{distribution}/tests
uv run qa registry list --product {product_id}
```

Replace the absolute path with this package's directory. Installing the build
backend may access the configured package index. The offline test checks authoring,
references, and compilation through fake ports. Add focused behavior/input/mutation
tests and populate `tests/profiles/compile.yaml` as operations are implemented.
An empty catalog passing validation is not product coverage.

`schemas/operation-registry.schema.json` helps YAML editors; it does not validate
canonical source correctness or all runtime bindings. Refresh it from
`qa registry schema --output json` when the runtime changes.

For the first operation, provide ProductSetup with a nonsecret template or pass
`--template /path/to/profile-template.yaml` to these first two commands:

```sh
uv run qa config requirements --product {product_id} --operation OP
uv run qa config init --product {product_id} --operation OP --profile first
uv run qa registry describe OP --product {product_id}
uv run qa doctor OP --product {product_id} --profile first
uv run qa plan OP --product {product_id} --profile first --input-file input.json
```

Replace OP and prepare input.json from its declared parameters. Review endpoints,
auth/TLS references, and credential configuration. The compile test profile is only
an offline fixture. Doctor without --network and plan perform local checks; live
invocation requires an explicit QA task and scope.
""",
    }
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_DIRECTORY_EXISTS",
            f"product directory already exists: {destination}",
            source="authoring",
            hint="Choose a new directory; scaffolding never overwrites files.",
        ) from error
    except OSError as error:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_DIRECTORY_UNAVAILABLE",
            "could not create the product directory",
            source="authoring",
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
            "PRODUCT_SCAFFOLD_WRITE_FAILED",
            "could not finish writing the product scaffold",
            source="authoring",
            hint="The new directory may be partially written; inspect it before retrying with a new directory.",
        ) from error
    return {
        "product": product_id,
        "package": package,
        "directory": str(destination),
        "files": sorted(files),
        "operationCount": 0,
        "nextSteps": [
            "Read AGENTS.md and verify the product's canonical contracts before adding operations.",
            "Install this package into the current runtime environment using the generated README.",
            "Add offline compile fixtures and request/input/mutation tests with each operation.",
        ],
    }
