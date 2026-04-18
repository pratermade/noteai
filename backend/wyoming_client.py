from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_RATE = 16000
_WIDTH = 2
_CHANNELS = 1
_CHUNK = 4096


async def convert_to_pcm(audio_bytes: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0",
        "-ar", str(_RATE), "-ac", str(_CHANNELS), "-f", "s16le",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()[-500:]}")
    return stdout


async def _send_event(writer: asyncio.StreamWriter, event_type: str, data: dict, payload: bytes | None = None) -> None:
    msg = {"type": event_type, "data": data, "payload_length": len(payload) if payload else 0}
    writer.write(json.dumps(msg).encode() + b"\n")
    if payload:
        writer.write(payload)
    await writer.drain()


async def _read_event(reader: asyncio.StreamReader) -> dict:
    line = await reader.readline()
    event = json.loads(line)
    # Wyoming >= 1.8.0: event data follows as a separate JSON block
    data_length = event.pop("data_length", 0)
    if data_length > 0:
        data_bytes = await reader.readexactly(data_length)
        event["data"] = json.loads(data_bytes)
    # Binary payload (audio chunks etc.)
    payload_length = event.get("payload_length", 0)
    if payload_length > 0:
        event["payload"] = await reader.readexactly(payload_length)
    return event


async def transcribe(audio_bytes: bytes, host: str, port: int, timeout: float = 30.0) -> str:
    pcm = await convert_to_pcm(audio_bytes)
    if not pcm:
        raise RuntimeError("Audio converted to empty PCM — recording may be too short or in an unsupported format")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=10.0
    )
    try:
        audio_info = {"rate": _RATE, "width": _WIDTH, "channels": _CHANNELS}
        await _send_event(writer, "audio-start", audio_info)

        for i in range(0, len(pcm), _CHUNK):
            chunk = pcm[i:i + _CHUNK]
            await _send_event(writer, "audio-chunk", audio_info, payload=chunk)

        await _send_event(writer, "audio-stop", {})

        async def _wait_for_transcript() -> str:
            while True:
                event = await _read_event(reader)
                logger.debug("Wyoming event: %s", event.get("type"))
                data = event.get("data") or {}
                if event["type"] == "transcript":
                    return data.get("text", "")
                if event["type"] == "error":
                    raise RuntimeError(data.get("text", "Wyoming error"))

        return await asyncio.wait_for(_wait_for_transcript(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
