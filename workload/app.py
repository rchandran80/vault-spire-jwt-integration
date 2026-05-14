import os
import time
import json
import base64
import subprocess
import requests

# Defaults allow running directly on the host (outside Docker)
VAULT_ADDR      = os.environ.get("VAULT_ADDR", "https://vault-ns-testing-public-vault-0ec0178c.3ddd9d7e.z1.hashicorp.cloud:8200")
VAULT_NAMESPACE = os.environ.get("VAULT_NAMESPACE", "admin")
AUDIENCE        = "vault"
VAULT_ROLE      = "workload-role"

# Detect execution context: inside Docker container or on host
IN_DOCKER = os.path.exists("/.dockerenv")

# Socket path differs between Docker (volume mount) and host (bind mount)
if IN_DOCKER:
    SOCKET_PATH = "/opt/spire/sockets/agent.sock"
else:
    SOCKET_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "sockets", "agent.sock"
    )

SEPARATOR = "=" * 60


def decode_jwt_payload(token):
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def fetch_jwt_svid():
    print(f"\n{SEPARATOR}")
    print("STEP 1: Fetch JWT-SVID from SPIRE Agent")
    print(SEPARATOR)
    print(f"  Runtime : {'Docker container' if IN_DOCKER else 'macOS host (via docker exec)'}")
    print(f"  Socket  : {SOCKET_PATH}")
    print(f"  Audience: {AUDIENCE}")

    if IN_DOCKER:
        # Binary is available inside the container
        cmd = [
            "/opt/spire/bin/spire-agent", "api", "fetch", "jwt",
            "-audience", AUDIENCE,
            "-socketPath", SOCKET_PATH,
        ]
    else:
        # On macOS: SPIRE has no Darwin binary — reach into the running container
        cmd = [
            "docker", "exec", "spire-agent",
            "/opt/spire/bin/spire-agent", "api", "fetch", "jwt",
            "-audience", AUDIENCE,
            "-socketPath", "/opt/spire/sockets/agent.sock",
        ]

    print(f"  Command : {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    lines = result.stdout.splitlines()
    jwt = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("token("):
            for jwt_line in lines[i + 1:]:
                candidate = jwt_line.strip()
                if candidate:
                    jwt = candidate
                    break
        if stripped.startswith("token:"):
            jwt = stripped.split("token:", 1)[1].strip()

    if not jwt:
        raise ValueError(f"Could not parse JWT-SVID from agent output:\n{result.stdout}")

    header_b64 = jwt.split(".")[0]
    header_b64 += "=" * (4 - len(header_b64) % 4)
    header = json.loads(base64.urlsafe_b64decode(header_b64))
    claims = decode_jwt_payload(jwt)

    print(f"  [OK] JWT-SVID received from SPIRE Agent")
    print(f"\n  JWT Header:")
    print(f"    alg : {header.get('alg')}")
    print(f"    kid : {header.get('kid')}")
    print(f"    typ : {header.get('typ')}")
    print(f"\n  JWT Claims:")
    print(f"    sub : {claims.get('sub')}")
    print(f"    aud : {claims.get('aud')}")
    print(f"    iat : {claims.get('iat')}  (issued at)")
    print(f"    exp : {claims.get('exp')}  (expires at)")
    print(f"\n  Token (truncated): {jwt[:80]}...")
    return jwt


def vault_login(jwt_token):
    print(f"\n{SEPARATOR}")
    print("STEP 2: Authenticate to HCP Vault with JWT-SVID")
    print(SEPARATOR)
    print(f"  Vault Address  : {VAULT_ADDR}")
    print(f"  Vault Namespace: {VAULT_NAMESPACE}")
    print(f"  Auth Mount     : auth/jwt/")
    print(f"  Role           : {VAULT_ROLE}")
    print(f"  Endpoint       : POST /v1/auth/jwt/login")

    url = f"{VAULT_ADDR}/v1/auth/jwt/login"
    headers = {"X-Vault-Namespace": VAULT_NAMESPACE}

    print(f"\n  Sending JWT-SVID to Vault...")
    resp = requests.post(url, json={"role": VAULT_ROLE, "jwt": jwt_token}, headers=headers)

    if not resp.ok:
        raise RuntimeError(f"Vault login failed {resp.status_code}: {resp.text}")

    auth = resp.json()["auth"]
    print(f"\n  [OK] Vault authentication successful")
    print(f"\n  Vault Token Details:")
    print(f"    token         : {auth['client_token'][:20]}... (truncated)")
    print(f"    accessor      : {auth.get('accessor', 'n/a')[:20]}...")
    print(f"    token_type    : {auth.get('token_type')}")
    print(f"    lease_duration: {auth.get('lease_duration')}s")
    print(f"    renewable     : {auth.get('renewable')}")
    print(f"    policies      : {auth.get('policies')}")
    print(f"    metadata      : {auth.get('metadata')}")
    return auth["client_token"]


def read_secret(vault_token, path):
    print(f"\n{SEPARATOR}")
    print("STEP 3: Read Secret from HCP Vault")
    print(SEPARATOR)
    print(f"  KV Mount : secret/")
    print(f"  Path     : {path}")
    print(f"  Endpoint : GET /v1/secret/data/{path}")

    url = f"{VAULT_ADDR}/v1/secret/data/{path}"
    headers = {
        "X-Vault-Token": vault_token,
        "X-Vault-Namespace": VAULT_NAMESPACE,
    }

    print(f"\n  Reading secret using Vault token...")
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()

    data   = resp.json()
    secret = data["data"]["data"]
    meta   = data["data"]["metadata"]

    print(f"\n  [OK] Secret retrieved successfully")
    print(f"\n  Secret Metadata:")
    print(f"    version      : {meta.get('version')}")
    print(f"    created_time : {meta.get('created_time')}")
    print(f"    destroyed    : {meta.get('destroyed')}")
    print(f"\n  Secret Data:")
    for k, v in secret.items():
        print(f"    {k} = {v}")
    return secret


def main():
    print(SEPARATOR)
    print("SPIRE + HashiCorp Vault SPIFFE Auth Demo")
    print("Trust Domain : demo.realpage.local")
    print("Workload SVID: spiffe://demo.realpage.local/workload/app")
    print("Vault Cluster: HCP Vault Dedicated")
    print(f"Runtime      : {'Docker container' if IN_DOCKER else 'macOS host'}")
    print(SEPARATOR)

    if not IN_DOCKER:
        print("\nRunning on host — checking Docker is available for SPIRE binary access...")
        result = subprocess.run(["docker", "ps", "--filter", "name=spire-agent", "--format", "{{.Names}}"],
                                capture_output=True, text=True)
        if "spire-agent" not in result.stdout:
            raise RuntimeError("spire-agent container is not running. Start it with: docker compose up -d spire-agent")
        print("  [OK] spire-agent container is running")
    else:
        print("\nWaiting for SPIRE Agent socket to be ready...")
        time.sleep(5)
        print("  [OK] Socket ready")

    jwt = fetch_jwt_svid()
    vault_token = vault_login(jwt)
    read_secret(vault_token, "realpage/demo")

    print(f"\n{SEPARATOR}")
    print("RESULT: SUCCESS")
    print(SEPARATOR)
    print("  SPIRE Agent   -> JWT-SVID issued for workload identity")
    print("  HCP Vault     -> JWT-SVID validated, Vault token issued")
    print("  Secret access -> Policy enforced, secret retrieved")
    print(f"\n  Full flow: SPIRE -> Vault JWT auth -> KV secret read")
    print(f"  COMPLETE\n")


if __name__ == "__main__":
    main()
