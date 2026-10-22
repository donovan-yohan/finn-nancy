"""Tailnet-wide ingress against the actual isolated candidate HTTP boundary."""
import json
import httpx
import ipaddress
import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.plaid_sandbox.web import create_app

ORIGIN = "https://generated-node.example." + "ts.net:8782"
OWNER = "generated-owner@example.invalid"
SYNTHETIC_NETWORK = ipaddress.IPv4Network("100." "64.0.0/10")
SERVER_ADDRESS = str(SYNTHETIC_NETWORK.network_address + 7)
OTHER_ADDRESS = str(SYNTHETIC_NETWORK.network_address + 8)
SYNTHETIC_V6_ADDRESS = "fd7a:115c:a1e0::8"
IDENTITY = {"Tailscale-User-Login": OWNER, "X-Forwarded-Proto": "https", "X-Forwarded-For": SERVER_ADDRESS}


def gateway(tmp_path):
    from app.plaid_sandbox.tailnet import create_gateway
    upstream = create_app(tmp_path / "generated-candidate", port=8772)
    transport = httpx.ASGITransport(app=upstream, client=("127.0.0.1", 5000))
    return create_gateway(ORIGIN, transport=transport), upstream


@pytest.mark.parametrize("source", [OTHER_ADDRESS, SYNTHETIC_V6_ADDRESS])
def test_tagged_tailnet_device_without_user_identity_can_render_and_mutate(tmp_path, source):
    app, upstream = gateway(tmp_path)
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": source}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=headers) as client:
        assert client.get("/connections").status_code == 200
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        response = client.post("/api/sandbox/member", json={"name": "Generated Tagged Member"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert response.status_code == 200
    assert upstream.state.store.snapshot()["members"][0]["name"] == "Generated Tagged Member"


def test_owner_can_render_and_mutate_actual_candidate_with_exact_public_origin(tmp_path):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        page = client.get("/connections")
        assert page.status_code == 200
        assert 'class="db-pill sandbox-badge">Sandbox · test data</span>' in page.text
        assert "Secure" in page.headers["set-cookie"]
        state = client.get("/api/sandbox/state").json()
        response = client.post("/api/sandbox/member", json={"name": "Generated Member"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": state["csrf"]})
        assert response.status_code == 200
        assert upstream.state.store.snapshot()["members"][0]["name"] == "Generated Member"
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 200
        assert str(home.url) == ORIGIN + "/"
        assert 'location' not in home.headers
        assert client.get("/api/sandbox/overview").status_code == 200


def test_missing_https_and_non_loopback_ingress_are_rejected(tmp_path):
    app, upstream = gateway(tmp_path)
    for identity in ({}, {"Tailscale-User-Login": OWNER, "X-Forwarded-For": SERVER_ADDRESS},
                     {**IDENTITY, "X-Forwarded-Proto": "http"}):
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=identity) as client:
            assert client.get("/connections").status_code == 403
    for peer in (("192.0.2.5", 5001), (SERVER_ADDRESS, 5001), ("localhost", 5001), ("::ffff:127.0.0.1", 5001)):
        with TestClient(app, base_url=ORIGIN, client=peer, headers=IDENTITY) as client:
            assert client.get("/connections").status_code == 403
    assert upstream.state.store.snapshot()["members"] == []


def test_missing_real_connection_peer_is_rejected(tmp_path):
    app, upstream = gateway(tmp_path)
    @app.middleware("http")
    async def remove_peer(request, call_next):
        request.scope["client"] = None
        return await call_next(request)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        assert client.get("/connections").status_code == 403


def test_public_origin_csrf_and_host_are_not_weakened_by_proxy(tmp_path):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        for origin in (None, "https://evil.invalid", "http://127.0.0.1:8772"):
            headers = {"X-CSRF-Token": csrf}
            if origin:
                headers["Origin"] = origin
            assert client.post("/api/sandbox/member", json={"name": "Rejected"}, headers=headers).status_code == 403
        assert client.post("/api/sandbox/member", json={"name": "Rejected"}, headers={"Origin": ORIGIN}).status_code == 403
        assert client.post("/api/sandbox/member", json={"name": "Rejected"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": "generated-wrong-token"}).status_code == 403
        assert client.get("/connections", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.get("/connections", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/sandbox/member", content=b"x" * 16385, headers={"Origin": ORIGIN}).status_code == 413
    assert upstream.state.store.snapshot()["members"] == []


def test_forwarding_headers_are_stripped_before_actual_app_boundary(tmp_path):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append(dict(request.headers))
        return await call_next(request)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        assert client.get("/connections", headers={"Authorization": "generated-not-forwarded",
            "Forwarded": "for=192.0.2.8", "X-Forwarded-Host": "evil.invalid", "Tailscale-User-Name": "Generated User"}).status_code == 200
    assert seen[0]["host"] == "127.0.0.1:8772"
    assert not any(k.startswith(("x-forwarded-", "tailscale-")) for k in seen[0])
    assert "authorization" not in seen[0]
    assert "forwarded" not in seen[0]
    assert seen[0]["accept-encoding"] == "identity"


@pytest.mark.parametrize("identity", [OWNER, "generated-viewer@example.invalid", "third@example.invalid"])
def test_untagged_tailnet_identities_can_render_and_mutate(tmp_path, identity):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001),
                    headers={**IDENTITY, "Tailscale-User-Login": identity, "X-Forwarded-For": OTHER_ADDRESS}) as client:
        assert client.get("/connections").status_code == 200
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        assert client.post("/api/sandbox/member", json={"name": "Generated Member"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 200
    assert upstream.state.store.snapshot()["members"][0]["name"] == "Generated Member"


def test_source_chains_are_rejected_in_either_order(tmp_path):
    app, upstream = gateway(tmp_path)
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": SERVER_ADDRESS}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=headers) as client:
        assert client.get("/connections").status_code == 200
        assert client.get("/connections", headers={"X-Forwarded-For": f"{SERVER_ADDRESS}, {OTHER_ADDRESS}"}).status_code == 403
        assert client.get("/connections", headers={"X-Forwarded-For": f"{OTHER_ADDRESS}, {SERVER_ADDRESS}"}).status_code == 403
        assert client.get("/connections", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.get("/connections", headers={"X-Forwarded-For": SERVER_ADDRESS, "Tailscale-User-Login": "other@example.invalid"}).status_code == 200


@pytest.mark.parametrize("source", [None, "", "not-an-ip", OTHER_ADDRESS + ":443", "[" + SYNTHETIC_V6_ADDRESS + "]",
    SYNTHETIC_V6_ADDRESS + "%lo0", "100.064.0.7", "127.0.0.1", "::1",
    str(ipaddress.IPv4Network("10." "0.0.0/8").network_address + 8),
    str(ipaddress.IPv4Network("192." "168.0.0/16").network_address + 8),
    "192.0.2.8", "8.8.8.8", str(SYNTHETIC_NETWORK.network_address - 1),
    str(SYNTHETIC_NETWORK.broadcast_address + 1), "fd7a:115c:a1df:ffff::1",
    "fd7a:115c:a1e1::1", "fd00::8", "2001:db8::8", "::ffff:" + OTHER_ADDRESS, ", " + OTHER_ADDRESS,
    OTHER_ADDRESS + ",", OTHER_ADDRESS + ", " + OTHER_ADDRESS])
def test_missing_malformed_and_non_tailnet_sources_never_reach_upstream(tmp_path, source):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append(request.url.path)
        return await call_next(request)
    headers = {"Tailscale-User-Login": OWNER, "X-Forwarded-Proto": "https"}
    if source is not None:
        headers["X-Forwarded-For"] = source
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=headers) as client:
        response = client.get("/connections")
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    assert seen == []


@pytest.mark.parametrize("sources", [(OTHER_ADDRESS, OTHER_ADDRESS), (OTHER_ADDRESS, "192.0.2.8"), ("192.0.2.8", OTHER_ADDRESS)])
def test_duplicate_source_header_instances_are_rejected(tmp_path, sources):
    app, upstream = gateway(tmp_path)
    headers = [("X-Forwarded-Proto", "https"), ("Tailscale-User-Login", OWNER),
               ("X-Forwarded-For", sources[0]), ("x-forwarded-for", sources[1])]
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001)) as client:
        assert client.get("/connections", headers=headers).status_code == 403


@pytest.mark.parametrize("source", [str(SYNTHETIC_NETWORK.network_address), str(SYNTHETIC_NETWORK.broadcast_address),
    "fd7a:115c:a1e0::", "fd7a:115c:a1e0:ffff:ffff:ffff:ffff:ffff"])
def test_exact_tailnet_networks_are_accepted_over_ipv6_loopback(tmp_path, source):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("::1", 5001),
                    headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": source}) as client:
        assert client.get("/connections", headers={"Host": "127.0.0.1:8773"}).status_code == 200


@pytest.mark.parametrize("host", ["localhost:8773", "127.0.0.1:8772", "127.0.0.1", "[::1]:8773",
    ORIGIN.removeprefix("https://").replace(":8782", ":443"), ORIGIN.removeprefix("https://") + ".evil.invalid"])
def test_other_hosts_are_rejected(tmp_path, host):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        assert client.get("/connections", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
def test_non_get_head_post_methods_never_reach_upstream(tmp_path, method):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append(request.method)
        return await call_next(request)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        assert client.request(method, "/connections", headers={"Origin": ORIGIN}).status_code == 405
    assert seen == []


def test_session_cookie_and_csrf_pair_remain_required(tmp_path):
    app, upstream = gateway(tmp_path)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        assert client.get("/api/sandbox/state").status_code == 403
        page = client.get("/connections")
        cookie = page.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("secure", "httponly", "samesite=strict"))
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        client.cookies.clear()
        assert client.post("/api/sandbox/member", json={"name": "Rejected"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 403
        client.get("/connections")
        assert client.post("/api/sandbox/member", json={"name": "Rejected"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 403
    assert upstream.state.store.snapshot()["members"] == []


def test_exact_request_body_limit_and_query_are_preserved(tmp_path):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append((request.url.query, await request.body()))
        return await call_next(request)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        seen.clear()
        body = b'{"name":"Generated Bounded Member"}'
        body += b" " * (16384 - len(body))
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"}
        assert client.post("/api/sandbox/member?generated=exact", content=body, headers=headers).status_code == 200
        assert seen == [("generated=exact", body)]
        assert client.post("/api/sandbox/member", content=body + b" ", headers=headers).status_code == 413
        assert len(seen) == 1
    assert len(upstream.state.store.snapshot()["members"]) == 1


@pytest.mark.parametrize("size,expected", [(2 * 1024 * 1024, 200), (2 * 1024 * 1024 + 1, 502)])
def test_response_size_remains_bounded(tmp_path, size, expected):
    app, upstream = gateway(tmp_path)
    @upstream.get("/generated-response")
    def generated_response():
        return Response(b"x" * size, headers={"X-Generated-Not-Forwarded": "generated"})
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        response = client.get("/generated-response")
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert "x-generated-not-forwarded" not in response.headers
        if expected == 200:
            assert len(response.content) == size
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("location,expected", [
    ("/connections?generated=1", "/connections?generated=1"),
    ("http://127.0.0.1:8772/connections?generated=1", ORIGIN + "/connections?generated=1"),
    ("https://evil.invalid/", None), ("//evil.invalid/", None), ("relative", None),
    ("http://127.0.0.1:8772.evil.invalid/", None),
])
def test_redirects_are_not_followed_and_only_local_targets_are_forwarded(tmp_path, location, expected):
    app, upstream = gateway(tmp_path)
    @upstream.get("/generated-redirect")
    def generated_redirect():
        return Response(status_code=302, headers={"Location": location})
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        response = client.get("/generated-redirect", follow_redirects=False)
        assert response.status_code == (302 if expected is not None else 502)
        assert response.headers.get("location") == expected
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path", [("GET", "/connections"), ("HEAD", "/static/plaid-sandbox.css")])
@pytest.mark.parametrize("supplied_origin", [None, ORIGIN])
def test_cross_site_top_level_navigation_reaches_unchanged_app(tmp_path, method, path, supplied_origin):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append(dict(request.headers))
        return await call_next(request)
    headers = {**IDENTITY, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    if supplied_origin is not None:
        headers["Origin"] = supplied_origin
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=headers) as client:
        response = client.request(method, path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "sec-fetch-site" not in seen[0]
        assert seen[0]["sec-fetch-mode"] == "navigate"
        assert seen[0]["sec-fetch-dest"] == "document"
        if supplied_origin is not None:
            assert seen[0]["origin"] == "http://127.0.0.1:8772"
        assert client.get("/connections", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert seen[1]["sec-fetch-site"] == "same-origin"


@pytest.mark.parametrize("method,path,mode,dest,supplied_origin", [
    ("GET", "/api/sandbox/state", "cors", "empty", ORIGIN),
    ("GET", "/api/sandbox/state", "same-origin", "empty", ORIGIN),
    ("GET", "/connections", "navigate", "iframe", None),
    ("GET", "/connections", "navigate", "frame", None),
    ("GET", "/connections", "navigate", None, None),
    ("GET", "/connections", None, "document", None),
    ("GET", "/connections", "no-cors", "document", None),
    ("POST", "/api/sandbox/member", "navigate", "document", ORIGIN),
    ("POST", "/api/sandbox/member", "cors", "empty", ORIGIN),
    ("GET", "/connections", "navigate", "document", "https://evil.invalid"),
    ("HEAD", "/connections", "navigate", "document", "null"),
])
def test_cross_site_fetch_frames_mutations_and_wrong_origin_never_reach_upstream(tmp_path, method, path, mode, dest, supplied_origin):
    app, upstream = gateway(tmp_path)
    seen = []
    @upstream.middleware("http")
    async def capture(request, call_next):
        seen.append(request.url.path)
        return await call_next(request)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers=IDENTITY) as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        seen.clear()
        headers = {"Sec-Fetch-Site": "cross-site", "X-CSRF-Token": csrf}
        for key, value in (("Sec-Fetch-Mode", mode), ("Sec-Fetch-Dest", dest), ("Origin", supplied_origin)):
            if value is not None:
                headers[key] = value
        assert client.request(method, path, headers=headers, json={"name": "Rejected"}).status_code == 403
    assert seen == []
    assert upstream.state.store.snapshot()["members"] == []


@pytest.mark.parametrize("origin", [ORIGIN.replace("https:", "http:"), "https://evil.invalid:8782", ORIGIN + "/path", ORIGIN.replace("://", "://invalid-user@"), ORIGIN + "/?x=1"])
def test_unsafe_public_origin_is_refused(tmp_path, origin):
    from app.plaid_sandbox.tailnet import create_gateway
    with pytest.raises(ValueError):
        create_gateway(origin)


def test_startup_uses_only_self_dns_and_preserves_loopback_no_proxy_boundary(tmp_path, monkeypatch):
    import os
    import uvicorn
    from app.plaid_sandbox import tailnet
    calls = []
    def status(command, **kwargs):
        assert command == ["/opt/homebrew/bin/tailscale", "status", "--json"]
        assert kwargs == {"text": True, "timeout": 10}
        # No User, Peer, UserID or TailscaleIPs is needed, including on tagged hosts.
        return json.dumps({"Self": {"DNSName": ORIGIN.removeprefix("https://").removesuffix(":8782") + "."}})
    def run(app, **kwargs):
        calls.append((app, kwargs, dict(os.environ)))
    monkeypatch.setattr(tailnet.os, "environ", {"TMPDIR": str(tmp_path), "GENERATED_INHERITED_VALUE": "discarded"})
    monkeypatch.setattr("sys.argv", ["tailnet"])
    monkeypatch.setattr(tailnet.subprocess, "check_output", status)
    monkeypatch.setattr(uvicorn, "run", run)
    tailnet.main()
    assert len(calls) == 1
    app, settings, environment = calls[0]
    assert settings == {"host": "127.0.0.1", "port": 8773, "proxy_headers": False, "access_log": False, "log_level": "warning"}
    assert environment == {"PATH": os.defpath, "TMPDIR": str(tmp_path), "STRICT_LOCAL_MODE": "true"}
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5001), headers={**IDENTITY, "Host": "evil.invalid"}) as client:
        assert client.get("/connections").status_code == 403
