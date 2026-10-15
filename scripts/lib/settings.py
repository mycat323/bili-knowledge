"""流水线配置与路径。

配置来源是仓库根目录的 `.env`（不进版本库），密钥只从环境变量读取，
不写入任何生成的知识库文件。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SITE_ROOT = SCRIPTS_DIR.parent  # bili-knowledge/
REPO_ROOT = SITE_ROOT.parent  # 存放「知识库/」的上级目录
VAULT_ROOT = REPO_ROOT / "知识库"
CACHE_ROOT = SITE_ROOT / ".cache" / "fetch"
ENV_FILE = SITE_ROOT / ".env"
BUILD_CONTENT = SCRIPTS_DIR / "build-content.mjs"


class ConfigError(RuntimeError):
    pass


def load_env_file(path: Path = ENV_FILE) -> None:
    """把 .env 读进环境变量，已存在的环境变量优先，便于临时覆盖。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(f"缺少配置 {name}，请在 {ENV_FILE} 中填写（可参考 .env.example）")
    return value


def _optional_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 {name} 必须是整数，当前为 {raw!r}") from exc


def _as_bool(raw: str | None) -> bool | None:
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    stt_base_url: str
    stt_model: str
    stt_api_key: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_enable_thinking: bool | None
    audio_chunk_seconds: int
    llm_max_tokens: int
    request_timeout_seconds: int = 1200


def load_settings() -> Settings:
    load_env_file()
    provider = (os.getenv("STT_PROVIDER") or "dashscope_flash").strip()
    if provider != "dashscope_flash":
        # 实时流式与文件转写通道没有在本流水线中实现，这里明确拒绝而不是静默降级。
        raise ConfigError(
            f"本流水线目前只支持 STT_PROVIDER=dashscope_flash，当前为 {provider!r}"
        )
    return Settings(
        stt_base_url=_required("STT_BASE_URL"),
        stt_model=_required("STT_MODEL"),
        stt_api_key=_required("STT_API_KEY"),
        llm_base_url=_required("LLM_BASE_URL"),
        llm_model=_required("LLM_MODEL"),
        llm_api_key=_required("LLM_API_KEY"),
        llm_enable_thinking=_as_bool(os.getenv("LLM_ENABLE_THINKING")),
        audio_chunk_seconds=_optional_int("AUDIO_CHUNK_SECONDS", 240),
        llm_max_tokens=_optional_int("LLM_MAX_TOKENS", 8000),
    )


@dataclass(slots=True)
class VaultSpec:
    key: str
    name: str
    dir: str
    subject: str

    @property
    def source_dir(self) -> Path:
        return VAULT_ROOT / self.dir


# build-content.mjs 的 VAULTS 是站点侧的唯一事实来源，这里解析而不是另建一份注册表，
# 避免两边配置漂移。解析失败时给出明确提示，要求显式传参。
_VAULT_BLOCK = re.compile(
    r"\{\s*key:\s*'([^']+)',\s*name:\s*'([^']+)',\s*dir:\s*'([^']+)',\s*subject:\s*'([^']+)'",
    re.S,
)


def read_vault_registry() -> dict[str, VaultSpec]:
    if not BUILD_CONTENT.exists():
        raise ConfigError(f"找不到 {BUILD_CONTENT}")
    text = BUILD_CONTENT.read_text(encoding="utf-8")
    registry: dict[str, VaultSpec] = {}
    for match in _VAULT_BLOCK.finditer(text):
        key, name, directory, subject = match.groups()
        registry[key] = VaultSpec(key=key, name=name, dir=directory, subject=subject)
    if not registry:
        raise ConfigError(
            f"没能从 {BUILD_CONTENT.name} 的 VAULTS 中解析出任何知识库条目；"
            "请确认该数组仍是 `key / name / dir / subject` 的书写顺序"
        )
    return registry
