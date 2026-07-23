"""``python -m watch2`` の起動エントリポイント。"""

from __future__ import annotations

import asyncio
import logging
import signal

from dotenv import load_dotenv

from .bot import WatchClient
from .config import load_config, load_discord_token


async def _run() -> None:
    load_dotenv()
    config = load_config()
    token = load_discord_token()
    client = WatchClient(config.projects)

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        if not client.is_closed():
            asyncio.create_task(client.close())

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_shutdown)
        except NotImplementedError:
            pass

    try:
        await client.start(token)
    finally:
        if not client.is_closed():
            await client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()

