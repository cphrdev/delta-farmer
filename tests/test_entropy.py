from decimal import Decimal

import pytest

from clients.entropy import EntropyClient
from lib.hip3 import normalize_hip3_symbols, print_hip3_info


def test_normalize_symbols_adds_io_namespace():
    assert normalize_hip3_symbols(["SNDK", "io:NBIS"], "io") == ["io:SNDK", "io:NBIS"]


def test_client_scopes_metadata_to_io():
    client = EntropyClient("test", "0x" + "01" * 32)

    assert client.dex_prefix == "io"
    assert client._resolve("SNDK") == ("io", "io:SNDK")
    assert client._resolve("io:NBIS") == ("io", "io:NBIS")


async def test_client_rejects_non_io_order():
    client = EntropyClient("test", "0x" + "01" * 32)

    with pytest.raises(ValueError, match="only trades io: symbols"):
        await client._place_order("xyz:SNDK", "bid", Decimal("1"), Decimal("1"), "Gtc")


async def test_volume_sums_entropy_fill_notional(monkeypatch):
    client = EntropyClient("test", "0x" + "01" * 32)

    async def fills(_since=None):
        return [
            {"coin": "io:SNDK", "px": "50", "sz": "2"},
            {"coin": "io:NBIS", "px": "25.5", "sz": "4"},
        ]

    monkeypatch.setattr(client, "fetch_fills", fills)

    assert await client.volume() == Decimal("202")


async def test_info_shows_total_account_burn(monkeypatch):
    class FakeTable:
        instance = None

        def __init__(self, *columns):
            self.columns = columns
            self.rows = []
            FakeTable.instance = self

        def add_row(self, *values):
            self.rows.append(values)

        def print(self):
            pass

    client = EntropyClient("acc1", "0x" + "01" * 32)

    async def registered():
        return True

    async def volume():
        return Decimal("123")

    async def realized_pnl(dex_prefixes):
        assert dex_prefixes == ("xyz", "io")
        return Decimal("-7.5")

    async def balance():
        return Decimal("500")

    async def account_mode():
        return "unifiedAccount"

    monkeypatch.setattr("lib.hip3.AutoTable", FakeTable)
    monkeypatch.setattr("lib.hip3.warn_legacy_hyperliquid_accounts", lambda *_: None)
    monkeypatch.setattr(client, "registered", registered)
    monkeypatch.setattr(client, "volume", volume)
    monkeypatch.setattr(client, "realized_pnl", realized_pnl)
    monkeypatch.setattr(client, "balance", balance)
    monkeypatch.setattr(client, "account_mode", account_mode)

    await print_hip3_info([client], "Entropy", "entropy")

    assert FakeTable.instance is not None
    assert [column.name for column in FakeTable.instance.columns] == [
        "",
        "Account",
        "Address",
        "Volume",
        "Burn",
        "Balance",
        "Mode",
    ]
    assert FakeTable.instance.rows[0][3:] == (
        Decimal("123"),
        Decimal("7.5"),
        Decimal("500"),
        "unifiedAccount",
    )


async def test_realized_pnl_combines_xyz_and_io_fills_fees_and_funding(monkeypatch):
    client = EntropyClient("test", "0x" + "01" * 32)

    async def fills(_since=None):
        return [
            {"coin": "xyz:XYZ100", "closedPnl": "-2", "fee": "0.40"},
            {"coin": "io:SNDK", "closedPnl": "-3", "fee": "0.50"},
            {"coin": "io:NBIS", "closedPnl": "0", "fee": "0.10"},
            {"coin": "hyna:ETH", "closedPnl": "-100", "fee": "10"},
            {"coin": "BTC", "closedPnl": "-100", "fee": "10"},
        ]

    async def fundings(_since=None):
        return [
            {"delta": {"coin": "xyz:XYZ100", "usdc": "-0.25"}},
            {"delta": {"coin": "io:SNDK", "usdc": "0.10"}},
            {"delta": {"coin": "hyna:ETH", "usdc": "-20"}},
        ]

    monkeypatch.setattr(client, "_fetch_fills", fills)
    monkeypatch.setattr(client, "_fetch_fundings", fundings)

    assert await client.realized_pnl(("xyz", "io")) == Decimal("-6.15")
