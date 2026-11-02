"""The fixed shapes the studio services exchange: inputs and outputs of every service, as pydantic models.

A service's output is validated here before the next service sees it; a model answer that does not
validate is a failed step. `*_SCHEMA` constants are what goes into Meta's `json_schema` response format.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_WORDS_PER_BEAT = 12
SPOT_SECONDS = 30.0            # the product video is a 30-second vertical spot
# VieNeu read 27 words in 8.8 s on 2 Nov 2026 (about 3 words a second): a 30 s spot with a 3 s end card needs
# about 75 spoken words. A script below this is sent back once with its word count.
MIN_SPOT_WORDS = 65
FRAME_SIZE = "1080x1920"       # 9:16 first frames and final mp4


def _schema(model: type[BaseModel]) -> dict:
    """JSON schema for Meta's structured output: no `$defs` indirection, every property required."""
    schema = model.model_json_schema(mode="serialization")
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {}
            for key, value in node.items():
                if key in ("title", "default"):
                    continue
                out[key] = {name: inline(prop) for name, prop in value.items()} if key == "properties" else inline(value)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return {"title": model.__name__, **inline(schema)}


# ---- script ----------------------------------------------------------------------------------

class Claim(BaseModel):
    text: str
    source: Literal["listing", "label", "seller"]   # where the claim comes from; nothing is invented
    evidence: str = ""


class ScriptInput(BaseModel):
    listing: dict                                    # the listing JSON the app produced
    photos: list[str] = []                           # paths of the seller's photos
    brief: str = ""                                  # one line from the seller: tone, audience
    allowed_claims: list[str] = []                   # claims the seller may make beyond the listing


class Script(BaseModel):
    product: str
    beats: list[str] = Field(min_length=4, max_length=8)      # voice-over lines, one idea each
    supers: list[str] = []                                     # on-screen text
    cta: str
    claims: list[Claim] = []
    language: str = "vi"

    @field_validator("beats")
    @classmethod
    def _short_beats(cls, beats: list[str]) -> list[str]:
        for beat in beats:
            if len(beat.split()) > MAX_WORDS_PER_BEAT:
                raise ValueError(f"beat longer than {MAX_WORDS_PER_BEAT} words: {beat!r}")
        return beats


# ---- shot list -------------------------------------------------------------------------------

class Bible(BaseModel):
    """Continuity: the same person, place and props in every shot."""
    character: str = ""
    location: str = ""
    props: list[str] = []
    time_of_day: str = ""                            # "" = the time the script names (shotlist.time_from_script)


class ShotListInput(BaseModel):
    script: Script
    bible: Bible = Bible()


class Shot(BaseModel):
    id: str
    beat_index: int = Field(default=0, ge=0)
    seconds: float = Field(ge=5.0, le=8.0)
    action: str                                      # one action per shot
    camera: str = "locked"                           # the camera does not move
    light: str = ""
    frame_prompt: str                                # the first frame (Muse Image)
    motion_prompt: str                               # what moves (render engine)
    ref_roles: list[str] = []                        # which reference images this shot needs


class ShotList(BaseModel):
    shots: list[Shot] = Field(min_length=1)
    bible: Bible = Bible()

    @property
    def seconds(self) -> float:
        return sum(s.seconds for s in self.shots)


# ---- references ------------------------------------------------------------------------------

class RefsInput(BaseModel):
    shotlist: ShotList
    photos: list[str] = []                           # the real product, for the packshot
    character_photos: list[str] = []                 # licensed face photos (e.g. CC0) used instead of painting one
    work_dir: str | None = None


class RefImage(BaseModel):
    role: str                                        # character | location | prop | product
    angle: str = ""                                  # front | three-quarter | profile | ""
    path: str
    source: Literal["muse-image", "photo", "supplied", "mock"] = "muse-image"


class Refs(BaseModel):
    images: list[RefImage] = []

    def for_role(self, role: str) -> list[RefImage]:
        return [i for i in self.images if i.role == role]


# ---- voice -----------------------------------------------------------------------------------

class VoiceInput(BaseModel):
    script: Script
    work_dir: str | None = None
    tts: str = "mock"                                # tts plug-in id: vieneu | human | mock


class LineCheck(BaseModel):
    beat_index: int
    said: str
    heard: str
    wer: float = Field(ge=0.0)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(ge=0.0)


class VoiceCheck(BaseModel):
    audio_path: str
    tts: str
    transcript: str
    lines: list[LineCheck] = []
    wer: float = Field(ge=0.0)
    seconds: float = Field(ge=0.0)

    @property
    def beat_map(self) -> list[tuple[int, float, float]]:
        return [(line.beat_index, line.start_s, line.end_s) for line in self.lines]


# ---- first frames ----------------------------------------------------------------------------

class FramesInput(BaseModel):
    shot: Shot
    refs: Refs = Refs()
    candidates: int = Field(default=3, ge=1, le=4)
    second_opinion: bool = False                     # ask the external reviewer plug-in too
    work_dir: str | None = None


class FramePick(BaseModel):
    shot_id: str
    candidates: list[str]                            # paths of the painted first frames
    picked: int = Field(ge=0)
    reasons: list[str] = []                          # why the picked one, one line each
    rejected: dict[str, str] = {}                    # candidate path → what is wrong (hands, text, ...)
    second_opinion: str | None = None

    @property
    def path(self) -> str:
        return self.candidates[self.picked]


# ---- render ----------------------------------------------------------------------------------

class RenderInput(BaseModel):
    shot: Shot
    frame_path: str
    work_dir: str | None = None
    engine: str | None = None                        # kling | mock; None → from the environment


class RenderJob(BaseModel):
    shot_id: str
    engine: str
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    seconds: float
    clip_path: str | None = None
    url: str | None = None
    credits: int | None = None
    error: str = ""


# ---- qc --------------------------------------------------------------------------------------

class CheckItem(BaseModel):
    name: str
    ok: bool
    evidence: str = ""                               # what was seen, with a timestamp when it matters


class Beat(BaseModel):
    at_s: float = Field(ge=0.0)
    what: str


class QcInput(BaseModel):
    shot: Shot
    clip_path: str
    refs: Refs = Refs()
    accept: list[str] = ["one action", "camera locked", "no extra hands", "no text", "product unchanged"]
    second_opinion: bool = False


class QcVerdict(BaseModel):
    shot_id: str
    clip_path: str
    checks: list[CheckItem] = []
    beats: list[Beat] = []
    usable_start_s: float = Field(ge=0.0)
    usable_end_s: float = Field(ge=0.0)
    verdict: Literal["accept", "retry", "reject"]
    director_note: str = ""                          # how to change the prompt for the next round
    second_opinion: str | None = None

    @field_validator("usable_end_s")
    @classmethod
    def _window(cls, end: float, info) -> float:
        start = info.data.get("usable_start_s", 0.0)
        if end < start:
            raise ValueError(f"usable window ends ({end}) before it starts ({start})")
        return end

    @property
    def usable_seconds(self) -> float:
        return self.usable_end_s - self.usable_start_s


# ---- edit ------------------------------------------------------------------------------------

class EditInput(BaseModel):
    script: Script
    voice: VoiceCheck
    takes: list[QcVerdict] = Field(min_length=1)     # accepted takes only
    packshot_path: str | None = None                 # the real box, from the seller's photo
    work_dir: str | None = None
    render: bool = False                             # also run ffmpeg (needs ffmpeg)


class EditClip(BaseModel):
    shot_id: str
    clip_path: str
    in_s: float = Field(ge=0.0)
    out_s: float = Field(ge=0.0)
    at_s: float = Field(ge=0.0)                      # position on the timeline


class Subtitle(BaseModel):
    text: str
    start_s: float = Field(ge=0.0)
    end_s: float = Field(ge=0.0)


class EditPlan(BaseModel):
    width: int = 1080
    height: int = 1920
    clips: list[EditClip] = Field(min_length=1)
    subtitles: list[Subtitle] = []
    voice_path: str
    packshot_path: str | None = None
    packshot_seconds: float = 2.0
    end_card: str = "Hình ảnh tạo bằng AI"           # the AI label on the end card
    loudness_lufs: float = -14.0
    output_path: str
    rendered: bool = False

    @property
    def seconds(self) -> float:
        return max((c.at_s + (c.out_s - c.in_s) for c in self.clips), default=0.0) + self.packshot_seconds


# ---- judge -----------------------------------------------------------------------------------

class JudgeInput(BaseModel):
    video_path: str
    script: Script | None = None


class Criterion(BaseModel):
    name: str
    score: float = Field(ge=0.0, le=10.0)
    at_s: list[float] = []
    note: str = ""


class JudgeReport(BaseModel):
    video_path: str
    qc_items: list[CheckItem] = []                   # 14 technical checks
    criteria: list[Criterion] = []                   # 6 editorial criteria
    total: float = Field(ge=0.0, le=10.0)
    summary: str = ""


SCRIPT_SCHEMA = _schema(Script)
SHOTLIST_SCHEMA = _schema(ShotList)
FRAME_PICK_SCHEMA = _schema(FramePick)
QC_SCHEMA = _schema(QcVerdict)
EDIT_PLAN_SCHEMA = _schema(EditPlan)
JUDGE_SCHEMA = _schema(JudgeReport)
