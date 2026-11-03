"""Send events to the stage over WebSocket (port 8765): replay a file, or run the day live and push as it happens.

    python -m sim.serve --replay runs/live_final/events.jsonl --speed 3120     # 26 h in 30 s
    python -m sim.serve --live --mode live --out runs/live_<ts>                  # only on stage if asked, good network

One JSON event per message, in file order. A client that connects late first receives everything sent so far, so
the stage can be reloaded mid-run. The stage opens /demo?ws=ws://localhost:8765.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve

from sim.workload import read_events


class Hub:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.clients: set[ServerConnection] = set()
        self.done = asyncio.Event()

    async def handler(self, ws: ServerConnection) -> None:
        for message in list(self.sent):
            await ws.send(message)
        self.clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    def publish(self, event: dict) -> None:
        message = json.dumps(event, ensure_ascii=False)
        self.sent.append(message)
        for ws in list(self.clients):
            asyncio.ensure_future(ws.send(message))


async def replay(hub: Hub, path: Path, speed: float, wait_for_client: bool) -> None:
    events = read_events(path)
    while wait_for_client and not hub.clients:
        await asyncio.sleep(0.1)
    start, t0 = time.perf_counter(), events[0]["t"]
    for e in events:
        due = (e["t"] - t0) / speed
        delay = due - (time.perf_counter() - start)
        if delay > 0:
            await asyncio.sleep(delay)
        hub.publish(e)
    hub.done.set()


async def live(hub: Hub, mode: str, seed: int, out: Path) -> None:
    from sim.run_day import run
    await run(mode, seed, out, on_event=hub.publish)
    hub.done.set()


async def main_async(args: argparse.Namespace) -> None:
    hub = Hub()
    async with serve(hub.handler, args.host, args.port):
        print(f"ws://{args.host}:{args.port}  ({'replay ' + str(args.replay) if args.replay else 'live ' + args.mode})",
              flush=True)
        if args.replay:
            await replay(hub, args.replay, args.speed, not args.no_wait)
        else:
            out = args.out or Path("runs") / f"{args.mode}_{time.strftime('%Y%m%d-%H%M%S')}"
            await live(hub, args.mode, args.seed, out)
        print(f"sent {len(hub.sent)} events", flush=True)
        if args.linger:
            await asyncio.sleep(args.linger)


def main() -> None:
    ap = argparse.ArgumentParser(description="WebSocket feed for the stage")
    ap.add_argument("--replay", type=Path, help="events.jsonl to replay")
    ap.add_argument("--speed", type=float, default=3120, help="simulated seconds per real second (3120: 26 h in 30 s)")
    ap.add_argument("--live", action="store_true", help="run the day and push events as they happen")
    ap.add_argument("--mode", choices=("mock", "live"), default="live")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-wait", action="store_true", help="replay starts at once instead of waiting for a client")
    ap.add_argument("--linger", type=float, default=600, help="seconds to keep serving after the last event")
    args = ap.parse_args()
    if not args.replay and not args.live:
        ap.error("pass --replay FILE or --live")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
