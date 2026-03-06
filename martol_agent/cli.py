"""Sync entry point for the `martol` console script."""

import asyncio
import sys


def main():
    from martol_agent.wrapper import main as async_main

    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
