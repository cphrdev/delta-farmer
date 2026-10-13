from decimal import Decimal

import pytest

from clients.tradexyz import TradeXYZClient
from lib.hip3 import normalize_hip3_symbols


def test_normalize_symbols_adds_xyz_namespace():
    assert normalize_hip3_symbols(["SP500", "xyz:GOLD"], "xyz") == [
        "xyz:SP500",
        "xyz:GOLD",
    ]


def test_normalize_symbols_rejects_another_namespace():
    with pytest.raises(ValueError, match="expected xyz: symbol"):
        normalize_hip3_symbols(["io:SNDK"], "xyz")


def test_client_scopes_metadata_to_xyz():
    client = TradeXYZClient("test", "0x" + "01" * 32)

    assert client.dex_prefix == "xyz"
    assert client._resolve("SP500") == ("xyz", "xyz:SP500")
    assert client._resolve("xyz:GOLD") == ("xyz", "xyz:GOLD")


async def test_client_rejects_non_xyz_order():
    client = TradeXYZClient("test", "0x" + "01" * 32)

    with pytest.raises(ValueError, match="only trades xyz: symbols"):
        await client._place_order("BTC", "bid", Decimal("1"), Decimal("1"), "Gtc")
