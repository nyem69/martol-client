"""Entry point for `python -m martol_agent`."""

import asyncio
import sys

from martol_agent.wrapper import main

try:
    asyncio.run(main())
except (KeyboardInterrupt, asyncio.CancelledError):
    pass  # Handled by signal handler in wrapper.main()
sys.exit(0)
