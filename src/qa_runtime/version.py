"""Installed distribution version; pyproject.toml is the single source."""

from importlib.metadata import version

VERSION = version("agentqit")
