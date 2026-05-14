# Project Context — SPIRE + Vault SPIFFE Auth POC

## What This Project Is

A local Docker POC demonstrating workload identity using SPIFFE/SPIRE v1.14.1 and HashiCorp Vault.
A Python workload fetches a JWT-SVID from the SPIRE Agent and exchanges it for a Vault token to
read secrets from KV-v2. No static credentials involved.

Published repo: https://github.com/rchandran80/vault-spire-jwt-integration
Trust domain: `demo.realpage.local`
Workload SPIFFE ID: `spiffe://demo.realpage.local/workload/app`

---

## Current State (main branch)

Works end-to-end using Vault's **JWT auth method** (`auth/jwt/`) with a static EC public key
extracted from the SPIRE bundle. The workload posts the JWT in the request body to
`/v1/auth/jwt/login`. Targets HCP Vault Dedicated (cloud).

This is the working fallback. The feature branch replaces it with the purpose-built SPIFFE method.

---

## Why auth/spiffe/ Failed in the Previous Session (Critical Context)

Root cause: the `Authorization: Bearer <jwt>` header was stripped by Vault's request pipeline
before reaching the SPIFFE plugin.

Fix — one tuning flag applied at enable/tune time:
```bash
vault auth enable -passthrough-request-headers="Authorization" spiffe
# or on an existing mount:
vault auth tune -passthrough-request-headers="Authorization" spiffe/
```

This was NOT applied previously. Everything else (bundle, role, audience, JWT signature) was
correct.

---

## Next Task: feature/spiffe-auth-method Branch

Switch from `auth/jwt/` to `auth/spiffe/` using:
- A **local Vault Enterprise 2.0.0 container** on `spire-net` (HCP Vault cannot reach local Docker)
- The **SPIRE OIDC Discovery Provider** container so Vault uses `profile=https_web_bundle` for
  automatic bundle refresh on key rotation (this is the key capability to demonstrate to RealPage)

### Why `https_web_bundle` instead of `static`

`static` requires re-running setup on every SPIRE JWT key rotation (every 168h = 7 days per
`ca_ttl` in server.conf). `https_web_bundle` has Vault poll the OIDC provider's JWKS endpoint
automatically — key rotation is transparent. This is what RealPage would use in production.

### Why local Vault Enterprise (not HCP Vault)

HCP Vault is cloud-hosted and cannot reach containers on a local Docker network. Running
Vault Enterprise locally on `spire-net` means Vault can directly reach the OIDC Discovery
Provider at `https://spire-oidc:443/keys` on the same Docker bridge network.

---

## Architecture: feature/spiffe-auth-method

```
spire-net (Docker bridge)
│
├── spire-server     (port 8081)   — CA, registry, JWT signing keys
├── spire-agent                    — node-attested, serves workload API via Unix socket
├── spire-oidc       (port 443)    — OIDC Discovery Provider: exposes /keys (JWKS)
│                                    TLS via self-signed cert (mounted from host)
├── vault-ent        (port 8200)   — Vault Enterprise 2.0.0 dev mode
│                                    auth/spiffe/ with profile=https_web_bundle
│                                    polls https://spire-oidc:443/keys for bundle
└── workload                       — Python app, JWT-SVID → auth/spiffe/login
```

---

## Files to Create/Modify (feature branch only)

**Do NOT modify on this branch:**
`vault/setup.sh`, `vault/policy.hcl`, `spire/server/server.conf`, `spire/agent/agent.conf`,
`workload/Dockerfile`, `workload/requirements.txt`

| File | Change |
|---|---|
| `docker-compose.yml` | Add `vault-ent` and `spire-oidc` services; update `workload` env |
| `.env` | Add `VAULT_LICENSE`, `SPIFFE_VAULT_ADDR=http://127.0.0.1:8200`, `SPIFFE_VAULT_TOKEN=root` |
| `spire/oidc/oidc-discovery-provider.conf` | New — OIDC provider config |
| `data/oidc/` | New directory — TLS cert/key written here by setup-spiffe.sh at runtime |
| `vault/setup-spiffe.sh` | New — generates TLS cert, configures auth/spiffe/, seeds secret |
| `workload/app.py` | Switch to auth/spiffe/ endpoint, Authorization: Bearer header, namespace guard |

---

## docker-compose.yml Additions

```yaml
  spire-oidc:
    image: ghcr.io/spiffe/oidc-discovery-provider:1.14.1
    container_name: spire-oidc
    user: "0"
    depends_on:
      - spire-agent
    volumes:
      - ./spire/oidc:/opt/spire/conf/oidc        # provider config
      - ./data/sockets:/opt/spire/sockets          # agent socket (for bundle polling)
      - ./data/oidc:/opt/spire/data/oidc           # TLS cert + key written by setup
    command: ["-config", "/opt/spire/conf/oidc/oidc-discovery-provider.conf"]
    ports:
      - "8443:443"
    networks:
      - spire-net

  vault-ent:
    image: hashicorp/vault-enterprise:2.0.0-ent
    container_name: vault-ent
    user: "0"
    cap_add:
      - IPC_LOCK
    environment:
      - VAULT_LICENSE=${VAULT_LICENSE}
      - VAULT_DEV_ROOT_TOKEN_ID=root
      - VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200
    command: ["vault", "server", "-dev", "-dev-root-token-id=root",
              "-dev-listen-address=0.0.0.0:8200"]
    ports:
      - "8200:8200"
    networks:
      - spire-net
```

Update the `workload` service:
```yaml
    depends_on:
      - spire-agent
      - vault-ent
    environment:
      - VAULT_ADDR=http://vault-ent:8200
      - VAULT_NAMESPACE=
      - SPIFFE_ENDPOINT_SOCKET=unix:///opt/spire/sockets/agent.sock
```

---

## spire/oidc/oidc-discovery-provider.conf

```hcl
log_level  = "DEBUG"
log_format = "text"

domains = ["spire-oidc"]

# Poll the SPIRE Agent workload API to retrieve the JWT bundle (public keys)
workload_api {
  socket_path  = "/opt/spire/sockets/agent.sock"
  trust_domain = "demo.realpage.local"
  poll_interval = "10s"
}

# Serve HTTPS using a static cert/key pair generated by setup-spiffe.sh
serving_cert_file {
  cert_file_path = "/opt/spire/data/oidc/server.crt"
  key_file_path  = "/opt/spire/data/oidc/server.key"
}

# Health check endpoints
health_checks {
  bind_port = "8080"
  live_path  = "/live"
  ready_path = "/ready"
}
```

---

## TLS for the OIDC Discovery Provider

### What the image contains
The `ghcr.io/spiffe/oidc-discovery-provider:1.14.1` image is distroless — binary only at
`/opt/spire/bin/oidc-discovery-provider`. No shell, no pre-built certs, no CA material.

### Three TLS options the provider supports
| Option | Mechanism | POC viable? |
|---|---|---|
| ACME | Auto-fetches from Let's Encrypt | No — requires public DNS |
| `serving_cert_file` | Static cert + key you provide | Yes — self-signed |
| `insecure_addr` | HTTP only | No — `https_web_bundle` requires HTTPS |

### Chosen approach: self-signed cert generated in setup-spiffe.sh

`setup-spiffe.sh` generates a self-signed cert and key into `./data/oidc/` before the OIDC
provider container starts. The self-signed cert also serves as its own CA:

```bash
mkdir -p ../data/oidc
openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
  -keyout ../data/oidc/server.key \
  -out    ../data/oidc/server.crt \
  -subj "/CN=spire-oidc" \
  -addext "subjectAltName=DNS:spire-oidc"
```

The same `server.crt` is passed to Vault as `endpoint_root_ca_truststore_pem`.

### SPIRE Server bundle endpoint (port 8443) — NOT needed
The SPIRE Server bundle endpoint is only required for `profile=https_spiffe_bundle` (SPIFFE
federation mTLS). For `profile=https_web_bundle`, Vault talks to the OIDC provider only.
The SPIRE Server bundle endpoint stays disabled in server.conf.

---

## vault/setup-spiffe.sh Outline

```bash
#!/usr/bin/env bash
set -euo pipefail

source ../.env 2>/dev/null || true

export VAULT_ADDR="${SPIFFE_VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_TOKEN="${SPIFFE_VAULT_TOKEN:-root}"
unset VAULT_NAMESPACE

# 1. Generate self-signed TLS cert for spire-oidc (if not already present)
mkdir -p ../data/oidc
if [ ! -f ../data/oidc/server.crt ]; then
  openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
    -keyout ../data/oidc/server.key \
    -out    ../data/oidc/server.crt \
    -subj "/CN=spire-oidc" \
    -addext "subjectAltName=DNS:spire-oidc"
fi
OIDC_CA_PEM=$(cat ../data/oidc/server.crt)

# 2. Enable SPIFFE auth with Authorization header passthrough
vault auth enable -passthrough-request-headers="Authorization" spiffe || echo "Already enabled"

# 3. Write policy
vault policy write workload-policy policy.hcl

# 4. Configure auth/spiffe/ using https_web_bundle profile
#    Vault polls https://spire-oidc:443/keys automatically on rotation
vault write auth/spiffe/config \
  trust_domain="demo.realpage.local" \
  profile="https_web_bundle" \
  endpoint_url="https://spire-oidc/keys" \
  endpoint_root_ca_truststore_pem="$OIDC_CA_PEM" \
  audience="vault"

# 5. Create role — workload_id_patterns is the path AFTER trust domain
vault write auth/spiffe/role/workload-role \
  workload_id_patterns="/workload/app" \
  token_policies="workload-policy" \
  token_ttl="1h"

# 6. Seed secret
vault secrets enable -path=secret kv-v2 || echo "Already enabled"
vault kv put secret/realpage/demo \
  api_key="super-secret-demo-key-spiffe" \
  db_password="demo-db-pass-spiffe-789"
```

### Critical: workload_id_patterns value
The SPIFFE plugin strips the trust domain before pattern matching:
`spiffe://demo.realpage.local/workload/app` → workload_id = `/workload/app`
Pattern must be `/workload/app` NOT the full SPIFFE ID.

### Critical: Full SPIFFE bundle vs JWT-only
With `https_web_bundle`, Vault fetches the bundle from the OIDC provider itself — you do NOT
manually pass the bundle. The OIDC provider serves only JWT keys (it reads from the workload
API which returns the JWT bundle). The x509/kid issue from the previous session does NOT apply.

---

## workload/app.py Changes

Three changes from main branch:

**1. Default VAULT_ADDR:**
```python
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://localhost:8200")
```

**2. Namespace guard (dev server has no namespace):**
```python
VAULT_NAMESPACE = os.environ.get("VAULT_NAMESPACE", "")
# In both vault_login and read_secret, only set header if non-empty:
headers = {}
if VAULT_NAMESPACE:
    headers["X-Vault-Namespace"] = VAULT_NAMESPACE
```

**3. vault_login — SPIFFE endpoint + Authorization header:**
```python
def vault_login(jwt_token):
    url = f"{VAULT_ADDR}/v1/auth/spiffe/login"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    if VAULT_NAMESPACE:
        headers["X-Vault-Namespace"] = VAULT_NAMESPACE
    resp = requests.post(url, json={"role": VAULT_ROLE}, headers=headers)
    ...
```

---

## End-to-End Execution Order

```bash
# 1. Fill .env with VAULT_LICENSE
# 2. mkdir -p data/{server,agent,sockets,oidc}
# 3. Start SPIRE Server
docker compose up -d spire-server && sleep 5
# 4. Generate join token → paste into spire/agent/agent.conf
# 5. Start SPIRE Agent
docker compose up -d spire-agent && sleep 8
# 6. Register workload entry on SPIRE Server (parentID = agent's attested SPIFFE ID)
# 7. Generate OIDC TLS cert + start OIDC provider
cd vault && bash setup-spiffe.sh  # generates cert, then:
docker compose up -d spire-oidc && sleep 5
# 8. Start Vault Enterprise
docker compose up -d vault-ent && sleep 5
# 9. Configure Vault (auth/spiffe/, role, policy, secret) — setup-spiffe.sh continues
# 10. Build and run workload
docker compose build workload
docker compose run --rm workload
```

Note: setup-spiffe.sh generates the TLS cert before the OIDC provider starts so the cert
is in place when the container mounts `./data/oidc/`.

---

## Key Technical Details (Preserved from Main Branch)

### Join Token
- `spire/agent/agent.conf` has `join_token = "REPLACE_WITH_GENERATED_TOKEN"`
- Regenerate with: `docker exec spire-server /opt/spire/bin/spire-server token generate -spiffeID spiffe://demo.realpage.local/agent/local | awk '{print $NF}'`
- Workload entry `parentID` must match agent's actual attested SPIFFE ID:
  `spiffe://demo.realpage.local/spire/agent/join_token/<token-value>`

### PID Namespace Sharing
- `workload` service uses `pid: "service:spire-agent"` — required so the unix WorkloadAttestor
  resolves the calling process UID across container boundaries

### macOS Host Mode
- app.py detects `/.dockerenv`; on Mac uses `docker exec spire-agent` for the SPIRE binary
- SPIRE publishes Linux-only binaries — no macOS binary exists

### Vault Enterprise License
- Required in `.env` as `VAULT_LICENSE=<key>`
- Dev mode has a 6-hour grace without license; seal after that

### Commit Hygiene
- No `Co-Authored-By` trailers in commit messages
- Commits authored solely by Roopesh Chandran
- Push to: https://github.com/rchandran80/vault-spire-jwt-integration
