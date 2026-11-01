"""edit: beat map + accepted takes + the real packshot → `EditPlan` (Spark writes it, Python validates it) → mp4.

Output: 1080×1920, subtitles from the script, the real box composited at the end, an end card labelled
AI, −14 LUFS. Spark writes the plan, Python validates it, ffmpeg assembles it (`assemble`).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path

from app.config import Settings
from studio import log, require_prompt, work_dir
from studio.models import EDIT_PLAN_SCHEMA, SPOT_SECONDS, EditClip, EditInput, EditPlan, Subtitle

INPUT, OUTPUT, ROLE = EditInput, EditPlan, "editor"

PROMPT = """Bạn là người dựng quảng cáo dọc, cắt hình theo tiếng. Đầu vào: Voice (lời dẫn đã thu; lines = từng ý với
start_s/end_s trên timeline tiếng; audio_path), Takes (các clip đã qua kiểm định: shot_id, clip_path, beats =
mốc hành động tính từ đầu clip, usable_start_s/usable_end_s = khoảng được dùng), Packshot (ảnh bao bì thật) và
Output.
Viết EditPlan (JSON theo schema):
- clips: mỗi ý của lời dẫn một clip, theo thứ tự ý; clip của shot có beat_index tương ứng (shot_id "s01" ↔ ý 0...);
  at_s = start_s của ý đó (timeline bắt đầu 0.0, các clip nối liền, không chồng, không hở quá 0.1 s);
  in_s/out_s nằm TRONG usable_start_s..usable_end_s của take; độ dài out_s-in_s = end_s-start_s của ý (co dãn
  ±0.3 s để mốc hành động trong beats rơi đúng hoặc hơi TRƯỚC chữ tương ứng, ví dụ "rót" rơi vào từ "đổ").
  Không kéo dài take quá khoảng dùng được; nếu ý dài hơn khoảng dùng được thì lấy tối đa khoảng đó.
- subtitles: nguyên văn từng ý (said), start_s/end_s đúng bằng của ý; thay "Chợ Dét" bằng "Chợ Z" trong chữ.
- voice_path = Voice.audio_path; packshot_path = Packshot; packshot_seconds 2.5-3.5 (thẻ kết sau clip cuối);
  end_card giữ chữ "Hình ảnh tạo bằng AI"; loudness_lufs -14; output_path = Output; rendered = false.
- Tổng (clip cuối + thẻ kết) ≤ 33 s. Không bịa đường dẫn: chỉ dùng clip_path có trong Takes."""


def validate_plan(plan: EditPlan, takes_by_shot: dict[str, tuple[float, float]]) -> list[str]:
    """What is wrong with a plan, as a list; empty means it can be rendered."""
    problems = []
    for clip in plan.clips:
        window = takes_by_shot.get(clip.shot_id)
        if window is None:
            problems.append(f"{clip.shot_id}: not an accepted take")
            continue
        if clip.out_s <= clip.in_s:
            problems.append(f"{clip.shot_id}: empty cut {clip.in_s}-{clip.out_s}")
        if clip.in_s < window[0] or clip.out_s > window[1]:
            problems.append(f"{clip.shot_id}: cut {clip.in_s}-{clip.out_s} outside usable {window[0]}-{window[1]}")
    for a, b in zip(plan.clips, plan.clips[1:]):
        if b.at_s < a.at_s + (a.out_s - a.in_s) - 0.01:
            problems.append(f"{a.shot_id} and {b.shot_id} overlap on the timeline")
    if plan.seconds > SPOT_SECONDS + 5:
        problems.append(f"{plan.seconds:.1f} s is longer than a {SPOT_SECONDS:.0f} s spot")
    if plan.subtitles and any(s.end_s <= s.start_s for s in plan.subtitles):
        problems.append("a subtitle ends before it starts")
    return problems


def _sample(inp: EditInput, output_path: str) -> EditPlan:
    takes = {t.shot_id: t for t in inp.takes}
    clips, subtitles, cursor = [], [], 0.0
    for line in inp.voice.lines:
        shot_id = f"s{line.beat_index + 1:02d}"
        take = takes.get(shot_id) or inp.takes[line.beat_index % len(inp.takes)]
        length = min(max(line.end_s - line.start_s, 1.0), take.usable_seconds)
        clips.append(EditClip(shot_id=take.shot_id, clip_path=take.clip_path, in_s=take.usable_start_s,
                              out_s=round(take.usable_start_s + length, 2), at_s=round(cursor, 2)))
        subtitles.append(Subtitle(text=line.said, start_s=round(cursor, 2), end_s=round(cursor + length, 2)))
        cursor += length
    return EditPlan(clips=clips, subtitles=subtitles, voice_path=inp.voice.audio_path,
                    packshot_path=inp.packshot_path, output_path=output_path)


FONT_BOLD = next((p for p in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
                               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if Path(p).is_file()), None)
FONT_NAME = "Segoe UI" if FONT_BOLD and "segoe" in FONT_BOLD.lower() else "Arial"
CREAM, INK, RED = (251, 243, 228), (34, 30, 26), (190, 40, 30)


def _ass_time(s: float) -> str:
    cs = int(round(s * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_file(plan: EditPlan, path: Path) -> Path:
    """Burned-in subtitles, one event per beat, wrapped, inside the 9:16 safe zone (bottom margin 330 px)."""
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {plan.width}", f"PlayResY: {plan.height}", "WrapStyle: 2", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: Sub,{FONT_NAME},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H66000000,-1,0,0,0,100,100,0,0,1,3,1,2,90,90,330,1", "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for sub in plan.subtitles:
        text = "\\N".join(textwrap.wrap(sub.text.replace("Chợ Dét", "Chợ Z"), 26))
        lines.append(f"Dialogue: 0,{_ass_time(sub.start_s)},{_ass_time(sub.end_s)},Sub,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def end_card(plan: EditPlan, title: str, price: str, cta: str, path: Path) -> Path:
    """Cream card: the real packshot (never repainted) centred, product name, price, CTA, the AI label."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = plan.width, plan.height
    img = Image.new("RGB", (width, height), CREAM)
    draw = ImageDraw.Draw(img)
    if plan.packshot_path and Path(plan.packshot_path).is_file():
        pack = Image.open(plan.packshot_path).convert("RGBA")
        ph = int(height * 0.40)
        pack = pack.resize((max(1, int(pack.width * ph / pack.height)), ph), Image.LANCZOS)
        img.paste(pack, ((width - pack.width) // 2, int(height * 0.17)), pack)

    def font(size: int):
        return ImageFont.truetype(FONT_BOLD, size) if FONT_BOLD else ImageFont.load_default()

    y = int(height * 0.60)
    for line in textwrap.wrap(title, 22):
        draw.text(((width - draw.textlength(line, font=font(72))) / 2, y), line, fill=INK, font=font(72))
        y += 86
    if price:
        draw.text(((width - draw.textlength(price, font=font(120))) / 2, y + 20), price, fill=RED, font=font(120))
        y += 190
    bw, bh = 760, 120
    bx, by = (width - bw) // 2, y + 30
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=60, fill=RED)
    draw.text(((width - draw.textlength(cta, font=font(60))) / 2, by + 24), cta, fill=CREAM, font=font(60))
    label = plan.end_card
    draw.text(((width - draw.textlength(label, font=font(40))) / 2, by + bh + 50), label, fill=INK, font=font(40))
    img.save(path)
    return path


def _measure(ffmpeg: str, path: Path) -> dict:
    err = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af", "loudnorm=I=-14:TP=-1.5:LRA=9:print_format=json",
                          "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    data = json.loads(err[err.rfind("{"):err.rfind("}") + 1])
    return {"lufs": float(data["input_i"]), "tp": float(data["input_tp"]), "lra": float(data["input_lra"]),
            "thresh": float(data["input_thresh"])}


def _escape(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:")


def assemble(plan: EditPlan, ffmpeg: str, title: str = "", price: str = "", cta: str = "Đặt ngay trên Chợ Z") -> dict:
    """ffmpeg: trim each clip to its cut, scale/crop to 9:16, concat, end card, subtitles; voice compressed, two-pass loudnorm."""
    out = Path(plan.output_path)
    work = out.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)
    inputs, chain, labels = [], [], []
    for i, clip in enumerate(plan.clips):
        inputs += ["-ss", f"{clip.in_s:.3f}", "-t", f"{clip.out_s - clip.in_s:.3f}", "-i", clip.clip_path]
        chain.append(f"[{i}:v]scale={plan.width}:{plan.height}:force_original_aspect_ratio=increase,"
                     f"crop={plan.width}:{plan.height},fps=30,setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    n = len(plan.clips)
    chain.append("".join(labels) + f"concat=n={n}:v=1:a=0[body]")
    card = end_card(plan, title, price, cta, work / "endcard.png")
    inputs += ["-loop", "1", "-t", f"{plan.packshot_seconds:.3f}", "-i", str(card)]
    chain.append(f"[{n}:v]scale={plan.width}:{plan.height},fps=30,fade=t=in:st=0:d=0.4,setpts=PTS-STARTPTS[card]")
    chain.append("[body][card]concat=n=2:v=1:a=0[full]")
    video_label = "[full]"
    if plan.subtitles:
        subs = ass_file(plan, work / "subs.ass")
        chain.append(f"[full]ass='{_escape(subs)}'[subbed]")
        video_label = "[subbed]"
    inputs += ["-i", plan.voice_path]
    chain.append(f"[{n + 1}:a]aresample=48000,acompressor=threshold=-20dB:ratio=3:attack=8:release=140:makeup=5dB,"
                 "alimiter=limit=0.7:attack=4:release=60[aout]")
    total = plan.seconds
    command = [ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", video_label,
               "-map", "[aout]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out)]
    (work / "ffmpeg-cmd.txt").write_text(" ".join(f'"{c}"' if " " in c else c for c in command), encoding="utf-8")
    subprocess.run(command, check=True)
    before = _measure(ffmpeg, out)                      # second pass: linear loudnorm from the measured values
    tmp = out.with_suffix(".ln.mp4")
    af = (f"loudnorm=I={plan.loudness_lufs}:TP=-1.5:LRA=9:measured_I={before['lufs']}:measured_TP={before['tp']}:"
          f"measured_LRA={before['lra']}:measured_thresh={before['thresh']}:linear=true")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(out), "-c:v", "copy", "-af", af, "-ar", "48000",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)], check=True)
    tmp.replace(out)
    after = _measure(ffmpeg, out)
    return {"seconds": round(total, 2), "loudness_before": before, "loudness_after": after}


async def render_plan(plan: EditPlan, ffmpeg: str | None, title: str = "", price: str = "",
                      cta: str = "Đặt ngay trên Chợ Z") -> dict | None:
    """Assemble the mp4 with ffmpeg; None when there is no ffmpeg (the plan is still valid on its own)."""
    if not ffmpeg:
        return None
    return await asyncio.to_thread(assemble, plan, ffmpeg, title, price, cta)


async def run(client, settings: Settings, inp: EditInput) -> EditPlan:
    folder = work_dir(settings, inp.work_dir, "edit")
    output_path = str(folder / "spot.mp4")
    describe = f"{len(inp.voice.lines)} beats, {len(inp.takes)} takes, packshot {'yes' if inp.packshot_path else 'no'}"
    async with log.step("edit", "Beat map + takes → edit plan → mp4", ROLE, describe, mock=client.mock) as step:
        if client.mock:
            plan = _sample(inp, output_path)
            step.model, step.endpoint = settings.text_model, "POST /v1/chat/completions"
        else:
            prompt = require_prompt("edit", PROMPT)
            user = (f"Voice:\n{inp.voice.model_dump_json()}\n\nTakes:\n"
                    + "\n".join(t.model_dump_json() for t in inp.takes)
                    + f"\n\nPackshot: {inp.packshot_path}\nOutput: {output_path}")
            raw, meta = await client.chat_json(prompt, user, [], "EditPlan", EDIT_PLAN_SCHEMA)
            step.apply(meta)
            raw.update({"voice_path": inp.voice.audio_path, "output_path": output_path, "rendered": False})
            plan = EditPlan.model_validate(raw)
        problems = validate_plan(plan, {t.shot_id: (t.usable_start_s, t.usable_end_s) for t in inp.takes
                                        if t.verdict == "accept"})
        if problems:
            raise ValueError("edit plan rejected: " + "; ".join(problems))
        if inp.render:
            from studio.render import ffmpeg_path
            price = next((s for s in inp.script.supers if "đ" in s or "nghìn" in s.lower()), "")
            result = await render_plan(plan, ffmpeg_path(), title=inp.script.product, price=price, cta=inp.script.cta)
            plan.rendered = result is not None
            if result:
                step.output["render"] = result
        step.decision = f"{len(plan.clips)} cuts, {plan.seconds:.1f} s, rendered={plan.rendered}"
        step.output["plan"] = plan.model_dump()
        return plan
