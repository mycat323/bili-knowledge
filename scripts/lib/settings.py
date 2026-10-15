"""流水线配置与路径。

配置来源是仓库根目录的 `.env`（不进版本库），密钥只从环境变量读取，
不写入任何生成的知识库文件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SITE_ROOT = SCRIPTS_DIR.parent  # bili-knowledge/
REPO_ROOT = SITE_ROOT.parent  # 存放「知识库/」的上级目录
VAULT_ROOT = REPO_ROOT / "知识库"
CACHE_ROOT = SITE_ROOT / ".cache" / "fetch"
ENV_FILE = SITE_ROOT / ".env"
VAULTS_FILE = SCRIPTS_DIR / "vaults.json"


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
    llm_disable_thinking: bool
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
        # 实测：开启思考时 completion 里有 65%~80% 是推理 token，而正文长度
        # 几乎没有变化（2850 vs 2886 字）。批量处理时默认关掉，省钱也更快。
        llm_disable_thinking=_as_bool(os.getenv("LLM_DISABLE_THINKING")) is not False,
        audio_chunk_seconds=_optional_int("AUDIO_CHUNK_SECONDS", 240),
        llm_max_tokens=_optional_int("LLM_MAX_TOKENS", 8000),
    )


@dataclass(slots=True)
class VaultSpec:
    key: str
    name: str
    dir: str
    subject: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    illustration: str = ""
    avatar: str = ""
    # 原子笔记的命名域：{"光学现象": ["透镜成像", ...], ...}。
    # 留空时由抓取流水线根据视频标题自动推导一份（见 fetch_vault.resolve_taxonomy）。
    atom_domains: dict[str, list[str]] = field(default_factory=dict)

    @property
    def source_dir(self) -> Path:
        return VAULT_ROOT / self.dir

    def to_dict(self) -> dict:
        data = {
            "key": self.key,
            "name": self.name,
            "dir": self.dir,
            "subject": self.subject,
            "description": self.description,
            "tags": list(self.tags),
            "illustration": self.illustration,
            "avatar": self.avatar,
        }
        if self.atom_domains:
            data["atomDomains"] = self.atom_domains
        return data


# 知识库注册表是站点侧与抓取侧共用的一份数据（build-content.mjs 也读这个文件），
# 所以新增 UP 主只需要往这里加一条，不需要改任何代码。
def read_vault_registry() -> dict[str, VaultSpec]:
    if not VAULTS_FILE.exists():
        raise ConfigError(f"找不到知识库注册表 {VAULTS_FILE}")
    try:
        raw = json.loads(VAULTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{VAULTS_FILE.name} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{VAULTS_FILE.name} 应为一个非空数组")

    registry: dict[str, VaultSpec] = {}
    for index, item in enumerate(raw):
        try:
            spec = VaultSpec(
                key=item["key"],
                name=item["name"],
                dir=item["dir"],
                subject=item.get("subject", ""),
                description=item.get("description", ""),
                tags=list(item.get("tags") or []),
                illustration=item.get("illustration", ""),
                avatar=item.get("avatar", ""),
                atom_domains={
                    str(domain): [str(sub) for sub in (subs or [])]
                    for domain, subs in (item.get("atomDomains") or {}).items()
                },
            )
        except (TypeError, KeyError) as exc:
            raise ConfigError(f"{VAULTS_FILE.name} 第 {index + 1} 条缺少字段 {exc}") from exc
        registry[spec.key] = spec
    return registry


def register_vault(spec: VaultSpec) -> None:
    """把一个新知识库写进注册表，让 sync 能认出它。已存在同 key 时原地更新。"""
    raw = json.loads(VAULTS_FILE.read_text(encoding="utf-8"))
    entry = spec.to_dict()
    for index, item in enumerate(raw):
        if item.get("key") == spec.key:
            # atomDomains 是人工调过的分组口径，自动登记时不要把它覆盖掉。
            if not entry.get("atomDomains") and item.get("atomDomains"):
                entry["atomDomains"] = item["atomDomains"]
            raw[index] = {**item, **entry}
            break
    else:
        raw.append(entry)
    VAULTS_FILE.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
