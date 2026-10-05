"""Execute the archived engine, changing only the pinned TuShare HTTPS transport."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    # The engine Python imports its own quantlab package. Adapt only the SDK
    # transport, without replacing any engine module or strategy configuration.
    import tushare as ts

    def https_factory(*positional, **keywords):
        client = original(*positional, **keywords)
        url = getattr(client, "_DataApi__http_url", None)
        if url not in {"http://api.waditu.com/dataapi", "https://api.waditu.com/dataapi"}:
            raise ValueError("Unexpected TuShare transport in pinned SDK")
        client._DataApi__http_url = "https://api.waditu.com/dataapi"
        return client

    original = ts.pro_api
    ts.pro_api = https_factory
    from quantlab.scout.daily_runtime import read, worker

    worker(read(args.settings), args.state)


if __name__ == "__main__":
    main()
