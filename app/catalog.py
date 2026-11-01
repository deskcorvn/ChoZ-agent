"""The sample shop the demo publishes to: a JSON file standing in for the Chợ Z catalog (not called here)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from app import audit, guard
from app.pipeline import Run
from app.schema import Listing

EDITABLE = ("title", "description", "price_vnd", "stock", "category_id")


class Catalog:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "catalog.json"
        self.images = data_dir / "catalog"

    def items(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def remove(self, item_id: str) -> bool:
        """Take a listing off the catalog and delete its image (the seller asked to delete their data)."""
        items = self.items()
        keep = [i for i in items if i["id"] != item_id]
        if len(keep) == len(items):
            return False
        for i in items:
            if i["id"] == item_id:
                (self.images / i["image"]).unlink(missing_ok=True)
        self.path.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def publish(self, run: Run, run_dir: Path, edits: dict) -> dict:
        try:
            item = self._publish(run, run_dir, edits)
        except ValueError as exc:
            audit.append(audit.path_for(self.path.parent), "publish_refused", run.id, status=run.status,
                         verdict=(run.verification or {}).get("verdict"), reason=str(exc)[:120])
            raise
        audit.append(audit.path_for(self.path.parent), "published", run.id, seller=run.seller,
                     verdict=item.get("verification"),
                     acknowledged=item.get("acknowledged", []), masked=item.get("masked", []),
                     confirmed_by=edits.get("confirmed_by", "seller (app)"))
        return item

    def _publish(self, run: Run, run_dir: Path, edits: dict) -> dict:
        if run.status == "blocked":
            raise ValueError("the check blocked this item; it cannot be listed")
        if run.status != "ready":
            raise ValueError(f"run is {run.status}, not ready to publish")
        verification = run.verification or {}
        if verification.get("verdict") == "warn" and not edits.get("acknowledge"):
            # Human in the loop: a warning (W rules) must be read and confirmed by the seller before it goes public.
            raise ValueError("the check warned about this listing; confirm you have read the warning to publish")
        listing = Listing.model_validate({**run.listing, **{k: v for k, v in edits.items() if k in EDITABLE}})
        # The seller may type into the preview: what they type is masked like what the model wrote.
        masked, kinds = guard.mask_listing(listing.model_dump())
        listing = Listing.model_validate(masked)
        missing = listing.missing_for_sale()
        if missing:
            raise ValueError(f"still missing: {', '.join(missing)}")
        self.images.mkdir(parents=True, exist_ok=True)
        image = run.studio_image or run.photos[0]
        target = self.images / f"{run.id}{Path(image).suffix}"
        shutil.copyfile(run_dir / image, target)
        item = {"id": run.id, "seller": run.seller, "published_at": time.strftime("%Y-%m-%d %H:%M:%S"), "image": target.name,
                "verification": (run.verification or {}).get("verdict"), **listing.model_dump(
                    exclude={"missing", "product_phrase_en", "background_prompt_en", "evidence", "provenance"})}
        if kinds:
            item["masked"] = kinds
        if verification.get("verdict") == "warn":
            item["acknowledged"] = [r["id"] for r in verification.get("rules") or []] or ["warning"]
        items = [i for i in self.items() if i["id"] != run.id] + [item]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        # Verify the outcome, not the intent: read the catalog back and compare what a buyer will see.
        stored = next((i for i in self.items() if i["id"] == run.id), None)
        if stored is None or any(stored.get(k) != item[k] for k in ("title", "price_vnd", "stock", "category", "image")):
            raise ValueError("the catalog did not store the listing as sent; nothing was published")
        run.status = "published"
        return item
