"""One-off ERC-8004 agent-identity registration for the teamwork agent.

Run on Rishu's PC (not in the trading loop):

    python -m backend.agent_studio.register --dry-run                 # testnet plan, no tx
    python -m backend.agent_studio.register --dry-run --estimate-gas  # + read-only RPC gas/balance check
    python -m backend.agent_studio.register                           # real testnet registration
    python -m backend.agent_studio.register --network mainnet --confirm-mainnet

Rules this script enforces:
* The signing key comes ONLY from ``$AGENT_STUDIO_PRIVATE_KEY`` (a separate small
  EVM key). There is no --private-key flag and the key is never printed/written.
* Testnet is the default; a real mainnet run needs ``--confirm-mainnet``.
* An existing identity file for the same network is never overwritten without
  ``--force``; an agent with our name already owned by this key is re-used, not
  registered twice.
* Output goes to a local, gitignored JSON (``backend/agent_studio/agent_identity.json``).

bnbagent 0.5.0 calls used (verified against the installed package source):
``EVMWalletProvider(password=, private_key=, persist=False)``,
``ERC8004Agent(wallet_provider=, network="bsc-testnet"|"bsc-mainnet")``,
``ERC8004Agent.get_local_agent_info(name)``, ``ERC8004Agent.generate_agent_uri(name=, description=, endpoints=)``,
``ERC8004Agent.register_agent(agent_uri=, metadata=)`` -> ``{transactionHash, agentId, agentURI, ...}``,
``ERC8004Agent.contract_address``, ``AgentEndpoint.from_dict``,
``ERC8004PartialRegistrationError`` / ``TransactionPendingError``,
dry-run only: ``bnbagent.erc8004.agent_uri.AgentURIGenerator.generate_registration_file``/
``generate_agent_uri`` and (``--estimate-gas``) ``bnbagent.core.abis.load_abi("IdentityRegistry.json")``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .agent_card import AGENT_NAME, build_metadata, build_registration_file
from .config import (
    NETWORKS,
    PRIVATE_KEY_ENV,
    SDK_VERSION,
    STUDIO_VERSION,
    TRADING_WALLET_ENV,
    NetworkInfo,
    default_identity_path,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_PENDING = 4
EXIT_FAILED = 5


class RegistrationError(Exception):
    """A clean, key-free error message for the operator."""


# ── SDK access (one seam so tests can swap in a fake) ─────────────────────────


def _load_sdk() -> SimpleNamespace:
    try:
        import bnbagent
        from bnbagent import (
            AgentEndpoint,
            ERC8004Agent,
            ERC8004PartialRegistrationError,
            EVMWalletProvider,
            TransactionPendingError,
        )
        from bnbagent.erc8004.agent_uri import AgentURIGenerator
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise RegistrationError(
            "bnbagent is not installed. Run: pip install -r backend/agent_studio/requirements.txt"
        ) from exc
    return SimpleNamespace(
        version=getattr(bnbagent, "__version__", "unknown"),
        AgentEndpoint=AgentEndpoint,
        ERC8004Agent=ERC8004Agent,
        EVMWalletProvider=EVMWalletProvider,
        PartialError=ERC8004PartialRegistrationError,
        PendingError=TransactionPendingError,
        AgentURIGenerator=AgentURIGenerator,
        Account=Account,
    )


def _read_key() -> str | None:
    key = (os.environ.get(PRIVATE_KEY_ENV) or "").strip()
    return key or None


def _derive_address(sdk: SimpleNamespace, key: str) -> str:
    try:
        return sdk.Account.from_key(key).address
    except Exception:  # noqa: BLE001
        # Never echo the underlying error: some libraries include the input.
        raise RegistrationError(
            f"${PRIVATE_KEY_ENV} is not a valid 32-byte hex private key"
        ) from None


def _guard_not_trading_wallet(address: str) -> None:
    trading = (os.environ.get(TRADING_WALLET_ENV) or "").strip().lower()
    if trading and trading == address.lower():
        raise RegistrationError(
            "The identity key resolves to the trading wallet address. Use a separate, small key."
        )


# ── Output ────────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_identity_record(
    *,
    net: NetworkInfo,
    erc8004_id: int | None,
    registry: str,
    tx_hash: str | None,
    owner_address: str,
    sdk_version: str,
    status: str,
    agent_uri: str | None = None,
) -> dict[str, Any]:
    return {
        "erc8004_id": erc8004_id,
        "registry": registry,
        "tx_hash": tx_hash,
        "network": net.cli_name,
        "chain_id": net.chain_id,
        "studio_version": STUDIO_VERSION,
        "sdk_version": sdk_version,
        "owner_address": owner_address,
        "agent_name": AGENT_NAME,
        "explorer_url": net.agent_url(erc8004_id) if erc8004_id is not None else None,
        "tx_url": net.tx_url(tx_hash) if tx_hash else None,
        "status": status,
        "registered_at": _utc_now(),
        "agent_uri": agent_uri,
    }


def write_identity(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _existing_for_network(path: Path, net: NetworkInfo) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and data.get("network") == net.cli_name
        and data.get("erc8004_id") is not None
    ):
        return data
    return None


def _tx_hash_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if hasattr(value, "hex") and not isinstance(value, str):
        h = value.hex()
        return h if h.startswith("0x") else "0x" + h
    s = str(value)
    return s if s.startswith("0x") else "0x" + s


# ── Dry run ───────────────────────────────────────────────────────────────────


def _preview_registration(
    sdk: SimpleNamespace | None, net: NetworkInfo, reg: dict[str, Any]
) -> dict[str, Any]:
    if sdk is None:
        return {"registration_file": reg, "agent_uri_bytes": None}
    gen = sdk.AgentURIGenerator
    kwargs = {
        "name": reg["name"],
        "description": reg["description"],
        "endpoints": reg["endpoints"],
        "identity_registry": net.registry,
        "chain_id": net.chain_id,
    }
    file_ = gen.generate_registration_file(**kwargs)
    uri = gen.generate_agent_uri(**kwargs)
    return {
        "registration_file": file_,
        "agent_uri": uri,
        "agent_uri_bytes": len(uri.encode()),
    }


def estimate_gas(
    net: NetworkInfo, agent_uri: str, metadata: list[dict[str, str]], address: str
) -> dict[str, Any]:
    """Read-only: eth_estimateGas for register(uri, metadata) + balance + gas price."""
    from bnbagent.config import resolve_network
    from bnbagent.core.abis import load_abi
    from bnbagent.core.contract_mixin import min_gas_price_wei
    from bnbagent.erc8004.constants import BUILT_WITH_KEY, get_built_with_value
    from web3 import Web3

    rpc = resolve_network(net.sdk_name).rpc_url
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    if w3.eth.chain_id != net.chain_id:
        raise RegistrationError(f"RPC {rpc} is not chain {net.chain_id}")
    items = list(metadata)
    if not any(e["key"] == BUILT_WITH_KEY for e in items):
        items.append({"key": BUILT_WITH_KEY, "value": get_built_with_value()})
    md = [
        {"metadataKey": e["key"], "metadataValue": e["value"].encode()} for e in items
    ]
    c = w3.eth.contract(
        address=Web3.to_checksum_address(net.registry),
        abi=load_abi("IdentityRegistry.json"),
    )
    gas = c.functions.register(agent_uri, md).estimate_gas(
        {"from": Web3.to_checksum_address(address)}
    )
    price = max(
        int(w3.eth.gas_price), min_gas_price_wei(net.chain_id)
    )  # SDK's own floor
    bal = int(w3.eth.get_balance(Web3.to_checksum_address(address)))
    # register_agent() sends register + setAgentURI (rewrites the URI with the id).
    worst = 2 * gas * price
    return {
        "rpc": rpc,
        "register_gas": gas,
        "gas_price_gwei": price / 1e9,
        "est_cost_bnb_two_txs_if_self_paid": worst / 1e18,
        "owner_balance_bnb": bal / 1e18,
        "enough_if_self_paid": bal >= worst,
    }


def dry_run(args: argparse.Namespace, net: NetworkInfo, out=print) -> int:
    reg = build_registration_file(args.status_url)
    metadata = build_metadata()
    try:
        sdk = _load_sdk()
    except RegistrationError as exc:
        sdk = None
        out(f"note: {exc} (showing plain registration fields)")
    preview = _preview_registration(sdk, net, reg)
    key = _read_key()
    owner = None
    if key and sdk is not None:
        owner = _derive_address(sdk, key)
        _guard_not_trading_wallet(owner)
    plan = {
        "dry_run": True,
        "network": net.cli_name,
        "sdk_network": net.sdk_name,
        "chain_id": net.chain_id,
        "registry": net.registry,
        "sdk_version": sdk.version if sdk else None,
        "studio_version": STUDIO_VERSION,
        "owner_address": owner or f"(set ${PRIVATE_KEY_ENV} to show)",
        "agent_name": reg["name"],
        "registration_file": preview["registration_file"],
        "onchain_metadata": metadata
        + [{"key": "built_with", "value": "(added by SDK)"}],
        "agent_uri_bytes": preview.get("agent_uri_bytes"),
        "transactions": [
            "register(agentURI, metadata) -> agentId",
            "setAgentURI(agentId, agentURI with registrations[agentId])",
        ],
        "gas": "MegaFuel paymaster sponsors when it accepts the tx (testnet registration is normally free); otherwise the owner key self-pays",
        "identity_file": str(args.identity_file),
    }
    out(json.dumps(plan, indent=2))
    if args.estimate_gas:
        if sdk is None or "agent_uri" not in preview:
            out("--estimate-gas needs bnbagent installed")
            return EXIT_USAGE
        addr = owner or "0x000000000000000000000000000000000000dEaD"
        try:
            est = estimate_gas(net, preview["agent_uri"], metadata, addr)
        except Exception as exc:  # noqa: BLE001 - read-only diagnostics; never fatal for a dry run
            out(f"gas estimate failed: {type(exc).__name__}: {exc}")
            return EXIT_OK
        if owner is None:
            est["owner_balance_bnb"] = None
            est["enough_if_self_paid"] = None
        out(json.dumps({"gas_estimate": est}, indent=2))
    out("DRY RUN: nothing was signed or sent, no file was written.")
    return EXIT_OK


# ── Real registration ─────────────────────────────────────────────────────────


def register(args: argparse.Namespace, net: NetworkInfo, out=print) -> int:
    path = Path(args.identity_file)
    if net.cli_name == "mainnet" and not args.confirm_mainnet:
        raise RegistrationError(
            "Refusing a real mainnet registration without --confirm-mainnet"
        )
    existing = _existing_for_network(path, net)
    if existing and not args.force:
        out(
            f"Already registered on {net.cli_name}: erc8004_id={existing['erc8004_id']} ({path}). Use --force to register again."
        )
        return EXIT_OK
    key = _read_key()
    if not key:
        raise RegistrationError(
            f"Set ${PRIVATE_KEY_ENV} (a separate small EVM key) in the environment"
        )

    sdk = _load_sdk()
    owner = _derive_address(sdk, key)
    _guard_not_trading_wallet(owner)
    try:
        # In-memory only: no keystore is written to ~/.bnbagent; password is a throwaway.
        wallet = sdk.EVMWalletProvider(
            password=secrets.token_hex(16), private_key=key, persist=False
        )
    except Exception:  # noqa: BLE001
        raise RegistrationError(
            f"Could not load ${PRIVATE_KEY_ENV} into the SDK wallet"
        ) from None
    del key

    agent = sdk.ERC8004Agent(wallet_provider=wallet, network=net.sdk_name)
    registry = agent.contract_address

    if not args.force:
        found = agent.get_local_agent_info(AGENT_NAME)
        if found and found.get("agent_id") is not None:
            rec = build_identity_record(
                net=net,
                erc8004_id=int(found["agent_id"]),
                registry=registry,
                tx_hash=None,
                owner_address=owner,
                sdk_version=sdk.version,
                status="recovered_existing",
                agent_uri=found.get("agent_uri"),
            )
            write_identity(path, rec)
            out(
                f"Agent '{AGENT_NAME}' already registered by {owner}: erc8004_id={rec['erc8004_id']}. Saved to {path}."
            )
            return EXIT_OK

    reg = build_registration_file(args.status_url)
    endpoints = [sdk.AgentEndpoint.from_dict(e) for e in reg["endpoints"]]
    agent_uri = agent.generate_agent_uri(
        name=reg["name"], description=reg["description"], endpoints=endpoints
    )
    out(
        f"Registering '{AGENT_NAME}' on {net.sdk_name} (registry {registry}) from {owner} ..."
    )
    try:
        result = agent.register_agent(agent_uri=agent_uri, metadata=build_metadata())
    except sdk.PartialError as exc:
        rec = build_identity_record(
            net=net,
            erc8004_id=int(exc.agent_id),
            registry=registry,
            tx_hash=_tx_hash_str(getattr(exc, "tx_hash", None)),
            owner_address=owner,
            sdk_version=sdk.version,
            status="registered_uri_update_pending",
            agent_uri=getattr(exc, "agent_uri", None),
        )
        write_identity(path, rec)
        out(
            f"Registered erc8004_id={exc.agent_id}, but the follow-up setAgentURI did not confirm. Saved to {path}; the id is valid."
        )
        return EXIT_PARTIAL
    except sdk.PendingError as exc:
        h = _tx_hash_str(getattr(exc, "tx_hash", None))
        out(
            f"Transaction broadcast but not confirmed yet: {h} ({net.tx_url(h) if h else 'no hash'}). Re-run later; it will detect the agent."
        )
        return EXIT_PENDING

    agent_id = result.get("agentId")
    if agent_id is None:
        raise RegistrationError(
            f"Registration tx {result.get('transactionHash')} returned no agentId"
        )
    rec = build_identity_record(
        net=net,
        erc8004_id=int(agent_id),
        registry=registry,
        tx_hash=_tx_hash_str(result.get("transactionHash")),
        owner_address=owner,
        sdk_version=sdk.version,
        status="registered",
        agent_uri=result.get("agentURI"),
    )
    write_identity(path, rec)
    out(
        json.dumps(
            {
                k: rec[k]
                for k in (
                    "erc8004_id",
                    "registry",
                    "tx_hash",
                    "network",
                    "studio_version",
                    "explorer_url",
                    "tx_url",
                )
            },
            indent=2,
        )
    )
    out(f"Saved to {path}")
    return EXIT_OK


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backend.agent_studio.register",
        description=f"Register the teamwork agent's ERC-8004 identity (bnbagent {SDK_VERSION}). Key is read only from ${PRIVATE_KEY_ENV}.",
    )
    p.add_argument(
        "--network",
        choices=sorted(NETWORKS),
        default="testnet",
        help="default: testnet",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be registered; sign/send nothing",
    )
    p.add_argument(
        "--estimate-gas",
        action="store_true",
        help="with --dry-run: read-only RPC gas + balance check (set RPC_URL_BSC_TESTNET/RPC_URL_BSC_MAINNET to override the SDK RPC)",
    )
    p.add_argument(
        "--status-url",
        default=None,
        help="public read-only status URL (default: $AGENT_PUBLIC_BASE_URL/api/system, omitted if unset)",
    )
    p.add_argument(
        "--identity-file",
        default=None,
        help="output JSON (default: backend/agent_studio/agent_identity.json, gitignored)",
    )
    p.add_argument(
        "--confirm-mainnet",
        action="store_true",
        help="required for a real mainnet registration",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="register even if an identity already exists",
    )
    return p


def main(argv: list[str] | None = None, out=print) -> int:
    args = build_parser().parse_args(argv)
    args.identity_file = (
        Path(args.identity_file).expanduser()
        if args.identity_file
        else default_identity_path()
    )
    net = NETWORKS[args.network]
    try:
        if args.dry_run:
            return dry_run(args, net, out=out)
        if args.estimate_gas:
            raise RegistrationError("--estimate-gas only works together with --dry-run")
        return register(args, net, out=out)
    except RegistrationError as exc:
        out(f"error: {exc}")
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001
        # SDK/RPC failures. Message comes from the SDK/web3, never contains the key.
        out(f"error: registration failed: {type(exc).__name__}: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
