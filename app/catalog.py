"""The sample shop the demo publishes to: a JSON file standing in for the Chợ Z catalog (not called here)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from app import guard
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

    def publish(self, run: Run, run_dir: Path, edits: dict) -> dict:
        if run.status == "blocked":
            raise ValueError("the check blocked this item; it cannot be listed")
        if run.status != "ready":
            raise ValueError(f"run is {run.status}, not ready to publish")
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
        item = {"id": run.id, "published_at": time.strftime("%Y-%m-%d %H:%M:%S"), "image": target.name,
                "verification": (run.verification or {}).get("verdict"), **listing.model_dump(
                    exclude={"missing", "product_phrase_en", "background_prompt_en"})}
        if kinds:
            item["masked"] = kinds
        items = [i for i in self.items() if i["id"] != run.id] + [item]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        # Verify the outcome, not the intent: read the catalog back and compare what a buyer will see.
        stored = next((i for i in self.items() if i["id"] == run.id), None)
        if stored is None or any(stored.get(k) != item[k] for k in ("title", "price_vnd", "stock", "category", "image")):
            raise ValueError("the catalog did not store the listing as sent; nothing was published")
        run.status = "published"
        return item
