import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_archives", Path(__file__).parents[2] / "tools" / "verify_archives.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "member",
    [
        "../outside",
        "/absolute/file",
        "project/.agentqit/token.json",
        "project/.git/config",
        "project/profiles/.env",
        "project/certificates/private.key",
        "project/browser/state.jks",
        "project/__pycache__/cache.pyc",
    ],
)
def test_operational_and_unsafe_archive_members_are_rejected(member):
    with pytest.raises(ValueError):
        module.validate_paths([member])


def test_public_source_and_license_paths_are_accepted():
    module.validate_paths(["qa_runtime/engine.py", "agentqit-0.1.0/LICENSE", "agentqit-0.1.0/docs/workspaces.md"])
