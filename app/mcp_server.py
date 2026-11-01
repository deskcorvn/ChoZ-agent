"""The listing agent as an MCP server, so other agents (Claude Desktop, an IDE agent, another marketplace bot) can use it
as a tool. Same agent, same guards: the photos and words go through the Meta pipeline, a warned listing still needs
`acknowledge=True` to publish, a blocked one cannot be published, contact data is masked.

    python -m app.mcp_server            # stdio transport (Claude Desktop / Claude Code config: see README)

Tools: create_listing, answer_question, publish_listing, get_metrics. Photos are read from local paths, which must be
image files under $LISTING_MCP_PHOTO_ROOT (default: the current directory) and at most 15 MB each.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import config, metrics
from app.catalog import Catalog
from app.meta_api import make_client
from app.pipeline import Agent

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MAX_PHOTO_BYTES = 15 * 1024 * 1024


def summary(run) -> dict:
    """What a calling agent needs: status, the listing with the source of each number, the check, the next step."""
    listing = run.listing or {}
    v = run.verification or {}
    next_step = {"needs_answer": "call answer_question with the seller's answer",
                 "ready": "show the listing to the seller; call publish_listing (acknowledge=True if verdict is warn)",
                 "blocked": "cannot be published", "failed": "see error"}.get(run.status, "")
    return {
        "run_id": run.id, "status": run.status, "next_step": next_step, "question": run.question or None,
        "listing": {k: listing.get(k) for k in ("title", "description", "category", "price_vnd", "stock", "unit",
                                                "attributes", "provenance")} if listing else None,
        "check": {"verdict": v.get("verdict"), "message_vi": v.get("summary_vi"), "rules": v.get("rules"),
                  "prohibited_reason": v.get("prohibited_reason") or None,
                  "instructions_found": v.get("instructions_found") or None} if v else None,
        "studio_photo": str(Path(run.id) / run.studio_image) if run.studio_image else None,
        "totals": run.totals(), "error": run.error or None,
    }


def build(settings: config.Settings | None = None, client=None) -> MCPServer:
    settings = settings or config.load()
    client = client or make_client(settings)
    agent = Agent(client, settings.data_dir / "runs")
    catalog = Catalog(settings.data_dir)
    root = Path(os.environ.get("LISTING_MCP_PHOTO_ROOT", os.getcwd())).resolve()
    server = MCPServer("choz-listing-agent", version="1.0",
                       instructions="Turns a seller's product photos and Vietnamese words into a verified Chợ Z listing "
                                    "using Meta models (Muse Spark, Muse Image, SAM 3.1, Voice Transcribe). Never publish "
                                    "without showing the listing and any warning to the seller first.")

    def find(run_id: str):
        run = agent.runs.get(run_id)
        if run is None:
            raise ToolError(f"unknown run {run_id}; runs live as long as this server process")
        return run

    def read_photo(path: str) -> bytes:
        p = Path(path).resolve()
        if root not in p.parents and p != root:
            raise ToolError(f"photo must be under {root}")
        if p.suffix.lower() not in PHOTO_SUFFIXES or not p.is_file():
            raise ToolError(f"not an image file: {p.name}")
        if p.stat().st_size > MAX_PHOTO_BYTES:
            raise ToolError(f"photo larger than 15 MB: {p.name}")
        return p.read_bytes()

    @server.tool(annotations=ToolAnnotations(title="Create a listing", readOnlyHint=False, openWorldHint=True))
    async def create_listing(photo_paths: list[str], seller_words: str) -> dict:
        """Create a marketplace listing from 1-3 product photos (local paths) and what the seller said, in Vietnamese
        (e.g. "Bánh đa cua Khánh Thọ, hộp, 22 nghìn một hộp, còn 80 hộp"). Returns the listing, the check against the
        photos with the policy rules it cites, the seller's own words behind price and stock, and the next step.
        Costs about $0.016 of Meta credits."""
        run = agent.create([read_photo(p) for p in photo_paths], None, seller_words)
        await agent.process(run)
        return summary(run)

    @server.tool(annotations=ToolAnnotations(title="Answer the agent's question", readOnlyHint=False))
    async def answer_question(run_id: str, answer: str) -> dict:
        """Give the seller's answer to the one follow-up question (price or stock), in their words."""
        run = find(run_id)
        await agent.answer(run, None, answer)
        return summary(run)

    @server.tool(annotations=ToolAnnotations(title="Publish to the sample catalog", readOnlyHint=False,
                                             destructiveHint=False, idempotentHint=True))
    def publish_listing(run_id: str, acknowledge: bool = False) -> dict:
        """Publish a ready listing to the local sample catalog. If the check warned, the seller must have read the
        warning: pass acknowledge=True only after showing it to them. Blocked listings cannot be published."""
        run = find(run_id)
        try:
            return catalog.publish(run, settings.data_dir / "runs" / run.id, {"acknowledge": acknowledge})
        except ValueError as exc:     # blocked, unacknowledged warning, missing price/stock: the reason goes back
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=ToolAnnotations(title="Agent metrics", readOnlyHint=True))
    def get_metrics(days: int = 7) -> dict:
        """Runs, p50/p95 time, cost, verdicts, guards fired and rules cited over the last `days` days."""
        return metrics.collect(settings.data_dir / "runs", days)

    return server


if __name__ == "__main__":
    build().run("stdio")
