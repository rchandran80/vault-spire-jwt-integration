#!/usr/bin/env bash
set -euo pipefail

source ../.env 2>/dev/null || true

export VAULT_ADDR="${HCP_VAULT_ADDR}"
export VAULT_TOKEN="${HCP_VAULT_TOKEN}"
export VAULT_NAMESPACE="${HCP_VAULT_NAMESPACE:-admin}"

echo "==> Target: $VAULT_ADDR (namespace: $VAULT_NAMESPACE)"
echo ""

echo "==> [1/5] Enabling SPIFFE auth with Authorization header passthrough..."
# -passthrough-request-headers is required — Vault strips the Authorization: Bearer
# header by default before it reaches the SPIFFE plugin. Without this flag,
# every login returns 403 permission denied with no diagnostic information.
vault auth enable -passthrough-request-headers="Authorization" spiffe 2>/dev/null \
  || vault auth tune -passthrough-request-headers="Authorization" auth/spiffe/ \
  && echo "  auth/spiffe/ ready — passthrough header confirmed"

echo "==> [2/5] Writing Vault policy..."
vault policy write workload-policy policy.hcl

echo "==> [3/5] Fetching SPIRE JWT signing key (jwt-svid keys only)..."
# Extract only the jwt-svid keys from the SPIRE bundle.
# The full bundle also contains an x509-svid key with no kid field,
# which causes the spiffebundle parser to fail with "keyID cannot be empty".
BUNDLE=$(docker exec hcp-spire-server \
  /opt/spire/bin/spire-server bundle show -format spiffe | python3 -c "
import sys, json
b = json.load(sys.stdin)
b['keys'] = [k for k in b['keys'] if k.get('use') == 'jwt-svid']
print(json.dumps(b))
")
KID=$(echo "$BUNDLE" | python3 -c "import sys,json; print(json.load(sys.stdin)['keys'][0]['kid'])")
echo "  JWT key kid: $KID"

echo "==> [4/5] Configuring auth/spiffe/ with static bundle..."
# profile=static: bundle is provided inline.
# This is used instead of https_web_bundle because HCP Vault (cloud) cannot
# reach the SPIRE server on a local Docker network to fetch the bundle dynamically.
# After SPIRE rotates its JWT signing key (every ca_ttl = 168h = 7 days),
# re-run this script to push the updated bundle to HCP Vault.
vault write auth/spiffe/config \
  trust_domain="demo.realpage.local" \
  profile="static" \
  bundle="$BUNDLE" \
  audience="vault"

echo "==> [4/5] Creating SPIFFE role..."
vault write auth/spiffe/role/workload-role \
  workload_id_patterns="/workload/app" \
  token_policies="workload-policy" \
  token_ttl="1h"

echo "==> [5/5] Seeding KV secret..."
vault secrets enable -path=secret kv-v2 2>/dev/null || echo "  Already enabled"
vault kv put secret/realpage/demo \
  api_key="super-secret-demo-key" \
  db_password="demo-db-pass-123"

echo ""
echo "==> Done."
echo "  Auth mount : auth/spiffe/ (profile=static)"
echo "  Bundle kid : $KID"
echo "  Role       : workload-role → workload_id_patterns=/workload/app"
echo "  Policy     : workload-policy → secret/data/realpage/*"
echo ""
echo "  NOTE: SPIRE rotates JWT keys every 7 days (ca_ttl=168h in server.conf)."
echo "  After a key rotation, re-run this script to push the new bundle to HCP Vault."
