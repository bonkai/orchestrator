"""`python -m orchestrator` → boots uvicorn."""

import uvicorn

# init_db + ensure_runner happen in the FastAPI startup hook (app.py).


def main():
    uvicorn.run(
        "orchestrator.app:app",
        host="127.0.0.1",
        port=7878,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
