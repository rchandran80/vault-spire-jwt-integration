# SPIRE + HashiCorp Vault SPIFFE Auth — Local Docker POC

A self-contained proof-of-concept that demonstrates **workload identity** using [SPIFFE/SPIRE](https://spiffe.io) and [HashiCorp Vault](https://www.vaultproject.io). A containerised workload proves its identity to Vault using a short-lived cryptographically signed JWT-SVID — no static secrets, passwords, or API keys required at boot time.

---

## Architecture

```
┌──────────────────────────────── Docker (local Mac) ─────────────────────────────────┐
│                                                                                       │
│  ┌─────────────────┐   gRPC 8081   ┌─────────────────┐   Unix socket (bind mount)   │
│  │  SPIRE Server   │◄──────────────│  SPIRE Agent    │◄──────────────────────────── │
│  │  (CA + registry)│               │  (node attested)│                              │
│  └─────────────────┘               └────────┬────────┘                              │
│                                             │  JWT-SVID                              │
│                                    ┌────────▼────────┐                              │
│                                    │  Workload App   │                              │
│                                    │  (Python)       │                              │
│                                    └────────┬────────┘                              │
└─────────────────────────────────────────────┼─────────────────────────────────────-─┘
                                              │ HTTPS  POST /v1/auth/jwt/login
                                              ▼
                                  ┌───────────────────────┐
                                  │   HCP Vault Dedicated  │
                                  │   (cloud-hosted)       │
                                  │   auth/jwt/            │
                                  │   secret/realpage/demo │
                                  └───────────────────────┘
```

### Flow

1. **SPIRE Server** acts as a Certificate Authority for the trust domain `demo.realpage.local` and maintains a registry of workload identities.
2. **SPIRE Agent** attests to the server using a one-time join token and receives its own X.509 SVID.
3. **Workload App** connects to the Agent over a Unix socket and requests a **JWT-SVID** scoped to the audience `vault`.
4. The workload presents the JWT-SVID to **HCP Vault's JWT auth method** (`auth/jwt/`). Vault validates the signature against the SPIRE JWT signing key, checks `bound_audiences` and `bound_subject`, and issues a short-lived Vault token.
5. The workload uses the Vault token to read secrets from `secret/realpage/demo` — access enforced by policy.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| macOS | Apple Silicon or Intel | Tested on both |
| Docker Desktop | 4.x+ | Must be running |
| HCP Vault Dedicated | Any | Admin-level token required for setup |
| Python 3.9+ | System Python is fine | For running `app.py` directly on host |
| `vault` CLI | 1.x+ | For running `vault/setup.sh` |
| `gh` CLI | 2.x+ | Optional — for cloning this repo |

> **Note:** SPIRE does not publish macOS binaries. The workload app detects when it is running on a Mac and automatically uses `docker exec spire-agent` to reach the SPIRE binary inside the container.

---

## Project Structure

```
spire-vault-demo/
├── .env                        # Vault credentials — fill in before running
├── docker-compose.yml          # SPIRE Server, Agent, and Workload services
├── spire/
│   ├── server/server.conf      # SPIRE Server configuration
│   └── agent/agent.conf        # SPIRE Agent configuration (join token goes here)
├── vault/
│   ├── policy.hcl              # Vault policy granting read on secret/realpage/*
│   └── setup.sh                # One-time Vault configuration script
└── workload/
    ├── app.py                  # Python workload — fetches SVID, authenticates, reads secret
    ├── Dockerfile              # Multi-stage: copies spire-agent binary into Python image
    └── requirements.txt        # hvac, requests
```

Runtime state (SQLite, agent data, Unix socket) is written to `./data/` which is bind-mounted into containers. This directory is `.gitignore`d.

---

## Setup

### 1. Clone and configure credentials

```bash
git clone https://github.com/rchandran80/vault-spire-integration.git
cd vault-spire-integration/spire-vault-demo
```

Edit `.env` with your HCP Vault cluster details:

```bash
HCP_VAULT_ADDR=https://<your-cluster>.vault.hashicorp.cloud:8200
HCP_VAULT_TOKEN=<your-root-or-admin-token>
HCP_VAULT_NAMESPACE=admin
```

### 2. Create runtime data directories

```bash
mkdir -p data/{server,agent,sockets}
```

### 3. Start the SPIRE Server

```bash
docker compose up -d spire-server
sleep 5
docker logs spire-server 2>&1 | grep "Starting Server APIs"
```

Expected: `Starting Server APIs address="[::]:8081"`

### 4. Generate a join token and inject it into agent.conf

```bash
JOIN_TOKEN=$(docker exec spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` and replace `REPLACE_WITH_GENERATED_TOKEN` with the value printed above:

```hcl
join_token = "<paste token here>"
```

### 5. Start the SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected: `Node attestation was successful`

### 6. Register the workload entry

```bash
# Get the agent's SPIFFE ID from the attestation log
AGENT_SPIFFE_ID=$(docker logs spire-agent 2>&1 \
  | grep "Node attestation was successful" \
  | grep -o 'spiffe://[^ ]*')

docker exec spire-server \
  /opt/spire/bin/spire-server entry create \
  -spiffeID spiffe://demo.realpage.local/workload/app \
  -parentID "$AGENT_SPIFFE_ID" \
  -selector unix:uid:0
```

Verify:

```bash
docker exec spire-server /opt/spire/bin/spire-server entry show
```

### 7. Configure HCP Vault

Ensure the `vault` CLI is installed and `source .env` has been run, then:

```bash
cd vault && bash setup.sh && cd ..
```

This script:
- Enables the JWT auth method at `auth/jwt/`
- Extracts the SPIRE JWT signing public key and registers it as `jwt_validation_pubkeys`
- Creates role `workload-role` bound to `spiffe://demo.realpage.local/workload/app`
- Creates policy `workload-policy` granting read on `secret/realpage/*`
- Seeds a test secret at `secret/realpage/demo`

Verify:

```bash
vault auth list   # should show jwt/
vault read auth/jwt/role/workload-role
```

### 8. Run the workload

**Option A — inside Docker (recommended for first run):**

```bash
docker compose build workload
docker compose run --rm workload
```

**Option B — directly on your Mac:**

```bash
cd workload
pip3 install -r requirements.txt
python3 app.py
```

> The app auto-detects it is running on the host and uses `docker exec spire-agent` to reach the SPIRE binary. The `spire-agent` container must be running.

### Expected output

```
============================================================
SPIRE + HashiCorp Vault SPIFFE Auth Demo
Trust Domain : demo.realpage.local
Workload SVID: spiffe://demo.realpage.local/workload/app
Vault Cluster: HCP Vault Dedicated
============================================================

STEP 1: Fetch JWT-SVID from SPIRE Agent
  [OK] JWT-SVID received from SPIRE Agent
  JWT Claims:
    sub : spiffe://demo.realpage.local/workload/app
    aud : ['vault']

STEP 2: Authenticate to HCP Vault with JWT-SVID
  [OK] Vault authentication successful
  policies : ['default', 'workload-policy']

STEP 3: Read Secret from HCP Vault
  [OK] Secret retrieved successfully
  Secret Data:
    api_key = super-secret-demo-key
    db_password = demo-db-pass-123

RESULT: SUCCESS
```

---

## Notable Caveats

### Join token must be regenerated each run
The `join_token` in `spire/agent/agent.conf` is a **one-time-use value**. Every time the SPIRE Agent container is recreated from scratch (i.e. after `docker compose down`), you must:
1. Generate a new token with `spire-server token generate`
2. Update `agent.conf` before starting the agent

If the agent's `data/agent/` directory is preserved (not deleted), the agent can re-attest using its cached SVID without a new token.

### JWT signing key rotation
SPIRE rotates its JWT signing keys on a schedule (default every 24 hours). The `vault/setup.sh` script extracts the current key and pins it in Vault as a static `jwt_validation_pubkeys` value. After a key rotation, new JWTs will carry a different `kid` and Vault will reject them with 403.

**To handle this:** re-run `vault/setup.sh` after each SPIRE key rotation, or migrate to `jwks_url` (see [Production Considerations](#production-considerations) below).

### SPIRE auth method vs JWT auth method
HCP Vault ships with a `vault-plugin-auth-spiffe` plugin. This POC uses Vault's built-in **JWT auth method** (`auth/jwt/`) instead, for the following reasons:

- The SPIFFE plugin on this cluster returned 403 for all JWT-SVID login attempts via the `Authorization: Bearer` header, despite correct configuration. Investigation confirmed the plugin ignores `bound_audiences`, `bound_subject`, and `user_claim` parameters (they are silently dropped).
- The JWT auth method is the **official, documented path** for SPIRE JWT-SVID integration and is functionally equivalent — it validates the same SPIFFE JWT, enforces audience and subject claims, and issues identical Vault tokens.
- The SPIFFE plugin's primary purpose appears to be X.509 SVID (mTLS) authentication, not JWT bearer token auth.

### No macOS SPIRE binary
SPIRE only publishes Linux binaries. When `app.py` is run directly on macOS, it automatically falls back to `docker exec spire-agent` to invoke the binary inside the running container. This requires Docker to be running and the `spire-agent` container to be active.

### `insecure_bootstrap` is enabled
The agent config uses `insecure_bootstrap = true`, which skips TLS verification of the server certificate on first connection. This is intentional for a local dev setup where no pre-existing trust bundle is available. **Do not use this in production.**

### Shared PID namespace
The `workload` service in `docker-compose.yml` uses `pid: "service:spire-agent"`. This is required so the SPIRE Agent's Unix WorkloadAttestor can resolve the calling process's UID across container boundaries. Without this, the agent cannot attest the workload and will log `could not resolve caller information`.

---

## Production Considerations

This POC uses several shortcuts that are appropriate for local testing but should be replaced before production use:

| POC Approach | Production Replacement |
|---|---|
| `insecure_bootstrap = true` | Pre-distribute the SPIRE Server CA bundle as `trust_bundle_path` |
| Static `jwt_validation_pubkeys` in Vault | `jwks_url` pointing to SPIRE's bundle endpoint (port 8443), so Vault auto-refreshes on key rotation |
| Join token node attestation | Cloud-native attestor (AWS IID, GCP GCE, Azure MSI, Kubernetes PSAT) |
| SQLite datastore | PostgreSQL or MySQL for HA SPIRE Server |
| Single SPIRE Server | SPIRE Server HA with shared datastore |
| Shared PID namespace | Kubernetes — use the SPIRE K8s workload registrar instead |
| `user: "0"` (root containers) | Run SPIRE containers as non-root with appropriate volume permissions |

### JWKS URL configuration (recommended migration)

Once SPIRE Server is network-accessible from Vault, replace the static key in `setup.sh` with:

```bash
vault write auth/jwt/config \
  jwks_url="https://spire-server.internal:8443/keys" \
  default_role="workload-role"
```

And in `spire/server/server.conf`, re-enable the bundle endpoint:

```hcl
server {
  ...
  bundle_endpoint_enabled = true
  bundle_endpoint_address = "0.0.0.0"
  bundle_endpoint_port    = 8443
}
```

---

## Teardown

```bash
docker compose down
rm -rf data/
```

To fully reset including Vault configuration:

```bash
vault auth disable jwt
vault secrets disable secret
vault policy delete workload-policy
```

---

## References

- [SPIFFE/SPIRE Documentation](https://spiffe.io/docs/latest/)
- [Vault JWT Auth Method](https://developer.hashicorp.com/vault/docs/auth/jwt)
- [Vault SPIFFE Auth Method](https://developer.hashicorp.com/vault/docs/auth/spiffe)
- [SPIRE + Vault Integration Tutorial](https://developer.hashicorp.com/vault/tutorials/auth-methods/vault-spiffe)
- [HCP Vault Dedicated](https://developer.hashicorp.com/hcp/docs/vault)
