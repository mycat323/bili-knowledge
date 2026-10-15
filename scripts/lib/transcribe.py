"""语音转写：ffmpeg 切片 + 阿里云百炼千问转写。

所有 B 站视频在无登录 cookie 时都拿不到字幕（实测 `x/player/wbi/v2` 返回
`code=0` 但 `subtitles` 为空），因此转写是唯一的正文来源。切片后逐段转写，
避免单次请求超时或超出模型输入上限。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import dashscope

from .settings import Settings


class TranscribeError(RuntimeError):
    pass


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise TranscribeError("未找到 ffmpeg，请先安装并加入 PATH")


def probe_duration(path: Path) -> float:
    """读取音频时长（秒）。"""
    _require_ffmpeg()
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TranscribeError(f"读取音频时长失败：{result.stderr.strip()[-300:]}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise TranscribeError(f"无法解析音频时长：{result.stdout!r}") from exc


def split_audio(audio: Path, output_dir: Path, chunk_seconds: int) -> list[Path]:
    """按时长切片。用 `-c copy` 直接切流，不重新编码，几十秒的音频是瞬时的。"""
    _require_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("chunk-*.mp3"):
        stale.unlink()

    duration = probe_duration(audio)
    if duration <= chunk_seconds:
        target = output_dir / "chunk-0000.mp3"
        shutil.copyfile(audio, target)
        return [target]

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio),
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-c", "copy", str(output_dir / "chunk-%04d.mp3"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TranscribeError(f"音频切片失败：{result.stderr.strip()[-400:]}")
    chunks = sorted(output_dir.glob("chunk-*.mp3"))
    if not chunks:
        raise TranscribeError("音频切片后没有得到任何分片")
    return chunks


def _transcribe_chunk(chunk: Path, settings: Settings) -> str:
    dashscope.base_http_api_url = settings.stt_base_url.rstrip("/")
    response = dashscope.MultiModalConversation.call(
        api_key=settings.stt_api_key,
        model=settings.stt_model,
        messages=[
            {
                "role": "system",
                "content": [
                    {
                        "text": (
                            "请忠实转写音频中的所有语音，只输出转写文本，"
                            "不要总结、不要解释、不要回答内容。"
                        )
                    }
                ],
            },
            {"role": "user", "content": [{"audio": chunk.resolve().as_uri()}]},
        ],
        result_format="message",
    )
    status = getattr(response, "status_code", 200)
    if status != 200:
        message = getattr(response, "message", None) or getattr(response, "code", status)
        raise TranscribeError(f"{settings.stt_model} 转写失败（{status}）：{message}")
    try:
        content = response.output.choices[0].message.content
        text = "".join(str(item.get("text", "")) for item in content).strip()
    except (AttributeError, IndexError, TypeError) as exc:
        raise TranscribeError(f"{settings.stt_model} 返回结构异常：{str(response)[:300]}") from exc
    if not text:
        raise TranscribeError(f"{chunk.name} 未返回有效文本")
    return text


def transcribe(audio: Path, work_dir: Path, settings: Settings, *, log=print) -> str:
    """切片并逐段转写，返回拼接后的全文。"""
    chunks = split_audio(audio, work_dir / "chunks", settings.audio_chunk_seconds)
    pieces: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        log(f"    转写 {index}/{len(chunks)}：{chunk.name}")
        pieces.append(_transcribe_chunk(chunk, settings))
    return "\n".join(pieces)
