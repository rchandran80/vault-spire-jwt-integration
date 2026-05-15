# SPIRE + HashiCorp Vault Enterprise — SPIFFE Auth Method POC

A self-contained proof-of-concept demonstrating **workload identity** using
[SPIFFE/SPIRE](https://spiffe.io) v1.14.1 and
[HashiCorp Vault Enterprise](https://www.vaultproject.io) 2.0.0.
A containerised workload proves its identity to Vault using a short-lived
cryptographically signed JWT-SVID — no static secrets, passwords, or
API keys required at boot time.

This branch (`feature/spiffe-auth-method`) uses Vault's purpose-built
**SPIFFE auth method** (`auth/spiffe/`) backed by SPIRE's **native federation
bundle endpoint**. All services run locally on a shared Docker bridge network
(`spire-net`) — no HCP Vault or external tunnels required.

---

## Architecture

```
┌────────────────────────────── Docker (spire-net) ─────────────────────────────┐
│                                                                                 │
│  ┌─────────────────────────────┐  gRPC :8081  ┌──────────────────────────┐    │
│  │       SPIRE Server          │◄─────────────│      SPIRE Agent         │    │
│  │  CA + registry              │              │  join_token attested     │    │
│  │  jwt_issuer configured      │              └────────────┬─────────────┘    │
│  │                             │                           │  Unix socket      │
│  │  federation bundle          │              ┌────────────▼─────────────┐    │
│  │  endpoint  :8443  ──────────┼──────────────►  Workload App (Python)   │    │
│  │  SPIFFE bundle format       │  ←── Vault   │  pid: spire-agent        │    │
│  │  "use":"jwt-svid" keys      │  polls :8443  └────────────┬─────────────┘   │
│  └─────────────────────────────┘              │             │  JWT-SVID        │
│                                               │             │  Bearer header   │
│  ┌─────────────────────────────┐              │             ▼                  │
│  │   Vault Enterprise 2.0      │◄─────────────────── POST /v1/auth/            │
│  │   auth/spiffe/              │                         spiffe/login          │
│  │   profile=https_web_bundle  │                                               │
│  │   secret/ (KV-v2)           │                                               │
│  └─────────────────────────────┘                                               │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Flow

1. **SPIRE Server** acts as a Certificate Authority for trust domain
   `demo.realpage.local`. It maintains a workload registry and exposes its
   trust bundle over HTTPS at port 8443 via the native federation bundle
   endpoint. `jwt_issuer = "https://spire-server:8443"` is set so every
   JWT-SVID carries a matching `iss` claim.
2. **SPIRE Agent** attests to the server using a one-time join token and
   receives its own X.509 SVID. It provides the SPIFFE Workload API over a
   Unix domain socket.
3. **Workload App** connects to the Agent socket and requests a **JWT-SVID**
   scoped to the audience `vault`.
4. The workload presents the JWT-SVID to Vault's **SPIFFE auth method**
   (`auth/spiffe/`) via the `Authorization: Bearer` header. Vault fetches the
   trust bundle from SPIRE's federation endpoint, verifies the JWT signature,
   matches the workload ID pattern `/workload/app`, and issues a Vault token.
5. The workload uses the Vault token to **read a secret** from the KV-v2
   secrets engine at `secret/realpage/demo`.

---

## What Makes This Different from the `main` Branch

| Aspect | `main` | `feature/spiffe-auth-method` |
|---|---|---|
| Auth method | `auth/jwt/` | `auth/spiffe/` |
| Vault target | HCP Vault Dedicated (cloud) | Vault Enterprise 2.0.0 (Docker) |
| Bundle source | Static EC pubkey (manually extracted) | SPIRE federation endpoint (auto-refresh) |
| Key rotation | Breaks — requires `setup.sh` re-run | Transparent — Vault polls every 5 minutes |
| JWT delivery | POST body `{"jwt": "..."}` | `Authorization: Bearer` header |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS (Apple Silicon or Intel) | Tested on both |
| Docker Desktop 4.x+ | Must be running |
| Vault Enterprise license | Set as `VAULT_LICENSE` in `.env` |
| `vault` CLI | For running `vault/setup-spiffe.sh` from the host |
| `openssl` | For TLS cert generation |

> **Vault Enterprise license:** The `vault-ent` container requires a valid
> `VAULT_LICENSE` value in `.env`. Vault runs in dev mode — all data is
> in-memory and lost on every restart. A full Vault Enterprise license
> (without module restrictions) is required.

> **SPIRE binaries:** SPIRE does not publish macOS binaries. When `app.py` is
> run directly on macOS, it automatically falls back to `docker exec spire-agent`
> to reach the binary inside the container.

> **Docker file visibility:** Docker Desktop for macOS uses VirtioFS for bind
> mounts. Files written by root inside containers are not visible on the macOS
> host, and vice versa. All cert generation and data cleanup must be done from
> inside containers.

---

## Project Structure

```
spire-vault-demo/
├── .env                        # Credentials — fill in before running (gitignored)
├── docker-compose.yml          # spire-server, spire-agent, vault-ent, workload
├── spire/
│   ├── server/server.conf      # SPIRE Server: jwt_issuer + federation bundle :8443
│   └── agent/agent.conf        # SPIRE Agent: join token placeholder
├── vault/
│   ├── policy.hcl              # Vault policy: read on secret/data/realpage/*
│   └── setup-spiffe.sh         # One-time Vault configuration script
└── workload/
    ├── app.py                  # Python workload: SVID → auth/spiffe/ → KV read
    ├── Dockerfile              # Multi-stage: spire-agent binary + Python image
    └── requirements.txt        # requests
```

Runtime state (SQLite, agent data, sockets, TLS certs) is written to `./data/`,
which is bind-mounted into containers. This directory is `.gitignore`d.

---

## Setup

### 0. Clone and configure `.env`

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration/spire-vault-demo
git checkout feature/spiffe-auth-method
```

Edit `.env`:

```bash
VAULT_LICENSE=<your-vault-enterprise-license-key>
SPIFFE_VAULT_ADDR=http://127.0.0.1:8200
SPIFFE_VAULT_TOKEN=root
```

### 1. Create runtime directories

```bash
mkdir -p data/{server,agent,sockets}
```

### 2. Generate TLS cert for the SPIRE federation bundle endpoint

The cert must exist **before** `spire-server` starts. Generate it from inside a
container (Docker Desktop VirtioFS requires this):

```bash
docker run --rm --user 0 \
  -v "$(pwd)/data/server:/data" \
  python:3.12-slim bash -c "
    openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
      -keyout /data/bundle-server.key \
      -out    /data/bundle-server.crt \
      -subj '/CN=spire-server' \
      -addext 'subjectAltName=DNS:spire-server' 2>/dev/null
    echo 'Cert generated'
    ls /data/
  "
```

Expected output:
```
Cert generated
bundle-server.crt  bundle-server.key
```

### 3. Start SPIRE Server

```bash
docker compose up -d spire-server
sleep 5
docker logs spire-server 2>&1 | grep -E "Starting Server APIs|Serving bundle"
```

Expected:
```
Starting Server APIs address=[::]:8081
Serving bundle endpoint addr="0.0.0.0:8443" refresh_hint=5m0s
```

### 4. Generate join token and update `agent.conf`

```bash
JOIN_TOKEN=$(docker exec spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` — replace the `join_token` value:

```hcl
join_token = "<paste token here>"
```

> **Important:** Join tokens are single-use. Each full restart from scratch
> requires a new token and an update to `agent.conf`.

### 5. Start SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected:
```
Node attestation was successful  spiffe_id="spiffe://demo.realpage.local/spire/agent/join_token/<token>"
```

### 6. Register the workload SPIFFE entry

```bash
AGENT_ID=$(docker logs spire-agent 2>&1 \
  | grep "Node attestation was successful" \
  | grep -o 'spiffe://[^"]*')

docker exec spire-server \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://demo.realpage.local/workload/app \
  -parentID "$AGENT_ID" \
  -selector unix:uid:0
```

### 7. Start Vault Enterprise

```bash
docker compose up -d vault-ent
sleep 8
curl -s http://localhost:8200/v1/sys/health | python3 -m json.tool | grep -E "sealed|version"
```

Expected:
```json
"sealed": false,
"version": "2.0.0+ent",
```

### 8. Configure Vault

```bash
cd vault && bash setup-spiffe.sh && cd ..
```

This script:
- Generates the TLS cert for the SPIRE federation bundle endpoint (if absent)
- Enables `auth/spiffe/` with `-passthrough-request-headers="Authorization"`
- Configures `profile=https_web_bundle` pointing at `https://spire-server:8443`
- Creates role `workload-role` with `workload_id_patterns="/workload/app"`
- Creates policy `workload-policy` granting read on `secret/data/realpage/*`
- Seeds a test secret at `secret/realpage/demo`

Verify:

```bash
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root
vault read auth/spiffe/config | grep -E "profile|endpoint_url|cached_bundle"
```

Expected:
```
profile                        https_web_bundle
endpoint_url                   https://spire-server:8443
cached_bundle_sequence_number  1
```

### 9. Run the workload

**Option A — inside Docker (recommended):**

```bash
docker compose build workload
docker compose run --rm workload
```

**Option B — directly on your Mac:**

```bash
cd workload && pip3 install -r requirements.txt && python3 app.py
```

### Expected output

```
============================================================
SPIRE + Vault Enterprise SPIFFE Auth Demo
Auth Method  : auth/spiffe/ (profile=https_web_bundle)
Bundle Source: https://spire-server:8443 (SPIRE federation, native)
Vault Target : Vault Enterprise 2.0.0 (Docker spire-net)
Secret path  : secret/realpage/demo (KV-v2)
============================================================

STEP 1: Fetch JWT-SVID from SPIRE Agent
  [OK] JWT-SVID received from SPIRE Agent
  JWT Claims:
    sub : spiffe://demo.realpage.local/workload/app
    iss : https://spire-server:8443
    aud : ['vault']

STEP 2: Authenticate to Vault with JWT-SVID (SPIFFE auth method)
  [OK] Vault SPIFFE authentication successful
  policies : ['default', 'workload-policy']

STEP 3: Read Secret from Vault
  [OK] Secret retrieved successfully
  Secret Data:
    api_key = super-secret-demo-key
    db_password = demo-db-pass-123

RESULT: SUCCESS
  Full flow: SPIRE -> Vault SPIFFE auth -> KV secret read
```

---

## Notable Caveats

### Join token must be regenerated each full restart

The `join_token` in `agent.conf` is **single-use**. Every time you reset
`data/` and restart from scratch, generate a new token and update `agent.conf`.

### Data must be cleared from inside a container (macOS + Docker Desktop)

```bash
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
```

After cleaning, regenerate the TLS cert (Step 2) and join token (Step 4).

### SPIFFE auth method: `Authorization` header passthrough is mandatory

Vault strips the `Authorization: Bearer` header by default before it reaches
auth plugins. The `setup-spiffe.sh` script enables the mount with
`-passthrough-request-headers="Authorization"`. Without this flag, every login
returns `403 permission denied` with no diagnostic information.

### Federation bundle endpoint requires `file_sync_interval`

The `serving_cert_file` block in `server.conf` requires an explicit
`file_sync_interval` (set to `"1m"` in this project). Omitting it causes a Go
`time.ParseDuration("")` panic at startup.

### Admin socket must not be bind-mounted externally

Do not add `./data/server-api:/tmp/spire-server/private` as a volume mount
to `spire-server`. SPIRE manages its admin socket internally; an external bind
mount causes a silent crash immediately after startup.

### Vault Enterprise dev mode loses all state on restart

All Vault configuration (auth method, policies, secrets) is in-memory only.
After any `docker compose down`, re-run `vault/setup-spiffe.sh` completely.

### PID namespace sharing is required for the workload container

The `workload` service uses `pid: "service:spire-agent"`. This is required so
the SPIRE Agent's unix WorkloadAttestor can resolve the calling process's UID
across container boundaries. Without it, the agent cannot attest the workload.

### `insecure_bootstrap` is enabled

The agent uses `insecure_bootstrap = true`, which skips TLS verification of
the server certificate on first connection. Intentional for a local dev setup.
**Do not use this in production.**

---

## Key Rotation Transparency

When SPIRE rotates its JWT signing key (governed by `ca_ttl = 168h` — every
7 days), the `spiffe_sequence` counter in the federation bundle response
increments. Vault detects this on its next 5-minute poll (set by SPIRE's
`X-Spiffe-Bundle-Refresh-Hint: 5m0s` response header) and automatically
refreshes its cached bundle. The `spiffe_sequence` counter and refresh hint are
part of SPIRE's native federation bundle protocol — Vault reads both
automatically when polling the endpoint. No custom code or polling scripts are
required.

To simulate and verify:

```bash
# Force a CA rotation
docker exec spire-server /opt/spire/bin/spire-server ca rotate \
  -socketPath /tmp/spire-server/private/api.sock

# Wait up to 5 minutes, then confirm Vault refreshed
vault read auth/spiffe/config | grep cached_bundle_sequence_number
# Value should have incremented

# Confirm workload still authenticates with the new key
docker compose run --rm workload
```

---

## Teardown

```bash
docker compose down
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
mkdir -p data/{server,agent,sockets}
```

---

## References

- [SPIFFE/SPIRE Documentation](https://spiffe.io/docs/latest/)
- [Vault SPIFFE Auth Method](https://developer.hashicorp.com/vault/docs/auth/spiffe)
- [Vault SPIFFE Auth API](https://developer.hashicorp.com/vault/api-docs/auth/spiffe)
- [SPIRE Server Configuration Reference](https://spiffe.io/docs/latest/deploying/spire_server/)
- [SPIFFE Federation](https://spiffe.io/docs/latest/architecture/federation/readme/)
- [Vault Enterprise 2.0 Release Notes](https://www.hashicorp.com/en/blog/vault-enterprise-20-modernizes-identity-security-at-scale)
