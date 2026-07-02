# MCP Server Tester

A web-based tool for inspecting [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers.  
Connect to any MCP server, browse its tools, measure fetch latency, and estimate (or accurately count) how many tokens those tools consume in a Claude API request.

---

## Features

| Feature | Details |
|---------|---------|
| **Tool inspection** | Lists all tools with name, description, parameter breakdown, and input schema |
| **Token estimation** | Estimates input tokens per Claude API request (~4 chars/token heuristic) |
| **Accurate token counting** | Calls `POST /v1/messages/count_tokens` with your Anthropic API key for exact counts |
| **Fetch timing** | Shows MCP server fetch time and total client roundtrip time |
| **Multiple auth methods** | None · Bearer Token · OAuth2 Client Credentials · SSO (Authorization Code + PKCE) · Custom Header |
| **SSO auto-discovery** | Discovers OAuth endpoints from `/.well-known/oauth-authorization-server` and MCP `WWW-Authenticate` headers |
| **Dynamic Client Registration** | Registers an OAuth client automatically (RFC 7591) — no Client ID required |
| **Multiple transports** | Streamable HTTP (MCP 2025) and SSE, with automatic fallback |
| **Connection history** | Remembers the last 8 connections in the browser |

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

---

## Installation

### Using uv (recommended)

```bash
git clone https://github.com/ytkoka/mcp-tester.git
cd mcp-tester
uv venv
uv pip install \
  "mcp>=1.0.0" \
  "fastapi>=0.100.0" \
  "uvicorn[standard]>=0.20.0" \
  "httpx>=0.25.0"
```

### Using pip

```bash
git clone https://github.com/ytkoka/mcp-tester.git
cd mcp-tester
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install \
  "mcp>=1.0.0" \
  "fastapi>=0.100.0" \
  "uvicorn[standard]>=0.20.0" \
  "httpx>=0.25.0"
```

---

## Quick Start

```bash
./run.sh
```

or

```bash
.venv/bin/python main.py
```

Open **http://localhost:8080** in your browser.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT`   | `8080`  | Port the server listens on |

---

## Usage

### 1 — Connect to an MCP server

1. Enter the **MCP Server URL** (e.g. `https://api.example.com/mcp`)
2. Choose a **Transport** (Auto / Streamable HTTP / SSE)
3. Select an **Auth Method** and fill in credentials (see below)
4. Click **Connect & Fetch Tools**

The **Server Info** card shows the server name, protocol version, transport used, and timing:

- **MCP fetch** — time the backend spent connecting and listing tools  
- **Roundtrip** — total elapsed time from the browser click to the displayed result  

Color coding: green < 500 ms · yellow < 2 s · red ≥ 2 s

### 2 — Browse tools

Each tool card shows:
- Tool name and parameter count
- Estimated (or accurate) token cost badge, with a color-coded bar relative to the heaviest tool
- Expandable view with description, parameter tags (required ones are highlighted), and the full input schema

Use the **Search tools…** box to filter by name or description.

### 3 — Token counting

**Estimate mode** (default)  
Uses `~4 chars / token` as a rough approximation. The badge shows `~N`.

**Claude API mode** (accurate)  
1. Select **Claude API (accurate)** in the *Token Counting* section of the sidebar
2. Paste your `sk-ant-api03-…` API key (saved to `localStorage`)
3. Choose a model (Haiku 4.5 / Sonnet 4.6 / Opus 4.8)

After connecting, click **Count with Claude API**. The tool makes two parallel calls to `POST /v1/messages/count_tokens` — once with all tools, once without — and reports the difference as the accurate tool token cost. Badges update to show exact counts.

> **Note:** Per-tool counts are proportionally scaled from the accurate total.  
> The total figure is exact; individual tool figures are an approximation within that total.

### 4 — Copy tool definitions

In the Token Summary card or on each tool card:
- **Copy as Claude API format** — uses `input_schema` key, ready for `anthropic.messages.create(tools=[…])`
- **Copy as MCP format** — uses `inputSchema` key, the native MCP representation

---

## Auth Methods

### None
No authentication headers are added.

### Bearer Token
Adds `Authorization: Bearer <token>` to every MCP request.

### OAuth2 Client Credentials (OAuth CC)
Fetches a token from a token endpoint using the client credentials grant, then uses it as a Bearer token.

| Field | Required |
|-------|----------|
| Token Endpoint URL | ✓ |
| Client ID | ✓ |
| Client Secret | ✓ |
| Scope | optional |

### SSO (OAuth2 Authorization Code + PKCE)
Opens a browser popup for interactive SSO login. No manual Client ID is required if the server supports Dynamic Client Registration.

**Automatic flow:**

```
Connect clicked
  → Discover OAuth metadata from MCP server
      (/.well-known/oauth-authorization-server  or  401 WWW-Authenticate chain)
  → Register client via Dynamic Client Registration (RFC 7591)  ← no Client ID needed
  → Generate PKCE code_verifier / code_challenge
  → Open browser popup → user logs in → redirect to localhost/oauth/callback
  → Exchange code for token
  → Connect to MCP with Bearer token
```

**Advanced Settings** (expand the *▶ Advanced Settings* section) let you override any field manually — useful when the server doesn't support auto-discovery or dynamic registration.

### Custom Header
Adds a single arbitrary header (e.g. `X-API-Key: abc123`) to MCP requests.

---

## Project Structure

```
mcp-tester/
├── main.py          # FastAPI app — MCP client, OAuth endpoints, token counting
├── pyproject.toml   # Project metadata
├── run.sh           # Startup script
└── static/
    └── index.html   # Single-page UI (vanilla HTML/CSS/JS, no build step)
```

### API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Serves the UI |
| `POST` | `/api/connect` | Connects to an MCP server and returns tools + timing |
| `POST` | `/api/count-tokens` | Calls Claude API to count tokens accurately |
| `POST` | `/api/oauth/start` | Starts an SSO flow (discovery + registration + PKCE) |
| `GET`  | `/oauth/callback` | Receives the OAuth authorization code |
| `GET`  | `/api/oauth/status/{state}` | Polls for the SSO token |
| `POST` | `/api/oauth/discover` | Exposes OAuth metadata discovery results |

---

## Development

The server runs with hot-reload enabled by default (`uvicorn --reload`).  
Edit `main.py` or `static/index.html` and the changes take effect immediately.

```bash
# Run on a different port
PORT=9090 ./run.sh

# Verbose logging
LOG_LEVEL=debug .venv/bin/uvicorn main:app --reload --log-level debug
```

---

## License

MIT
