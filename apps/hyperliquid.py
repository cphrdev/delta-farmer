# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from clients.hyperliquid import HyperLiquidClient
from lib.cli import create_cli, noop_command, run_app, setup_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import AppConfig

SEASON_2_START = datetime(2024, 11, 29, tzinfo=UTC)


class HyperLiquidNativeClient(HyperLiquidClient):
    exchange = "hyperliquid"
    dex_prefix = ""


async def print_info(accs: list[HyperLiquidNativeClient]) -> None:
    table = AutoTable(
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Season 2", justify="right"),
        Column("Balance $", "{:,.2f}", total=sum),
        Column("Staked HYPE", "{:,.2f}", total=sum),
        Column("Staked $", "{:,.2f}", total=sum),
    )
    hype_price = await accs[0].hype_price() if accs else Decimal(0)

    async def row(acc: HyperLiquidNativeClient):
        # Matches the Hyperliquid UI's "Perps + Spot + Vaults / All-time" card.
        all_time, (season_2, complete), balance, staked = await asyncio.gather(
            acc.portfolio_volume("allTime"),
            acc.volume_since(SEASON_2_START),
            acc.balance(),
            acc.staked_hype(),
        )
        season_2_text = f"{season_2:,.0f}" if complete else f"≥{season_2:,.0f}"
        return (
            acc.name,
            short_addr(acc.address),
            all_time,
            season_2_text,
            balance,
            staked,
            staked * hype_price,
        )

    for values in await gather_accs(accs, row):
        table.add_row(*values)

    table.print()


async def main() -> None:
    cli = await create_cli("hyperliquid", "configs/hyperliquid.toml", ["privkey"])
    cfg = AppConfig.load(cli.config)
    all_accs, _act_accs = await setup_app(cli, cfg, HyperLiquidNativeClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case command:
            noop_command("Hyperliquid", command)


if __name__ == "__main__":
    run_app(main())
