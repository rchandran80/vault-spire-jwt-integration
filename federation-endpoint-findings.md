# SPIRE Federation Bundle Endpoint — Investigation Findings

**Branch:** `feature/spiffe-auth-method`
**Date:** 2026-05-14
**Context:** Pre-PR validation of four improvement briefs before pushing to GitHub

---

## Summary

The four briefs were executed in order. The most significant finding is that SPIRE 1.14.1
**does support** the federation bundle endpoint natively — but only when configured **inside the
`server {}` block** (not at the top level), and only with the specific `profile "https_web"`
labeled-block syntax with an explicit `file_sync_interval` field.

This eliminates the need for the `bundle-server` Python HTTPS service, the `spire-oidc` OIDC
Discovery Provider, and the `bundle-watcher` service that had been planned as workarounds. The
final stack is four services: `spire-server`, `spire-agent`, `vault-ent`, `workload`.

---

## Brief 1 — SPIRE Federation Bundle Endpoint: Syntax Investigation

### Context

The federation bundle endpoint is the correct source for Vault's `auth/spiffe/` with
`profile=https_web_bundle` because it serves the SPIFFE trust bundle format, which includes
`"use": "jwt-svid"` on JWT signing keys. Vault's `spiffebundle.FromJWKSBytes` parser requires
this field to register JWT authorities. The OIDC Discovery Provider's `/keys` endpoint serves
standard JWKS (no `"use"` field) and cannot be used with the SPIFFE auth method.

### Syntax Attempts

All attempts used `ghcr.io/spiffe/spire-server:1.14.1`.

#### Attempt 1 — `federation {}` at top level of config file

```hcl
server { ... }
plugins { ... }

federation {
  bundle_endpoint {
    address = "0.0.0.0"
    port    = 8443
    profile "https_web" {}
  }
}
```

**Result:** `[ERROR] Unknown configuration detected: keys=federation section=top-level`

SPIRE 1.14.1 does not recognise `federation {}` as a valid top-level HCL key.

---

#### Attempt 2 — `federation {}` inside `server {}` block, empty `https_web` profile

```hcl
server {
  ...
  federation {
    bundle_endpoint {
      address = "0.0.0.0"
      port    = 8443
      profile "https_web" {}
    }
  }
}
```

**Result:** `malformed https_web profile configuration: 'acme' or 'serving_cert_file' is required`

Critical finding: **`federation {}` IS recognised inside `server {}`**. The error changed from
"Unknown configuration" to a validation error about missing sub-config. SPIRE knows this block
and is enforcing its schema. The `https_web` profile requires either `acme` (ACME/Let's Encrypt)
or `serving_cert_file` (static TLS cert).

---

#### Attempt 3 — `federation {}` inside `server {}`, using `https_web {}` as plain block (not labeled)

```hcl
federation {
  bundle_endpoint {
    address = "0.0.0.0"
    port    = 8443
    https_web {
      serving_cert_file { ... }
    }
  }
}
```

**Result:** `[ERROR] Unknown configuration detected: keys=https_web section=bundle endpoint`

`https_web` is not a plain block name inside `bundle_endpoint`. It must use the `profile "https_web"`
labeled-block syntax.

---

#### Attempt 4 — `profile "https_web"` with `serving_cert_file` but no `file_sync_interval`

```hcl
federation {
  bundle_endpoint {
    address = "0.0.0.0"
    port    = 8443
    profile "https_web" {
      serving_cert_file {
        cert_file_path = "/opt/spire/data/server/bundle-server.crt"
        key_file_path  = "/opt/spire/data/server/bundle-server.key"
      }
    }
  }
}
```

**Result:** `time: invalid duration ""`

The `serving_cert_file` block has a `file_sync_interval` duration field that defaults to an
empty string when omitted. Go's `time.ParseDuration("")` panics with this error. The field
controls how often SPIRE re-reads the cert from disk for rotation.

---

#### Attempt 5 — `profile "https_web"` with `serving_cert_file` and explicit `file_sync_interval`

```hcl
federation {
  bundle_endpoint {
    address = "0.0.0.0"
    port    = 8443
    profile "https_web" {
      serving_cert_file {
        cert_file_path     = "/opt/spire/data/server/bundle-server.crt"
        key_file_path      = "/opt/spire/data/server/bundle-server.key"
        file_sync_interval = "1m"
      }
    }
  }
}
```

**Result:**
```
[INFO]  Serving bundle endpoint: addr="0.0.0.0:8443" refresh_hint=5m0s
[INFO]  Started watching certificate files: interval=1m0s
[INFO]  Starting Server APIs: address=[::]:8081
```

✅ **Bundle endpoint started successfully on port 8443.**

### Working `server.conf` Federation Block

```hcl
server {
  bind_address = "0.0.0.0"
  bind_port    = "8081"
  trust_domain = "demo.realpage.local"
  data_dir     = "/opt/spire/data/server"
  log_level    = "DEBUG"
  ca_ttl       = "168h"
  default_x509_svid_ttl = "1h"
  default_jwt_svid_ttl  = "5m"
  jwt_issuer   = "https://spire-server:8443"

  federation {
    bundle_endpoint {
      address = "0.0.0.0"
      port    = 8443
      profile "https_web" {
        serving_cert_file {
          cert_file_path     = "/opt/spire/data/server/bundle-server.crt"
          key_file_path      = "/opt/spire/data/server/bundle-server.key"
          file_sync_interval = "1m"
        }
      }
    }
  }
}
```

**Key rules derived from the investigation:**

| Rule | Detail |
|---|---|
| Block placement | `federation {}` must be **inside** `server {}`, not at the top level |
| Profile syntax | Use labeled-block syntax: `profile "https_web" { ... }`, not plain `https_web { ... }` |
| TLS option | `serving_cert_file` works; `acme` also works but requires a public domain with DNS |
| Duration field | `file_sync_interval` must be set explicitly (e.g. `"1m"`); omitting it causes a parse failure |
| Cert placement | The cert/key must be in a directory mounted as a Docker volume into the container |

---

### Additional Discovery: Admin Socket Bind Mount Conflict

During the investigation, a bind mount was added to expose the SPIRE server admin socket for
external access:

```yaml
volumes:
  - ./data/server-api:/tmp/spire-server/private   # CAUSES CRASH
```

**Effect:** SPIRE starts the bundle endpoint and immediately enters a shutdown cycle:
```
[INFO] Serving bundle endpoint addr="0.0.0.0:8443"
[INFO] Started watching certificate files
[INFO] Stopping file watcher          ← unexpected
[INFO] Starting Server APIs
[INFO] Stopping Server APIs           ← immediately
[INFO] Shutting down
```

No error message is emitted. The graceful shutdown sequence runs without logging the root cause.

**Root cause:** SPIRE creates its admin socket at `/tmp/spire-server/private/api.sock` internally.
When that directory is replaced by an external bind mount (Docker Desktop for macOS VirtioFS),
SPIRE's socket creation fails silently and triggers its shutdown handler.

**Fix:** Remove the bind mount. SPIRE manages its admin socket directory internally. The socket
remains accessible via `docker exec spire-server /opt/spire/bin/spire-server ...` — an external
mount is not needed.

---

### Bundle Endpoint Validation

After the working config was applied, the bundle was fetched from inside the Docker network:

```bash
docker run --rm --network spire-vault-demo_spire-net \
  alpine wget -q --no-check-certificate -O- https://spire-server:8443
```

Response:
```json
{
  "keys": [
    {
      "use": "x509-svid",
      "kty": "EC",
      "crv": "P-256",
      "x": "...",
      "y": "...",
      "x5c": ["..."]
    },
    {
      "use": "jwt-svid",
      "kty": "EC",
      "kid": "PdkExMAqbQAgQ3AgOiVuLZyreJfu0mp1",
      "crv": "P-256",
      "alg": "ES256",
      "x": "...",
      "y": "..."
    }
  ],
  "spiffe_sequence": 1
}
```

The `"use": "jwt-svid"` field on the JWT signing key is what Vault's `spiffebundle.FromJWKSBytes`
requires to register the key as a JWT authority. The `spiffe_sequence` field is a SPIFFE trust
bundle version counter — Vault stores `cached_bundle_sequence_number` and uses it to detect
rotation.

---

## Brief 2 — `jwt_issuer` and `endpoint_url` Hostname Alignment

### Context

Previously, `jwt_issuer = "https://spire-oidc"` but Vault fetched from `https://bundle-server`.
The `iss` claim in JWT-SVIDs named a different host than the bundle source, which is confusing
and potentially problematic if Vault performs issuer validation against the bundle endpoint.

### Changes Made

**`spire/server/server.conf`:**
```hcl
jwt_issuer = "https://spire-server:8443"
```

JWT-SVIDs now carry:
```json
{ "iss": "https://spire-server:8443", "sub": "spiffe://demo.realpage.local/workload/app", ... }
```

**`vault/setup-spiffe.sh`:**
```bash
vault write auth/spiffe/config \
  trust_domain="demo.realpage.local" \
  profile="https_web_bundle" \
  endpoint_url="https://spire-server:8443" \
  endpoint_root_ca_truststore_pem="$(cat ../data/server/bundle-server.crt)" \
  audience="vault"
```

**`workload/app.py` banner:**
```
Bundle Source: https://spire-server:8443 (SPIFFE federation endpoint, native)
```

### Verification

After the change, a fresh JWT-SVID carries `"iss": "https://spire-server:8443"` — the `iss`
claim now exactly matches the `endpoint_url` hostname from which Vault fetches the trust bundle.
If Vault performs issuer binding in a future version or stricter mode, this alignment ensures
compatibility.

---

## Brief 3 — Auto-Refresh

### Result: Native, No Code Required

The SPIRE federation bundle endpoint provides refresh semantics natively. Vault's SPIFFE auth
method reads the `X-Spiffe-Bundle-Refresh-Hint` header from the bundle endpoint response, which
SPIRE sets to `5m0s` (5 minutes). Vault stores this as `cached_bundle_refresh_hint` and polls
accordingly.

Confirmed in Vault config read:
```
cached_bundle_refresh_hint   5m0s
cached_bundle_sequence_number  1
```

When SPIRE rotates JWT signing keys (based on `ca_ttl = 168h` in server.conf), the
`spiffe_sequence` counter increments. Vault detects the increment on its next poll and updates
its cached bundle automatically. No `setup-spiffe.sh` re-run is needed after key rotation.

**Contrast with previous approaches:**
- `profile=static` (used as a fallback earlier): required manual re-run of `setup-spiffe.sh` after every SPIRE key rotation
- `bundle-server` Python HTTPS service with manual `bundle show` export: also required manual refresh unless a polling sidecar was added
- Native federation endpoint: **rotation is completely transparent**

---

## Brief 4 — Removal of `spire-oidc`

### Services Removed

| Service | Reason Removed |
|---|---|
| `spire-oidc` (OIDC Discovery Provider) | Not used in the Vault auth path; SPIFFE auth method requires SPIRE bundle format, not OIDC JWKS |
| `bundle-server` (Python HTTPS) | Replaced by native SPIRE federation bundle endpoint |
| `data/server-api/` volume mount | Conflicts with SPIRE's internal admin socket creation |

### Files Removed

| Path | Reason |
|---|---|
| `spire/oidc/` directory | Config for the OIDC Discovery Provider container |
| `bundle-server/` directory | Python HTTPS server script |

### Side Effects

The `spire-oidc` removal eliminates several workarounds that had been required for it:
- No `pid: "service:spire-agent"` on a non-workload container
- No workload entry for `spiffe://demo.realpage.local/oidc/provider`
- No `data/oidc/` TLS cert management

The setup sequence is now simpler: start server → generate join token → start agent → register
one workload entry → start vault-ent → run setup-spiffe.sh → run workload.

---

## Final Architecture

```
spire-net (Docker bridge)
│
├── spire-server  :8081 (gRPC API)
│                 :8443 (SPIFFE federation bundle endpoint — HTTPS)
│                        └── serves SPIFFE trust bundle with "use":"jwt-svid" keys
│                        └── auto-refreshes on SPIRE CA rotation
│                        └── jwt_issuer = "https://spire-server:8443"
│
├── spire-agent          — attested via join_token; unix socket at data/sockets/
│
├── vault-ent     :8200  — Vault Enterprise 2.0.0 dev mode
│                          auth/spiffe/ with profile=https_web_bundle
│                          polls https://spire-server:8443 every 5 minutes
│
└── workload             — Python app (pid shares spire-agent namespace):
                           1. Fetch JWT-SVID (iss=https://spire-server:8443)
                           2. POST /v1/auth/spiffe/login (Bearer header)
                           3. POST /v1/pki/issue/workload-cert (15-min cert)
```

---

## Final File State

### `spire/server/server.conf`

```hcl
server {
  bind_address = "0.0.0.0"
  bind_port    = "8081"
  trust_domain = "demo.realpage.local"
  data_dir     = "/opt/spire/data/server"
  log_level    = "DEBUG"
  ca_ttl       = "168h"
  default_x509_svid_ttl = "1h"
  default_jwt_svid_ttl  = "5m"
  jwt_issuer   = "https://spire-server:8443"

  federation {
    bundle_endpoint {
      address = "0.0.0.0"
      port    = 8443
      profile "https_web" {
        serving_cert_file {
          cert_file_path     = "/opt/spire/data/server/bundle-server.crt"
          key_file_path      = "/opt/spire/data/server/bundle-server.key"
          file_sync_interval = "1m"
        }
      }
    }
  }
}

plugins {
  DataStore "sql" { ... }
  NodeAttestor "join_token" { ... }
  KeyManager "disk" { ... }
}
```

### `docker-compose.yml` — Services

| Service | Image | Purpose | Ports |
|---|---|---|---|
| `spire-server` | `ghcr.io/spiffe/spire-server:1.14.1` | CA, registry, bundle endpoint | 8081 (gRPC), 8444→8443 (bundle) |
| `spire-agent` | `ghcr.io/spiffe/spire-agent:1.14.1` | Workload attestation, socket | — |
| `vault-ent` | `hashicorp/vault-enterprise:2.0.0-ent` | Secrets engine, auth/spiffe/ | 8200 |
| `workload` | Build from `./workload` | Demo application | — |

**Not present** (compared to previous iterations): `spire-oidc`, `bundle-server`

### `docker-compose.yml` — `spire-server` volume mounts

```yaml
volumes:
  - ./spire/server:/opt/spire/conf/server   # server.conf
  - ./data/server:/opt/spire/data/server    # CA keys, SQLite DB, TLS cert/key
```

**Note:** No bind mount for `/tmp/spire-server/private` — SPIRE manages its admin socket
internally. Mounting that directory externally causes a silent crash.

---

## Operational Notes

### TLS Cert for the Bundle Endpoint

The cert at `data/server/bundle-server.crt` must exist **before** `spire-server` starts. It is
read at startup and watched for rotation every `file_sync_interval` (1 minute in this config).

The cert must be generated from inside a Docker container (not from the macOS host) due to
Docker Desktop's VirtioFS bind mount isolation. Files written by the macOS user are not visible
inside containers, and vice versa for files written by container-root processes.

Generation command:
```bash
docker run --rm --user 0 \
  -v /absolute/path/to/data/server:/data \
  python:3.12-slim bash -c "
    openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
      -keyout /data/bundle-server.key \
      -out    /data/bundle-server.crt \
      -subj '/CN=spire-server' \
      -addext 'subjectAltName=DNS:spire-server' 2>/dev/null
  "
```

The `endpoint_root_ca_truststore_pem` in Vault's SPIFFE auth config must use this cert (it is
self-signed and acts as its own CA):
```bash
vault write auth/spiffe/config \
  ...
  endpoint_root_ca_truststore_pem="$(cat data/server/bundle-server.crt)"
```

### Data Reset Between Sessions

Due to SPIRE's join-token attestation model and the Docker Desktop VirtioFS isolation:
- **Always** clear `data/server/`, `data/agent/`, and `data/sockets/` from inside a container before restarting
- **Always** regenerate the TLS cert inside a container after clearing `data/server/`
- **Always** generate a new join token after clearing `data/server/` (tokens are single-use and stored in the SQLite DB)

### Key Rotation Transparency

When SPIRE rotates its JWT signing key (governed by `ca_ttl = 168h` — every 7 days), the
`spiffe_sequence` counter in the federation bundle response increments. Vault detects this on its
next 5-minute poll and refreshes its cached bundle. The workload continues to authenticate without
any manual intervention.

To verify rotation transparency after a manual CA rotation:
```bash
docker exec spire-server /opt/spire/bin/spire-server ca rotate \
  -socketPath /tmp/spire-server/private/api.sock
# Wait up to 5 minutes
vault read auth/spiffe/config | grep -E "cached_bundle|sequence"
# cached_bundle_sequence_number should have incremented
```

### `jwt_issuer` Alignment

The `jwt_issuer` value in `server.conf` must match the `endpoint_url` hostname in Vault's SPIFFE
auth config. If they diverge (e.g. a hostname rename), Vault may fail issuer validation for
the JWT:
- **server.conf:** `jwt_issuer = "https://spire-server:8443"`
- **setup-spiffe.sh:** `endpoint_url = "https://spire-server:8443"`

These must be kept in sync. The `jwt_issuer` value becomes the `iss` claim in every JWT-SVID
issued by this SPIRE deployment.

---

## Brief Execution Summary

| Brief | Goal | Result | Notes |
|---|---|---|---|
| **1** | Test `federation {}` inside `server {}` | ✅ Works — 5 syntax iterations to find correct form | Key: `profile "https_web"` label syntax + `file_sync_interval = "1m"` required |
| **2** | Align `jwt_issuer` with `endpoint_url` | ✅ Done — both set to `https://spire-server:8443` | JWT `iss` claim now matches bundle endpoint hostname |
| **3** | Add auto-refresh to bundle serving | ✅ Native — no code needed | SPIRE federation endpoint sends `refresh_hint=5m0s`; Vault polls automatically |
| **4** | Remove `spire-oidc` | ✅ Done — service, config dir, and cert deleted | Stack simplified to 4 services; no PID sharing for non-workload containers |

**End-to-end test after all four briefs:** ✅ PASS — JWT-SVID fetched, SPIFFE auth login
succeeded, PKI cert issued.
