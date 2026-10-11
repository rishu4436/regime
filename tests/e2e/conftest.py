"""e2e safety net: no test may run the real `baw`. LIVE-mode apps built in tests would otherwise read
`baw wallet settings --json` through WalletSettingsProvider's default subprocess runner. Tests that need wallet
settings pass their own `runner` (or the exchange's get_wallet_settings); everything else sees "unavailable"."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_baw_wallet_settings(monkeypatch):
    from backend.app import live

    def refuse(cmd):
        raise RuntimeError(f"{' '.join(cmd)}: real baw disabled in tests")
    monkeypatch.setattr(live.WalletSettingsProvider, "_run", staticmethod(refuse))
