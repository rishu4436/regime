# backend/agent_studio: BNB Agent Studio / ERC-8004 identity (Bot 5)

This gives **Regime**, our trading agent, an **on-chain ERC-8004 identity** on BNB Smart Chain using the
BNB Agent Studio Python SDK **`bnbagent` 0.5.0** (PyPI, verified against the installed source).
It also lets the API show that identity (`/api/system` → `agent_identity`).

Why it's done this way: Studio's TypeScript `bag` CLI 0.0.14 scaffolds a *seller* agent, and its managed runtime is
**testnet-only with no Agentic Wallet kind**. Our mainnet trading loop therefore stays on Rishu's PC, and we use the
SDK only to register the identity. The identity owner is a **separate small EVM key**. It never holds or moves
trading funds, and the trading wallet stays the Binance Agentic Wallet (MPC).

| File | What |
|---|---|
| `identity.py` | `get_agent_identity()` → `{erc8004_id, registry, studio_version, network, explorer_url}` or `None`. Read-only, no SDK, no RPC, never raises. |
| `agent_card.py` | Name, description, capabilities, endpoints and on-chain metadata (`build_registration_file`, `build_metadata`, `build_agent_card`). |
| `register.py` | One-off registration CLI (`python -m backend.agent_studio.register`). |
| `config.py` | Networks, registry addresses (same as the SDK presets), env var names, identity file path. |
| `X402_NOTES.md` | Notes on paying the Stock Analyze Agent with `baw x402-payment`. Notes only, no code. |
| `agent_identity.json` | **Output**, written by `register.py`. Gitignored (`backend/agent_studio/.gitignore`). |

## For Bot 6 (API wiring)

```python
from backend.agent_studio import get_agent_identity, build_agent_card

ident = get_agent_identity()  # None until Rishu registers
system["agent_identity"] = (
    {
        k: ident[k]
        for k in ("erc8004_id", "registry", "studio_version", "network", "explorer_url")
    }
    if ident
    else None
)
# optional: GET /api/agent-card -> build_agent_card(identity=ident)
```

It needs no extra dependencies. The `bnbagent` package is only needed to run `register.py`.

## What Rishu needs

- **Key:** a **new, separate** EVM private key used only for the identity. Don't use the trading wallet (it has no exportable key anyway),
  and don't use a wallet holding real funds. It is read **only** from the env var `AGENT_STUDIO_PRIVATE_KEY`. The script
  never prints or saves it, and it doesn't write a keystore (`persist=False`).
- **Gas:** `register_agent()` sends **two** transactions, `register(uri, metadata)` and then `setAgentURI(id, uri)`, which writes the id into
  the registration file. First the SDK asks the NodeReal MegaFuel paymaster to sponsor them. If the paymaster says no, the key pays.
  - **Testnet (default):** usually sponsored, so no BNB is needed. If the paymaster refuses, get tBNB from the BSC testnet faucet.
  - **Mainnet:** a read-only `eth_estimateGas` on 11 Oct gave about **957k gas** for `register` at a 0.1 gwei floor, so about
    **0.0002 BNB** for both transactions if self-paid. **Send 0.002 BNB** to the identity address to be safe, because nodes check
    `gas_limit × price` up front.
- **Network:** the hackathon track is **BSC mainnet** (`--network mainnet --confirm-mainnet`). Do a testnet run first if you want a rehearsal.
- Optional env vars:
  - `AGENT_TRADING_WALLET_ADDRESS=0x…` (the Agentic Wallet's **public** BSC address, from `baw wallet address --json`). It's stored as on-chain
    metadata `trading_wallet` so judges can link the identity to the trading wallet. The script also refuses to run if the key's address equals it.
  - `AGENT_PUBLIC_BASE_URL=https://…` (a public tunnel to our FastAPI). This adds a read-only `status` endpoint (`…/api/system`)
    to the registration file. If it isn't set, only the repo link is registered, so no placeholder goes on-chain.
  - `RPC_URL_BSC_TESTNET` / `RPC_URL_BSC_MAINNET` override the SDK's RPC. The SDK's default testnet RPC
    (`data-seed-prebsc-2-s2.binance.org:8545`) timed out from our box. `https://bsc-testnet-rpc.publicnode.com` worked.

## Exact steps (Windows PowerShell on Rishu's PC, from the repo root)

```powershell
py -3.12 -m venv .venv-studio          # Python >= 3.10
.\.venv-studio\Scripts\Activate.ps1
pip install -r backend\agent_studio\requirements.txt   # bnbagent==0.5.0

# 1) new identity key (prints address + key ONCE; store the key in your password manager)
python -c "from eth_account import Account; a=Account.create(); print(a.address); print(a.key.hex())"

$env:AGENT_STUDIO_PRIVATE_KEY = "<paste key>"            # this shell only
$env:AGENT_TRADING_WALLET_ADDRESS = "<Agentic Wallet BSC address>"   # optional

# 2) preview (sends nothing, writes nothing); --estimate-gas is a read-only RPC call
python -m backend.agent_studio.register --dry-run --estimate-gas
python -m backend.agent_studio.register --network mainnet --dry-run --estimate-gas

# 3a) optional rehearsal on testnet
$env:RPC_URL_BSC_TESTNET = "https://bsc-testnet-rpc.publicnode.com"
python -m backend.agent_studio.register --identity-file backend\agent_studio\agent_identity.testnet.json

# 3b) the real one (fund the identity address with ~0.002 BNB first)
python -m backend.agent_studio.register --network mainnet --confirm-mainnet

Remove-Item Env:AGENT_STUDIO_PRIVATE_KEY
```

The default output file `backend/agent_studio/agent_identity.json` is the one `get_agent_identity()` reads.
It holds `erc8004_id`, `registry`, `tx_hash`, `network`, `chain_id`, `studio_version`, `sdk_version`, `owner_address`,
`explorer_url` (8004scan), `tx_url` (BscScan), `status` and `registered_at`, and is gitignored.
To point the API at a different file, set `AGENT_STUDIO_IDENTITY_FILE`.

Safety rules the CLI enforces:
- Testnet is the default.
- A mainnet run requires `--confirm-mainnet`.
- There's no `--private-key` flag.
- An existing identity file for the same network isn't overwritten without `--force`.
- If this key already owns an agent named `Regime` (checked through the 8004scan indexer), that agent is reused instead of registering a second one.

Exit codes:
- `0` ok.
- `2` usage or refused.
- `3` registered, but `setAgentURI` didn't confirm. The id is still saved and valid.
- `4` the transaction is pending. Re-run later.
- `5` SDK or RPC failure.

## bnbagent 0.5.0 APIs used

`EVMWalletProvider(password=, private_key=, persist=False)` ·
`ERC8004Agent(wallet_provider=, network="bsc-testnet"|"bsc-mainnet")` ·
`.get_local_agent_info(name)` · `.generate_agent_uri(name=, description=, endpoints=)` ·
`.register_agent(agent_uri=, metadata=[{key, value}])` → `{transactionHash, agentId, agentURI, receipt}` · `.contract_address` ·
`AgentEndpoint.from_dict` · `ERC8004PartialRegistrationError` (`agent_id`, `tx_hash`) · `TransactionPendingError` (`tx_hash`).
Dry run only: `bnbagent.erc8004.agent_uri.AgentURIGenerator`, `bnbagent.core.abis.load_abi("IdentityRegistry.json")`,
`bnbagent.config.resolve_network` and `bnbagent.core.contract_mixin.min_gas_price_wei`.
Registries (SDK presets): mainnet `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (chain 56), testnet `0x8004A818BFB912233c491871b3d84c89A494BD9e` (chain 97).
The SDK adds its own `built_with` metadata entry. We avoid the reserved `agentWallet` key.

## Tests

`pytest backend/agent_studio`. Everything is offline, using a fake SDK for `register.py`. `test_agent_studio_sdk_contract.py` checks
our assumptions against the real installed `bnbagent` 0.5.0 and is skipped when it isn't installed (as in CI).
