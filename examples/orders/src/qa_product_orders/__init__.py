from pathlib import Path

from qa_runtime.ports.product import ProductGuide, ProductPlugin, ProductSetup


def product_plugin() -> ProductPlugin:
    root = Path(__file__).parent
    return ProductPlugin(
        product_id="orders",
        registry_path=root / "operations.yaml",
        setup=ProductSetup(root / "profile.example.yaml", template_fields=("services.orders.baseUrl",)),
        guides=(
            ProductGuide(
                id="orders-lifecycle",
                purpose="Observe order creation and cancellation across UI and API",
                payload={
                    "states": ["CREATED", "CANCELLED"],
                    "effects": "Creation allocates an ID; cancellation updates it without adding an order.",
                    "observations": ["Visible order title/status", "Exact API order and list membership"],
                    "limitations": "A completed click or HTTP response does not prove business correctness.",
                    "source": "Packaged requirements.md and openapi.json, version 1.0.0",
                },
                operation_ids=("orders.list", "orders.get", "orders.create", "orders.cancel"),
            ),
        ),
    )
