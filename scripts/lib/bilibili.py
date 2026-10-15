"""B 站数据访问。

只用免登录的公开接口，全部经实测确认在当前网络环境可用：

- 投稿列表：`x/series/recArchivesByKeywords`（免 cookie、免 WBI 签名，支持 pn 翻页）
- 视频详情：`x/web-interface/view`
- 音频流：`x/player/playurl` 的 DASH 音频轨（免 cookie）

刻意不使用 yt-dlp：实测 yt-dlp 拉单视频与 UP 投稿页都被风控返回 412，
即便手动带上 buvid3 也无效；而 playurl 接口稳定可用。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

LIST_API = "https://api.bilibili.com/x/series/recArchivesByKeywords"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"

PAGE_SIZE = 50
# 列表接口的 412 / -799 是风控与频控，退避重试比立刻放弃更合适。
RETRY_DELAYS = (2.0, 5.0, 12.0)


class BilibiliError(RuntimeError):
    pass


@dataclass(slots=True)
class VideoBrief:
    bvid: str
    aid: int
    title: str
    duration: int
    pubdate: int
    view: int
    description: str = ""


@dataclass(slots=True)
class VideoPart:
    page: int
    cid: int
    title: str
    duration: int


@dataclass(slots=True)
class VideoDetail:
    bvid: str
    aid: int
    title: str
    uploader: str
    pubdate: int
    parts: list[VideoPart] = field(default_factory=list)

    @property
    def is_multipart(self) -> bool:
        return len(self.parts) > 1


def _client() -> httpx.Client:
    return httpx.Client(headers=BROWSER_HEADERS, timeout=30, follow_redirects=True)


def _get_json(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    """带退避重试的 GET，把风控响应翻译成人能看懂的报错。"""
    last: Exception | None = None
    for attempt, delay in enumerate((0.0, *RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            response = client.get(url, params=params)
            if response.status_code == 412:
                last = BilibiliError("B 站风控拦截（HTTP 412）")
                continue
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last = exc
            continue

        code = payload.get("code")
        if code == 0:
            return payload.get("data") or {}
        if code in (-352, -799, -509):
            last = BilibiliError(f"B 站风控/频控（code={code}）：{payload.get('message')}")
            continue
        raise BilibiliError(f"B 站接口返回 code={code}：{payload.get('message')}")
    raise BilibiliError(f"请求失败（已重试 {len(RETRY_DELAYS)} 次）：{url} —— {last}")


def list_videos(mid: int, limit: int, *, page_size: int = PAGE_SIZE) -> list[VideoBrief]:
    """按发布时间从新到旧拉取一个 UP 主的投稿列表。"""
    videos: list[VideoBrief] = []
    page = 1
    with _client() as client:
        while len(videos) < limit:
            data = _get_json(
                client,
                LIST_API,
                {
                    "mid": mid,
                    "keywords": "",
                    "ps": min(page_size, limit - len(videos)),
                    "pn": page,
                },
            )
            archives = data.get("archives") or []
            if not archives:
                break
            for item in archives:
                videos.append(
                    VideoBrief(
                        bvid=item.get("bvid") or "",
                        aid=int(item.get("aid") or 0),
                        title=(item.get("title") or "").strip(),
                        duration=int(item.get("duration") or 0),
                        pubdate=int(item.get("pubdate") or 0),
                        view=int((item.get("stat") or {}).get("view") or 0),
                        description=(item.get("desc") or "").strip(),
                    )
                )
            total = int((data.get("page") or {}).get("total") or 0)
            if len(videos) >= limit or page * page_size >= total:
                break
            page += 1
            time.sleep(0.6)
    return [video for video in videos if video.bvid][:limit]


def video_detail(bvid: str) -> VideoDetail:
    with _client() as client:
        data = _get_json(client, VIEW_API, {"bvid": bvid})
    pages = data.get("pages") or []
    parts = [
        VideoPart(
            page=int(item.get("page") or index + 1),
            cid=int(item.get("cid") or 0),
            title=(item.get("part") or data.get("title") or bvid).strip(),
            duration=int(item.get("duration") or 0),
        )
        for index, item in enumerate(pages)
    ]
    if not parts or not parts[0].cid:
        raise BilibiliError(f"{bvid} 未返回可用的分 P / cid")
    return VideoDetail(
        bvid=bvid,
        aid=int(data.get("aid") or 0),
        title=(data.get("title") or bvid).strip(),
        uploader=((data.get("owner") or {}).get("name") or "").strip(),
        pubdate=int(data.get("pubdate") or 0),
        parts=parts,
    )


def download_audio(bvid: str, cid: int, destination: Path) -> Path:
    """把 DASH 音频轨下载为 mp3。

    先取 playurl 拿音频流地址，再带 Referer 直接下载，最后交给 ffmpeg 转 mp3。
    """
    if shutil.which("ffmpeg") is None:
        raise BilibiliError("未找到 ffmpeg，请先安装并加入 PATH（音频转码依赖它）")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with _client() as client:
        data = _get_json(
            client, PLAYURL_API, {"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1}
        )
        audio_streams = ((data.get("dash") or {}).get("audio")) or []
        if not audio_streams:
            raise BilibiliError(f"{bvid} 未返回 DASH 音频流（可能是付费或受限内容）")
        # 取码率最高的一路，转写准确率更稳。
        stream = max(audio_streams, key=lambda item: item.get("bandwidth") or 0)
        candidates = [stream.get("baseUrl") or stream.get("base_url")]
        candidates += stream.get("backupUrl") or stream.get("backup_url") or []

        source = destination.with_suffix(".m4s")
        error: Exception | None = None
        for candidate in [url for url in candidates if url]:
            try:
                with client.stream(
                    "GET",
                    candidate,
                    headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
                    timeout=180,
                ) as response:
                    response.raise_for_status()
                    with source.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                error = None
                break
            except httpx.HTTPError as exc:
                error = exc
        if error is not None:
            raise BilibiliError(f"{bvid} 音频流下载失败：{error}")

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vn", "-codec:a", "libmp3lame", "-b:a", "96k",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise BilibiliError(f"ffmpeg 转码失败：{result.stderr.strip()[-400:]}")
    finally:
        source.unlink(missing_ok=True)
    return destination
