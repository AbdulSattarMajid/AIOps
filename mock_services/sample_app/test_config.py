import config
import pytest


def test_config_reads_stripe_key(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_123")
    assert config.load_config() == {"stripe_key": "sk_test_123"}


def test_missing_stripe_key_has_clear_error(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(Exception) as error:
        config.load_config()
    assert not isinstance(error.value, KeyError)
    assert "STRIPE_API_KEY" in str(error.value)
