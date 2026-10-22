"""Banking is opt-in and private paths are never inferred from the ledger."""
from pathlib import Path

from app.config import Settings


def test_banking_configuration_defaults_fail_closed(monkeypatch):
    for name in ('PLAID_ENABLED', 'PLAID_PRIVATE_DIR', 'PLAID_CREDENTIALS_FILE',
                 'PLAID_ORIGIN', 'PLAID_COMPLETION_URI', 'STRICT_LOCAL_MODE'):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.plaid_enabled is False
    assert settings.plaid_private_dir is None
    assert settings.plaid_credentials_file is None
    assert settings.plaid_origin == ''
    assert settings.plaid_completion_uri == ''
    assert settings.strict_local_mode is True


def test_banking_configuration_accepts_only_explicit_operator_paths(monkeypatch):
    monkeypatch.setenv('PLAID_ENABLED', 'true')
    monkeypatch.setenv('PLAID_PRIVATE_DIR', '/generated/private/banking')
    monkeypatch.setenv('PLAID_CREDENTIALS_FILE', '/generated/private/plaid.json')
    monkeypatch.setenv('PLAID_ORIGIN', 'https://finance.example.invalid')
    monkeypatch.setenv('PLAID_COMPLETION_URI', 'https://finance.example.invalid/connections')
    settings = Settings(_env_file=None)
    assert settings.plaid_enabled is True
    assert settings.plaid_private_dir == Path('/generated/private/banking')
    assert settings.plaid_credentials_file == Path('/generated/private/plaid.json')
    assert settings.plaid_origin == 'https://finance.example.invalid'
    assert settings.plaid_completion_uri == 'https://finance.example.invalid/connections'
