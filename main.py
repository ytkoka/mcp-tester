import asyncio
import base64
import hashlib
import html as html_module
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MCP Server Tester")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── SSO / OAuth state store ───────────────────────────────────────
_oauth_pending: dict[str, dict] = {}
_OAUTH_TTL = 600  # 10 minutes


def _cleanup_oauth() -> None:
    now = time.time()
    expired = [k for k, v in _oauth_pending.items() if now - v["ts"] > _OAUTH_TTL]
    for k in expired:
        del _oauth_pending[k]


def _pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge_s256)"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _sso_page(title: str, body: str, color: str = "#c9d1d9", auto_close: bool = True) -> HTMLResponse:
    script = "<script>setTimeout(() => window.close(), 1800)</script>" if auto_close else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;text-align:center;padding:60px 32px;background:#0d1117;color:{color}">
<h2 style="margin-bottom:10px">{title}</h2>
<p style="color:#8b949e;font-size:14px">{body}</p>
{script}
</body></html>""")


class AuthConfig(BaseModel):
    type: str = "none"  # none, bearer, oauth2_cc, sso, header
    token: Optional[str] = None
    header_name: Optional[str] = None
    header_value: Optional[str] = None
    oauth_token_url: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    oauth_scope: Optional[str] = None


class ConnectRequest(BaseModel):
    url: str
    auth: AuthConfig = AuthConfig()
    transport: str = "auto"  # auto, streamable_http, sse


class ExecuteRequest(BaseModel):
    url: str
    auth: AuthConfig = AuthConfig()
    transport: str = "auto"
    tool_name: str
    tool_args: dict = {}


class ReadResourceRequest(BaseModel):
    url: str
    auth: AuthConfig = AuthConfig()
    transport: str = "auto"
    resource_uri: str


class GetPromptRequest(BaseModel):
    url: str
    auth: AuthConfig = AuthConfig()
    transport: str = "auto"
    prompt_name: str
    prompt_args: dict[str, str] = {}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_tool_tokens(tool: dict) -> int:
    # Serialize as Claude API format to estimate actual token usage
    claude_format = {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "input_schema": tool.get("inputSchema", {}),
    }
    return estimate_tokens(json.dumps(claude_format, ensure_ascii=False)) + 20


async def fetch_oauth_token(auth: AuthConfig) -> str:
    async with httpx.AsyncClient() as client:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": auth.oauth_client_id or "",
            "client_secret": auth.oauth_client_secret or "",
        }
        if auth.oauth_scope:
            data["scope"] = auth.oauth_scope
        resp = await client.post(auth.oauth_token_url or "", data=data, timeout=30.0)
        resp.raise_for_status()
        return resp.json()["access_token"]


async def build_headers(auth: AuthConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if auth.type == "bearer" and auth.token:
        headers["Authorization"] = f"Bearer {auth.token}"
    elif auth.type == "header" and auth.header_name and auth.header_value:
        headers[auth.header_name] = auth.header_value
    elif auth.type == "oauth2_cc":
        token = await fetch_oauth_token(auth)
        headers["Authorization"] = f"Bearer {token}"
    return headers


def tool_to_dict(tool: Any) -> dict[str, Any]:
    schema = tool.inputSchema
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    elif not isinstance(schema, dict):
        try:
            schema = dict(schema)
        except Exception:
            schema = {}
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": schema or {},
    }


def resource_to_dict(resource: Any) -> dict[str, Any]:
    return {
        "uri": str(getattr(resource, "uri", "")),
        "name": getattr(resource, "name", "") or "",
        "description": getattr(resource, "description", "") or "",
        "mimeType": getattr(resource, "mimeType", "") or "",
    }


def prompt_to_dict(prompt: Any) -> dict[str, Any]:
    args = []
    for a in (getattr(prompt, "arguments", None) or []):
        args.append({
            "name": getattr(a, "name", ""),
            "description": getattr(a, "description", "") or "",
            "required": bool(getattr(a, "required", False)),
        })
    return {
        "name": getattr(prompt, "name", ""),
        "description": getattr(prompt, "description", "") or "",
        "arguments": args,
    }


async def _list_primitives(session: ClientSession, caps: Any) -> tuple[list, list, list, dict]:
    """List only the primitives the server advertised in its initialize capabilities.

    Gating on caps prevents calling list_resources/list_prompts on servers that
    do not support them — those servers may hang indefinitely instead of returning
    a JSON-RPC error, which would exhaust the outer connect timeout.

    tools: called when caps is absent (old server) OR caps.tools is present.
    resources/prompts: called only when explicitly advertised.
    """
    tools, resources, prompts = [], [], []
    timing: dict[str, int] = {"list_tools_ms": 0, "list_resources_ms": 0, "list_prompts_ms": 0}

    if caps is None or getattr(caps, "tools", None) is not None:
        _ts = time.perf_counter()
        try:
            tools = [tool_to_dict(t) for t in (await session.list_tools()).tools]
        except Exception:
            pass
        timing["list_tools_ms"] = round((time.perf_counter() - _ts) * 1000)

    if caps is not None and getattr(caps, "resources", None) is not None:
        _ts = time.perf_counter()
        try:
            resources = [resource_to_dict(r) for r in (await session.list_resources()).resources]
        except Exception:
            pass
        timing["list_resources_ms"] = round((time.perf_counter() - _ts) * 1000)

    if caps is not None and getattr(caps, "prompts", None) is not None:
        _ts = time.perf_counter()
        try:
            prompts = [prompt_to_dict(p) for p in (await session.list_prompts()).prompts]
        except Exception:
            pass
        timing["list_prompts_ms"] = round((time.perf_counter() - _ts) * 1000)

    return tools, resources, prompts, timing


async def _connect_streamable(url: str, headers: dict) -> tuple[list, list, list, dict, dict]:
    tools, resources, prompts, info = [], [], [], {}
    timing: dict[str, int] = {}

    _t0 = time.perf_counter()
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        timing["transport_connect_ms"] = round((time.perf_counter() - _t0) * 1000)

        async with ClientSession(read, write) as session:
            _t1 = time.perf_counter()
            result = await session.initialize()
            timing["initialize_ms"] = round((time.perf_counter() - _t1) * 1000)

            if result.serverInfo:
                info = {"name": result.serverInfo.name, "version": result.serverInfo.version}
            info["protocol_version"] = str(result.protocolVersion) if result.protocolVersion else "unknown"

            tools, resources, prompts, prim_timing = await _list_primitives(session, result.capabilities)
            timing.update(prim_timing)

    return tools, resources, prompts, info, timing


async def _connect_sse(url: str, headers: dict) -> tuple[list, list, list, dict, dict]:
    tools, resources, prompts, info = [], [], [], {}
    timing: dict[str, int] = {}

    _t0 = time.perf_counter()
    async with sse_client(url, headers=headers) as (read, write):
        timing["transport_connect_ms"] = round((time.perf_counter() - _t0) * 1000)

        async with ClientSession(read, write) as session:
            _t1 = time.perf_counter()
            result = await session.initialize()
            timing["initialize_ms"] = round((time.perf_counter() - _t1) * 1000)

            if result.serverInfo:
                info = {"name": result.serverInfo.name, "version": result.serverInfo.version}
            info["protocol_version"] = str(result.protocolVersion) if result.protocolVersion else "unknown"

            tools, resources, prompts, prim_timing = await _list_primitives(session, result.capabilities)
            timing.update(prim_timing)

    return tools, resources, prompts, info, timing


def _serialize_content(result: Any) -> tuple[list[dict], bool]:
    items: list[dict] = []
    for item in (getattr(result, "content", None) or []):
        t = getattr(item, "type", None)
        if t == "text":
            items.append({"type": "text", "text": getattr(item, "text", "")})
        elif t == "image":
            items.append({"type": "image", "data": getattr(item, "data", ""), "mimeType": getattr(item, "mimeType", "image/png")})
        elif t == "resource":
            res = getattr(item, "resource", None)
            entry: dict = {"type": "resource", "uri": str(getattr(res, "uri", "")) if res else ""}
            if res:
                txt = getattr(res, "text", None)
                if txt is not None:
                    entry["text"] = txt
            items.append(entry)
        else:
            if hasattr(item, "model_dump"):
                try:
                    items.append(item.model_dump())
                    continue
                except Exception:
                    pass
            items.append({"type": str(t) if t else "unknown", "raw": str(item)})
    return items, bool(getattr(result, "isError", False))


def _serialize_resource_contents(result: Any) -> list[dict]:
    items: list[dict] = []
    for content in (getattr(result, "contents", None) or []):
        entry: dict = {
            "uri": str(getattr(content, "uri", "")),
            "mimeType": getattr(content, "mimeType", "") or "",
        }
        text = getattr(content, "text", None)
        blob = getattr(content, "blob", None)
        if text is not None:
            entry["type"] = "text"
            entry["text"] = text
        elif blob is not None:
            entry["type"] = "blob"
            entry["blob"] = blob
        else:
            entry["type"] = "unknown"
        items.append(entry)
    return items


def _serialize_prompt_messages(result: Any) -> list[dict]:
    messages: list[dict] = []
    for msg in (getattr(result, "messages", None) or []):
        role = str(getattr(msg, "role", "user"))
        content = getattr(msg, "content", None)
        t = getattr(content, "type", None)
        if t == "text":
            messages.append({"role": role, "type": "text", "text": getattr(content, "text", "") or ""})
        elif t == "image":
            messages.append({
                "role": role, "type": "image",
                "data": getattr(content, "data", "") or "",
                "mimeType": getattr(content, "mimeType", "") or "",
            })
        elif content is not None:
            if hasattr(content, "model_dump"):
                try:
                    messages.append({"role": role, **content.model_dump()})
                    continue
                except Exception:
                    pass
            messages.append({"role": role, "type": str(t) if t else "unknown", "raw": str(content)})
    return messages


async def _exec_streamable(url: str, headers: dict, tool_name: str, tool_args: dict) -> Any:
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, tool_args)


async def _exec_sse(url: str, headers: dict, tool_name: str, tool_args: dict) -> Any:
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, tool_args)


async def call_tool_on_server(url: str, headers: dict, transport: str, tool_name: str, tool_args: dict) -> tuple[Any, str]:
    timeout = 60.0
    if transport == "streamable_http":
        return await asyncio.wait_for(_exec_streamable(url, headers, tool_name, tool_args), timeout), "streamable_http"
    if transport == "sse":
        return await asyncio.wait_for(_exec_sse(url, headers, tool_name, tool_args), timeout), "sse"
    err_s = ""
    try:
        res = await asyncio.wait_for(_exec_streamable(url, headers, tool_name, tool_args), timeout)
        return res, "streamable_http"
    except Exception as e:
        err_s = str(e)
    try:
        res = await asyncio.wait_for(_exec_sse(url, headers, tool_name, tool_args), timeout)
        return res, "sse"
    except Exception as e:
        raise RuntimeError(f"Both transports failed.\n• Streamable HTTP: {err_s}\n• SSE: {e}")


async def _exec_read_resource_streamable(url: str, headers: dict, resource_uri: str) -> Any:
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.read_resource(resource_uri)


async def _exec_read_resource_sse(url: str, headers: dict, resource_uri: str) -> Any:
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.read_resource(resource_uri)


async def call_read_resource_on_server(url: str, headers: dict, transport: str, resource_uri: str) -> tuple[Any, str]:
    timeout = 30.0
    if transport == "streamable_http":
        return await asyncio.wait_for(_exec_read_resource_streamable(url, headers, resource_uri), timeout), "streamable_http"
    if transport == "sse":
        return await asyncio.wait_for(_exec_read_resource_sse(url, headers, resource_uri), timeout), "sse"
    err_s = ""
    try:
        res = await asyncio.wait_for(_exec_read_resource_streamable(url, headers, resource_uri), timeout)
        return res, "streamable_http"
    except Exception as e:
        err_s = str(e)
    try:
        res = await asyncio.wait_for(_exec_read_resource_sse(url, headers, resource_uri), timeout)
        return res, "sse"
    except Exception as e:
        raise RuntimeError(f"Both transports failed.\n• Streamable HTTP: {err_s}\n• SSE: {e}")


async def _exec_get_prompt_streamable(url: str, headers: dict, prompt_name: str, prompt_args: dict) -> Any:
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.get_prompt(prompt_name, prompt_args or None)


async def _exec_get_prompt_sse(url: str, headers: dict, prompt_name: str, prompt_args: dict) -> Any:
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.get_prompt(prompt_name, prompt_args or None)


async def call_get_prompt_on_server(url: str, headers: dict, transport: str, prompt_name: str, prompt_args: dict) -> tuple[Any, str]:
    timeout = 30.0
    if transport == "streamable_http":
        return await asyncio.wait_for(_exec_get_prompt_streamable(url, headers, prompt_name, prompt_args), timeout), "streamable_http"
    if transport == "sse":
        return await asyncio.wait_for(_exec_get_prompt_sse(url, headers, prompt_name, prompt_args), timeout), "sse"
    err_s = ""
    try:
        res = await asyncio.wait_for(_exec_get_prompt_streamable(url, headers, prompt_name, prompt_args), timeout)
        return res, "streamable_http"
    except Exception as e:
        err_s = str(e)
    try:
        res = await asyncio.wait_for(_exec_get_prompt_sse(url, headers, prompt_name, prompt_args), timeout)
        return res, "sse"
    except Exception as e:
        raise RuntimeError(f"Both transports failed.\n• Streamable HTTP: {err_s}\n• SSE: {e}")


async def connect_and_list_primitives(url: str, headers: dict, transport: str) -> tuple[list, list, list, dict, str, dict]:
    timeout = 30.0

    if transport == "streamable_http":
        tools, resources, prompts, info, timing = await asyncio.wait_for(_connect_streamable(url, headers), timeout)
        return tools, resources, prompts, info, "streamable_http", timing

    if transport == "sse":
        tools, resources, prompts, info, timing = await asyncio.wait_for(_connect_sse(url, headers), timeout)
        return tools, resources, prompts, info, "sse", timing

    # auto: try streamable first, fall back to SSE
    err_streamable = ""
    try:
        tools, resources, prompts, info, timing = await asyncio.wait_for(_connect_streamable(url, headers), timeout)
        return tools, resources, prompts, info, "streamable_http", timing
    except Exception as e:
        err_streamable = str(e)

    try:
        tools, resources, prompts, info, timing = await asyncio.wait_for(_connect_sse(url, headers), timeout)
        return tools, resources, prompts, info, "sse", timing
    except Exception as e:
        raise RuntimeError(
            f"Both transports failed.\n"
            f"• Streamable HTTP: {err_streamable}\n"
            f"• SSE: {e}"
        )


# ── SSO helpers ──────────────────────────────────────────────────

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/Azure IMDS, link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _assert_safe_discovery_url(url: str) -> None:
    """Raise ValueError if the URL targets a private/reserved IP or uses a non-http(s) scheme.
    Defends against SSRF when following attacker-controlled redirect chains in OAuth discovery."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL is missing a hostname")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # domain name literal — allow without DNS resolution
    if any(ip in net for net in _BLOCKED_NETWORKS):
        raise ValueError(f"URL targets a private/reserved address: {ip}")


def _assert_http_scheme(url: str) -> None:
    """Raise ValueError if the URL scheme is not http or https (e.g. javascript:, file:)."""
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme: {scheme!r}")


async def _discover_oauth_full(url: str) -> dict:
    """
    Full OAuth discovery following the MCP spec:
      1. RFC 8414 /.well-known/oauth-authorization-server on the server origin
      2. OpenID Connect /.well-known/openid-configuration
      3. MCP spec: GET url → 401 WWW-Authenticate resource_metadata → AS metadata
    Returns dict with found, authorization_endpoint, token_endpoint,
    registration_endpoint, issuer, scopes_supported.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=6.0) as client:
        # 1 & 2: well-known endpoints on the server origin
        for path in [
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ]:
            try:
                resp = await client.get(f"{base}{path}")
                if resp.status_code == 200:
                    d = resp.json()
                    return {
                        "found": True,
                        "authorization_endpoint": d.get("authorization_endpoint"),
                        "token_endpoint": d.get("token_endpoint"),
                        "registration_endpoint": d.get("registration_endpoint"),
                        "issuer": d.get("issuer"),
                        "scopes_supported": d.get("scopes_supported", []),
                    }
            except Exception:
                continue

        # 3: MCP spec — hit the resource URL, follow 401/403 chain.
        # Streamable HTTP endpoints only accept POST, so try GET first then POST
        # if GET doesn't yield a 401/403 with WWW-Authenticate.
        try:
            for attempt in ["get", "post"]:
                if attempt == "get":
                    resp = await client.get(url)
                else:
                    if resp.status_code not in (401, 403):
                        resp = await client.post(
                            url,
                            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                            headers={"Content-Type": "application/json"},
                        )
                if resp.status_code in (401, 403):
                    www_auth = resp.headers.get("WWW-Authenticate", "")
                    m = re.search(r'resource_metadata="([^"]+)"', www_auth)
                    if m:
                        _assert_safe_discovery_url(m.group(1))
                        meta_resp = await client.get(m.group(1))
                        if meta_resp.status_code == 200:
                            meta = meta_resp.json()
                            as_urls = meta.get("authorization_servers", [])
                            if as_urls:
                                _assert_safe_discovery_url(as_urls[0])
                                as_resp = await client.get(
                                    f"{as_urls[0]}/.well-known/oauth-authorization-server"
                                )
                                if as_resp.status_code == 200:
                                    d = as_resp.json()
                                    return {
                                        "found": True,
                                        "authorization_endpoint": d.get("authorization_endpoint"),
                                        "token_endpoint": d.get("token_endpoint"),
                                        "registration_endpoint": d.get("registration_endpoint"),
                                        "issuer": d.get("issuer"),
                                        "scopes_supported": d.get("scopes_supported", []),
                                    }
                    break  # 401/403 received but no usable metadata — stop here
        except Exception:
            pass

    return {"found": False}


async def _dynamic_register(registration_endpoint: str, redirect_uri: str) -> dict:
    """RFC 7591 Dynamic Client Registration — returns {client_id, client_secret?, ...}"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            registration_endpoint,
            json={
                "client_name": "MCP Tester",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",  # public client with PKCE
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


# ── SSO endpoints ────────────────────────────────────────────────

class OAuthStartRequest(BaseModel):
    mcp_url: str
    # All optional — auto-discovered if omitted
    auth_endpoint: str = ""
    token_endpoint: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    redirect_uri: str = "http://localhost:8080/oauth/callback"


class DiscoverRequest(BaseModel):
    url: str


@app.post("/api/oauth/start")
async def oauth_start(req: OAuthStartRequest):
    _cleanup_oauth()

    auth_endpoint = req.auth_endpoint.strip()
    token_endpoint = req.token_endpoint.strip()
    client_id = req.client_id.strip()
    client_secret = req.client_secret.strip()
    discovery_info: dict = {}

    # ── Step 1: auto-discover OAuth config if any field is missing ──
    if not auth_endpoint or not token_endpoint or not client_id:
        discovery_info = await _discover_oauth_full(req.mcp_url)
        if not discovery_info.get("found"):
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": (
                        "OAuth 設定を MCP サーバーから自動検出できませんでした。\n"
                        "詳細設定で Authorization URL・Token Endpoint・Client ID を入力してください。"
                    ),
                    "needs_manual": True,
                },
            )
        auth_endpoint = auth_endpoint or discovery_info.get("authorization_endpoint", "")
        token_endpoint = token_endpoint or discovery_info.get("token_endpoint", "")

    # ── Step 2: Dynamic Client Registration if client_id is still missing ──
    if not client_id:
        reg_ep = discovery_info.get("registration_endpoint")
        if not reg_ep:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": (
                        "サーバーが Dynamic Client Registration (RFC 7591) をサポートしていません。\n"
                        "詳細設定で Client ID を入力してください。"
                    ),
                    "needs_client_id": True,
                },
            )
        try:
            reg = await _dynamic_register(reg_ep, req.redirect_uri)
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Dynamic Client Registration に失敗しました: {e}\n詳細設定で Client ID を入力してください。",
                    "needs_client_id": True,
                },
            )
        client_id = reg.get("client_id", "")
        client_secret = reg.get("client_secret", client_secret)
        logger.info("Dynamic registration succeeded: client_id=%s", client_id)

    if not auth_endpoint or not token_endpoint or not client_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "auth_endpoint / token_endpoint / client_id が取得できませんでした。"},
        )

    try:
        _assert_http_scheme(auth_endpoint)
        _assert_http_scheme(token_endpoint)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    # ── Step 3: Build PKCE auth URL ──────────────────────────────
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    _oauth_pending[state] = {
        "verifier": verifier,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": req.redirect_uri,
        "result": None,
        "ts": time.time(),
    }

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": req.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if req.scope:
        params["scope"] = req.scope

    auth_url = f"{auth_endpoint}?{urlencode(params)}"
    return {
        "success": True,
        "state": state,
        "auth_url": auth_url,
        "client_id": client_id,
        "issuer": discovery_info.get("issuer"),
        "dynamic_registration": bool(discovery_info.get("registration_endpoint") and not req.client_id),
    }


@app.get("/oauth/callback")
async def oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if not state or state not in _oauth_pending:
        return _sso_page("⚠️ Invalid state", "このウィンドウを閉じてください", "#f85149", auto_close=False)

    pending = _oauth_pending[state]

    if error:
        desc = html_module.escape(error_description or "")
        pending["result"] = {"error": f"{error}: {desc}"}
        return _sso_page("⚠️ 認証エラー", html_module.escape(f"{error}: {error_description or ''}"), "#f85149")

    if not code:
        pending["result"] = {"error": "認証コードが受信できませんでした"}
        return _sso_page("⚠️ エラー", "認証コードが受信できませんでした", "#f85149")

    try:
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "code_verifier": pending["verifier"],
        }
        if pending.get("client_secret"):
            data["client_secret"] = pending["client_secret"]

        async with httpx.AsyncClient() as client:
            resp = await client.post(pending["token_endpoint"], data=data, timeout=30.0)
            resp.raise_for_status()
            token_data = resp.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(f"access_token がレスポンスに含まれていません (keys: {list(token_data.keys())})")

        pending["result"] = {"token": access_token}
        return _sso_page("✅ 認証成功", "このウィンドウは自動的に閉じます", "#3fb950")

    except Exception as e:
        pending["result"] = {"error": str(e)}
        return _sso_page("⚠️ トークン取得失敗", html_module.escape(str(e)), "#f85149", auto_close=False)


@app.get("/api/oauth/status/{state}")
async def oauth_status(state: str):
    if state not in _oauth_pending:
        return {"status": "not_found"}
    result = _oauth_pending[state].get("result")
    if result is None:
        return {"status": "pending"}
    del _oauth_pending[state]
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    return {"status": "ready", "token": result["token"]}


@app.post("/api/oauth/discover")
async def oauth_discover(req: DiscoverRequest):
    result = await _discover_oauth_full(req.url)
    if result.get("found"):
        result["supports_dynamic_registration"] = bool(result.get("registration_endpoint"))
    return result


class CountTokensRequest(BaseModel):
    tools: list[dict]
    api_key: str
    model: str = "claude-haiku-4-5-20251001"


@app.post("/api/count-tokens")
async def count_tokens_api(req: CountTokensRequest):
    """Call Claude API count_tokens with and without tools; return accurate per-tool breakdown."""
    claude_tools = [
        {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {}),
        }
        for t in req.tools
    ]

    hdrs = {
        "x-api-key": req.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp_with, resp_base = await asyncio.gather(
                client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    headers=hdrs,
                    json={"model": req.model, "tools": claude_tools,
                          "messages": [{"role": "user", "content": "hi"}]},
                    timeout=20.0,
                ),
                client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    headers=hdrs,
                    json={"model": req.model,
                          "messages": [{"role": "user", "content": "hi"}]},
                    timeout=20.0,
                ),
            )

        if resp_with.status_code != 200:
            body = resp_with.json() if "application/json" in resp_with.headers.get("content-type", "") else {}
            msg = body.get("error", {}).get("message") or resp_with.text
            return JSONResponse(status_code=resp_with.status_code,
                                content={"success": False, "error": msg})

        total_with: int = resp_with.json()["input_tokens"]
        total_base: int = resp_base.json().get("input_tokens", 0)
        tool_tokens = max(0, total_with - total_base)

        # Proportionally distribute tool_tokens using rough estimates as weights
        estimated = [estimate_tool_tokens(t) for t in req.tools]
        total_est = sum(estimated)
        if total_est > 0 and tool_tokens > 0:
            per_tool = [max(1, round(tool_tokens * e / total_est)) for e in estimated]
        elif req.tools:
            per_tool = [round(tool_tokens / len(req.tools))] * len(req.tools)
        else:
            per_tool = []

        return {
            "success": True,
            "total_input_tokens": total_with,
            "baseline_tokens": total_base,
            "tool_tokens": tool_tokens,
            "per_tool_tokens": per_tool,
            "model": req.model,
        }

    except httpx.TimeoutException:
        return JSONResponse(status_code=408, content={"success": False, "error": "Request timed out"})
    except Exception as e:
        logger.exception("count_tokens_api failed")
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.post("/api/connect")
async def connect_to_mcp(req: ConnectRequest):
    try:
        headers = await build_headers(req.auth)

        t0 = time.perf_counter()
        tools_raw, resources_raw, prompts_raw, server_info, transport_used, fetch_timing = await connect_and_list_primitives(
            req.url, headers, req.transport
        )
        fetch_ms = round((time.perf_counter() - t0) * 1000)

        tools = []
        total_tokens = 0
        for t in tools_raw:
            tok = estimate_tool_tokens(t)
            total_tokens += tok
            props = t.get("inputSchema", {}).get("properties", {})
            required = t.get("inputSchema", {}).get("required", [])
            tools.append({
                **t,
                "estimated_tokens": tok,
                "param_count": len(props),
                "required_count": len(required),
            })

        tools.sort(key=lambda x: x["estimated_tokens"], reverse=True)

        return {
            "success": True,
            "transport_used": transport_used,
            "fetch_time_ms": fetch_ms,
            "fetch_timing": fetch_timing,
            "server_info": server_info,
            "tool_count": len(tools),
            "tools": tools,
            "resource_count": len(resources_raw),
            "resources": resources_raw,
            "prompt_count": len(prompts_raw),
            "prompts": prompts_raw,
            "token_summary": {
                "tools_total": total_tokens,
                "overhead": 50,
                "per_request_estimate": total_tokens + 50,
            },
        }
    except Exception as e:
        logger.exception("Connection failed")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e), "error_type": type(e).__name__},
        )


@app.post("/api/execute")
async def execute_tool_api(req: ExecuteRequest):
    try:
        headers = await build_headers(req.auth)
        t0 = time.perf_counter()
        result, transport_used = await call_tool_on_server(
            req.url, headers, req.transport, req.tool_name, req.tool_args
        )
        exec_ms = round((time.perf_counter() - t0) * 1000)
        content, is_error = _serialize_content(result)
        return {
            "success": True,
            "tool_name": req.tool_name,
            "is_error": is_error,
            "content": content,
            "exec_time_ms": exec_ms,
            "transport_used": transport_used,
        }
    except Exception as e:
        logger.exception("Tool execution failed")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e), "error_type": type(e).__name__},
        )


@app.post("/api/resources/read")
async def read_resource_api(req: ReadResourceRequest):
    try:
        headers = await build_headers(req.auth)
        t0 = time.perf_counter()
        result, transport_used = await call_read_resource_on_server(
            req.url, headers, req.transport, req.resource_uri
        )
        exec_ms = round((time.perf_counter() - t0) * 1000)
        contents = _serialize_resource_contents(result)
        return {
            "success": True,
            "uri": req.resource_uri,
            "contents": contents,
            "exec_time_ms": exec_ms,
            "transport_used": transport_used,
        }
    except Exception as e:
        logger.exception("Resource read failed")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e), "error_type": type(e).__name__},
        )


@app.post("/api/prompts/get")
async def get_prompt_api(req: GetPromptRequest):
    try:
        headers = await build_headers(req.auth)
        t0 = time.perf_counter()
        result, transport_used = await call_get_prompt_on_server(
            req.url, headers, req.transport, req.prompt_name, req.prompt_args
        )
        exec_ms = round((time.perf_counter() - t0) * 1000)
        messages = _serialize_prompt_messages(result)
        return {
            "success": True,
            "prompt_name": req.prompt_name,
            "description": getattr(result, "description", "") or "",
            "messages": messages,
            "exec_time_ms": exec_ms,
            "transport_used": transport_used,
        }
    except Exception as e:
        logger.exception("Prompt get failed")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e), "error_type": type(e).__name__},
        )


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def run():
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    run()
