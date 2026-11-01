"""The fixed shapes the agent works with: the JSON schemas sent to Muse Spark and the models that check its answers.

Structured output constrains decoding to the schema, and pydantic checks the result again: a listing
that does not validate is a failed step, not something to patch up.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import ROOT

# The model picks an ASCII id; the seller sees the Vietnamese name. Measured 31/10/2026: with the
# Vietnamese names themselves as the enum, 2 of 5 food products came back as the first two entries
# ("Thời trang nam", "Thời trang nữ").
CATEGORIES: list[dict] = json.loads((ROOT / "samples" / "categories.json").read_text(encoding="utf-8-sig"))
CATEGORY_NAMES: dict[str, str] = {c["id"]: c["name"] for c in CATEGORIES}

REQUIRED_FOR_SALE = ("price_vnd", "stock")


class Attribute(BaseModel):
    name: str
    value: str


class Listing(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str
    category_id: str
    category: str = ""                   # the Vietnamese name, filled in from category_id
    attributes: list[Attribute] = []
    variants: list[str] = []
    price_vnd: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    unit: str | None = None
    missing: list[str] = []
    product_phrase_en: str = Field(max_length=60)
    background_prompt_en: str
    evidence: dict = {}            # {"price_quote", "stock_quote"}: the seller's own words behind the numbers
    provenance: dict = {}          # filled in code: per field, the source and whether the quote was found verbatim

    @field_validator("product_phrase_en")
    @classmethod
    def _phrase_or_fallback(cls, value: str) -> str:
        # Measured 1 Nov 2026: for a prescription antibiotic Spark returned an empty phrase (it declines to name
        # the item for the cut-out), and the whole run failed validation instead of reaching the check that blocks
        # it. An empty phrase is not a reason to stop; SAM gets a generic one and the verifier still decides.
        value = value.strip()
        return value if len(value) >= 2 else "product"

    @field_validator("category_id")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CATEGORY_NAMES:
            raise ValueError(f"unknown category id {value!r}")
        return value

    @model_validator(mode="after")
    def _category_name(self) -> Listing:
        self.category = CATEGORY_NAMES[self.category_id]
        return self

    def missing_for_sale(self) -> list[str]:
        return [name for name in REQUIRED_FOR_SALE if getattr(self, name) is None]


class Check(BaseModel):
    claim: str
    status: Literal["consistent", "not_visible", "contradicted"]
    note: str = ""


class Verification(BaseModel):
    verdict: Literal["pass", "warn", "block"]
    prohibited: bool
    prohibited_reason: str = ""
    checks: list[Check] = []
    summary_vi: str
    instructions_found: str = ""   # text in the photos or words that tries to instruct the agent (quoted), else ""
    rule_ids: list[str] = []       # ids from policy/listing-policy.md the verifier applied
    rules: list[dict] = []         # filled in code: the cited rules that exist ({id, title})


class SaleFacts(BaseModel):
    """What a seller's answer to the follow-up question settles."""
    price_vnd: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)


_NULLABLE_INT = {"type": ["integer", "null"], "minimum": 0}

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Vietnamese product title, at most 120 characters"},
        "description": {"type": "string", "description": "Vietnamese sales description, 2-5 short sentences"},
        "category_id": {"type": "string", "enum": list(CATEGORY_NAMES)},
        "attributes": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                      "required": ["name", "value"]},
        },
        "variants": {"type": "array", "items": {"type": "string"},
                     "description": "Sizes, colours or other options the seller named"},
        "price_vnd": {**_NULLABLE_INT, "description": "Price in Vietnamese dong, null if the seller did not say"},
        "stock": {**_NULLABLE_INT, "description": "Units in stock, null if the seller did not say"},
        "unit": {"type": ["string", "null"], "description": "Selling unit in Vietnamese, e.g. cái, hộp, kg"},
        "missing": {"type": "array", "items": {"type": "string"},
                    "description": "Field names the seller did not give"},
        "product_phrase_en": {"type": "string",
                              "description": "1-4 English words naming the product as seen, for segmentation"},
        "background_prompt_en": {"type": "string",
                                 "description": "An empty studio scene for this product: no product, people, text or logos"},
        "evidence": {"type": "object",
                     "properties": {"price_quote": {"type": "string"}, "stock_quote": {"type": "string"}},
                     "required": ["price_quote", "stock_quote"],
                     "description": "The exact words from the seller's note the price and the stock come from, "
                                    "copied verbatim; \"\" when the field is null"},
    },
    "required": ["title", "description", "category_id", "attributes", "variants", "price_vnd", "stock",
                 "unit", "missing", "product_phrase_en", "background_prompt_en", "evidence"],
}

VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "warn", "block"]},
        "prohibited": {"type": "boolean"},
        "prohibited_reason": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"claim": {"type": "string"},
                                     "status": {"type": "string",
                                                "enum": ["consistent", "not_visible", "contradicted"]},
                                     "note": {"type": "string"}},
                      "required": ["claim", "status", "note"]},
        },
        "summary_vi": {"type": "string"},
        "instructions_found": {"type": "string"},
        "rule_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "prohibited", "prohibited_reason", "checks", "summary_vi", "instructions_found", "rule_ids"],
}

SALE_FACTS_SCHEMA = {
    "type": "object",
    "properties": {"price_vnd": _NULLABLE_INT, "stock": _NULLABLE_INT},
    "required": ["price_vnd", "stock"],
}
