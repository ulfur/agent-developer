#!/usr/bin/env python3
"""Keyword-triggered recorder that also drives the e-ink overlay workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import List, Sequence
from urllib import error, request

import numpy as np
import pyaudio
from vosk import KaldiRecognizer, Model

from lib.auth_tokens import issue_service_token

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = REPO_ROOT / "models" / "vosk-model-small-en-us-0.15"
DEFAULT_BACKEND_URL = os.environ.get("NIGHTSHIFT_BACKEND_URL", "http://127.0.0.1:8080")
DEFAULT_AUTH_TOKEN = os.environ.get("NIGHTSHIFT_AUTH_TOKEN")
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_CLIP_DIR = REPO_ROOT / "logs" / "audio_clips"


class OverlayClient:
    """Small helper that hits the backend overlay API."""

    def __init__(
        self,
        base_url: str | None,
        data_dir: Path,
        *,
        email: str | None = None,
        token: str | None = None,
        timeout: float = 3.0,
        enabled: bool = True,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.data_dir = data_dir
        self.email = email
        self._token = token
        self.timeout = timeout
        self.enabled = enabled and bool(self.base_url)

    def show_message(
        self,
        title: str,
        lines: Sequence[str],
        *,
        duration_sec: float | None = None,
        invert: bool | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        payload = {
            "title": title,
            "lines": list(lines),
        }
        if duration_sec is not None:
            payload["duration_sec"] = duration_sec
        if invert is not None:
            payload["invert"] = invert
        return self._request("POST", "/api/eink/overlay", payload)

    def clear(self) -> bool:
        if not self.enabled:
            return False
        return self._request("DELETE", "/api/eink/overlay")

    def show_prompt(self) -> None:
        self.show_message("Yes boss?", ["Yes boss?"], duration_sec=2.0)

    def show_transcript(self, transcript: str, duration_sec: float) -> None:
        display_text = transcript.strip() or "(No speech detected)"
        self.show_message("Transcript", [display_text], duration_sec=duration_sec)

    def _request(self, method: str, path: str, payload: dict | None = None) -> bool:
        token = self._ensure_token()
        if not token:
            return False
        url = f"{self.base_url}{path}"
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        req = request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            return True
        except error.URLError as exc:
            print(f"[overlay] request failed: {exc}")
            return False

    def _ensure_token(self) -> str | None:
        if not self.enabled:
            return None
        if self._token:
            return self._token
        try:
            self._token = issue_service_token(self.data_dir, self.email)
        except RuntimeError as exc:
            print(f"[overlay] unable to issue token: {exc}")
            self.enabled = False
            return None
        return self._token


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH, help="Path to the Vosk model")
    parser.add_argument("--keyword", type=str, default="nightshift", help="Keyword to trigger recording")
    parser.add_argument("--device", type=int, default=0, help="PyAudio device index")
    parser.add_argument("--input-rate", type=int, default=44100, help="Microphone capture sample rate")
    parser.add_argument("--target-rate", type=int, default=16000, help="Recognizer target sample rate")
    parser.add_argument("--chunk", type=int, default=4000, help="Frames per read")
    parser.add_argument("--capture-seconds", type=float, default=10.0, help="Length of the recording window")
    parser.add_argument("--cooldown-seconds", type=float, default=8.0, help="Minimum delay between triggers")
    parser.add_argument("--result-duration", type=float, default=10.0, help="Seconds to keep transcript overlay visible")
    parser.add_argument("--backend-url", type=str, default=DEFAULT_BACKEND_URL, help="Nightshift backend base URL")
    parser.add_argument("--auth-email", type=str, default=None, help="Service account email for auth tokens")
    parser.add_argument("--auth-token", type=str, default=DEFAULT_AUTH_TOKEN, help="Use an existing Bearer token")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Path to data/ for auth secrets")
    parser.add_argument("--overlay-timeout", type=float, default=3.0, help="Overlay HTTP timeout in seconds")
    parser.add_argument("--disable-overlay", action="store_true", help="Skip overlay API calls")
    parser.add_argument("--save-clips", action="store_true", help="Persist captured WAV clips for debugging")
    parser.add_argument("--clip-dir", type=Path, default=DEFAULT_CLIP_DIR, help="Directory for saved clips")
    return parser.parse_args()


def load_model(path: Path) -> Model:
    if not path.exists():
        sys.exit(f"Model directory {path} does not exist. Download Vosk and try again.")
    return Model(str(path))


def build_keyword_recognizer(model: Model, rate: int, keyword: str) -> KaldiRecognizer:
    vocab = json.dumps([keyword.lower(), "[unk]"])
    recognizer = KaldiRecognizer(model, rate, vocab)
    recognizer.SetWords(False)
    return recognizer


def build_transcriber(model: Model, rate: int) -> KaldiRecognizer:
    recognizer = KaldiRecognizer(model, rate)
    recognizer.SetWords(True)
    return recognizer


def listen_for_keyword(
    keyword_recognizer: KaldiRecognizer,
    transcriber: KaldiRecognizer,
    keyword: str,
    input_rate: int,
    target_rate: int,
    chunk: int,
    device_index: int,
    *,
    capture_seconds: float,
    cooldown_seconds: float,
    result_duration: float,
    overlay: OverlayClient,
    save_clips: bool,
    clip_dir: Path,
) -> None:
    keyword = keyword.lower()
    cooldown = max(0.0, cooldown_seconds)
    last_trigger = 0.0
    if save_clips:
        clip_dir.mkdir(parents=True, exist_ok=True)
    audio = pyaudio.PyAudio()
    try:
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=input_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk,
        )
    except OSError as exc:
        print(f"Unable to open device {device_index} at {input_rate} Hz: {exc}")
        audio.terminate()
        return
    print(
        f"Listening on device {device_index} at {input_rate} Hz for keyword '{keyword}' (Ctrl-C to exit)"
    )
    try:
        while True:
            data = stream.read(chunk, exception_on_overflow=False)
            if not data:
                continue
            converted = resample_audio(data, input_rate, target_rate)
            triggered = False
            if keyword_recognizer.AcceptWaveform(converted):
                result = json.loads(keyword_recognizer.Result())
                text = (result.get("text") or "").strip().lower()
                if keyword in text.split():
                    triggered = True
            else:
                partial = json.loads(keyword_recognizer.PartialResult())
                partial_text = (partial.get("partial") or "").lower()
                if keyword in partial_text.split():
                    triggered = True
                    keyword_recognizer.Reset()
            if triggered:
                now = time.monotonic()
                if now - last_trigger < cooldown:
                    continue
                last_trigger = now
                keyword_recognizer.Reset()
                handle_detection(
                    stream,
                    transcriber,
                    data,
                    input_rate,
                    target_rate,
                    chunk,
                    capture_seconds,
                    result_duration,
                    overlay,
                    save_clips,
                    clip_dir,
                )
    except KeyboardInterrupt:
        print("\nStopping listener.")
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()


def handle_detection(
    stream: pyaudio.Stream,
    transcriber: KaldiRecognizer,
    first_chunk: bytes,
    input_rate: int,
    target_rate: int,
    chunk: int,
    capture_seconds: float,
    result_duration: float,
    overlay: OverlayClient,
    save_clips: bool,
    clip_dir: Path,
) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Keyword detected — recording {capture_seconds:.1f}s window")
    overlay.show_prompt()
    frames = record_phrase(stream, first_chunk, capture_seconds, input_rate, chunk)
    clip_path = None
    if save_clips:
        clip_path = write_wave_clip(frames, input_rate, clip_dir)
        print(f"Saved clip to {clip_path}")
    transcript = transcribe_audio(transcriber, frames, input_rate, target_rate)
    print(f"Transcript: {transcript or '(none)'}")
    overlay.show_transcript(transcript, duration_sec=result_duration)


def record_phrase(
    stream: pyaudio.Stream,
    first_chunk: bytes,
    capture_seconds: float,
    input_rate: int,
    chunk: int,
) -> List[bytes]:
    frames: List[bytes] = [first_chunk]
    total_frames = max(1, int((capture_seconds * input_rate) / chunk))
    for _ in range(total_frames - 1):
        data = stream.read(chunk, exception_on_overflow=False)
        if not data:
            continue
        frames.append(data)
    return frames


def transcribe_audio(
    recognizer: KaldiRecognizer,
    frames: Sequence[bytes],
    input_rate: int,
    target_rate: int,
) -> str:
    recognizer.Reset()
    for chunk in frames:
        converted = resample_audio(chunk, input_rate, target_rate)
        if not converted:
            continue
        recognizer.AcceptWaveform(converted)
    result = json.loads(recognizer.Result() or "{}")
    return (result.get("text") or "").strip()


def write_wave_clip(frames: Sequence[bytes], sample_rate: int, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"nightshift_clip_{timestamp}.wav"
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"".join(frames))
    return path


def resample_audio(data: bytes, input_rate: int, target_rate: int) -> bytes:
    if input_rate == target_rate or not data:
        return data
    samples = np.frombuffer(data, dtype=np.int16)
    if samples.size == 0:
        return b""
    ratio = target_rate / float(input_rate)
    new_length = max(1, int(samples.size * ratio))
    positions = np.linspace(0, samples.size, num=new_length, endpoint=False)
    resampled = np.interp(positions, np.arange(samples.size), samples).astype(np.int16)
    return resampled.tobytes()


def main() -> int:
    args = parse_args()
    model = load_model(args.model)
    keyword_recognizer = build_keyword_recognizer(model, args.target_rate, args.keyword)
    transcriber = build_transcriber(model, args.target_rate)
    data_dir = args.data_dir if args.data_dir.is_absolute() else REPO_ROOT / args.data_dir
    overlay_client = OverlayClient(
        args.backend_url,
        data_dir=data_dir,
        email=args.auth_email,
        token=args.auth_token,
        timeout=args.overlay_timeout,
        enabled=not args.disable_overlay,
    )
    clip_dir = args.clip_dir if args.clip_dir.is_absolute() else REPO_ROOT / args.clip_dir
    try:
        listen_for_keyword(
            keyword_recognizer,
            transcriber,
            args.keyword,
            args.input_rate,
            args.target_rate,
            args.chunk,
            args.device,
            capture_seconds=args.capture_seconds,
            cooldown_seconds=args.cooldown_seconds,
            result_duration=args.result_duration,
            overlay=overlay_client,
            save_clips=args.save_clips,
            clip_dir=clip_dir,
        )
    finally:
        overlay_client.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
