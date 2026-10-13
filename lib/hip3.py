# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from decimal import Decimal

from clients.hyperliquid import (
    HyperLiquidClient,
    migrate_hyperliquid_accounts,
    warn_legacy_hyperliquid_accounts,
)
from lib.cli import create_cli, noop_command, setup_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy


def normalize_hip3_symbols(symbols: list[str], dex_prefix: str) -> list[str]:
    result: list[str] = []
    for symbol in symbols:
        if ":" not in symbol:
            result.append(f"{dex_prefix}:{symbol}")
            continue
        if not symbol.startswith(f"{dex_prefix}:"):
            raise ValueError(f"expected {dex_prefix}: symbol, got {symbol!r}")
        result.append(symbol)
    return result


async def print_hip3_info[H: HyperLiquidClient](
    accs: list[H], app_name: str, cli_name: str
) -> None:
    table = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Mode", justify="left"),
    )
    legacy_accounts: list[str] = []

    async def row(acc: H):
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
    warn_legacy_hyperliquid_accounts(legacy_accounts, app_name, cli_name)


async def run_hip3_app[H: HyperLiquidClient](
    *,
    cli_name: str,
    app_name: str,
    config_path: str,
    dex_prefix: str,
    client_cls: type[H],
) -> None:
    cli = await create_cli(
        cli_name,
        config_path,
        ["privkey"],
        custom_commands={"migrate": lambda _: None},
    )
    cfg = AppConfig.load(cli.config)
    strategies = list(cfg.pools.values()) if cfg.pools else [cfg.strategy]
    for strategy in strategies:
        assert strategy is not None
        strategy.symbols = normalize_hip3_symbols(strategy.symbols, dex_prefix)

    all_accs, act_accs = await setup_app(cli, cfg, client_cls.from_config)

    match cli.command:
        case "info":
            await print_hip3_info(all_accs, app_name, cli_name)
        case "positions":
            await print_positions(act_accs)
        case "close":
            await close_all(act_accs)
        case "migrate":
            await migrate_hyperliquid_accounts(all_accs, app_name)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "deposit" | "withdraw" | "stats":
            noop_command(app_name, cli.command)
