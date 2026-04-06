#!/usr/bin/env bash
set -euo pipefail

# Load env (placeholder values OK; overridden below)
source ../.env 2>/dev/null || true

# These are sourced from ../.env — fill in your values there before running
export VAULT_ADDR="${HCP_VAULT_ADDR}"
export VAULT_TOKEN="${HCP_VAULT_TOKEN}"
export VAULT_NAMESPACE="${HCP_VAULT_NAMESPACE:-admin}"

echo "==> Enabling JWT auth method..."
vault auth enable jwt || echo "Already enabled"

echo "==> Writing Vault policy..."
vault policy write workload-policy policy.hcl

echo "==> Fetching SPIRE JWT signing key from local server..."
FULL_BUNDLE=$(docker exec spire-server \
  /opt/spire/bin/spire-server bundle show -format spiffe)

# Convert the EC JWT-SVID signing key to PEM for Vault's jwt_validation_pubkeys
SPIRE_PEM=$(echo "$FULL_BUNDLE" | python3 -c "
import sys, json, base64
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers, SECP256R1
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

b = json.load(sys.stdin)
for key in b['keys']:
    if key.get('use') == 'jwt-svid':
        x = base64.urlsafe_b64decode(key['x'] + '==')
        y = base64.urlsafe_b64decode(key['y'] + '==')
        nums = EllipticCurvePublicNumbers(int.from_bytes(x,'big'), int.from_bytes(y,'big'), SECP256R1())
        pem = nums.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        print(pem, end='')
        break
")

echo "==> Configuring JWT auth with SPIRE signing key..."
vault write auth/jwt/config \
  jwt_validation_pubkeys="$SPIRE_PEM" \
  default_role="workload-role"

echo "==> Creating Vault JWT role for workload SPIFFE ID..."
vault write auth/jwt/role/workload-role \
  role_type="jwt" \
  bound_audiences="vault" \
  user_claim="sub" \
  bound_subject="spiffe://demo.realpage.local/workload/app" \
  token_policies="workload-policy" \
  token_ttl="1h"

echo "==> Seeding a test secret..."
vault secrets enable -path=secret kv-v2 || echo "Already enabled"
vault kv put secret/realpage/demo \
  api_key="super-secret-demo-key" \
  db_password="demo-db-pass-123"

echo "==> Done. Vault is configured for SPIFFE auth."
