# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from strategy import TradingClient

from .hyperliquid import HyperLiquidClient


class EntropyClient(HyperLiquidClient):
    """Entropy's `io` HIP-3 perpetual DEX on Hyperliquid."""

    exchange = "entropy"
    dex_prefix = "io"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return EntropyClient

    async def _place_order(self, symbol: str, *args, **kwargs):
        if not symbol.startswith("io:"):
            raise ValueError(f"EntropyClient only trades io: symbols, got {symbol!r}")
        return await super()._place_order(symbol, *args, **kwargs)
