import os
import socket

import pyotp
import requests

import config  # noqa: F401  (loads .env into the environment)

# ── Credentials ─────────────────────────────────────────────
# Read from the environment (or the git-ignored .env file, see .env.example).
# Never hardcode them here.
CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "")
PIN         = os.environ.get("ANGEL_PIN", "")
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "")
API_KEY     = os.environ.get("ANGEL_API_KEY", "")

BASE_URL = "https://apiconnect.angelone.in"

session = requests.Session()




# ── Utility Functions ───────────────────────────────────────
def get_local_ip():
    return socket.gethostbyname(socket.gethostname())

def get_public_ip():
    try:
        return requests.get("https://api.ipify.org").text
    except Exception:
        return "127.0.0.1"

def generate_totp():
    return pyotp.TOTP(TOTP_SECRET).now()


def _require_credentials():
    missing = [name for name, value in (
        ("ANGEL_CLIENT_CODE", CLIENT_CODE), ("ANGEL_PIN", PIN),
        ("ANGEL_TOTP_SECRET", TOTP_SECRET), ("ANGEL_API_KEY", API_KEY),
    ) if not value]
    if missing:
        raise RuntimeError(f"Angel One credentials not set: {', '.join(missing)} "
                           f"(set them in the environment or .env, see .env.example)")


# ── Step 1: Login ───────────────────────────────────────────
def login():
    url = f"{BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"

    _require_credentials()
    totp = generate_totp()

    payload = {
        "clientcode": CLIENT_CODE,
        "password": PIN,
        "totp": totp,
        "state": "live"
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": get_local_ip(),
        "X-ClientPublicIP": get_public_ip(),
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": API_KEY
    }

    res = session.post(url, json=payload, headers=headers)
    data = res.json()

    if not data.get("status"):
        raise Exception(f"[LOGIN] Failed: {data}")

    print("[LOGIN] Success — JWT and tokens received")

    return data["data"]


# ── Step 2: Generate Token (Optional Refresh) ───────────────
def generate_token(jwt_token, refresh_token):
    url = f"{BASE_URL}/rest/auth/angelbroking/jwt/v1/generateTokens"

    payload = {
        "refreshToken": refresh_token
    }

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": get_local_ip(),
        "X-ClientPublicIP": get_public_ip(),
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": API_KEY
    }

    res = session.post(url, json=payload, headers=headers)
    data = res.json()

    if not data.get("status"):
        raise Exception(f"[TOKEN_REFRESH] Failed: {data}")

    print("[TOKEN_REFRESH] Success — new JWT received")

    return data["data"]


# ── MAIN ────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("AngelOne Auto Login — client: " + CLIENT_CODE)
    print("=" * 50)

    login_data = login()

    jwt_token     = login_data["jwtToken"]
    refresh_token = login_data["refreshToken"]
    feed_token    = login_data["feedToken"]


    # Save tokens
    with open("angel_tokens.txt", "w") as f:
        f.write(f"jwt_token={jwt_token}\n")
        f.write(f"refresh_token={refresh_token}\n")
        f.write(f"feed_token={feed_token}\n")

    print("[SAVED] Tokens written to angel_tokens.txt")