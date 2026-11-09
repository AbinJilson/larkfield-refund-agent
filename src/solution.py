"""Service entry point: load dotenv, then run the FastAPI app with uvicorn."""

from __future__ import annotations

import os


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    import uvicorn

    from .app import create_app

    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    log_level = os.getenv("LOG_LEVEL", "info").strip().lower() or "info"
    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
