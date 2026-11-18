"""Run the mock store: python -m mock_store [--host 127.0.0.1] [--port 8780]."""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    args = ap.parse_args()
    uvicorn.run("mock_store.app:app_from_env", factory=True, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
