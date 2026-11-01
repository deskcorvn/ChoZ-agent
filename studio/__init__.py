"""Studio: from a listing to a 30-second product video, as nine small services (preview).

Each service is a module with `async def run(client, settings, inp) -> out`, pydantic in and out, and
one `studio.log.Step` per model call. Chain: script → shotlist → refs → voice → frames → render → qc →
edit → judge. Meta models do the writing, painting, picking, checking and judging; the video engine
(Kling over MCP) and the second-opinion reviewer are plug-ins behind small interfaces.

A tenth service, `banner`, makes an animated shop banner from the same listing with Meta models only.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from app.config import Settings

SERVICES = ("script", "shotlist", "refs", "voice", "frames", "render", "qc", "edit", "judge", "banner")


class PromptMissing(NotImplementedError):
    """A live call was attempted for a service whose prompt is not written yet."""


def require_prompt(service: str, prompt: str) -> str:
    if not prompt.strip():
        raise PromptMissing(f"TODO(prompt): the {service} prompt is not written yet; run with --mock")
    return prompt


def service(name: str):
    if name not in SERVICES:
        raise KeyError(f"unknown studio service {name!r}; one of {', '.join(SERVICES)}")
    return importlib.import_module(f"studio.{name}")


def work_dir(settings: Settings, requested: str | None, *parts: str) -> Path:
    """Where a service writes files: the requested folder, else `data/studio/<parts>`."""
    path = Path(requested) if requested else settings.data_dir / "studio"
    for part in parts:
        path = path / part
    path.mkdir(parents=True, exist_ok=True)
    return path


async def run_service(name: str, client, settings: Settings, payload: dict) -> dict:
    """Validate `payload` as the service's input, run it, return the output as JSON-ready dict."""
    module = service(name)
    inp = module.INPUT.model_validate(payload)
    out = await module.run(client, settings, inp)
    return out.model_dump()
