"""Tailnet-wide ingress; the candidate itself stays loopback-only.

Tailscale Serve must enforce access rules, terminate HTTPS and overwrite XFF.
No credential, database, or Plaid transport is loaded by this ingress.
"""
from __future__ import annotations

import argparse
import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

UPSTREAM = "http://127.0.0.1:8772"
PUBLIC_PORT = 8782
GATEWAY_PORT = 8773
ORIGIN_PATTERN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z0-9-]+\.ts\.net:8782")
REQUEST_HEADERS = {"accept", "content-type", "cookie", "x-csrf-token", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "user-agent"}
RESPONSE_HEADERS = {"content-type", "content-security-policy", "referrer-policy", "x-content-type-options"}
TAILNET_NETWORKS = (ipaddress.IPv4Network("100." "64.0.0/10"), ipaddress.IPv6Network("fd7a:115c:a1e0::/48"))


def create_gateway(origin: str, *, transport=None):
    if not isinstance(origin, str) or not ORIGIN_PATTERN.fullmatch(origin):
        raise ValueError("An exact private Tailnet HTTPS origin on port 8782 is required")
    authority = origin.removeprefix("https://")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def ingress(request: Request, path: str):
        def denied(code=403):
            return JSONResponse({"error": "private_sandbox_ingress_rejected"}, status_code=code, headers={"Cache-Control": "no-store"})
        # Serve overwrites XFF with one authenticated peer address. Reject
        # duplicate fields and chains rather than selecting a trusted-looking IP.
        sources = request.headers.getlist("x-forwarded-for")
        if len(sources) != 1 or "%" in sources[0]:
            return denied()
        try:
            source = ipaddress.ip_address(sources[0].strip())
        except ValueError:
            return denied()
        cross_site = request.headers.get("sec-fetch-site") == "cross-site"
        navigation = (cross_site and request.method in {"GET", "HEAD"}
                      and request.headers.get("sec-fetch-mode") == "navigate"
                      and request.headers.get("sec-fetch-dest") == "document")
        if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("host") not in {authority, f"127.0.0.1:{GATEWAY_PORT}"}
                or request.headers.get("x-forwarded-proto") != "https"
                or not any(source in network for network in TAILNET_NETWORKS)
                or (cross_site and not navigation)):
            return denied()
        if request.method not in {"GET", "HEAD", "POST"}:
            return denied(405)
        supplied_origin = request.headers.get("origin")
        if (supplied_origin is not None and supplied_origin != origin) or (request.method == "POST" and supplied_origin != origin):
            return denied()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16384:
                return denied(413)
            body.extend(chunk)
        headers = {key: value for key, value in request.headers.items() if key in REQUEST_HEADERS}
        if navigation:
            # Only verified top-level safe navigation may cross this unchanged
            # app's blanket cross-site check; Origin was still checked above.
            headers.pop("sec-fetch-site", None)
        headers.update({"Host": "127.0.0.1:8772", "Accept-Encoding": "identity"})
        if supplied_origin is not None:
            # Outer origin was checked above; retain the unchanged app's exact
            # loopback Host/Origin/CSRF contract behind this trusted last hop.
            headers["Origin"] = UPSTREAM
        target = UPSTREAM + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=15) as client:
                async with client.stream(request.method, target, headers=headers, content=bytes(body)) as upstream:
                    payload = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        if len(payload) + len(chunk) > 2 * 1024 * 1024:
                            return denied(502)
                        payload.extend(chunk)
                    response_headers = {key: value for key, value in upstream.headers.items() if key in RESPONSE_HEADERS}
                    response_headers["Cache-Control"] = "no-store"
                    location = upstream.headers.get("location")
                    if location:
                        if location.startswith(UPSTREAM + "/"):
                            location = origin + location[len(UPSTREAM):]
                        elif not location.startswith("/") or location.startswith("//"):
                            return denied(502)
                        response_headers["Location"] = location
                    response = Response(bytes(payload), status_code=upstream.status_code, headers=response_headers)
                    for cookie in upstream.headers.get_list("set-cookie"):
                        if not re.search(r";\s*secure(?:;|$)", cookie, re.IGNORECASE):
                            cookie += "; Secure"
                        response.headers.append("Set-Cookie", cookie)
                    return response
        except httpx.HTTPError:
            return JSONResponse({"error": "sandbox_origin_unavailable"}, status_code=502, headers={"Cache-Control": "no-store"})
    return app


def main():
    # Preserve no inherited credentials, proxies, transports, or model settings.
    tmp = os.environ.get("TMPDIR", str(Path(__file__).resolve().parents[2] / "data" / "sandbox-runtime-tmp"))
    os.environ.clear()
    os.environ.update({"PATH": os.defpath, "TMPDIR": tmp, "STRICT_LOCAL_MODE": "true"})
    parser = argparse.ArgumentParser(description="Private Tailnet-wide ingress for the Sandbox candidate")
    parser.parse_args()
    status = json.loads(subprocess.check_output(["/opt/homebrew/bin/tailscale", "status", "--json"], text=True, timeout=10))
    node = status["Self"]
    origin = "https://" + node["DNSName"].removesuffix(".") + f":{PUBLIC_PORT}"
    app = create_gateway(origin)
    print("Private Sandbox ingress: " + origin, flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=GATEWAY_PORT, proxy_headers=False, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
