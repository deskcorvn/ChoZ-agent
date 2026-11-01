"""voice: `Script` → one voice-over take (TTS plug-in) → `VoiceCheck`: Voice Transcribe reads it back, WER per beat.

The TTS engine is a plug-in outside Meta (VieNeu-TTS or a recorded human). Meta's part is the check:
the transcript must match the script, beat by beat, and gives the beat map the edit is cut to.
"""
from __future__ import annotations

import io
import wave

from app.config import Settings
from studio import log, work_dir
from studio.models import LineCheck, Script, VoiceCheck, VoiceInput

INPUT, OUTPUT, ROLE = VoiceInput, VoiceCheck, "voice"

SECONDS_PER_BEAT_MOCK = 1.2
RATE = 16000


def wer(said: str, heard: str) -> float:
    """Word error rate of `heard` against `said` (Levenshtein over words), 0.0 when identical."""
    a, b = said.lower().split(), heard.lower().split()
    if not a:
        return 0.0 if not b else 1.0
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(a)


def silence_wav(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00" * int(seconds * RATE))
    return buffer.getvalue()


async def synthesize(script: Script, engine: str) -> bytes:
    """TTS plug-in. `mock` writes silence of the right length; other engines are not wired yet."""
    if engine == "mock":
        return silence_wav(SECONDS_PER_BEAT_MOCK * len(script.beats))
    raise NotImplementedError(f"TODO(live): TTS plug-in {engine!r} (VieNeu-TTS / human take) is not wired yet")


async def run(client, settings: Settings, inp: VoiceInput) -> VoiceCheck:
    folder = work_dir(settings, inp.work_dir, "voice")
    engine = "mock" if client.mock else inp.tts
    describe = f"{len(inp.script.beats)} beats, tts={engine}"
    async with log.step("voice", "Script → voice-over → read back (ASR)", ROLE, describe, mock=client.mock) as step:
        audio = await synthesize(inp.script, engine)
        path = folder / "voice.wav"
        path.write_bytes(audio)
        seconds = len(audio) / (RATE * 2)
        if client.mock:
            transcript = " ".join(inp.script.beats)
            step.model, step.endpoint = settings.asr_model, "POST /v1/asr/transcribe"
        else:
            transcript, meta = await client.transcribe(audio)
            step.apply(meta)
        # TODO(live): per-beat timing from the ASR's word timestamps; until then beats are spread evenly.
        per_beat = seconds / len(inp.script.beats)
        heard_words = transcript.split()
        lines, cursor = [], 0
        for i, beat in enumerate(inp.script.beats):
            n = len(beat.split())
            heard = " ".join(heard_words[cursor:cursor + n])
            cursor += n
            lines.append(LineCheck(beat_index=i, said=beat, heard=heard, wer=round(wer(beat, heard), 3),
                                   start_s=round(i * per_beat, 2), end_s=round((i + 1) * per_beat, 2)))
        check = VoiceCheck(audio_path=str(path), tts=engine, transcript=transcript, lines=lines,
                           wer=round(wer(" ".join(inp.script.beats), transcript), 3), seconds=round(seconds, 2))
        step.decision = f"WER {check.wer:.2f}, {check.seconds:.1f} s"
        step.output["voice"] = check.model_dump()
        return check
