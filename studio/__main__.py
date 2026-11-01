"""`python -m studio <service> --in file.json [--out file.json] [--mock]` and `python -m studio demo <product_dir>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from app import config
from app.meta_api import make_client
from studio import SERVICES, run_service
from studio.demo import run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m studio", description=__doc__)
    parser.add_argument("service", choices=(*SERVICES, "demo"))
    parser.add_argument("target", nargs="?", help="demo: a product folder, e.g. samples/products/05-chao-hau-hop")
    parser.add_argument("--in", dest="inp", help="input JSON file (service's input model)")
    parser.add_argument("--out", help="output JSON file (service) or folder (demo); default: stdout / data/studio")
    parser.add_argument("--mock", action="store_true", help="use the mock client even when a key is set")
    args = parser.parse_args(argv)

    settings = config.load()
    if args.mock:
        settings = replace(settings, mock=True)
    client = make_client(settings)

    async def go():
        try:
            if args.service == "demo":
                if not args.target:
                    parser.error("demo needs a product folder")
                return await run_demo(client, settings, Path(args.target), Path(args.out) if args.out else None)
            if not args.inp:
                parser.error(f"{args.service} needs --in file.json")
            payload = json.loads(Path(args.inp).read_text(encoding="utf-8"))
            return await run_service(args.service, client, settings, payload)
        finally:
            await client.aclose()

    result = asyncio.run(go())
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if hasattr(sys.stdout, "reconfigure"):      # Vietnamese on a cp1252 console or pipe
        sys.stdout.reconfigure(encoding="utf-8")
    if args.service == "demo":
        summary = Path(result["work_dir"]) / "demo.json"
        summary.write_text(text, encoding="utf-8")
        print(text)
        print(f"wrote {summary} ({'mock' if settings.mock else 'live'})", file=sys.stderr)
    elif args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({'mock' if settings.mock else 'live'})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
