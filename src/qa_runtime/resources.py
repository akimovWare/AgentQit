"""Declarative resource references shared by composition and onboarding."""

STANDARD_RESOURCE_BINDINGS: dict[str, tuple[str, str]] = {
    "http": ("services", "service"),
    "db-query": ("databases", "database"),
    "kafka-consume": ("kafka", "cluster"),
    "kafka-publish": ("kafka", "cluster"),
    "kafka-offsets": ("kafka", "cluster"),
    "s3": ("s3", "storage"),
    "kubernetes": ("kubernetes", "cluster"),
}
