"""The listing agent as an MCP server, so other agents (Claude Desktop, an IDE agent, another marketplace bot) can use it
as a tool. Same agent, same guards: the photos and words go through the Meta pipeline, a blocked listing cannot be published, contact data is
masked, and nothing is published until the person at the client confirms it. The confirmation is an MCP elicitation
(a form the client shows its user, with the warning in it), not a tool argument, so the calling model cannot approve
its own publish; clients without elicitation get a refusal and a pointer to the Chợ Z app.

    python -m app.mcp_server            # stdio transport (Claude Desktop / Claude Code config: see README)

Tools: create_listing, answer_question, publish_listing, get_metrics. Photos are read from local paths, which must be
image files under $LISTING_MCP_PHOTO_ROOT (default: the current directory) and at most 15 MB each.
"""

import os
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app import config, metrics
from app.catalog import Catalog
from app.meta_api import make_client
from app.pipeline import Agent


class SellerConfirms(BaseModel):
    publish: bool = Field(description="Đăng tin này lên Chợ Z")
    read_warning: bool = Field(default=False, description="Tôi đã đọc cảnh báo (nếu có)")


def confirmation_message(run) -> str:
    listing, v = run.listing or {}, run.verification or {}
    price = f"{listing.get('price_vnd') or 0:,}".replace(",", ".")
    lines = [f"Đăng tin lên Chợ Z: {listing.get('title')}",
             f"Giá {price}đ · còn {listing.get('stock')} {listing.get('unit') or ''}".strip()]
    if v.get("verdict") == "warn":
        rules = ", ".join(r.get("id", "") for r in v.get("rules") or []) or "cảnh báo"
        lines.append(f"Cảnh báo ({rules}): {v.get('summary_vi')}")
    return "\n".join(lines)


PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MAX_PHOTO_BYTES = 15 * 1024 * 1024


def summary(run) -> dict:
    """What a calling agent needs: status, the listing with the source of each number, the check, the next step."""
    listing = run.listing or {}
    v = run.verification or {}
    next_step = {"needs_answer": "call answer_question with the seller's answer",
                 "ready": "call publish_listing; the seller confirms the listing and any warning in your client",
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

    def ask_seller(run_id: str) -> Elicit[SellerConfirms]:
        """Runs before publish_listing: refuse what cannot be published, otherwise ask the person at the client."""
        run = find(run_id)
        verdict = (run.verification or {}).get("verdict")
        if run.status != "ready" or verdict == "block":
            raise ToolError(f"run is {run.status}{' and blocked' if verdict == 'block' else ''}; nothing to publish")
        return Elicit(confirmation_message(run), SellerConfirms)

    @server.tool(annotations=ToolAnnotations(title="Publish to the sample catalog", readOnlyHint=False,
                                             destructiveHint=False, idempotentHint=True))
    def publish_listing(run_id: str,
                        answer: Annotated[ElicitationResult[SellerConfirms], Resolve(ask_seller)]) -> dict:
        """Publish a ready listing to the local sample catalog. The person at the client is asked to confirm (and to
        confirm any warning) through an MCP elicitation form; the calling model cannot confirm on their behalf.
        Blocked listings cannot be published."""
        run = find(run_id)
        warned = (run.verification or {}).get("verdict") == "warn"
        if answer.action != "accept" or not answer.data.publish:
            raise ToolError("the seller did not confirm; not published")
        if warned and not answer.data.read_warning:
            raise ToolError("the seller did not confirm the warning; not published")
        try:
            item = catalog.publish(run, settings.data_dir / "runs" / run.id, {"acknowledge": warned})
        except ValueError as exc:     # missing price/stock and similar: the reason goes back
            raise ToolError(str(exc)) from exc
        return {**item, "confirmed_by": "seller (MCP elicitation)"}

    @server.tool(annotations=ToolAnnotations(title="Agent metrics", readOnlyHint=True))
    def get_metrics(days: int = 7) -> dict:
        """Runs, p50/p95 time, cost, verdicts, guards fired and rules cited over the last `days` days."""
        return metrics.collect(settings.data_dir / "runs", days)

    return server


if __name__ == "__main__":
    build().run("stdio")
