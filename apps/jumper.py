# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.jumper import JumperClient
from lib.cli import create_cli, noop_command, run_app, setup_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy


async def print_info(accs: list[JumperClient]) -> None:
    table = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("HL Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: JumperClient):
        registered = await acc.registered()
        if not registered:
            return ("✗", acc.name, short_addr(acc.address), 0, 0, 0)
        profile = await acc.profile()
        return (
            "✓",
            acc.name,
            short_addr(acc.address),
            profile.volume,
            -profile.pnl,
            profile.balance,
        )

    for values in await gather_accs(accs, row):
        table.add_row(*values)

    table.print()


async def main() -> None:
    cli = await create_cli("jumper", "configs/jumper.toml", ["privkey"])
    cfg = AppConfig.load(cli.config)
    all_accs, act_accs = await setup_app(cli, cfg, JumperClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "deposit" | "withdraw" | "stats":
            noop_command("Jumper", cli.command)


if __name__ == "__main__":
    run_app(main())
