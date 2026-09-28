"""Shared fixtures."""

import pytest


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Resolve the recorder database before hass exists.

    The autouse fixture below requests hass before a test's own fixtures, so
    recorder_mock (which needs its database URL before hass) would otherwise
    come too late.
    """


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/halfhour in every test."""
    yield
