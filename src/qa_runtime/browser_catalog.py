"""Typed browser capabilities; application behavior and action selection remain with the caller."""

from qa_runtime.models import OperationDefinition, RegistryDocument
from qa_runtime.registry import OperationRegistry


def browser_operations() -> tuple[OperationDefinition, ...]:
    page = {"type": "string", "maxLength": 80}
    target = {
        "snapshot_id": {"type": "string", "required": True, "minLength": 1, "maxLength": 80},
        "element_ref": {"type": "string", "required": True, "minLength": 1, "maxLength": 80},
    }
    fields = {
        "observe": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 0}},
        "navigate": {"url": {"type": "string", "required": True, "minLength": 1, "maxLength": 4096}},
        "click": target,
        "fill": {
            **target,
            "value": {"type": "string", "maxLength": 8192},
            "credential_ref": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "select": {**target, "value": {"type": "string", "required": True, "maxLength": 8192}},
        "screenshot": {},
    }
    return tuple(
        OperationDefinition.model_validate(
            {
                "id": f"browser.{action}",
                "kind": "browser",
                "purpose": f"Browser {action}: one bounded capability",
                "mutating": action not in {"observe", "screenshot"},
                "admission": "diagnostic" if action in {"observe", "screenshot"} else "expert-only",
                "parameters": {
                    "page_id": page,
                    **parameters,
                    **(
                        {"dialog_policy": {"type": "string", "enum": ["dismiss", "accept"], "default": "dismiss"}}
                        if action not in {"observe", "screenshot"}
                        else {}
                    ),
                },
                "config": {"action": action},
            }
        )
        for action, parameters in fields.items()
    )


def include_browser_operations(registry: OperationRegistry) -> OperationRegistry:
    return OperationRegistry(
        RegistryDocument(
            apiVersion="qa.runtime/v1",
            kind="OperationRegistry",
            operations=[*registry.operations(), *browser_operations()],
            codecs=registry.codecs(),
        )
    )
