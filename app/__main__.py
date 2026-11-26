import asyncio
import sys

import uvicorn
from dotenv import load_dotenv

from app.core import settings
from app.core.logging import configure_logging, server_logging_config

load_dotenv()


def main() -> None:
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    # Set Compatible event loop policy on Windows Systems.
    # On Windows systems, the default ProactorEventLoop can cause issues with
    # certain async database drivers like psycopg (PostgreSQL driver).
    # The WindowsSelectorEventLoopPolicy provides better compatibility and prevents
    # "RuntimeError: Event loop is closed" errors when working with database connections.
    # This needs to be set before running the application server.
    # Refer to the documentation for more information.
    # https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-operations
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        log_config=server_logging_config(settings.LOG_LEVEL, settings.LOG_FORMAT),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
    )


if __name__ == "__main__":
    main()
elif __name__ == "__mp_main__":
    # Uvicorn's reload worker imports the entry module without calling main().
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
