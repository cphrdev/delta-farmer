# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Ship it, then read the docs
import asyncio
from decimal import Decimal

from clients.arcus import ArcusClient
from lib.cli import create_cli, noop_command, run_app, setup_app
from lib.http import ApiError
from lib.logger import logger
from lib.table import AutoTable, Column
from lib.utils import short_addr
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy

# MARK: Reports


async def print_info(accs: list[ArcusClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column(
            "$/100k",
            "${:,.2f}",
            compute=lambda r: r["Burn"] / r["Volume"] * Decimal("1e5"),
            guard=lambda r: r["Volume"] > 0,
        ),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rank", "{:,.0f}"),
        Column("Tier", justify="left"),
    )

    async def row(acc: ArcusClient):
        addr = short_addr(acc.address)
        try:
            if not await acc.registered():
                return ("✗", acc.name, addr, 0, 0, 0, 0, None, None)
            p = await acc.profile()
            return ("✓", acc.name, p.addr, p.volume, -p.pnl, p.points, p.balance, p.rank, p.mode)
        except ApiError as e:
            # Arcus is access-gated: a wallet that was never granted access answers 403
            # on every endpoint. Report it rather than failing the whole table.
            logger.warning(f"{acc.name}: {e}")
            return ("✗", acc.name, addr, 0, 0, 0, 0, None, None)

    for r in await asyncio.gather(*[row(acc) for acc in accs]):
        tbl.add_row(*r)

    tbl.print()


# MARK: Main


async def main():
    cli = await create_cli("arcus", "configs/arcus.toml", ["privkey"])
    cfg = AppConfig.load(cli.config)

    all_accs, act_accs = await setup_app(cli, cfg, ArcusClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "positions":
            await print_positions(act_accs)
        case "stats" | "deposit" | "withdraw":
            noop_command("Arcus", cli.command)


if __name__ == "__main__":
    run_app(main())
