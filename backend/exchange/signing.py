"""HMAC request signing for the Binance Web3 API.

Spec: https://web3.binance.com/en/dev-docs/authentication
  preHash   = timestamp + METHOD + requestPath + body
  X-OC-SIGN = Base64(HMAC-SHA256(secretKey, preHash))
- timestamp: ISO-8601 UTC with milliseconds, identical to the X-OC-TIMESTAMP header.
- requestPath: the path *including the `/build` base-path prefix* plus the raw (as-sent) query string.
- body: raw JSON body for POST, "" for GET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import quote

HEADER_API_KEY = "X-OC-APIKEY"
HEADER_TIMESTAMP = "X-OC-TIMESTAMP"
HEADER_SIGN = "X-OC-SIGN"
HEADER_RECV_WINDOW = "X-OC-RECV-WINDOW"
HEADER_NONCE = "X-OC-NONCE"


def iso_timestamp(now: Optional[datetime] = None) -> str:
    """`2026-05-11T10:08:57.715Z` (UTC, millisecond precision)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def encode_query(params: Optional[Mapping[str, Any]]) -> str:
    """Percent-encode like JS encodeURIComponent (space -> %20, not +). Order is preserved, None values dropped.

    The exact same string must be sent on the wire and signed (no re-ordering / decoding).
    """
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f"{quote(str(k), safe='-_.~')}={quote(str(v), safe='-_.~')}")
    return "&".join(parts)


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    pre_hash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def signed_headers(
    api_key: str,
    secret: str,
    method: str,
    request_path: str,
    body: str = "",
    timestamp: Optional[str] = None,
    recv_window_ms: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    if not request_path.startswith("/build/"):
        # The #1 cause of 40102 per the docs: signing without the /build prefix.
        raise ValueError("request_path must include the /build base-path prefix")
    ts = timestamp or iso_timestamp()
    headers = {
        HEADER_API_KEY: api_key,
        HEADER_TIMESTAMP: ts,
        HEADER_SIGN: sign(secret, ts, method, request_path, body),
    }
    if recv_window_ms is not None:
        headers[HEADER_RECV_WINDOW] = str(int(recv_window_ms))
    if nonce:
        headers[HEADER_NONCE] = nonce
    return headers
