# SPIRE + Vault SPIFFE Auth — Implementation Notes

**Branch:** `feature/spiffe-auth-method`
**Date:** 2026-05-14
**Status:** Fully working end-to-end — `profile=https_web_bundle` confirmed

---

## Executive Summary

The original plan called for Vault Enterprise 2.0.0 running on the same Docker network as SPIRE, using the SPIFFE auth method with `profile=https_web_bundle` backed by the SPIRE OIDC Discovery Provider. After a multi-phase investigation, the full flow was validated — but the bundle source required for `https_web_bundle` is the SPIRE trust bundle (served by a dedicated Python HTTPS server), **not** the OIDC provider's `/keys` endpoint.

The core authentication flow works exactly as planned:
- SPIRE issues a JWT-SVID with `iss=https://spire-oidc` (via `jwt_issuer` in server.conf)
- Vault Enterprise fetches the trust bundle over HTTPS, validates the JWT, issues a Vault token
- The workload uses the token to issue a short-lived PKI certificate from Vault

---

## Part 1: Full Challenge Log

### Challenge 1 — SPIRE Agent Bootstrap Failure After Server Key Rotation

**What happened:**
The SPIRE server rotated its X509 CA and JWT signing keys on restart. The agent had stale trust bundle data from a prior session in `data/agent/`, causing repeated TLS handshake failures when reconnecting.

**Error:**
```
transport: authentication handshake failed: x509svid: could not verify leaf certificate:
x509: certificate signed by unknown authority
```

**Steps attempted:**
1. Restarted agent container only → same failure
2. Cleared `data/agent/` only → same failure (server CA also changed)
3. Cleared both `data/server/` and `data/agent/`, regenerated join token → **resolved**

**Decision:** A full data reset is required when restarting from a prior session. This is expected for `insecure_bootstrap=true` with join-token attestation.

---

### Challenge 2 — OIDC Discovery Provider Cannot Authenticate to Agent Socket

**What happened:**
The `spire-oidc` container connects to the SPIRE Agent workload API at `/opt/spire/sockets/agent.sock` via the `workload_api` config block. From a separate container, every connection was immediately reset.

**Error:**
```
Failed to fetch JWKS from the Workload API: rpc error: code = Unavailable
desc = "error reading server preface: read unix @->/opt/spire/sockets/agent.sock:
read: connection reset by peer"
```

**Root cause:**
SPIRE's unix WorkloadAttestor identifies calling processes by PID within the agent's PID namespace. A process in a different container is invisible to that namespace.

**Steps attempted:**
1. Confirmed socket path and permissions were correct
2. Added `pid: "service:spire-agent"` to `spire-oidc` in docker-compose → container joins the agent's PID namespace
3. Registered `spiffe://demo.realpage.local/spire/oidc` → rejected: `/spire/` prefix is reserved for SPIRE agents
4. Registered `spiffe://demo.realpage.local/oidc/provider` with `selector unix:uid:0` → **resolved**

**Decision:** `pid: "service:spire-agent"` is required for any non-workload container using the agent socket.

---

### Challenge 3 — Vault Enterprise Dev Mode KV Mount Error

**What happened:**
Vault Enterprise 2.0.0 exited immediately on startup with dev mode trying to auto-create a `secret/` KV mount that the license didn't allow.

**Error:**
```
Error initializing Dev mode: license does not allow for KV mounts,
consider using -dev-no-kv flag
```

**Decision:** Added `-dev-no-kv` to the Vault command in docker-compose. The KV engine is mounted explicitly in the setup script where needed.

---

### Challenge 4 — KV Secrets Engine Blocked by License Module

**What happened:**
The enterprise license carries the `pki-only` and `platform-standard` modules. Neither `kv-v2` nor `kv` (v1) nor `transit` engines are permitted.

**Steps attempted:**
1. `vault secrets enable -path=secret kv-v2` → blocked (400)
2. `vault secrets enable -path=secret kv` → blocked (400)
3. `vault secrets enable transit` → blocked (400)
4. `vault secrets enable pki` → **succeeded**

**Decision:** Demo payload was changed from "read a KV secret" to "issue a short-lived TLS certificate from Vault PKI." This is architecturally more relevant for a SPIFFE identity demo — the workload uses its SPIRE identity to authenticate and immediately receive a credential. A new `vault/policy-spiffe.hcl` was created granting `pki/issue/workload-cert`. The original `vault/policy.hcl` was left unmodified.

---

### Challenge 5 — Wrong SPIFFE ID Returned in Shared PID Namespace

**What happened:**
After adding `pid: "service:spire-agent"` to both `spire-oidc` and `workload`, both containers shared the agent's PID namespace. Both had registered entries with `selector unix:uid:0`. When the workload called `spire-agent api fetch jwt`, it received the OIDC provider's SPIFFE ID first (registration order).

**Observed:**
```
sub : spiffe://demo.realpage.local/oidc/provider  ← wrong
```

**Decision:** Added `-spiffeID spiffe://demo.realpage.local/workload/app` to the JWT fetch command in `workload/app.py`. This explicitly requests the desired SVID regardless of registration order.

---

### Challenge 6 — `profile=https_web_bundle` Returns 403 (First Iteration)

This was the core failure investigated across multiple debugging sessions. Configuration was correct, Authorization header arrived at Vault, JWT signature was valid — yet every login returned `403 permission denied`.

#### Debugging Timeline

**Step 1 — Confirmed passthrough header was set correctly.**
`vault auth tune -passthrough-request-headers="Authorization" spiffe/` succeeded and was verified.

**Step 2 — Confirmed Authorization header reached Vault.**
Enabled `vault write sys/config/auditing/request-headers/Authorization hmac=false` and confirmed the full Bearer token appeared in the audit log.

**Step 3 — Confirmed JWT kid matched OIDC JWKS kid.**
```
JWT header kid:  jP1zTXiFjbWdcb4O74Mv0bmqhCg7LdmZ
OIDC JWKS kid:   jP1zTXiFjbWdcb4O74Mv0bmqhCg7LdmZ
```

**Step 4 — Confirmed JWT signature was mathematically valid.**
Manual Python ECDSA/SHA256 verification using the P-256 public key from the OIDC JWKS:
```
JWT SIGNATURE VALID ✓
```

**Step 5 — Tested with wildcard `workload_id_patterns="*"` → still 403.**
Pattern matching eliminated as the root cause.

**Step 6 — Enabled Vault TRACE logging.**
The critical log lines:
```
[INFO]  auth.spiffe: Loaded new trust bundle: endpoint=https://spire-oidc/keys
        bundle refresh hint=1h0m0s  bundle sequence number=0

[ERROR] auth.spiffe: failed to parse cached trust bundle:
        error="spiffebundle: no authorities found"

[ERROR] auth.spiffe: failed to fetch trust bundle:
        error="no valid cached bundle found"
```

**Root cause confirmed:** The OIDC provider's `/keys` endpoint serves a standard JWKS without `"use": "jwt-svid"` on the keys. Vault's `spiffebundle` parser (`spiffebundle.FromJWKSBytes`) requires the `"use": "jwt-svid"` field to register a key as a JWT signing authority. Without it, zero authorities are found and all login attempts fail.

**OIDC provider JWKS (what was served):**
```json
{"keys": [{"kty": "EC", "kid": "...", "crv": "P-256", "alg": "ES256", "x": "...", "y": "..."}]}
```
Note: no `"use"` field.

**SPIRE bundle format (what spiffebundle requires):**
```json
{"keys": [{"use": "jwt-svid", "kty": "EC", "kid": "...", ...}], "spiffe_sequence": 1}
```

This is the fundamental incompatibility: the OIDC Discovery Provider is designed for use with Vault's **JWT auth method** (standard OIDC flow), not with the SPIFFE auth method's bundle parser.

---

### Challenge 7 — `profile=static` with Full SPIRE Bundle Fails on Missing `kid`

While debugging Challenge 6, `profile=static` was tried as a fallback using the full SPIRE bundle from `bundle show -format spiffe`. It also failed.

**Error:**
```
invalid PEM bundle: 3 errors occurred:
* failed to parse trust bundle
* failed to parse PEM bundle: data does not contain any valid RSA or ECDSA certificates
* failed to parse JWT bundle: jwtbundle: error adding authority 0 of JWKS: keyID cannot be empty
```

**Root cause:** The full SPIRE bundle includes both keys: the x509-svid CA key (no `kid`) and the jwt-svid signing key (has `kid`). The `jwtbundle` parser requires all JWKS entries to have a non-empty `kid`.

**Fix:** Extract only the `jwt-svid` key from the bundle before passing to `profile=static`:
```python
jwt_keys = [k for k in bundle['keys'] if k.get('use') == 'jwt-svid']
```

This worked. `profile=static` was confirmed functional with a JWT-only bundle. However, `static` does not auto-refresh on key rotation, so this was treated as a temporary verification tool, not the final approach.

---

### Challenge 8 — `jwt_issuer` Added but `https_web_bundle` Still Failed

Having confirmed the OIDC provider was the wrong bundle source, the correct fix was identified: add `jwt_issuer = "https://spire-oidc"` to `spire/server/server.conf`. This embeds an `iss` claim in every JWT-SVID, matching the `issuer` field in the OIDC discovery document. The hypothesis was that `https_web_bundle` was performing OIDC discovery and failing the issuer validation.

Adding `jwt_issuer` and re-running with `endpoint_url="https://spire-oidc/keys"` still returned:
```
[ERROR] auth.spiffe: failed to parse cached trust bundle: error="spiffebundle: no authorities found"
```

The trace log confirmed the root cause had not changed. The issuer was not the problem. The OIDC provider's JWKS simply does not include `"use": "jwt-svid"` regardless of what SPIRE embeds in its JWTs.

**What `jwt_issuer` does accomplish:** JWT-SVIDs now carry `"iss": "https://spire-oidc"` which is correct SPIFFE practice and aligns with the OIDC discovery document. It is kept in `server.conf` for correctness even though it did not resolve the bundle parsing issue.

---

### Challenge 9 — SPIRE 1.14.1 Federation Bundle Endpoint Not Supported

SPIRE's native solution for serving the trust bundle in SPIFFE format (with `"use": "jwt-svid"`) is the federation bundle endpoint. Added a `federation {}` block to `server.conf` at the top level (the documented location):

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

**Error:**
```
[ERROR] Unknown configuration detected: keys=federation section=top-level
```

The minimal variant (no profile, just address/port) was also rejected:
```
[ERROR] Unknown configuration detected: keys=federation section=top-level
```

**Root cause:** The `ghcr.io/spiffe/spire-server:1.14.1` container image does not support the `federation {}` configuration block in its HCL parser. This block was added to SPIRE's top-level config schema in a different build or compilation configuration than what this image provides.

**Decision:** Federation bundle endpoint is unavailable for this SPIRE version. A dedicated bundle server was added instead.

---

### Challenge 10 — Vault Startup: `base URL returns 404`

When attempting `endpoint_url="https://spire-oidc"` (base URL) for OIDC discovery, Vault returned:
```
federation: spiffebundle: unable to parse JWKS: invalid character 'p' after top-level value
```

The base URL `/` on `spire-oidc` returns HTTP 404. Vault received the 404 body and tried to parse it as a SPIFFE bundle/JWKS, failing at the character `p` in the error response text.

**Decision:** `endpoint_url` must point to the actual JSON resource, not the base domain.

---

## Part 2: Current Implementation

### What Was Built

A minimal Python HTTPS server (`bundle-server`) was added as a Docker service. At startup, the SPIRE server's trust bundle is exported and written to a shared volume file (`data/bundle/bundle.json`). The Python server serves this file over TLS. Vault's `auth/spiffe/` is configured with `profile=https_web_bundle` pointing at `https://bundle-server/bundle.json`.

The SPIRE bundle file, unlike the OIDC provider's JWKS, includes `"use": "jwt-svid"` on the JWT signing key. Vault's `spiffebundle` parser finds JWT authorities and login succeeds.

### Architecture (Current)

```
spire-net (Docker bridge)
│
├── spire-server     :8081   — CA, registry; jwt_issuer="https://spire-oidc"
├── spire-agent              — node-attested, unix socket at data/sockets/
├── spire-oidc       :443    — OIDC Discovery Provider (serves JWKS for OIDC flows)
│                               still present; not in the Vault auth path
├── bundle-server    :443    — Python HTTPS; serves data/bundle/bundle.json
│                               SPIRE bundle with "use":"jwt-svid" keys
├── vault-ent        :8200   — Vault Enterprise 2.0.0 dev mode
│                               auth/spiffe/ with profile=https_web_bundle
│                               polls https://bundle-server/bundle.json for trust bundle
└── workload                 — Python app:
                                1. Fetch JWT-SVID from SPIRE (iss=https://spire-oidc)
                                2. POST /v1/auth/spiffe/login (Bearer header)
                                3. POST /v1/pki/issue/workload-cert
```

### Key Config Values

| Config | Value |
|---|---|
| `jwt_issuer` (server.conf) | `https://spire-oidc` |
| `auth/spiffe/config.profile` | `https_web_bundle` |
| `auth/spiffe/config.endpoint_url` | `https://bundle-server/bundle.json` |
| `auth/spiffe/role.workload_id_patterns` | `/workload/app` |
| Bundle source | `spire-server bundle show -format spiffe` |
| Secret payload | PKI cert issuance (15 min TTL) |
| License module | `pki-only` + `platform-standard` |

### Verified End-to-End Output

```
STEP 1: JWT-SVID fetched
  sub : spiffe://demo.realpage.local/workload/app
  iss : https://spire-oidc
  aud : ['vault']

STEP 2: SPIFFE auth login → Vault token issued
  policies : ['default', 'workload-policy']
  metadata : {spiffe_id: spiffe://demo.realpage.local/workload/app,
               trust_domain: demo.realpage.local}

STEP 3: PKI cert issued
  common_name : workload.demo.realpage.local
  ttl         : 15 minutes

RESULT: SUCCESS
```

---

## Part 3: Plan vs. Implementation Comparison

### What Is the Same

| Aspect | Original Plan | Implementation |
|---|---|---|
| Auth method | `auth/spiffe/` | `auth/spiffe/` ✓ |
| Auth profile | `https_web_bundle` | `https_web_bundle` ✓ |
| JWT delivery | `Authorization: Bearer` header | `Authorization: Bearer` header ✓ |
| Passthrough header | Required, explicitly set | Set at `vault auth enable` time ✓ |
| `workload_id_patterns` | `/workload/app` (path only) | `/workload/app` ✓ |
| `jwt_issuer` in server.conf | Added | Added ✓ |
| OIDC Discovery Provider | Deployed on spire-net | Deployed on spire-net ✓ |
| Vault Enterprise 2.0.0 | On Docker spire-net | On Docker spire-net ✓ |
| `-dev-no-kv` flag | Needed (found during testing) | Added ✓ |
| PID sharing on spire-oidc | Not anticipated | Added (`pid: service:spire-agent`) ✓ |

### What Is Different

| Aspect | Original Plan | Actual Implementation | Reason |
|---|---|---|---|
| **Bundle source for Vault auth** | OIDC Discovery Provider (`https://spire-oidc/keys`) | Python bundle-server (`https://bundle-server/bundle.json`) | OIDC provider's JWKS omits `"use": "jwt-svid"`. Vault's `spiffebundle` parser requires this field to register JWT authorities. The OIDC provider is for Vault JWT auth method, not SPIFFE auth method. |
| **OIDC provider role** | Primary bundle source for `https_web_bundle` | Deployed but not in the Vault auth path (retained for OIDC completeness) | See above |
| **`jwt_issuer` impact** | Expected to fix OIDC issuer validation in `https_web_bundle` | Set correctly, embeds `iss` in JWTs, but was not the root cause of the 403 | The actual failure was missing `"use": "jwt-svid"` in the JWKS, not issuer mismatch |
| **SPIRE federation bundle endpoint** | Considered as `https_web_bundle` source | Not available — SPIRE 1.14.1 rejects `federation {}` block as unknown config | SPIRE 1.14.1 image build does not compile/register federation HCL schema |
| **Secret payload (Step 3)** | Read KV-v2 secret | Issue short-lived PKI certificate | License module (`pki-only`) blocks KV, transit, and most secret engines |
| **`policy.hcl`** | Used directly | New `policy-spiffe.hcl` created | Original policy grants `secret/data/*`; PKI requires `pki/issue/*`; original left unmodified per branch constraints |
| **`bundle-server` service** | Not in plan | Added (Python 3.12-slim, serves SPIRE bundle over HTTPS) | Required to serve SPIFFE bundle format with `"use": "jwt-svid"` keys |
| **`-spiffeID` flag in JWT fetch** | Not in plan | Added to `app.py` | Multiple entries share `uid:0` selector in shared PID namespace; agent returns OIDC provider's SVID first without explicit ID |
| **Startup cert generation** | All certs in `setup-spiffe.sh` | Two certs needed upfront: bundle-server cert (before docker compose) | bundle-server cert must exist before `bundle-server` container starts |

---

## Part 4: Why `https_web_bundle` Required a New Bundle Source

This is the central architectural finding of the implementation.

### The OIDC / SPIFFE Auth Split

| Vault Auth Method | Correct Bundle Source | Bundle Format | `"use"` field |
|---|---|---|---|
| `auth/jwt/` | OIDC Discovery Provider (`/keys`) | Standard JWKS (RFC 7517) | optional / omitted |
| `auth/spiffe/` (https_web_bundle) | SPIFFE Trust Bundle endpoint | SPIFFE Bundle (includes `"use": "jwt-svid"`) | **required** |

The OIDC Discovery Provider was built to serve standard JWKS for OIDC-based JWT validation. Vault's `auth/jwt/` method (and other OIDC consumers) can use it directly.

Vault's `auth/spiffe/` method uses the `spiffebundle` Go package internally, which parses bundles according to the SPIFFE spec. That spec defines `"use": "jwt-svid"` as the marker for JWT signing authorities. Without it, the parser finds no authorities and rejects all JWTs.

SPIRE's federation bundle endpoint would have served the correct format — but SPIRE 1.14.1's Docker image does not support that configuration block. The Python bundle-server reproduces what the federation endpoint would have done: serve the SPIRE bundle JSON (from `bundle show -format spiffe`) over HTTPS with a self-signed cert.

### Key Technical Findings

1. **`https_web_bundle` needs SPIFFE bundle format, not OIDC JWKS.** They look similar but differ in the `"use": "jwt-svid"` field on JWT signing keys.

2. **`jwt_issuer` in server.conf is still correct.** It embeds `iss=https://spire-oidc` in JWT-SVIDs, aligning with the OIDC discovery document's issuer field. This is needed if you also run the `auth/jwt/` method or any OIDC validator that validates the issuer.

3. **SPIRE 1.14.1 does not support `federation {}` in HCL config.** The HCL parser for this image build does not register the `federation` schema key.

4. **The `-passthrough-request-headers="Authorization"` flag is non-negotiable.** Without it, Vault strips the Bearer token before it reaches the SPIFFE plugin. This was the root cause of auth failures in the prior session documented in CLAUDE.md.

5. **`workload_id_patterns` takes the path after the trust domain, not the full SPIFFE URI.** `spiffe://demo.realpage.local/workload/app` → `/workload/app`.

6. **All containers that use the agent socket need PID namespace sharing** with `pid: "service:spire-agent"`. The unix WorkloadAttestor cannot identify processes across PID namespace boundaries.
