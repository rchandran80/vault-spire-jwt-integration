# SPIRE + Vault — SPIFFE Auth Method

Two self-contained proofs-of-concept demonstrating **workload identity** using
[SPIFFE/SPIRE](https://spiffe.io) v1.14.1 and HashiCorp Vault. A containerised workload
proves its identity to Vault using a short-lived cryptographically signed JWT-SVID — no
static secrets, passwords, or API keys required at boot time.

Both approaches use Vault's purpose-built **SPIFFE auth method** (`auth/spiffe/`) and read
the same KV-v2 secret after authentication. They differ in which Vault target is used and
how the trust bundle is sourced.

---

## Approaches

| | [`local-vault-spiffe-auth/`](./local-vault-spiffe-auth/) | [`hcp-vault-spiffe-auth/`](./hcp-vault-spiffe-auth/) |
|---|---|---|
| **Vault target** | Vault Enterprise 2.0.0 (Docker, local) | HCP Vault Dedicated (cloud) |
| **Bundle profile** | `https_web_bundle` | `static` |
| **Bundle source** | SPIRE native federation endpoint (`:8443`) | JWT key extracted at setup time |
| **Key rotation** | Transparent — Vault polls every 5 minutes | Manual — re-run setup after rotation |
| **Docker services** | 4 (spire-server, spire-agent, vault-ent, workload) | 3 (spire-server, spire-agent, workload) |
| **License required** | Vault Enterprise license | HCP Vault credentials |
| **Best for** | Demonstrating auto-refresh, local dev | Integrating with an existing HCP Vault cluster |

---

## `profile=static` vs `profile=https_web_bundle`

Both profiles are available on the `auth/spiffe/` method. The choice depends on whether
Vault can reach the SPIRE server's bundle endpoint over the network.

**`profile=static`** — the SPIFFE trust bundle (JWT signing keys) is extracted once from the
SPIRE server and stored inline in Vault's configuration. Simple to set up — no network
connectivity between SPIRE and Vault is required. The limitation is that when SPIRE rotates
its JWT signing key (every 7 days by default), Vault's cached bundle goes stale and all
logins fail until the setup script is re-run.

**`profile=https_web_bundle`** — Vault fetches the SPIFFE trust bundle directly from the
SPIRE server's federation bundle endpoint (`https://spire-server:8443`) on a 5-minute poll
interval set by SPIRE's `X-Spiffe-Bundle-Refresh-Hint` response header. Key rotation is
completely transparent — Vault detects the new `spiffe_sequence` value on its next poll and
refreshes automatically. Requires Vault and SPIRE to be on the same network (both running
in Docker in `local-vault-spiffe-auth/`).

The bundle endpoint served by SPIRE includes keys with `"use": "jwt-svid"`, which is
required by Vault's `spiffebundle` parser. Standard OIDC JWKS endpoints (e.g. from an OIDC
Discovery Provider) do not include this field and cannot be used with `auth/spiffe/`.

---

## Getting Started

Clone the repo and navigate to the approach you want to run:

```bash
git clone https://github.com/rchandran80/vault-spire-jwt-integration.git
cd vault-spire-jwt-integration
```

**To use local Vault Enterprise with automatic key rotation:**
```bash
cd local-vault-spiffe-auth
# Follow local-vault-spiffe-auth/README.md
```

**To use HCP Vault Dedicated with static bundle:**
```bash
cd hcp-vault-spiffe-auth
# Follow hcp-vault-spiffe-auth/README.md
```

Each subdirectory is fully self-contained with its own `docker-compose.yml`, setup scripts,
and step-by-step README.

---

## Shared Architecture

Both approaches share the same SPIRE setup and workload identity flow:

```
SPIRE Server (CA)
      │ gRPC attestation
      ▼
SPIRE Agent  ──── Unix socket ────► Workload App
                                         │
                                         │ JWT-SVID
                                         │ Authorization: Bearer
                                         ▼
                                    Vault auth/spiffe/
                                         │
                                         │ Vault token
                                         ▼
                                    secret/realpage/demo (KV-v2)
```

- Trust domain: `demo.realpage.local`
- Workload SPIFFE ID: `spiffe://demo.realpage.local/workload/app`
- `workload_id_patterns`: `/workload/app` (path after trust domain is stripped)
- Vault policy: `secret/data/realpage/*` read access

---

## Production Considerations

| POC Approach | Production Replacement |
|---|---|
| Join token node attestation | Cloud-native attestor (AWS IID, GCP GCE, Azure MSI, Kubernetes PSAT) |
| `insecure_bootstrap = true` | Pre-distribute SPIRE Server CA bundle as `trust_bundle_path` |
| SQLite datastore | PostgreSQL or MySQL for HA SPIRE Server |
| Vault dev mode (in-memory) | Vault Enterprise with Raft integrated storage |
| Self-signed TLS cert (local approach) | CA-signed cert or ACME via `profile "https_web" { acme { ... } }` |
| `profile=static` (HCP approach) | `profile=https_web_bundle` when SPIRE is reachable from Vault |
| Root containers (`user: "0"`) | Non-root with correct volume permissions |

---

## References

- [SPIFFE/SPIRE Documentation](https://spiffe.io/docs/latest/)
- [Vault SPIFFE Auth Method](https://developer.hashicorp.com/vault/docs/auth/spiffe)
- [SPIFFE Federation](https://spiffe.io/docs/latest/architecture/federation/readme/)
- [Vault Enterprise 2.0 Release Notes](https://www.hashicorp.com/en/blog/vault-enterprise-20-modernizes-identity-security-at-scale)
