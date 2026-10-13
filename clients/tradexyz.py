# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from strategy import TradingClient

from .hyperliquid import HyperLiquidClient


class TradeXYZClient(HyperLiquidClient):
    """Trade.xyz's `xyz` HIP-3 perpetual DEX on Hyperliquid."""

    exchange = "tradexyz"
    dex_prefix = "xyz"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return TradeXYZClient

    async def _place_order(self, symbol: str, *args, **kwargs):
        if not symbol.startswith("xyz:"):
            raise ValueError(f"TradeXYZClient only trades xyz: symbols, got {symbol!r}")
        return await super()._place_order(symbol, *args, **kwargs)
