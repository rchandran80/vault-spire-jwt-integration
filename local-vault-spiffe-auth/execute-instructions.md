# Execution Instructions

**SPIRE + Vault Enterprise — SPIFFE Auth Method POC**
**Branch:** `feature/spiffe-auth-method`

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Desktop | Must be running |
| `vault` CLI | `brew install vault` |
| Vault Enterprise license | Set as `VAULT_LICENSE` in `.env` |
| `openssl` | System default on macOS is fine |

---

## First-Time Setup

### Step 0 — Clone and configure `.env`

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration/spire-vault-demo
git checkout feature/spiffe-auth-method
```

Edit `.env` and set:

```bash
VAULT_LICENSE=<your-enterprise-license-key>
SPIFFE_VAULT_ADDR=http://127.0.0.1:8200
SPIFFE_VAULT_TOKEN=root
```

---

### Step 1 — Create runtime directories

```bash
mkdir -p data/{server,agent,sockets}
```

---

### Step 2 — Generate TLS cert for the SPIRE bundle endpoint

> **Important:** This must be run inside a container. Docker Desktop for macOS uses VirtioFS
> for bind mounts — files written by the macOS host are not visible inside containers. If you
> generate the cert directly on the host, SPIRE will fail to start with
> `bundle-server.crt: no such file or directory`.

```bash
docker run --rm --user 0 \
  -v "$(pwd)/data/server:/data" \
  python:3.12-slim bash -c "
    openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
      -keyout /data/bundle-server.key \
      -out    /data/bundle-server.crt \
      -subj '/CN=spire-server' \
      -addext 'subjectAltName=DNS:spire-server' 2>/dev/null
    echo 'Cert generated'; ls /data/
  "
```

Expected output:
```
Cert generated
bundle-server.crt  bundle-server.key
```

---

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

---

### Step 4 — Generate join token and update `agent.conf`

```bash
JOIN_TOKEN=$(docker exec local-spire-server \
  /opt/spire/bin/spire-server token generate \
  -spiffeID spiffe://demo.realpage.local/agent/local \
  | awk '{print $NF}')
echo "Token: $JOIN_TOKEN"
```

Edit `spire/agent/agent.conf` and replace the `join_token` value:

```hcl
join_token = "<paste token here>"
```

> **Note:** Join tokens are single-use. Every full restart from scratch requires a new token.

---

### Step 5 — Start SPIRE Agent

```bash
docker compose up -d spire-agent
sleep 8
docker logs local-spire-agent 2>&1 | grep "Node attestation was successful"
```

Expected:
```
Node attestation was successful  spiffe_id="spiffe://.../join_token/<token>"
```

---

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
```

Verify:

```bash
docker exec local-spire-server /opt/spire/bin/spire-server entry show
```

---

### Step 7 — Start Vault Enterprise

```bash
docker compose up -d vault-ent
sleep 8
curl -s http://localhost:8200/v1/sys/health | python3 -c \
  "import sys,json; h=json.load(sys.stdin); print('sealed:', h['sealed'], '| version:', h['version'])"
```

Expected:
```
sealed: False | version: 2.0.0+ent
```

---

### Step 8 — Configure Vault (SPIFFE auth + KV)

```bash
cd vault && bash setup-spiffe.sh && cd ..
```

The script pauses once — press **Enter** when prompted (all services are already running).

Verify after setup completes:

```bash
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root
vault read auth/spiffe/config | grep -E "profile|endpoint_url|cached_bundle_sequence"
vault secrets list | grep secret
```

Expected:
```
profile                        https_web_bundle
endpoint_url                   https://spire-server:8443
cached_bundle_sequence_number  1
secret/          kv-v2
```

---

### Step 9 — Build and run workload

```bash
docker compose build workload
docker compose run --rm workload
```

Expected final lines:
```
STEP 3: Read Secret from Vault
  [OK] Secret retrieved successfully
  Secret Data:
    api_key = super-secret-demo-key
    db_password = demo-db-pass-123

RESULT: SUCCESS
  Full flow: SPIRE -> Vault SPIFFE auth -> KV secret read
  COMPLETE
```

---

## Subsequent Runs (after `docker compose down`)

After a full teardown, clear data from **inside containers** — do not use `rm -rf data/*`
from the macOS host, as Docker Desktop VirtioFS isolation means host-written deletions may
not be visible inside containers.

```bash
docker run --rm --user 0 -v "$(pwd)/data/server:/data"  alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/agent:/data"   alpine sh -c "rm -rf /data/*"
docker run --rm --user 0 -v "$(pwd)/data/sockets:/data" alpine sh -c "rm -rf /data/*"
mkdir -p data/{server,agent,sockets}
```

Then repeat **Steps 2–9**.

**Shortcut** — If only `vault-ent` was restarted and the SPIRE stack is still intact, skip
Steps 1–6 and re-run Steps 7, 8, and 9 only.

---

## Troubleshooting

### SPIRE Server exits immediately after "Serving bundle endpoint"

**Cause:** A `./data/server-api:/tmp/spire-server/private` bind mount in docker-compose
conflicts with SPIRE's internal admin socket creation.

**Fix:** Remove that volume entry from the `spire-server` service in `docker-compose.yml`.

---

### `x509: certificate signed by unknown authority` during agent attestation

**Cause:** Stale trust bundle in `data/agent/` from a previous run with a different SPIRE
Server CA.

**Fix:** Clear agent data from inside a container, generate a new join token (Step 4), and
restart the agent.

```bash
docker run --rm --user 0 -v "$(pwd)/data/agent:/data" alpine sh -c "rm -rf /data/*"
```

---

### `bundle-server.crt: no such file or directory` on SPIRE Server startup

**Cause:** The TLS cert was generated on the macOS host. Host-written files are not visible
inside containers via VirtioFS.

**Fix:** Re-run Step 2 exactly as shown using `docker run --user 0`.

---

### Vault SPIFFE login returns `403 permission denied`

**Cause A:** The `Authorization` header is being stripped by Vault before reaching the SPIFFE
plugin. The `vault auth enable` command must include `-passthrough-request-headers="Authorization"`.
Re-run `setup-spiffe.sh`.

**Cause B:** Vault's bundle cache contains no JWT authorities. Check `vault-ent` trace logs
for `spiffebundle: no authorities found`. This means `endpoint_url` points at a standard JWKS
endpoint (RFC 7517 format) that does not include `"use":"jwt-svid"` on the keys. Vault's
`spiffebundle` parser requires this field to register JWT signing authorities. Only the SPIRE
federation bundle endpoint (`https://spire-server:8443`) serves the correct SPIFFE bundle
format. Verify `endpoint_url` in Vault and re-run `setup-spiffe.sh`.

---

### Join token rejected during attestation

**Cause:** Tokens are single-use. Even a failed attestation attempt consumes the token.

**Fix:** Generate a new join token (Step 4) and update `agent.conf`.

---

### `time: invalid duration ""` on SPIRE Server startup

**Cause:** The `serving_cert_file` block inside `profile "https_web"` in `server.conf` is
missing `file_sync_interval`.

**Fix:** Add `file_sync_interval = "1m"` to the `serving_cert_file` block:

```hcl
serving_cert_file {
  cert_file_path     = "/opt/spire/data/server/bundle-server.crt"
  key_file_path      = "/opt/spire/data/server/bundle-server.key"
  file_sync_interval = "1m"
}
```

---

### Step 10 — Verify Key Rotation Transparency *(optional — strong demo proof point)*

This step demonstrates that SPIRE JWT key rotation requires no manual intervention in Vault.

```bash
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root

# Record the current bundle sequence number
vault read auth/spiffe/config | grep cached_bundle_sequence_number

# Force an immediate SPIRE CA rotation
docker exec local-spire-server /opt/spire/bin/spire-server ca rotate \
  -socketPath /tmp/spire-server/private/api.sock

# Wait up to 5 minutes for Vault's next poll cycle, then re-check
vault read auth/spiffe/config | grep cached_bundle_sequence_number
# The sequence number should have incremented by 1

# Confirm the workload authenticates successfully with the new signing key
docker compose run --rm workload
```

Expected: the sequence number increments and the workload run completes with `RESULT: SUCCESS`.

The `spiffe_sequence` counter and `refresh_hint` header are part of SPIRE's native federation
bundle protocol — Vault reads both automatically when polling the endpoint. No custom code or
polling scripts are required.

**What this proves:** On the `main` branch, this sequence would break workload authentication
until `setup.sh` is re-run manually. On this branch, Vault fetches the updated bundle
automatically and authentication continues without any operator action.

---

## Key Configuration Reference

| Setting | Value |
|---|---|
| Trust domain | `demo.realpage.local` |
| Workload SPIFFE ID | `spiffe://demo.realpage.local/workload/app` |
| JWT issuer (`jwt_issuer`) | `https://spire-server:8443` |
| Bundle endpoint | `https://spire-server:8443` (SPIRE federation, native) |
| Bundle auto-refresh | Every 5 minutes (set by SPIRE via `refresh_hint`) |
| Vault auth mount | `auth/spiffe/` |
| Vault role | `workload-role` |
| `workload_id_patterns` | `/workload/app` (path after trust domain is stripped) |
| Vault policy | `workload-policy` → allows `secret/data/realpage/*` |
| KV secret path | `secret/realpage/demo` (KV-v2) |
| Vault root token | `root` (dev mode — reset on every restart) |
| Vault API (host) | `http://localhost:8200` |
| SPIRE gRPC API (host) | `localhost:8081` |
| Bundle HTTPS (host) | `https://localhost:8444` → maps to container `:8443` |
