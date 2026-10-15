"""语音转写：ffmpeg 切片 + 阿里云百炼千问转写。

所有 B 站视频在无登录 cookie 时都拿不到字幕（实测 `x/player/wbi/v2` 返回
`code=0` 但 `subtitles` 为空），因此转写是唯一的正文来源。切片后逐段转写，
避免单次请求超时或超出模型输入上限。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import dashscope

from .settings import Settings

# 转写是整条流水线里最贵的一步，失败就重试比交给人工重跑便宜得多。
STT_ATTEMPTS = 3
STT_RETRY_DELAYS = (3.0, 8.0)


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


def _wait_readable(path: Path, timeout: float = 15.0) -> None:
    """等切片变成可读。

    Windows 上 ffmpeg 刚写完的 mp3 可能被安全软件的实时扫描短暂占用，
    直接读/上传会报 `WinError 32 另一个程序正在使用此文件`。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            with path.open("rb") as handle:
                handle.read(1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def split_audio(audio: Path, output_dir: Path, chunk_seconds: int) -> list[Path]:
    """按时长切片。用 `-c copy` 直接切流，不重新编码，几十秒的音频是瞬时的。

    每次调用都用新的子目录：上一次跑剩下的、可能仍被占用的切片文件
    不会和这一次撞名，重复跑同一个视频也不会互相干扰。
    """
    _require_ffmpeg()
    shutil.rmtree(output_dir, ignore_errors=True)
    target_dir = output_dir / uuid.uuid4().hex[:8]
    target_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(audio)
    if duration <= chunk_seconds:
        target = target_dir / "chunk-0000.mp3"
        shutil.copyfile(audio, target)
        _wait_readable(target)
        return [target]

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio),
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-c", "copy", str(target_dir / "chunk-%04d.mp3"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TranscribeError(f"音频切片失败：{result.stderr.strip()[-400:]}")
    chunks = sorted(target_dir.glob("chunk-*.mp3"))
    if not chunks:
        raise TranscribeError("音频切片后没有得到任何分片")
    for chunk in chunks:
        _wait_readable(chunk)
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
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, *STT_RETRY_DELAYS)):
            if delay:
                # 多为文件被短暂占用或网络抖动，稍等再试一次即可。
                log(f"      重试第 {attempt} 次（{type(last).__name__}）")
                time.sleep(delay)
            try:
                _wait_readable(chunk)
                pieces.append(_transcribe_chunk(chunk, settings))
                last = None
                break
            except Exception as exc:  # noqa: BLE001 - 转写失败一律重试，不区分异常类型
                last = exc
        if last is not None:
            raise TranscribeError(f"{chunk.name} 转写失败（已重试 {len(STT_RETRY_DELAYS)} 次）：{last}")
    return "\n".join(pieces)
