# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.tradexyz import TradeXYZClient
from lib.cli import run_app
from lib.hip3 import run_hip3_app


async def main() -> None:
    await run_hip3_app(
        cli_name="tradexyz",
        app_name="Trade.xyz",
        config_path="configs/tradexyz.toml",
        dex_prefix="xyz",
        client_cls=TradeXYZClient,
    )


if __name__ == "__main__":
    run_app(main())
