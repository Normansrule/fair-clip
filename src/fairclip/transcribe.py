"""Stage 2: transcription with word-level timestamps.

Two engines:
* ``sidecar``: a transcript JSON supplied with the source (the demo uses this; also handy when
  the rights holder provides captions). Validated against the strict Transcript schema.
* ``faster-whisper``: local speech-to-text. The audio is decoded to raw PCM *inside the media
  sandbox* first, and faster-whisper only ever receives a NumPy array, so no media decoder runs
  on the host.

Transcript text is untrusted data (it is whatever someone said on camera). It is never
executed and never treated as instructions; see llm.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from .models import Sentence, Transcript, Word

_SAFE_WORD = re.compile(r"[\x00-\x1f\x7f]")


def load_sidecar(path: Path) -> Transcript:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("transcript sidecar larger than 20 MB")
    return Transcript.model_validate(json.loads(path.read_text("utf-8")))


def whisper_transcribe(pcm_s16: np.ndarray, model_size: str = "small", language: str = "en") -> Transcript:
    try:
        from faster_whisper import WhisperModel  # optional dependency: pip install .[asr]
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("faster-whisper not installed: pip install 'fair-clip[asr]'") from exc
    audio = pcm_s16.astype(np.float32) / 32768.0
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(audio, language=language, word_timestamps=True, vad_filter=True)
    sentences = []
    for seg in segments:
        words = [Word(text=_SAFE_WORD.sub("", w.word).strip()[:64] or "…", start=float(w.start), end=float(w.end))
                 for w in (seg.words or [])]
        if words:
            sentences.append(Sentence(start=words[0].start, end=words[-1].end, text=seg.text.strip()[:2000], words=words))
    return Transcript(language=language, engine=f"faster-whisper:{model_size}", sentences=sentences)
