from __future__ import annotations

from importlib.metadata import entry_points

from qa_runtime.contracts import ExitCode
from qa_runtime.errors import RuntimeFailure
from qa_runtime.ports.product import PRODUCT_PLUGIN_API_VERSION, ProductPlugin

PRODUCT_ENTRY_POINT_GROUP = "qa_runtime.products"


def installed_product_ids() -> list[str]:
    """List registered names without importing any product code."""
    return sorted({point.name for point in entry_points(group=PRODUCT_ENTRY_POINT_GROUP)})


def select_product(product: str | None) -> str:
    """Use an explicit selection or the only installed product; never prefer a brand."""
    if product:
        return product
    available = installed_product_ids()
    if len(available) == 1:
        return available[0]
    raise RuntimeFailure(
        ExitCode.INVALID_INPUT,
        "PRODUCT_REQUIRED",
        "select a product with --product or QA_CLI_PRODUCT",
        source="plugins",
        details={"installedProducts": available},
        hint="Install a product package if the list is empty; otherwise choose one of the installed names.",
    )


def product_plugin_for(product: str) -> ProductPlugin:
    matches = list(entry_points(group=PRODUCT_ENTRY_POINT_GROUP, name=product))
    if not matches:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_NOT_FOUND",
            f"product package '{product}' is not installed",
            source="plugins",
        )
    if len(matches) > 1:
        providers = ", ".join(sorted(entry_point.value for entry_point in matches))
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "DUPLICATE_PRODUCT",
            f"product '{product}' has multiple providers: {providers}",
            source="plugins",
        )

    loaded = matches[0].load()
    plugin = loaded() if callable(loaded) else loaded
    if not isinstance(plugin, ProductPlugin):
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_INVALID",
            f"product provider '{product}' did not return ProductPlugin",
            source="plugins",
        )
    if plugin.product_id != product:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_INVALID",
            f"product provider '{product}' declares product_id '{plugin.product_id}'",
            source="plugins",
        )
    if plugin.api_version != PRODUCT_PLUGIN_API_VERSION:
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "PRODUCT_VERSION_UNSUPPORTED",
            f"product '{product}' uses unsupported plugin API version '{plugin.api_version}'",
            source="plugins",
        )
    if not plugin.registry_path.exists():
        raise RuntimeFailure(
            ExitCode.INVALID_INPUT,
            "REGISTRY_NOT_FOUND",
            f"product '{product}' returned missing registry source {plugin.registry_path}",
            source="plugins",
        )
    guide_ids: set[str] = set()
    for guide in plugin.guides:
        if not guide.id.strip() or not guide.purpose.strip():
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PRODUCT_INVALID",
                f"product '{product}' declares a guide with an empty id or purpose",
                source="plugins",
            )
        if guide.id in guide_ids:
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PRODUCT_INVALID",
                f"product '{product}' declares guide '{guide.id}' more than once",
                source="plugins",
            )
        guide_ids.add(guide.id)
        if any(not operation_id.strip() for operation_id in guide.operation_ids) or len(guide.operation_ids) != len(
            set(guide.operation_ids)
        ):
            raise RuntimeFailure(
                ExitCode.INVALID_INPUT,
                "PRODUCT_INVALID",
                f"product '{product}' declares invalid operation references in guide '{guide.id}'",
                source="plugins",
            )
    return plugin
