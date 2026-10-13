# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from decimal import Decimal

from clients.hyperliquid import migrate_hyperliquid_accounts, warn_legacy_hyperliquid_accounts
from clients.tradexyz import TradeXYZClient
from lib.cli import create_cli, noop_command, run_app, setup_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy


def _normalize_symbols(symbols: list[str]) -> list[str]:
    return [symbol if ":" in symbol else f"xyz:{symbol}" for symbol in symbols]


async def print_info(accs: list[TradeXYZClient]) -> None:
    table = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Mode", justify="left"),
    )
    legacy_accounts: list[str] = []

    async def row(acc: TradeXYZClient):
        registered, balance, mode = await asyncio.gather(
            acc.registered(), acc.balance(), acc.account_mode()
        )
        if mode != "unifiedAccount":
            legacy_accounts.append(acc.name)
        return (
            "✓" if registered else "✗",
            acc.name,
            short_addr(acc.address),
            balance if registered else Decimal(0),
            mode,
        )

    for values in await gather_accs(accs, row):
        table.add_row(*values)

    table.print()
    warn_legacy_hyperliquid_accounts(legacy_accounts, "TradeXYZ")


async def main() -> None:
    cli = await create_cli(
        "tradexyz",
        "configs/tradexyz.toml",
        ["privkey"],
        custom_commands={"migrate": lambda _: None},
    )
    cfg = AppConfig.load(cli.config)
    strategies = list(cfg.pools.values()) if cfg.pools else [cfg.strategy]
    for strategy in strategies:
        assert strategy is not None
        strategy.symbols = _normalize_symbols(strategy.symbols)

    all_accs, act_accs = await setup_app(cli, cfg, TradeXYZClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "close":
            await close_all(act_accs)
        case "migrate":
            await migrate_hyperliquid_accounts(all_accs, "TradeXYZ")
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "deposit" | "withdraw" | "stats":
            noop_command("Trade.xyz", cli.command)


if __name__ == "__main__":
    run_app(main())
