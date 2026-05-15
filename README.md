# SPIRE + HashiCorp Vault — SPIFFE Auth Method POC

Two self-contained proofs-of-concept demonstrating **workload identity** using
[SPIFFE/SPIRE](https://spiffe.io) v1.14.1 and HashiCorp Vault. A containerised
workload proves its identity to Vault using a short-lived cryptographically signed
JWT-SVID — no static secrets, passwords, or API keys required at boot time.

Both approaches use Vault's purpose-built **SPIFFE auth method** (`auth/spiffe/`)
and read the same KV-v2 secret after authentication. They differ in which Vault
target is used and how the trust bundle is sourced.

---

## Approaches at a Glance

| | `hcp-vault-spiffe-auth/` | `local-vault-spiffe-auth/` |
|---|---|---|
| **Vault target** | HCP Vault Dedicated (your existing cluster) | Vault Enterprise 2.0.0 (Docker, local) |
| **Bundle profile** | `static` | `https_web_bundle` |
| **Bundle source** | JWT key extracted at setup time | SPIRE native federation endpoint (`:8443`) |
| **Key rotation** | Manual — re-run setup after rotation | Transparent — Vault polls every 5 minutes |
| **Docker services** | 3 (spire-server, spire-agent, workload) | 4 (+ vault-ent) |
| **License needed** | HCP Vault credentials only | Vault Enterprise license |
| **Start here if…** | You want to test against your real Vault cluster | You want to see full auto-refresh architecture |

---

## `profile=static` vs `profile=https_web_bundle`

This is the central difference between the two approaches.

**`profile=static`** stores the SPIRE JWT signing key directly in Vault's
configuration at setup time. No network path between SPIRE and Vault is required —
which makes it the correct choice when your Vault cluster (HCP Vault, cloud-hosted)
cannot reach your SPIRE server (running locally on Docker). The limitation is that
when SPIRE rotates its JWT signing key (every 7 days by default), the stored bundle
goes stale and logins fail until the setup script is re-run.

**`profile=https_web_bundle`** points Vault at SPIRE's native federation bundle
endpoint (`https://spire-server:8443`). Vault polls this endpoint on a schedule set
by SPIRE's `X-Spiffe-Bundle-Refresh-Hint` response header (5 minutes in this demo).
When SPIRE rotates keys, it increments the `spiffe_sequence` counter in the bundle
response. Vault detects the change on its next poll and refreshes automatically — no
human intervention, no re-running scripts. This requires Vault and SPIRE to be on
the same network, which is why this approach runs Vault Enterprise locally in Docker.

The SPIRE federation bundle endpoint serves keys with `"use": "jwt-svid"`, which
Vault's `spiffebundle` parser requires. Standard OIDC JWKS endpoints omit this
field and cannot be used with `auth/spiffe/`.

---

## Shared Flow

Both approaches share the same SPIRE setup and workload identity flow:

```
SPIRE Server (CA + registry)
      │
      │  gRPC :8081  (node attestation)
      ▼
SPIRE Agent ──── Unix socket ────► Workload App
                                        │
                                        │  JWT-SVID
                                        │  (sub: spiffe://demo.realpage.local/workload/app)
                                        │  (aud: vault)
                                        │
                                        │  POST /v1/auth/spiffe/login
                                        │  Authorization: Bearer <jwt>
                                        ▼
                                   Vault auth/spiffe/
                                   workload_id_patterns: /workload/app
                                   policy: secret/data/realpage/* read
                                        │
                                        │  Vault token
                                        ▼
                                   secret/realpage/demo (KV-v2)
                                   → api_key, db_password
```

- Trust domain: `demo.realpage.local`
- Workload SPIFFE ID: `spiffe://demo.realpage.local/workload/app`
- Audience claim: `vault`

---

## Prerequisites

| Requirement | `hcp-vault-spiffe-auth/` | `local-vault-spiffe-auth/` |
|---|---|---|
| Docker Desktop | ✅ Required | ✅ Required |
| HCP Vault credentials | ✅ Required | Not needed |
| Vault Enterprise license | Not needed | ✅ Required |
| `vault` CLI | ✅ Required (runs setup script) | ✅ Required |
| `openssl` | Not needed | ✅ Required (TLS cert generation) |

> **Docker Desktop for macOS:** Both projects use VirtioFS bind mounts. Files
> written by container-root processes are not visible on the macOS host, and
> vice versa. All cert generation and data cleanup must be done from inside
> containers using `docker run --user 0`. Do not use `rm -rf data/` from the host.

---

## Run: `hcp-vault-spiffe-auth/` — HCP Vault with `profile=static`

### Step 0 — Clone and configure

> ⚠️ **Fill in `.env` before running any command.** Running with the
> placeholder values (`<your-cluster>`, `<your-root-or-admin-token>`)
> produces a confusing DNS error deep in the Python stack trace —
> `Failed to resolve '%3cuour-cluster%3e.vault.hashicorp.cloud'` — where
> the angle brackets are URL-encoded and the real cause (unfilled `.env`)
> is not obvious.

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration
cd hcp-vault-spiffe-auth
```

Edit `.env` with your HCP Vault Dedicated cluster details:

```
HCP_VAULT_ADDR=https://<your-cluster>.vault.hashicorp.cloud:8200
HCP_VAULT_TOKEN=<your-root-or-admin-token>
HCP_VAULT_NAMESPACE=admin
```

### Step 1 — Create runtime directories

```bash
mkdir -p data/{server,agent,sockets}
```

### Step 2 — Start SPIRE Server

```bash
docker compose up -d spire-server
sleep 5
docker logs hcp-spire-server 2>&1 | grep "Starting Server APIs"
```

Expected:
```
Starting Server APIs address=[::]:8081
```

### Step 3 — Generate join token and update `agent.conf`

```bash
JOIN_TOKEN=$(docker exec hcp-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` and replace `REPLACE_WITH_GENERATED_TOKEN`:

```hcl
join_token = "<paste token here>"
```

> Join tokens are **single-use**. Every full restart from scratch requires a new
> token. If attestation fails, generate a new token — even a failed attempt
> consumes it.

### Step 4 — Start SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs hcp-spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected:
```
Node attestation was successful  spiffe_id="spiffe://demo.realpage.local/spire/agent/join_token/<token>"
```

### Step 5 — Register the workload SPIFFE entry

```bash
AGENT_ID=$(docker logs hcp-spire-agent 2>&1 \
  | grep "Node attestation was successful" \
  | grep -o 'spiffe://[^"]*')

docker exec hcp-spire-server \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://demo.realpage.local/workload/app \
  -parentID "$AGENT_ID" \
  -selector unix:uid:0

docker exec hcp-spire-server \
  /opt/spire/bin/spire-server entry show
```

### Step 6 — Configure HCP Vault

```bash
source .env
export VAULT_ADDR="$HCP_VAULT_ADDR" VAULT_TOKEN="$HCP_VAULT_TOKEN" VAULT_NAMESPACE="$HCP_VAULT_NAMESPACE"
cd vault && bash setup-hcp-spiffe.sh && cd ..
```

This script:
- Enables `auth/spiffe/` with `-passthrough-request-headers="Authorization"` on your HCP Vault cluster
- Extracts only the JWT signing key from SPIRE's bundle (filters out the x509 key that has no `kid`)
- Configures `profile=static` with the JWT-only bundle
- Creates role `workload-role` with `workload_id_patterns="/workload/app"`
- Creates policy `workload-policy` granting read on `secret/data/realpage/*`
- Seeds the demo secret at `secret/realpage/demo`

Verify:

```bash
vault read auth/spiffe/config | grep -E "profile|trust_domain"
vault kv get secret/realpage/demo
```

### Step 7 — Build and run the workload

```bash
docker compose build workload
docker compose run --rm workload
```

**Or run directly on your Mac (Docker must be running):**

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
  JWT Claims:
    sub : spiffe://demo.realpage.local/workload/app
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
  Full flow: SPIRE -> HCP Vault SPIFFE auth -> KV secret read
```

### Teardown

```bash
docker compose down
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
```

> ⚠️ **Do not use `rm -rf data/` from the macOS terminal.** Docker
> Desktop uses VirtioFS for bind mounts — files written by container-root
> processes are cached in a separate layer and remain visible inside
> containers even after a host-side deletion. The symptom of a
> host-side delete is `x509: certificate signed by unknown authority`
> on the next agent startup. Always use the `docker run --user 0 ... alpine`
> cleanup commands shown above.

### Key rotation (profile=static)

SPIRE rotates its JWT signing key every 7 days (`ca_ttl = 168h` in `server.conf`).
After rotation, new JWT-SVIDs carry a new `kid` that Vault's static bundle no longer
recognises. Logins will fail with `403 permission denied` until the bundle is refreshed:

```bash
source .env
export VAULT_ADDR="$HCP_VAULT_ADDR" VAULT_TOKEN="$HCP_VAULT_TOKEN" VAULT_NAMESPACE="$HCP_VAULT_NAMESPACE"
cd vault && bash setup-hcp-spiffe.sh && cd ..
```

---

## Run: `local-vault-spiffe-auth/` — Vault Enterprise with `profile=https_web_bundle`

### Step 0 — Clone and configure

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration
cd local-vault-spiffe-auth
```

Edit `.env` with your Vault Enterprise license:

```
VAULT_LICENSE=<your-vault-enterprise-license-key>
SPIFFE_VAULT_ADDR=http://127.0.0.1:8200
SPIFFE_VAULT_TOKEN=root
```

### Step 1 — Create runtime directories

```bash
mkdir -p data/{server,agent,sockets}
```

### Step 2 — Generate TLS cert for the SPIRE federation bundle endpoint

> **Important:** This must be run inside a container. Docker Desktop for macOS uses
> VirtioFS for bind mounts — files written by the host are not visible inside
> containers. If you generate the cert directly on the host, SPIRE will fail to start.

```bash
docker run --rm --user 0 \
  -v "$(pwd)/data/server:/data" \
  python:3.12-slim bash -c "
    openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
      -keyout /data/bundle-server.key \
      -out    /data/bundle-server.crt \
      -subj '/CN=spire-server' \
      -addext 'subjectAltName=DNS:spire-server' 2>/dev/null
    echo 'Cert generated:'
    ls /data/
  "
```

Expected:
```
Cert generated:
bundle-server.crt  bundle-server.key
```

### Step 3 — Start SPIRE Server

```bash
docker compose up -d spire-server
sleep 5
docker logs local-spire-server 2>&1 | grep -E "Starting Server APIs|Serving bundle"
```

Expected:
```
Starting Server APIs address=[::]:8081
Serving bundle endpoint addr="0.0.0.0:8443" refresh_hint=5m0s
```

Both lines must appear. If only `8081` appears but not `Serving bundle endpoint`,
the TLS cert was not generated correctly — re-run Step 2.

### Step 4 — Generate join token and update `agent.conf`

```bash
JOIN_TOKEN=$(docker exec local-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` and replace `REPLACE_WITH_GENERATED_TOKEN`:

```hcl
join_token = "<paste token here>"
```

### Step 5 — Start SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs local-spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected:
```
Node attestation was successful  spiffe_id="spiffe://demo.realpage.local/spire/agent/join_token/<token>"
```

### Step 6 — Register the workload SPIFFE entry

```bash
AGENT_ID=$(docker logs local-spire-agent 2>&1 \
  | grep "Node attestation was successful" \
  | grep -o 'spiffe://[^"]*')

docker exec local-spire-server \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://demo.realpage.local/workload/app \
  -parentID "$AGENT_ID" \
  -selector unix:uid:0

docker exec local-spire-server \
  /opt/spire/bin/spire-server entry show
```

### Step 7 — Start Vault Enterprise

```bash
docker compose up -d vault-ent
sleep 8
curl -s http://localhost:8200/v1/sys/health \
  | python3 -m json.tool | grep -E "sealed|version"
```

Expected:
```json
"sealed": false,
"version": "2.0.0+ent",
```

### Step 8 — Configure Vault

```bash
cd vault && bash setup-spiffe.sh
```

The script generates the TLS cert check, then pauses and prints:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cert is ready. Start the full stack, then press Enter to configure  │
│  Vault (spire-server, spire-agent, vault-ent must all be healthy):   │
└──────────────────────────────────────────────────────────────────────┘
```

All three containers are already running — **press Enter** to continue.

```bash
cd ..   # return to project root after script completes
```

This script:
- Enables `auth/spiffe/` with `-passthrough-request-headers="Authorization"`
- Configures `profile=https_web_bundle` pointing at `https://spire-server:8443`
- Creates role `workload-role` with `workload_id_patterns="/workload/app"`
- Creates policy `workload-policy` granting read on `secret/data/realpage/*`
- Seeds the demo secret at `secret/realpage/demo`

Verify that Vault is polling the SPIRE bundle endpoint:

```bash
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root
vault read auth/spiffe/config | grep -E "profile|endpoint_url|cached_bundle"
```

Expected:
```
profile                        https_web_bundle
endpoint_url                   https://spire-server:8443
cached_bundle_sequence_number  1
cached_bundle_refresh_hint     5m0s
```

### Step 9 — Build and run the workload

```bash
docker compose build workload
docker compose run --rm workload
```

**Or run directly on your Mac (all containers must be running):**

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

### Step 10 — Verify key rotation transparency (optional)

One of the primary goals of `profile=https_web_bundle` is that SPIRE JWT signing
key rotation requires no manual intervention. To demonstrate this:

```bash
# Force a CA rotation on the SPIRE server
docker exec local-spire-server \
  /opt/spire/bin/spire-server ca rotate \
  -socketPath /tmp/spire-server/private/api.sock

# Check the current sequence number
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root
vault read auth/spiffe/config | grep cached_bundle_sequence_number

# Wait up to 5 minutes for Vault to poll and detect the rotation, then re-check:
# cached_bundle_sequence_number should have incremented by 1

# Confirm the workload still authenticates with the rotated key:
docker compose run --rm workload
```

### Teardown

```bash
docker compose down
docker run --rm --user 0 -v "$(pwd)/data/server:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
```

> ⚠️ **Do not use `rm -rf data/` from the macOS terminal.** Docker
> Desktop uses VirtioFS for bind mounts — files written by container-root
> processes are cached in a separate layer and remain visible inside
> containers even after a host-side deletion. The symptom of a
> host-side delete is `x509: certificate signed by unknown authority`
> on the next agent startup. Always use the `docker run --user 0 ... alpine`
> cleanup commands shown above.

> After teardown, the TLS cert is deleted along with `data/server/`. Re-run Step 2
> before starting again.

---

## Troubleshooting

### `403 permission denied` on Vault SPIFFE login

**Cause A:** The `Authorization: Bearer` header was not enabled as a passthrough header
on the Vault auth mount. The setup scripts handle this automatically. Re-run the
relevant setup script.

**Cause B** (local approach only): `endpoint_url` points at a standard JWKS endpoint
(e.g. an OIDC Discovery Provider) instead of the SPIRE federation endpoint. Only
`https://spire-server:8443` returns the SPIFFE bundle format that Vault's
`spiffebundle` parser requires (`"use":"jwt-svid"` on JWT signing keys).

### `x509: certificate signed by unknown authority` during agent startup

Stale trust bundle in `data/agent/` from a previous session with a different SPIRE
Server CA. Clear agent data and generate a new join token:

```bash
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
# Generate new join token (Step 3/4) and update agent.conf
```

### SPIRE Server exits immediately after "Serving bundle endpoint" (local approach)

A `./data/server-api:/tmp/spire-server/private` bind mount in docker-compose is
causing a silent crash — SPIRE manages that directory internally. Remove the mount
from the `spire-server` service if you added it.

### `time: invalid duration ""` on SPIRE Server startup (local approach)

The `serving_cert_file` block in `server.conf` is missing `file_sync_interval`.
It must be set explicitly — omitting it causes a Go parse panic:

```hcl
serving_cert_file {
  cert_file_path     = "/opt/spire/data/server/bundle-server.crt"
  key_file_path      = "/opt/spire/data/server/bundle-server.key"
  file_sync_interval = "1m"
}
```

### bundle-server.crt not found on SPIRE Server startup (local approach)

The TLS cert was generated on the macOS host instead of inside a container.
VirtioFS prevents the container from reading host-written files. Re-run Step 2
exactly as shown using `docker run --user 0`.

### Join token rejected during attestation

Tokens are single-use — even a failed attestation attempt consumes the token.
Generate a new token (Step 3 or Step 4) and update `agent.conf`.

### `rpc error: Token has already been used`

The join token in `agent.conf` was consumed by a prior attestation attempt —
including failed ones. Tokens are single-use regardless of whether attestation
succeeded. Generate a new token and update `agent.conf`:

```bash
# HCP approach:
JOIN_TOKEN=$(docker exec hcp-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local | awk '{print $NF}')

# Local approach:
JOIN_TOKEN=$(docker exec local-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local | awk '{print $NF}')

echo "New token: $JOIN_TOKEN"
# Update join_token = "..." in spire/agent/agent.conf
```

---

## Production Considerations

| POC Approach | Production Replacement |
|---|---|
| Join token node attestation | Cloud-native attestor: AWS IID, Azure MSI, Kubernetes PSAT |
| `insecure_bootstrap = true` | Pre-distribute SPIRE Server CA bundle as `trust_bundle_path` |
| SQLite datastore | PostgreSQL or MySQL for SPIRE Server HA |
| Vault Enterprise dev mode (in-memory) | Vault Enterprise with Raft integrated storage |
| Self-signed cert for bundle endpoint | CA-signed cert or ACME (Let's Encrypt) |
| `profile=static` on HCP Vault | `profile=https_web_bundle` when SPIRE is reachable from Vault |
| Root containers (`user: "0"`) | Non-root with correct volume permissions |
| Shared PID namespace | Kubernetes — use the SPIRE K8s workload registrar |

### ACME configuration for production bundle endpoint

When SPIRE is deployed with a publicly accessible domain, replace the
`serving_cert_file` block with an `acme` block — no `endpoint_root_ca_truststore_pem`
is needed in Vault's config since Let's Encrypt is already trusted:

```hcl
federation {
  bundle_endpoint {
    address = "0.0.0.0"
    port    = 443
    profile "https_web" {
      acme {
        domain_name  = "spire.yourcompany.com"
        email        = "platform@yourcompany.com"
        tos_accepted = true
      }
    }
  }
}
```

---

## References

- [SPIFFE/SPIRE Documentation](https://spiffe.io/docs/latest/)
- [Vault SPIFFE Auth Method](https://developer.hashicorp.com/vault/docs/auth/spiffe)
- [Vault SPIFFE Auth API](https://developer.hashicorp.com/vault/api-docs/auth/spiffe)
- [SPIRE Server Configuration Reference](https://spiffe.io/docs/latest/deploying/spire_server/)
- [SPIFFE Federation](https://spiffe.io/docs/latest/architecture/federation/readme/)
- [Vault Enterprise 2.0 Release Notes](https://www.hashicorp.com/en/blog/vault-enterprise-20-modernizes-identity-security-at-scale)
