# MCP Server Tester

A web-based tool for inspecting [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers.  
Connect to any MCP server, browse its **Tools, Resources, and Prompts**, measure fetch latency, estimate token usage, **score the quality of tool definitions**, and **compare two servers side by side**.

---

## Features

| Feature | Details |
|---------|---------|
| **Tool inspection** | Lists all tools with name, description, parameter breakdown, and input schema |
| **Resource inspection** | Lists all resources with URI, name, mimeType; read any resource to view its contents |
| **Prompt inspection** | Lists all prompts with arguments; fill in arguments and render the prompt messages |
| **Token estimation** | Estimates input tokens per Claude API request (~4 chars/token heuristic) |
| **Accurate token counting** | Calls `POST /v1/messages/count_tokens` with your Anthropic API key for exact counts |
| **Fetch timing** | Shows MCP server fetch time, roundtrip time, and a per-phase timing breakdown waterfall |
| **Auth Inspector** | After connecting, shows the auth method used, headers sent, decoded access token claims (exp, iss, sub, scope), and OAuth endpoints; SSO access tokens are cached and reused until expiry |
| **LLM Readiness Score** | Grades tool definitions A–F across 5 dimensions; highlights which tools need improvement |
| **Compare Mode** | Connects to two servers in parallel and compares performance, tokens, quality scores, and documentation |
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

On connect, the tool simultaneously fetches **Tools, Resources, and Prompts** from the server.  
Primitives not supported by the server simply show an empty state — no error is raised.

The **Server Info** card shows the server name, protocol version, transport used, and timing:

- **MCP fetch** — time the backend spent connecting and listing all primitives
- **Roundtrip** — total elapsed time from the browser click to the displayed result

Color coding: green < 500 ms · yellow < 2 s · red ≥ 2 s

Click **▶ Timing breakdown** to expand a per-phase waterfall chart showing where time was spent:

| Phase | What it measures |
|-------|-----------------|
| `transport_connect` | Time to enter the transport context (TCP setup for SSE; near-zero for streamable HTTP which connects lazily) |
| `initialize` | MCP `initialize` handshake — includes the actual TCP connection for streamable HTTP |
| `list_tools` | Time to call `tools/list` and receive all tool definitions |
| `list_resources` | Time to call `resources/list` (shown only if server advertises resources capability) |
| `list_prompts` | Time to call `prompts/list` (shown only if server advertises prompts capability) |
| `network_overhead` | Roundtrip minus MCP fetch — browser↔backend network time |

Each bar is scaled relative to the longest phase. The percentage column shows each phase's share of the total roundtrip.

### 2 — Auth Inspector

After a successful connection, an **Auth Inspector** section appears in the sidebar. It shows the full details of the active authentication — useful for troubleshooting auth failures and verifying that credentials are being sent as expected.

#### What is displayed

| Section | Content |
|---------|---------|
| **Auth method** | Badge showing the active method (SSO (PKCE) / Bearer Token / OAuth CC / Custom Header / None) |
| **Headers sent** | The exact HTTP headers sent to the MCP server. Bearer / SSO access tokens are partially masked; click **show** to reveal the full value or **copy** to copy it |
| **Access token claims** | If the token is a JWT, decoded claims are shown: `exp` (expiry with time remaining — yellow if < 10 min, red if expired), `iss`, `sub`, `scope`, `aud` |
| **OAuth metadata** | For SSO and OAuth CC: the discovered or configured `issuer`, authorization endpoint, token endpoint, client ID, and whether Dynamic Client Registration was used |
| **Access token validity** | For OAuth CC: the token lifetime reported by the authorization server (`expires_in`) |

> **Note:** "Access token" is used throughout Auth Inspector to distinguish OAuth credentials from the AI input tokens counted in the Token Summary card.

#### SSO access token caching

After a successful SSO login, the access token is cached in `localStorage` keyed by the MCP server URL. On subsequent connections to the same server:

- If the cached token is still valid (with a 60-second buffer before expiry), the OAuth browser popup is **skipped** and the cached token is used directly. The sidebar shows `Using cached access token (expires in Xh Xm)`.
- If the token has expired, the full SSO flow runs again automatically.
- Click **Force re-auth** in the Auth Inspector to clear the cached token and trigger a fresh login regardless of expiry.

The cache is stored in `localStorage` under the key `mcp-token-cache` and persists across browser sessions.

### 3 — Browse Tools


Switch to the **Tools** tab. Each tool card shows:
- Tool name and parameter count
- Estimated (or accurate) token cost badge, with a color-coded bar relative to the heaviest tool
- Expandable view with description, parameter tags (required ones are highlighted), and the full input schema
- **▶ Execute** section to call the tool and view the result inline

Use the **Search tools…** box to filter by name or description.

### 4 — Browse Resources

Switch to the **Resources** tab. Each resource card shows:
- Resource name and URI
- MIME type badge (if provided)
- Description
- **▶ Read** button — fetches the resource contents from the server and displays them inline
  - Text content is pretty-printed as JSON when parseable
  - Binary image blobs are rendered as `<img>` elements
  - Other binary content shows a type summary

Use the **Search resources…** box to filter by name, URI, or description.

### 5 — Browse Prompts

Switch to the **Prompts** tab. Each prompt card shows:
- Prompt name and argument count
- Description and argument tags (required ones are highlighted)
- Input form auto-generated from the argument list — one text field per argument
- **▶ Get Prompt** button — calls the server with the supplied arguments and renders the returned message list

Messages are displayed in a conversation view with **user** / **assistant** role labels.

Use the **Search prompts…** box to filter by name or description.

### 6 — Token counting

**Estimate mode** (default)  
Uses `~4 chars / token` as a rough approximation. The badge shows `~N`.

**Claude API mode** (accurate)  
1. Select **Claude API (accurate)** in the *Token Counting* section of the sidebar
2. Paste your `sk-ant-api03-…` API key (saved to `localStorage`)
3. Choose a model (Haiku 4.5 / Sonnet 4.6 / Opus 4.8)

After connecting, click **Count with Claude API**. The tool makes two parallel calls to `POST /v1/messages/count_tokens` — once with all tools, once without — and reports the difference as the accurate tool token cost. Badges update to show exact counts.

> **Note:** Per-tool counts are proportionally scaled from the accurate total.  
> The total figure is exact; individual tool figures are an approximation within that total.

### 7 — Copy tool definitions

In the Token Summary card or on each tool card:
- **Copy as Claude API format** — uses `input_schema` key, ready for `anthropic.messages.create(tools=[…])`
- **Copy as MCP format** — uses `inputSchema` key, the native MCP representation

### 8 — LLM Readiness Score

After connecting, a **LLM Readiness Score** card appears automatically below the Token Summary. It evaluates how well-defined the server's tools are for any LLM — before you run a single query.

#### Scoring dimensions

| Dimension | Weight | What is measured |
|-----------|--------|-----------------|
| Tool descriptions | 20% | Character length of each tool's description (0 pts if absent, up to 100 pts for 200+ chars) |
| Param descriptions | 25% | % of parameters that have a non-empty `description` field |
| Type definitions | 25% | % of parameters with an explicit `type`; bonus for `enum`, `format`, `pattern`, range constraints |
| Required annotation | 15% | Whether the `required` array is present and correctly marks some (not all) params as mandatory |
| Schema specificity | 15% | % of parameters that carry at least one constraint (`enum`, `format`, `pattern`, min/max, etc.) |

Each dimension scores 0–100. The **Overall Score** is the weighted average.

#### Grades

| Grade | Score | Meaning |
|-------|-------|---------|
| A | 90–100 | LLM-ready — definitions are thorough and unambiguous |
| B | 75–89 | Good — minor gaps, Claude will generally use tools correctly |
| C | 60–74 | Adequate — some descriptions or types are missing |
| D | 45–59 | Needs improvement — Claude may struggle to choose the right tool or arguments |
| F | < 45 | Poor — definitions are too sparse for reliable use |

#### Color coding

- Bar color: green ≥ 75 · yellow ≥ 50 · red < 50
- Warning tags appear at the bottom of the card for actionable issues:
  - *N tools missing description*
  - *N tools have untyped parameters*
  - *N tools have undescribed parameters*
  - *N tools missing required annotation*

### 9 — Compare two servers

Click **⚡ Compare Two Servers** at the bottom of the sidebar to enter Compare Mode.  
Both servers are scored independently and the results appear in the Quality Metrics card for easy side-by-side comparison.

1. Enter **Server A** and **Server B** URLs, transports, and auth settings
2. Click **⚡ Run Comparison** — both servers are contacted simultaneously

> Auth options in Compare Mode: None, Bearer Token, Custom Header.  
> For OAuth flows, complete authentication in normal mode first and paste the resulting Bearer token here.

#### Reading the results

**Comparison Results card** — performance and primitive counts side by side:

| Row | What it measures | Lower/Higher is better |
|-----|-----------------|------------------------|
| Status | Connection success or error message | — |
| Roundtrip | Browser→backend→server total time | Lower |
| MCP Fetch | Backend time to connect and list all primitives | Lower |
| Transport | Which transport was negotiated (streamable_http / sse) | — |
| Tools | Number of tools exposed | — |
| Est. Tokens | Total estimated tokens for all tool definitions | Lower (cheaper per request) |
| Resources | Number of resources exposed | — |
| Prompts | Number of prompts exposed | — |

The **Server B (vs A)** column shows a coloured percentage diff: green = B improved relative to A, red = B regressed. The row with the better value is **bolded green** for latency and token metrics.

The **Estimated Token Usage** bar chart visualises the token gap between the two servers at a glance.

**Quality Metrics card** — LLM Readiness Score and documentation richness side by side:

The top rows show the heuristic quality score (see §7) computed for each server's tool set:

| Row | What it measures | Better |
|-----|-----------------|--------|
| Overall Score | Weighted average of the 5 scoring dimensions, shown with letter grade | Higher |
| ↳ Tool descriptions | Dimension score (0–100) | Higher |
| ↳ Param descriptions | Dimension score (0–100) | Higher |
| ↳ Type definitions | Dimension score (0–100) | Higher |
| ↳ Required annotation | Dimension score (0–100) | Higher |
| ↳ Schema specificity | Dimension score (0–100) | Higher |

Below those are descriptive statistics:

| Metric | What it measures | Higher/Lower is better |
|--------|-----------------|------------------------|
| Tool desc coverage | % of tools that have a non-empty description | Higher |
| Avg desc length | Mean character count of tool descriptions | Higher |
| Param desc rate | % of parameters that have a `description` field | Higher |
| Tokens / tool | Est. tokens ÷ tool count | Lower — leaner schemas |
| Avg params / tool | Mean number of parameters per tool | Context-dependent |
| Required param % | Required parameters as a fraction of all parameters | Context-dependent |
| Tool overlap | Shared tool names ÷ all unique names (green ≥ 70% · yellow ≥ 40% · grey < 40%) | — |

**Tool / Resource / Prompt Diff cards** — show which primitives exist only in A, only in B, or in both:

- **A only** (blue tags) — primitives present on Server A but absent on Server B
- **Both** (grey tags) — primitives with the same name on both servers
- **B only** (green tags) — primitives present on Server B but absent on Server A

The count on the right of each row is the number of items in that group.

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
| `POST` | `/api/connect` | Connects and lists Tools, Resources, and Prompts |
| `POST` | `/api/execute` | Calls a tool and returns its result |
| `POST` | `/api/resources/read` | Reads a resource by URI |
| `POST` | `/api/prompts/get` | Gets a rendered prompt with supplied arguments |
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
