from decimal import Decimal

import pytest

from clients.entropy import EntropyClient
from lib.hip3 import normalize_hip3_symbols


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
