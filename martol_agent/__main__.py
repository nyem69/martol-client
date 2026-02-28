"""Entry point for `python -m martol_agent`."""

import asyncio

from martol_agent.wrapper import main

asyncio.run(main())
