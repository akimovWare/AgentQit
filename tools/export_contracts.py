"""Export reviewable public schema snapshots; --check never rewrites the baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qa_runtime.contracts import CommandResult
from qa_runtime.models import Profile, RegistryDocument
from qa_runtime.workspace import WorkspaceManifest


def schemas() -> dict:
    return {
        "workspace-v1": WorkspaceManifest.model_json_schema(by_alias=True),
        "profile-v1": Profile.model_json_schema(by_alias=True),
        "registry-v1": RegistryDocument.model_json_schema(by_alias=True),
        "result-v1": CommandResult.model_json_schema(by_alias=True),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    for name, schema in schemas().items():
        destination = directory / f"{name}.json"
        rendered = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.check:
            if not destination.is_file() or destination.read_text() != rendered:
                raise SystemExit(
                    f"Schema changed: {destination.name}; review compatibility before refreshing snapshots"
                )
        else:
            directory.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered)
    print("Public schema snapshots match" if args.check else "Public schema snapshots exported")


if __name__ == "__main__":
    main()
