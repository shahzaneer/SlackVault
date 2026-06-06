import pytest
import tempfile
import os
import yaml

from src.registry.app_registry import AppRegistry


@pytest.fixture
def registry():
    config = {
        "apps": [
            {
                "aliases": ["payments", "payment-service", "payments-api", "payments-service"],
                "secret_path": "slackvault/{environment}/payments-service",
            },
            {
                "aliases": ["auth", "auth-service", "authentication"],
                "secret_path": "slackvault/{environment}/auth-service",
            },
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        f.flush()
        reg = AppRegistry(config_path=f.name)
    os.unlink(f.name)
    return reg


def test_exact_match(registry):
    result = registry.resolve("payments")
    assert result == "slackvault/{environment}/payments-service"


def test_fuzzy_match(registry):
    result = registry.resolve("paymen")
    assert result == "slackvault/{environment}/payments-service"


def test_no_match(registry):
    result = registry.resolve("nonexistent-app")
    assert result is None


def test_normalized_lowercase(registry):
    result = registry.resolve("Auth-Service")
    assert result == "slackvault/{environment}/auth-service"


def test_known_apps(registry):
    apps = registry.get_known_apps()
    assert len(apps) == 2
