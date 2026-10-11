"""Localhost-only dashboard API for Regime's ERC-8004 identity (Agent Studio).

Lets the dashboard do what ``register.py`` does from the command line:

    GET  /api/agent-studio/status      -> {registered, network, identity_address, bnb_balance,
                                           bnb_needed, erc8004_id, explorer_url, has_key}
    POST /api/agent-studio/create-key  -> {identity_address}
    POST /api/agent-studio/register    body {network, confirm_mainnet: true}
                                       -> {erc8004_id, explorer_url, tx_hash}

Mount it in the app with ``app.include_router(router)``.

Rules:
* Every endpoint is localhost-only (same check as the wallet sign-in endpoints), 403 otherwise.
* The identity key is created on this PC and saved to a gitignored file under ``logs/``
  (owner-only permissions). No endpoint, error or log line ever contains it, and there is
  no export endpoint: to back it up, copy the file.
* ``$AGENT_STUDIO_PRIVATE_KEY`` (the CLI flow) wins over the file when set.
* ``bnbagent`` and ``eth_account`` are imported lazily, so this module imports without them.
* Registration reuses ``register.register`` and writes the same ``agent_identity.json``
  that ``identity.get_agent_identity`` reads. A lock stops double clicks registering twice.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from argparse import Namespace
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import register as reg
from .config import (
    NETWORKS,
    PRIVATE_KEY_ENV,
    NetworkInfo,
    default_identity_path,
    network_info,
)
from .identity import get_agent_identity, load_identity_record

logger = logging.getLogger(__name__)

#: Where create-key saves the identity key. Gitignored (``logs/`` and this exact path).
KEY_FILE_ENV = "AGENT_STUDIO_KEY_FILE"
KEY_FILE_RELATIVE = "logs/agent_studio_identity.key"
#: Which network the dashboard registers on (the hackathon track is BSC mainnet).
NETWORK_ENV = "AGENT_STUDIO_NETWORK"
DEFAULT_NETWORK = "mainnet"
#: BNB to send to the identity address before registering. Same figure as the README:
#: a read-only estimate gave ~0.0002 BNB for both txs; 0.002 leaves room for the
#: gas_limit x price check nodes do up front.
BNB_NEEDED = 0.002
#: Public BSC RPCs used only for the read-only eth_getBalance. Override with the same
#: env vars the SDK reads (RPC_URL_BSC_MAINNET / RPC_URL_BSC_TESTNET, then RPC_URL).
DEFAULT_RPC = {
    "mainnet": "https://bsc-dataseed.binance.org",
    "testnet": "https://bsc-testnet-rpc.publicnode.com",
}
RPC_TIMEOUT_S = 4.0
BALANCE_CACHE_S = 5.0

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

_lock = threading.Lock()  # create-key and register never run concurrently
_balance_cache: dict[tuple[str, str], tuple[float, Optional[float]]] = {}


# ── localhost guard (mirrors backend/app/main.py require_local) ───────────────


def require_local(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    fwd = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
    hh = (request.headers.get("host") or "localhost").strip().lower()
    hh = hh[1:].split("]")[0] if hh.startswith("[") else hh.rsplit(":", 1)[0] if hh.count(":") == 1 else hh
    origin = (request.headers.get("origin") or "").strip().lower()
    oh = urlsplit(origin).hostname if origin and origin != "null" else "localhost"
    if host not in LOCAL_HOSTS or any(h not in LOCAL_HOSTS for h in fwd) or hh not in LOCAL_HOSTS or oh not in LOCAL_HOSTS:
        raise HTTPException(
            403,
            "On-chain registration only works on the PC running the Regime backend "
            "(open the dashboard at http://localhost there).",
        )


router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"], dependencies=[Depends(require_local)])


# ── helpers ───────────────────────────────────────────────────────────────────


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def key_file_path() -> Path:
    override = (os.environ.get(KEY_FILE_ENV) or "").strip()
    return Path(override).expanduser() if override else repo_root() / KEY_FILE_RELATIVE


def configured_network() -> NetworkInfo:
    net = network_info(os.environ.get(NETWORK_ENV) or DEFAULT_NETWORK)
    return net or NETWORKS[DEFAULT_NETWORK]


def parse_network(name: str | None) -> NetworkInfo | None:
    """Accept 'mainnet', 'bsc-mainnet', 'BSC Mainnet', 56 ... -> NetworkInfo."""
    raw = str(name or "").strip().lower()
    if not raw:
        return configured_network()
    if raw.isdigit():
        return network_info(int(raw))
    if "test" in raw:
        return NETWORKS["testnet"]
    if "main" in raw:
        return NETWORKS["mainnet"]
    return network_info(raw)


def _load_account_cls():
    try:
        from eth_account import Account
    except ImportError:
        raise HTTPException(
            503,
            "The eth-account package isn't installed, so Regime can't create or read the identity key. "
            "Run: pip install -r backend/agent_studio/requirements.txt",
        ) from None
    return Account


def _read_key_file() -> str | None:
    p = key_file_path()
    try:
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def current_key() -> str | None:
    """The identity key: $AGENT_STUDIO_PRIVATE_KEY first, then the key file."""
    return (os.environ.get(PRIVATE_KEY_ENV) or "").strip() or _read_key_file()


def _address_for(key: str, strict: bool) -> str | None:
    try:
        Account = _load_account_cls()
    except HTTPException:
        if strict:
            raise
        return None
    try:
        return Account.from_key(key).address
    except Exception:  # noqa: BLE001 - never echo the error, it may contain the key
        if strict:
            raise HTTPException(
                400,
                "The saved identity key isn't a valid private key. Check "
                f"${PRIVATE_KEY_ENV} or the key file at {KEY_FILE_RELATIVE}.",
            ) from None
        return None


def rpc_url(net: NetworkInfo) -> str:
    per_net = f"RPC_URL_{net.sdk_name.upper().replace('-', '_')}"
    return (os.environ.get(per_net) or os.environ.get("RPC_URL") or DEFAULT_RPC[net.cli_name]).strip()


def _rpc_get_balance(url: str, address: str) -> float:
    """One read-only eth_getBalance call. Returns BNB."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
    ).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S) as resp:  # noqa: S310 - fixed RPC URL
        data = json.loads(resp.read().decode())
    return int(data["result"], 16) / 1e18


def bnb_balance(net: NetworkInfo, address: str | None, fresh: bool = False) -> float | None:
    """Balance in BNB, or None when the RPC can't be reached. Never raises."""
    if not address:
        return None
    k = (net.cli_name, address.lower())
    hit = _balance_cache.get(k)
    if hit and not fresh and time.monotonic() - hit[0] < BALANCE_CACHE_S:
        return hit[1]
    try:
        bal: float | None = _rpc_get_balance(rpc_url(net), address)
    except Exception as exc:  # noqa: BLE001
        logger.info("agent studio: balance lookup failed: %s", type(exc).__name__)
        bal = None
    _balance_cache[k] = (time.monotonic(), bal)
    return bal


def _status_network(ident: dict[str, Any] | None) -> NetworkInfo:
    if ident:
        return network_info(ident.get("network") or "") or configured_network()
    return configured_network()


# ── endpoints ─────────────────────────────────────────────────────────────────


class RegisterBody(BaseModel):
    network: Optional[str] = None
    confirm_mainnet: bool = False


@router.get("/status")
def status() -> dict[str, Any]:
    ident = get_agent_identity()
    net = _status_network(ident)
    key = current_key()
    address = _address_for(key, strict=False) if key else None
    if address is None and ident:
        address = (load_identity_record() or {}).get("owner_address")
    return {
        "registered": ident is not None,
        "network": net.cli_name,
        "identity_address": address,
        "bnb_balance": bnb_balance(net, address),
        "bnb_needed": BNB_NEEDED,
        "erc8004_id": ident["erc8004_id"] if ident else None,
        "explorer_url": ident["explorer_url"] if ident else None,
        "has_key": key is not None,
    }


@router.post("/create-key")
def create_key() -> dict[str, str]:
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Another identity wallet step is already running. Wait a moment and refresh.")
    try:
        if (os.environ.get(PRIVATE_KEY_ENV) or "").strip():
            raise HTTPException(409, f"An identity key is already set in ${PRIVATE_KEY_ENV}. Regime will use that one.")
        path = key_file_path()
        if path.exists():
            raise HTTPException(
                409,
                f"An identity wallet already exists on this PC ({KEY_FILE_RELATIVE}). "
                "Regime won't overwrite it.",
            )
        Account = _load_account_cls()
        acct = Account.create()
        key_hex = acct.key.hex()
        key_hex = key_hex if key_hex.startswith("0x") else "0x" + key_hex
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            raise HTTPException(409, "An identity wallet already exists on this PC. Regime won't overwrite it.") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key_hex + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        del key_hex
        logger.info("agent studio: created identity key for %s", acct.address)
        return {"identity_address": acct.address}
    finally:
        _lock.release()


@router.post("/register")
def register_identity(body: RegisterBody) -> dict[str, Any]:
    net = parse_network(body.network)
    if net is None:
        raise HTTPException(400, f"Unknown network '{body.network}'. Use mainnet or testnet.")
    if net.cli_name == "mainnet" and body.confirm_mainnet is not True:
        raise HTTPException(400, "Registering on BSC mainnet sends real transactions. Tick the confirmation box first.")
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "Registration is already in progress. Wait for it to finish.")
    try:
        ident = get_agent_identity()
        if ident is not None:
            raise HTTPException(
                409,
                f"Regime is already registered on {ident['network']} (ERC-8004 id {ident['erc8004_id']}).",
            )
        key = current_key()
        if not key:
            raise HTTPException(400, "Create the identity wallet first.")
        address = _address_for(key, strict=True)
        bal = bnb_balance(net, address, fresh=True)
        if bal is None:
            raise HTTPException(
                503, "Couldn't read the identity wallet's BNB balance from BNB Chain. Check the internet connection and try again."
            )
        if bal < BNB_NEEDED:
            raise HTTPException(
                400,
                f"The identity wallet {address} has {bal:.4f} BNB. Send at least {BNB_NEEDED} BNB "
                f"on BNB Smart Chain ({net.cli_name}) to it, then try again.",
            )
        try:
            reg._load_sdk()
        except reg.RegistrationError:
            raise HTTPException(
                503,
                "The bnbagent package isn't installed on this PC. "
                "Run: pip install -r backend/agent_studio/requirements.txt and restart Regime.",
            ) from None

        path = default_identity_path()
        args = Namespace(identity_file=path, confirm_mainnet=True, force=False, status_url=None)
        notes: list[str] = []
        try:
            code = reg.register(args, net, out=notes.append, key=key)
        except reg.RegistrationError as exc:
            raise HTTPException(400, str(exc)) from None
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK/RPC failure; messages never contain the key
            logger.warning("agent studio: registration failed: %s", type(exc).__name__)
            raise HTTPException(502, f"Registration failed: {type(exc).__name__}: {exc}") from None
        finally:
            del key
            _balance_cache.clear()

        if code == reg.EXIT_PENDING:
            raise HTTPException(
                504,
                "The registration was sent but isn't confirmed yet. "
                + (notes[-1] if notes else "")
                + " Wait a minute, then press Register again: Regime will find the agent instead of registering twice.",
            )
        rec = load_identity_record(path)
        if code not in (reg.EXIT_OK, reg.EXIT_PARTIAL) or not rec or rec.get("erc8004_id") is None:
            raise HTTPException(502, "Registration failed. " + (notes[-1] if notes else ""))
        return {
            "erc8004_id": rec["erc8004_id"],
            "explorer_url": rec.get("explorer_url"),
            "tx_hash": rec.get("tx_hash"),
        }
    finally:
        _lock.release()
