"""voice: `Script` → one voice-over take (TTS plug-in) → `VoiceCheck`: Voice Transcribe reads it back, WER per beat.

The TTS engine is a plug-in outside Meta: `vieneu` runs VieNeu-TTS through `$VIENEU_PYTHON` (the venv
that has it) and `$VIENEU_VO` (its vo.py) with the voice in `$VIENEU_VOICE`; `mock` writes a short tone
per word so the chain runs anywhere. Meta's part is the check: the transcript must match the script,
beat by beat. The beat map (where each beat starts and ends) comes from the pauses between beats
(ffmpeg silencedetect); the edit is cut to it.
"""
from __future__ import annotations

import asyncio
import io
import math
import os
import re
import subprocess
import tempfile
import unicodedata
import wave
from pathlib import Path

from app.config import Settings
from studio import log, work_dir
from studio.models import LineCheck, Script, VoiceCheck, VoiceInput
from studio.render import ffmpeg_path

INPUT, OUTPUT, ROLE = VoiceInput, VoiceCheck, "voice"

RATE = 16000
WORDS_PER_SECOND = 3.3          # the pace of the narrator presets we measured (3.0-3.5 words/s)
PAUSE_S = 0.45                  # the gap a "[ngắt]" becomes in the one-take narration
MIN_PAUSE_S = 0.38              # a silence at least this long is a beat boundary
NOISE_DB = -38


def wer(said: str, heard: str) -> float:
    """Word error rate of `heard` against `said` (Levenshtein over words), 0.0 when identical."""
    a, b = _words(said), _words(heard)
    if not a:
        return 0.0 if not b else 1.0
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(a)


def _words(text: str) -> list[str]:
    """Lower-cased words without punctuation; an ASR stutter ("đổ đổ") is not a misread."""
    text = unicodedata.normalize("NFC", text.lower())
    words = re.sub(r"[^\w\s]", " ", text).split()
    return [w for i, w in enumerate(words) if i == 0 or w != words[i - 1]]


def _pcm(samples: list[int]) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return buffer.getvalue()


def mock_wav(script: Script) -> bytes:
    """A quiet 220 Hz tone for the duration each beat would take to read, a pause between beats."""
    samples: list[int] = []
    for i, beat in enumerate(script.beats):
        seconds = max(0.6, len(beat.split()) / WORDS_PER_SECOND)
        for n in range(int(seconds * RATE)):
            samples.append(int(6000 * math.sin(2 * math.pi * 220 * n / RATE)))
        if i < len(script.beats) - 1:
            samples += [0] * int(PAUSE_S * RATE)
    return _pcm(samples)


def vieneu_wav(script: Script) -> bytes:
    """VieNeu-TTS through its own interpreter: one take, "[ngắt]" between beats, 48 kHz WAV back."""
    python = os.environ.get("VIENEU_PYTHON", "").strip()
    vo = os.environ.get("VIENEU_VO", "").strip()
    voice = os.environ.get("VIENEU_VOICE", "Hải Đăng").strip()
    if not (python and vo and Path(python).is_file() and Path(vo).is_file()):
        raise RuntimeError("VieNeu-TTS plug-in needs VIENEU_PYTHON (its venv python) and VIENEU_VO (vo.py)")
    with tempfile.TemporaryDirectory() as tmp:
        text = Path(tmp) / "script.txt"
        out = Path(tmp) / "vo.wav"
        text.write_text(" [ngắt] ".join(script.beats), encoding="utf-8")
        result = subprocess.run([python, vo, "--text", str(text), "--voice", voice, "--out", str(out),
                                 "--pause", str(PAUSE_S)], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", cwd=str(Path(vo).parent))
        if result.returncode != 0 or not out.is_file():
            raise RuntimeError(f"VieNeu-TTS failed: {result.stderr[-400:]}")
        return out.read_bytes()


async def synthesize(script: Script, engine: str) -> bytes:
    """TTS plug-in: `mock` (tone), `vieneu` (VieNeu-TTS). A human take would be read from a file instead."""
    if engine == "mock":
        return mock_wav(script)
    if engine == "vieneu":
        return await asyncio.to_thread(vieneu_wav, script)
    raise NotImplementedError(f"TTS plug-in {engine!r} is not wired; use mock or vieneu")


def asr_wav(audio: bytes, ffmpeg: str | None) -> bytes:
    """Voice Transcribe wants mono s16le at 16 kHz; the TTS take is kept as it is for the edit."""
    with wave.open(io.BytesIO(audio), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    if (rate, channels, width) in ((RATE, 1, 2), (24000, 1, 2)):
        return audio
    if ffmpeg:
        result = subprocess.run([ffmpeg, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le",
                                 "pipe:1"], input=audio, capture_output=True)          # raw PCM: a piped WAV has no length
        if result.returncode == 0 and result.stdout:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(result.stdout)
            return buffer.getvalue()
    with wave.open(io.BytesIO(audio), "rb") as w:              # no ffmpeg: pick every n-th sample of channel 1
        frames = w.readframes(w.getnframes())
    step = max(1, round(rate / RATE)) * channels * width
    samples = [int.from_bytes(frames[i:i + 2], "little", signed=True) for i in range(0, len(frames) - 1, step)]
    return _pcm(samples)


def wav_seconds(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as w:
        return w.getnframes() / w.getframerate()


def speech_spans(path: Path, ffmpeg: str | None) -> list[tuple[float, float]]:
    """Stretches of speech between silences of at least MIN_PAUSE_S, from ffmpeg's silencedetect."""
    if not ffmpeg:
        return []
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af",
                             f"silencedetect=n={NOISE_DB}dB:d={MIN_PAUSE_S}", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = result.stderr
    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if not match:
        return []
    total = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    spans, cursor = [], 0.0
    for start, end in zip(starts, ends):
        if start - cursor > 0.15:
            spans.append((cursor, start))
        cursor = end
    if total - cursor > 0.15:
        spans.append((cursor, total))
    return spans


def beat_lines(beats: list[str], transcript: str, spans: list[tuple[float, float]], seconds: float) -> list[LineCheck]:
    """One LineCheck per beat: its words aligned in the transcript, its time from the pause map (or spread evenly)."""
    heard_words, said_counts = _words(transcript), [len(_words(b)) for b in beats]
    aligned = len(spans) == len(beats)
    lines, cursor = [], 0
    for i, beat in enumerate(beats):
        n = said_counts[i]
        heard = " ".join(heard_words[cursor:cursor + n])
        cursor += n
        if aligned:
            start, end = spans[i]
        else:
            start, end = i * seconds / len(beats), (i + 1) * seconds / len(beats)
        lines.append(LineCheck(beat_index=i, said=beat, heard=heard, wer=round(wer(beat, heard), 3),
                               start_s=round(start, 2), end_s=round(end, 2)))
    return lines


async def run(client, settings: Settings, inp: VoiceInput) -> VoiceCheck:
    folder = work_dir(settings, inp.work_dir, "voice")
    engine = "mock" if client.mock else inp.tts
    describe = f"{len(inp.script.beats)} beats, tts={engine}"
    async with log.step("voice", "Script → voice-over → read back (ASR)", ROLE, describe, mock=client.mock) as step:
        audio = await synthesize(inp.script, engine)
        path = folder / "voice.wav"
        path.write_bytes(audio)
        seconds = wav_seconds(audio)
        if client.mock:
            transcript = " ".join(inp.script.beats)
            step.model, step.endpoint = settings.asr_model, "POST /v1/asr/transcribe"
        else:
            transcript, meta = await client.transcribe(await asyncio.to_thread(asr_wav, audio, ffmpeg_path()))
            step.apply(meta)
        spans = await asyncio.to_thread(speech_spans, path, ffmpeg_path())
        lines = beat_lines(inp.script.beats, transcript, spans, seconds)
        check = VoiceCheck(audio_path=str(path), tts=engine, transcript=transcript, lines=lines,
                           wer=round(wer(" ".join(inp.script.beats), transcript), 3), seconds=round(seconds, 2))
        step.output["pauses_match"] = len(spans) == len(inp.script.beats)
        step.decision = (f"WER {check.wer:.2f}, {check.seconds:.1f} s, beat map "
                         f"{'from pauses' if len(spans) == len(inp.script.beats) else 'spread evenly'}")
        step.output["voice"] = check.model_dump()
        return check
