# SPIRE + HCP Vault Dedicated — SPIFFE Auth Method POC

A proof-of-concept that demonstrates workload identity using SPIFFE/SPIRE v1.14.1 and
[HCP Vault Dedicated](https://developer.hashicorp.com/hcp/docs/vault). A containerised
workload fetches a JWT-SVID from the SPIRE Agent and authenticates to HCP Vault using the
SPIFFE auth method (`auth/spiffe/` with `profile=static`), then reads a KV-v2 secret — no
static credentials, passwords, or API keys required at boot time.

**Three Docker services:** `spire-server`, `spire-agent`, `workload`. HCP Vault is cloud-hosted
and requires no local Vault container.

---

## Architecture

```
┌─────────────── Docker (spire-net) ───────────────┐
│                                                    │
│  ┌───────────────┐  gRPC :8081  ┌───────────────┐ │
│  │  SPIRE Server │◄─────────────│  SPIRE Agent  │ │
│  │  CA + registry│              │  join_token   │ │
│  └───────────────┘              └───────┬───────┘ │
│                                         │ Unix socket
│                               ┌─────────▼───────┐ │
│                               │ Workload (Python)│ │
│                               └─────────┬───────┘ │
└─────────────────────────────────────────┼─────────┘
                                          │ JWT-SVID
                                          │ Authorization: Bearer
                                          ▼
                               ┌─────────────────────┐
                               │   HCP Vault Dedicated│
                               │   auth/spiffe/       │
                               │   profile=static     │
                               │   secret/ (KV-v2)    │
                               └─────────────────────┘
```

### Flow

1. **SPIRE Server** is the CA for trust domain `demo.realpage.local`.
2. **SPIRE Agent** attests via join token and serves the SPIFFE Workload API on a Unix socket.
3. **Workload** requests a JWT-SVID scoped to audience `vault`.
4. The workload POSTs the JWT-SVID to `auth/spiffe/login` on HCP Vault via the
   `Authorization: Bearer` header. Vault verifies the signature against the pre-configured
   static trust bundle and issues a Vault token.
5. The workload reads `secret/realpage/demo` from the KV-v2 secrets engine.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS (Apple Silicon or Intel) | Tested on both |
| Docker Desktop 4.x+ | Must be running |
| HCP Vault Dedicated cluster | Admin-level token required |
| `vault` CLI | For running `vault/setup-hcp-spiffe.sh` from the host |

> **No Vault Enterprise license needed.** HCP Vault Dedicated provides Vault Enterprise
> capabilities as a managed service. No local Vault container is used.

> **Authorization header passthrough:** The `setup-hcp-spiffe.sh` script configures
> `auth/spiffe/` with `-passthrough-request-headers="Authorization"`. This is mandatory —
> without it, every login returns `403 permission denied` with no diagnostic information.

> **SPIRE binaries:** SPIRE does not publish macOS binaries. When `app.py` is run directly
> on macOS, it falls back to `docker exec hcp-spire-agent` to reach the binary inside the
> container.

---

## Project Structure

```
hcp-vault-spiffe-auth/
├── .env                        # HCP Vault credentials — fill in before running
├── docker-compose.yml          # spire-server, spire-agent, workload (no local Vault)
├── spire/
│   ├── server/server.conf      # SPIRE Server config (no federation endpoint needed)
│   └── agent/agent.conf        # SPIRE Agent: join token placeholder
├── vault/
│   ├── policy.hcl              # Vault policy: read on secret/data/realpage/*
│   └── setup-hcp-spiffe.sh    # Configure auth/spiffe/ on HCP Vault
└── workload/
    ├── app.py                  # Python workload: SVID → auth/spiffe/ → KV read
    ├── Dockerfile              # Multi-stage: spire-agent binary + Python image
    └── requirements.txt
```

---

## Setup

### 0. Clone and configure `.env`

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration/hcp-vault-spiffe-auth
```

Edit `.env` with your HCP Vault cluster details:

```bash
HCP_VAULT_ADDR=https://<your-cluster>.vault.hashicorp.cloud:8200
HCP_VAULT_TOKEN=<your-root-or-admin-token>
HCP_VAULT_NAMESPACE=admin
```

### 1. Create runtime directories

```bash
mkdir -p data/{server,agent,sockets}
```

### 2. Start SPIRE Server

```bash
docker compose up -d spire-server
sleep 5
docker logs hcp-spire-server 2>&1 | grep "Starting Server APIs"
```

Expected:
```
Starting Server APIs address=[::]:8081
```

### 3. Generate join token and update `agent.conf`

```bash
JOIN_TOKEN=$(docker exec hcp-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` — replace the `join_token` value:

```hcl
join_token = "<paste token here>"
```

> **Important:** Join tokens are single-use. Each full restart requires a new token.

### 4. Start SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs hcp-spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected:
```
Node attestation was successful  spiffe_id="spiffe://demo.realpage.local/spire/agent/join_token/<token>"
```

### 5. Register the workload SPIFFE entry

```bash
AGENT_ID=$(docker logs hcp-spire-agent 2>&1 \
  | grep "Node attestation was successful" \
  | grep -o 'spiffe://[^"]*')

docker exec hcp-spire-server \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://demo.realpage.local/workload/app \
  -parentID "$AGENT_ID" \
  -selector unix:uid:0
```

### 6. Configure HCP Vault

```bash
source .env
export VAULT_ADDR="$HCP_VAULT_ADDR" VAULT_TOKEN="$HCP_VAULT_TOKEN" VAULT_NAMESPACE="$HCP_VAULT_NAMESPACE"
cd vault && bash setup-hcp-spiffe.sh && cd ..
```

This script:
- Enables `auth/spiffe/` with `-passthrough-request-headers="Authorization"`
- Extracts the JWT-SVID signing key from the SPIRE bundle (jwt-svid keys only)
- Configures `profile=static` with the extracted bundle
- Creates role `workload-role` with `workload_id_patterns="/workload/app"`
- Creates policy `workload-policy` granting read on `secret/data/realpage/*`
- Seeds a test secret at `secret/realpage/demo`

### 7. Run the workload

**Option A — inside Docker:**

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
SPIRE + HCP Vault SPIFFE Auth Demo
Auth Method  : auth/spiffe/ (profile=static)
Bundle Source: static JWT key (manual refresh on SPIRE key rotation)
Vault Target : HCP Vault Dedicated
Secret path  : secret/realpage/demo (KV-v2)
============================================================

STEP 1: Fetch JWT-SVID from SPIRE Agent
  [OK] JWT-SVID received from SPIRE Agent

STEP 2: Authenticate to Vault with JWT-SVID (SPIFFE auth method)
  [OK] Vault SPIFFE authentication successful
  policies : ['default', 'workload-policy']

STEP 3: Read Secret from Vault
  [OK] Secret retrieved successfully
  Secret Data:
    api_key = super-secret-demo-key
    db_password = demo-db-pass-123

RESULT: SUCCESS
  Full flow: SPIRE -> HCP Vault SPIFFE auth -> KV secret read
```

---

## Notable Caveats

### `profile=static` requires manual bundle refresh after SPIRE key rotation

SPIRE rotates its JWT signing key on a schedule governed by `ca_ttl = 168h` (every 7 days
by default). With `profile=static`, the bundle is pinned at setup time. After a key rotation:

1. New JWTs will carry the new `kid` in their header
2. HCP Vault's cached bundle won't have the new key
3. All logins will return `403 permission denied` until the bundle is updated

**To refresh:** re-run `setup-hcp-spiffe.sh` while SPIRE is running to push the new key.

For automatic key rotation handling, use `local-vault-spiffe-auth/` instead, which uses
`profile=https_web_bundle` with the SPIRE native federation endpoint.

### Authorization header passthrough is mandatory

The `setup-hcp-spiffe.sh` script applies `-passthrough-request-headers="Authorization"`.
Without this, HCP Vault strips the Bearer token before it reaches the SPIFFE plugin.

### Join token must be regenerated each full restart

The `join_token` in `agent.conf` is single-use. After resetting `data/` and restarting,
generate a new token and update `agent.conf`.

### Data cleanup on macOS

Docker Desktop uses VirtioFS — files written by container-root processes are not deletable
from the macOS host directly. Use:

```bash
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
```

---

## Production Upgrade Path

| POC Approach | Production Replacement |
|---|---|
| `profile=static` (manual refresh) | `profile=https_web_bundle` with SPIRE federation endpoint accessible from Vault |
| Join token node attestation | Cloud-native attestor (AWS IID, GCP GCE, Azure MSI, Kubernetes PSAT) |
| SQLite datastore | PostgreSQL or MySQL for HA SPIRE Server |
| HCP Vault Dedicated | Vault Enterprise self-hosted with Raft storage |

When SPIRE is accessible from HCP Vault (e.g., exposed via a public load balancer or VPN),
switch to `profile=https_web_bundle` by pointing `endpoint_url` at the SPIRE federation
bundle endpoint. Vault will then auto-refresh the bundle on every SPIRE key rotation.

---

## Teardown

```bash
docker compose down
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
mkdir -p data/{server,agent,sockets}
```

To clean up HCP Vault configuration:
```bash
source .env
export VAULT_ADDR="$HCP_VAULT_ADDR" VAULT_TOKEN="$HCP_VAULT_TOKEN" VAULT_NAMESPACE="$HCP_VAULT_NAMESPACE"
vault auth disable spiffe
vault secrets disable secret
vault policy delete workload-policy
```

---

## References

- [SPIFFE/SPIRE Documentation](https://spiffe.io/docs/latest/)
- [Vault SPIFFE Auth Method](https://developer.hashicorp.com/vault/docs/auth/spiffe)
- [HCP Vault Dedicated](https://developer.hashicorp.com/hcp/docs/vault)
