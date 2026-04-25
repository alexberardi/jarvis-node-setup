"""Lightweight Schlage API client using httpx + stdlib crypto.

Replaces pyschlage (which pulls boto3/botocore/pycognito ~100MB) with
direct Cognito SRP authentication and REST calls.  Only depends on
httpx (already in node requirements) and Python stdlib (hashlib, hmac).
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Cognito / Schlage constants (same values pyschlage hard-codes)
# ---------------------------------------------------------------------------

_COGNITO_REGION = "us-west-2"
_POOL_ID = "us-west-2_2zhrVs9d4"
_POOL_ID_HASH = _POOL_ID.split("_")[1]  # "2zhrVs9d4"
_CLIENT_ID = "t5836cptp2s1il0u9lki03j5"
_CLIENT_SECRET = "1kfmt18bgaig51in4j4v1j3jbe7ioqtjhle5o6knqc5dat0tpuvo"
_COGNITO_URL = f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/"

_API_BASE = "https://api.allegion.yonomi.cloud/v1"
_API_KEY = "hnuu9jbbJr7MssFDWm5nU2Z7nG5Q5rxsaqWsE7e9"
_TIMEOUT = 60

_INFO_BITS = b"Caldera Derived Key"

# RFC 5054 3072-bit SRP group (same prime AWS Cognito uses)
_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_G_HEX = "2"

_BIG_N = int(_N_HEX, 16)
_G = int(_G_HEX, 16)


# ---------------------------------------------------------------------------
# SRP helper functions (ported from pycognito.aws_srp)
# ---------------------------------------------------------------------------


def _hash_sha256(buf: bytes | bytearray) -> str:
    """SHA-256 hex digest, zero-padded to 64 chars."""
    return hashlib.sha256(buf).hexdigest().zfill(64)


def _hex_hash(hex_str: str) -> str:
    """SHA-256 of raw bytes represented by *hex_str*."""
    return _hash_sha256(bytes.fromhex(hex_str))


def _pad_hex(value: int) -> str:
    """Convert int to even-length hex, prepend 00 if high bit set."""
    h = format(value, "x")
    if len(h) % 2 == 1:
        h = "0" + h
    elif h[0] in "89abcdef":
        h = "00" + h
    return h


def _compute_hkdf(ikm: bytes, salt: bytes) -> bytes:
    """Single-block HKDF-SHA256 with info='Caldera Derived Key'."""
    prk = _hmac.new(salt, ikm, hashlib.sha256).digest()
    return _hmac.new(prk, _INFO_BITS + b"\x01", hashlib.sha256).digest()[:16]


def _secret_hash(username: str) -> str:
    """Cognito SECRET_HASH = Base64(HMAC_SHA256(client_secret, username + client_id))."""
    msg = (username + _CLIENT_ID).encode("utf-8")
    sig = _hmac.new(_CLIENT_SECRET.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.standard_b64encode(sig).decode("utf-8")


def _cognito_timestamp() -> str:
    """Timestamp in the format Cognito expects (no leading zero on day)."""
    raw = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
    return re.sub(r" 0(\d) ", r" \1 ", raw)


# Precompute k = H(pad(N) || pad(g))
_K: int = int(_hex_hash(_pad_hex(_BIG_N) + _pad_hex(_G)), 16)


# ---------------------------------------------------------------------------
# Cognito SRP auth (no boto3)
# ---------------------------------------------------------------------------


class CognitoSRPAuth:
    """Authenticate against AWS Cognito via SRP over plain HTTPS.

    Uses only httpx + stdlib — no boto3, no pycognito.
    """

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        # Generate ephemeral key pair
        self._small_a = int.from_bytes(os.urandom(128), "big") % _BIG_N
        self._big_a = pow(_G, self._small_a, _BIG_N)
        # Tokens (populated after authenticate())
        self.id_token: str | None = None
        self.access_token: str | None = None
        self.refresh_token: str | None = None

    # -- public ---------------------------------------------------------------

    def authenticate(self) -> None:
        """Run the full SRP handshake and store tokens."""
        # Step 1: InitiateAuth with SRP_A
        auth_params = {
            "USERNAME": self.username,
            "SRP_A": format(self._big_a, "x"),
            "SECRET_HASH": _secret_hash(self.username),
        }
        init_resp = self._cognito_post("InitiateAuth", {
            "AuthFlow": "USER_SRP_AUTH",
            "ClientId": _CLIENT_ID,
            "AuthParameters": auth_params,
        })

        # Step 2: solve the PASSWORD_VERIFIER challenge
        challenge = init_resp["ChallengeParameters"]
        response_params = self._solve_challenge(challenge, auth_params)

        verify_resp = self._cognito_post("RespondToAuthChallenge", {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ClientId": _CLIENT_ID,
            "ChallengeResponses": response_params,
        })

        tokens = verify_resp["AuthenticationResult"]
        self.id_token = tokens["IdToken"]
        self.access_token = tokens["AccessToken"]
        self.refresh_token = tokens["RefreshToken"]

    # -- private --------------------------------------------------------------

    @staticmethod
    def _cognito_post(action: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.post(
            _COGNITO_URL,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}",
            },
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _solve_challenge(
        self, challenge: dict[str, str], request_params: dict[str, str],
    ) -> dict[str, str]:
        user_id = challenge["USER_ID_FOR_SRP"]
        salt_hex = challenge["SALT"]
        srp_b = int(challenge["SRP_B"], 16)
        secret_block_b64 = challenge["SECRET_BLOCK"]
        internal_username = challenge.get("USERNAME", request_params["USERNAME"])

        timestamp = _cognito_timestamp()
        hkdf = self._password_auth_key(user_id, srp_b, salt_hex)

        # HMAC signature over pool-hash + user + secret-block + timestamp
        msg = (
            _POOL_ID_HASH.encode("utf-8")
            + user_id.encode("utf-8")
            + base64.standard_b64decode(secret_block_b64)
            + timestamp.encode("utf-8")
        )
        signature = base64.standard_b64encode(
            _hmac.new(hkdf, msg, hashlib.sha256).digest()
        ).decode("utf-8")

        return {
            "TIMESTAMP": timestamp,
            "USERNAME": internal_username,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block_b64,
            "PASSWORD_CLAIM_SIGNATURE": signature,
            "SECRET_HASH": _secret_hash(internal_username),
        }

    def _password_auth_key(
        self, username: str, server_b: int, salt_hex: str,
    ) -> bytes:
        """Derive the HKDF key from the SRP shared secret."""
        u = int(_hex_hash(_pad_hex(self._big_a) + _pad_hex(server_b)), 16)
        if u == 0:
            raise ValueError("SRP safety check: u must not be zero")

        # x = H(salt || H(pool_hash || username || ":" || password))
        pw_hash = _hash_sha256(
            f"{_POOL_ID_HASH}{username}:{self.password}".encode("utf-8")
        )
        x = int(_hex_hash(_pad_hex(int(salt_hex, 16)) + pw_hash), 16)

        # S = (B - k * g^x) ^ (a + u*x)  mod N
        g_mod = pow(_G, x, _BIG_N)
        s = pow(server_b - _K * g_mod, self._small_a + u * x, _BIG_N)

        return _compute_hkdf(
            bytes.fromhex(_pad_hex(s)),
            bytes.fromhex(_pad_hex(u)),
        )


# ---------------------------------------------------------------------------
# Schlage REST client
# ---------------------------------------------------------------------------


@dataclass
class SchlageLock:
    """Minimal representation of a Schlage lock from the API."""

    device_id: str
    name: str
    model_name: str
    is_locked: bool
    is_jammed: bool
    battery_level: int | None
    connected: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SchlageLock:
        attrs = data.get("attributes", {})
        lock_state = attrs.get("lockState", 0)
        return cls(
            device_id=data["deviceId"],
            name=data.get("name", ""),
            model_name=data.get("modelName", ""),
            is_locked=lock_state == 1,
            is_jammed=lock_state == 2,
            battery_level=attrs.get("batteryLevel"),
            connected=attrs.get("connected", False),
        )


class SchlageClient:
    """Thin REST wrapper around the Allegion cloud API."""

    def __init__(self, auth: CognitoSRPAuth) -> None:
        self._auth = auth

    def _request(
        self, method: str, path: str, **kwargs: Any,
    ) -> httpx.Response:
        url = f"{_API_BASE}/{path}"
        headers = {
            "Authorization": f"Bearer {self._auth.access_token or ''}",
            "X-Api-Key": _API_KEY,
        }
        resp = httpx.request(method, url, headers=headers, timeout=_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp

    def get_locks(self) -> list[SchlageLock]:
        """List all locks on the account."""
        resp = self._request("GET", "devices", params={"archetype": "lock"})
        return [SchlageLock.from_json(d) for d in resp.json()]

    def lock(self, device_id: str) -> None:
        """Lock a device."""
        self._request("PUT", f"devices/{device_id}", json={"attributes": {"lockState": 1}})

    def unlock(self, device_id: str) -> None:
        """Unlock a device."""
        self._request("PUT", f"devices/{device_id}", json={"attributes": {"lockState": 0}})

    def refresh(self, device_id: str) -> SchlageLock:
        """Fetch current state of a single lock."""
        resp = self._request("GET", f"devices/{device_id}")
        return SchlageLock.from_json(resp.json())
