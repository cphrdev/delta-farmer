# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from clients.entropy import EntropyClient
from lib.cli import run_app
from lib.hip3 import run_hip3_app


async def main() -> None:
    await run_hip3_app(
        cli_name="entropy",
        app_name="Entropy",
        config_path="configs/entropy.toml",
        dex_prefix="io",
        client_cls=EntropyClient,
    )


if __name__ == "__main__":
    run_app(main())
