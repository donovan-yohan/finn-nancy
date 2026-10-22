"""Generated-only contracts for the isolated Plaid Sandbox candidate."""
import importlib
import importlib.util
import json


def test_sandbox_response_boundary_is_bounded_redacted_and_redirect_free(monkeypatch):
    mod = sandbox_module()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    for response in [
        httpx.Response(302, headers={"location": "https://production.plaid.com"}),
        httpx.Response(400, json={"error_code": "INVALID_ACCESS_TOKEN", "error_message": "generated-secret"}),
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"access_token": "access-production-generated"}),
        httpx.Response(200, json=[]),
        httpx.Response(200, stream=httpx.ByteStream(b"compressed"), headers={"content-encoding": "gzip"}),
    ]:
        seen = []
        client = mod.SandboxClient(enabled=True, credentials=("generated-client", "generated-secret"),
            transport=httpx.MockTransport(lambda req: seen.append(req) or response))
        with pytest.raises(mod.SandboxError) as exc:
            client.call("/accounts/get", access_token="access-sandbox-generated")
        assert "generated-secret" not in str(exc.value)
        assert len(seen) == 1
        assert seen[0].url.host == "sandbox.plaid.com"
        assert seen[0].url.scheme == "https"
        assert seen[0].headers["accept-encoding"] == "identity"
    def broken(req):
        raise httpx.ReadTimeout("generated-secret", request=req)
    client = mod.SandboxClient(enabled=True, credentials=("generated-client", "generated-secret"), transport=httpx.MockTransport(broken))
    with pytest.raises(mod.SandboxError, match="network_uncertain"):
        client.call("/accounts/get", access_token="access-sandbox-generated")
    client = mod.SandboxClient(enabled=True, credentials=("generated-client", "generated-secret"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"accounts": []})))
    assert client.call("/accounts/get", access_token="access-sandbox-generated") == {"accounts": []}

import httpx
import pytest
from pathlib import Path


def test_private_setup_is_disabled_by_default_and_persists_only_private_status(tmp_path):
    sandbox_module()
    storemod = importlib.import_module("app.plaid_sandbox.store")
    store = storemod.Store(tmp_path / "candidate")
    assert store.status() == {"configured": False, "enabled": False, "consent_version": "plaid-sandbox.v1"}
    assert store.credentials() is None
    with pytest.raises(ValueError):
        store.configure("generated-client", "generated-secret", "old-consent")
    store.configure("generated-client", "generated-secret", "plaid-sandbox.v1")
    assert store.status()["enabled"]
    assert store.credentials() == ("generated-client", "generated-secret")
    assert "generated-secret" not in json.dumps(store.status())
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert storemod.Store(store.root).status()["configured"]
    store.disable()
    assert not store.status()["enabled"]
    assert store.credentials() is None
    unsafe = tmp_path / "alias"
    unsafe.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ValueError):
        storemod.Store(unsafe)


def test_private_file_handoff_is_metadata_only_until_current_consent(tmp_path, monkeypatch):
    import os
    from app.plaid_sandbox.store import Store
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'plaid-sandbox.json'
    path.write_text(json.dumps({'environment':'sandbox','client_id':'generated-client','secret':'generated-secret'}))
    path.chmod(0o600)
    store = Store(tmp_path/'candidate', credentials_file=path)
    reads=[]
    original=os.read
    def read(fd,amount):
        reads.append(True)
        return original(fd,amount)
    monkeypatch.setattr(os,'read',read)
    assert store.status()['configured']
    assert not store.status()['enabled']
    assert store.credentials() is None
    assert not reads
    with pytest.raises(ValueError):store.enable_private_file('old-consent','actor')
    store.enable_private_file('plaid-sandbox.v1','actor')
    assert not reads
    assert store.credentials()==('generated-client','generated-secret')
    assert reads
    assert 'generated-secret' not in json.dumps(store.status())
    store.disable()
    reads.clear()
    assert store.credentials() is None
    assert not reads
    for mode in [0o644,0o660]:
        path.chmod(mode)
        assert not store.status()['configured']
        with pytest.raises(ValueError):store.enable_private_file('plaid-sandbox.v1','actor')
    path.chmod(0o600)
    private.chmod(0o755)
    assert not store.status()['configured']
    private.chmod(0o700)
    path.unlink()
    path.symlink_to(tmp_path/'outside')
    assert not store.status()['configured']
    assert store.credentials() is None


def test_private_handoff_rejects_ambiguous_and_non_sandbox_files(tmp_path):
    from app.plaid_sandbox.store import Store
    from app.plaid_sandbox.client import SandboxError
    private=tmp_path/'private';private.mkdir(mode=0o700)
    path=private/'plaid-sandbox.json'
    for text in [
        '{"environment":"production","environment":"sandbox","client_id":"generated-client","secret":"generated-secret"}',
        json.dumps({'environment':'production','client_id':'generated-client','secret':'generated-secret'}),
        'generated-secret invalid JSON',
    ]:
        path.write_text(text);path.chmod(0o600)
        store=Store(tmp_path/'candidate',credentials_file=path)
        store.enable_private_file('plaid-sandbox.v1','actor')
        with pytest.raises(SandboxError) as exc:store.credentials()
        assert 'generated-secret' not in str(exc.value)
        store.disable()


def test_launcher_uses_fixed_isolated_root_and_scrubs_inherited_transports(monkeypatch, tmp_path):
    assert importlib.util.find_spec("app.plaid_sandbox.launch"), "isolated launcher missing"
    launch = importlib.import_module("app.plaid_sandbox.launch")
    monkeypatch.setenv("DB_PATH", "/not-authorized/live.sqlite")
    monkeypatch.setenv("HERMES_CHAT_TOKEN", "generated-inherited-secret")
    monkeypatch.setenv("LOCAL_LLM_BASE", "https://external.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    env = launch.clean_environment()
    assert not {"DB_PATH", "HERMES_CHAT_TOKEN", "LOCAL_LLM_BASE", "HTTPS_PROXY"} & env.keys()
    assert env["STRICT_LOCAL_MODE"] == "true"
    assert launch.candidate_root() == Path(launch.__file__).resolve().parents[2] / "data" / "plaid-sandbox"
    import subprocess
    result = subprocess.run([str(Path(launch.__file__).resolve().parents[2]/"scripts"/"plaid-sandbox"), "--help"],capture_output=True,text=True,env={**env,"DB_PATH":"/not-authorized/live.sqlite"})
    assert result.returncode == 0
    assert "loopback" in result.stdout.lower()
    assert "generated-inherited-secret" not in result.stdout+result.stderr
    isolated_env={'FN_SANDBOX_CLEAN_PROCESS':'1','HERMES_CHAT_TOKEN':'generated-inherited-secret','DB_PATH':'generated-operator-path','HTTPS_PROXY':'https://proxy.invalid'}
    monkeypatch.setattr(launch.os,'environ',isolated_env)
    monkeypatch.setattr('sys.argv',['sandbox-launch','--help'])
    with pytest.raises(SystemExit):launch.main()
    remaining_names=sorted(set(isolated_env)&{'HERMES_CHAT_TOKEN','DB_PATH','HTTPS_PROXY'})
    assert remaining_names == []


def sandbox_module():
    # A missing candidate is an assertion failure, not a collection/import error.
    assert importlib.util.find_spec("app.plaid_sandbox"), "sandbox boundary is not implemented"
    return importlib.import_module("app.plaid_sandbox.client")


def test_sandbox_network_boundary_refuses_before_dispatch():
    mod = sandbox_module()
    requests = []
    transport = httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(200, json={}))
    for enabled, credentials in [(False, ("generated-client", "generated-secret")), (True, None)]:
        client = mod.SandboxClient(enabled=enabled, credentials=credentials, transport=transport)
        with pytest.raises(mod.SandboxError):
            client.call("/accounts/get", access_token="access-sandbox-generated")
    client = mod.SandboxClient(enabled=True, credentials=("generated-client", "generated-secret"), transport=transport)
    for path, fields in [
        ("https://production.plaid.com/accounts/get", {}),
        ("//production.plaid.com/accounts/get", {}),
        ("/accounts/get?base=production", {}),
        ("/accounts/get", {"access_token": "access-production-generated"}),
        ("/item/public_token/exchange", {"public_token": "public-development-generated"}),
        ("/link/token/get", {"link_token": "link-production-generated"}),
    ]:
        with pytest.raises(mod.SandboxError):
            client.call(path, **fields)
    assert requests == []
