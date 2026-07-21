# conftest.py — project-level pytest configuration
import pytest


def pytest_configure(config):
    """Register custom pytest markers to avoid PytestUnknownMarkWarning."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require external services (Redis, MySQL). "
        "Auto-skipped when the service is not reachable.",
    )
