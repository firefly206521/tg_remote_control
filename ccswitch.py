"""联动 cc-switch：读取供应商列表，并把选中的供应商应用到 Claude Code / Codex。

cc-switch（GUI，无 CLI）把供应商存在 ~/.cc-switch/cc-switch.db（SQLite）：
- providers 表：id / app_type / name / settings_config(JSON) / category / is_current
- settings 表：common_config_claude（JSON 片段）、common_config_codex（TOML 片段）
- proxy_live_backup 表：非空表示该应用的 live 配置被本地代理接管，此时不能直接写

应用切换复刻 cc-switch 源码 services/provider/live.rs 的「非接管切换」语义：
- claude: ~/.claude/settings.json = deep_merge(供应商 settings_config, 通用配置 JSON)
  （通用配置覆盖冲突键；apiFormat 等内部字段不写入）
- codex:  ~/.codex/auth.json = 供应商 auth；
          ~/.codex/config.toml = deep_merge(供应商 config TOML, 通用配置 TOML)；
          供应商带 modelCatalog 时另写 ~/.codex/cc-switch-model-catalog.json

本模块对 cc-switch.db 只读，切换后尽力同步 ~/.cc-switch/settings.json 的
currentProvider* 键，让 GUI 显示一致；db 内的 is_current 不动（避免与运行中的
GUI 争写 SQLite）。
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import tomllib
from contextlib import closing
from pathlib import Path

import tomli_w

from config import PROJECT_ROOT

CC_DIR = Path.home() / ".cc-switch"
DB_PATH = CC_DIR / "cc-switch.db"
SETTINGS_PATH = CC_DIR / "settings.json"
CURRENT_KEY = {"claude": "currentProviderClaude", "codex": "currentProviderCodex"}
# cc-switch 写 live 前会剔除的内部字段（sanitize_claude_settings_for_live）
CLAUDE_INTERNAL_KEYS = {"api_format", "apiFormat", "openrouter_compat_mode", "openrouterCompatMode"}

BACKUP_ROOT = PROJECT_ROOT / "backups"

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CODEX_DIR = Path.home() / ".codex"
CODEX_AUTH = CODEX_DIR / "auth.json"
CODEX_CONFIG = CODEX_DIR / "config.toml"
CODEX_CATALOG = CODEX_DIR / "cc-switch-model-catalog.json"


class CcSwitchError(Exception):
    pass


def _open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise CcSwitchError(
            f"找不到 {DB_PATH}，请确认 cc-switch 已安装并至少运行过一次"
        )
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _load_common_config(agent: str):
    """通用配置片段：claude 返回 dict，codex 返回解析后的 TOML dict；无则空 dict。"""
    key = {"claude": "common_config_claude", "codex": "common_config_codex"}[agent]
    try:
        with closing(_open_db()) as con:
            row = con.execute("select value from settings where key=?", (key,)).fetchone()
    except CcSwitchError:
        raise
    except sqlite3.Error:
        return {}
    if not row or not row[0].strip():
        return {}
    if agent == "claude":
        return json.loads(row[0])
    return tomllib.loads(row[0])


def current_provider_id(agent: str) -> str:
    try:
        data = json.loads(SETTINGS_PATH.read_text("utf-8"))
        return data.get(CURRENT_KEY[agent]) or ""
    except (OSError, json.JSONDecodeError, KeyError):
        return ""


def list_providers(agent: str) -> list[dict]:
    """返回某 agent 的供应商列表（顺序与 GUI 列表一致），供编号选择。"""
    with closing(_open_db()) as con:
        rows = con.execute(
            "select id, name, category, is_current, settings_config, meta "
            "from providers where app_type=? order by coalesce(sort_index, rowid), rowid",
            (agent,),
        ).fetchall()
    result = []
    for pid, name, category, is_current, cfg_text, meta_text in rows:
        try:
            cfg = json.loads(cfg_text)
        except json.JSONDecodeError:
            cfg = {}
        try:
            meta = json.loads(meta_text or "{}")
        except json.JSONDecodeError:
            meta = {}
        base_url = ""
        if agent == "claude":
            base_url = (cfg.get("env") or {}).get("ANTHROPIC_BASE_URL", "")
            if not (cfg.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN"):
                base_url = base_url or "官方登录"
        else:
            base_url = _codex_base_url(cfg)
        result.append({
            "id": pid,
            "name": name,
            "category": category or "",
            "is_current": bool(is_current),
            "base_url": base_url,
            "config": cfg,
            "meta": meta,
        })
    return result


def _codex_base_url(cfg: dict) -> str:
    auth = cfg.get("auth") or {}
    if auth.get("auth_mode") == "chatgpt":
        return "官方登录（ChatGPT）"
    text = cfg.get("config") or ""
    m = re.search(r'base_url\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def render_list(agent: str) -> str:
    try:
        providers = list_providers(agent)
    except (CcSwitchError, sqlite3.Error) as exc:
        return f"❌ 读取 cc-switch 供应商失败：{exc}"
    if not providers:
        return f"cc-switch 里还没有 {agent} 的供应商，先在 GUI 中添加"
    current_id = current_provider_id(agent)
    lines = [f"🔌 cc-switch 供应商列表（{agent}）"]
    for idx, p in enumerate(providers, 1):
        mark = "👉" if (p["id"] == current_id if current_id else p["is_current"]) else "  "
        tag = "｜官方" if p["category"] == "official" else ""
        unavailable = _provider_unavailable_reason(agent, p)
        suffix = "｜需在 GUI 中切换" if unavailable else ""
        lines.append(f"{mark}{idx}. {p['name']}{tag}｜{p['base_url'] or '—'}{suffix}")
    lines.append("用法：/api 编号 切换并重启调度台")
    return "\n".join(lines)


def _deep_merge(target: dict, source: dict) -> dict:
    """source 覆盖 target 的冲突叶子键（对应 cc-switch json_deep_merge 的方向）。"""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _backup_live(agent: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    dest = BACKUP_ROOT / f"ccswitch-{agent}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    files = (
        [(SETTINGS_PATH, "cc-switch-settings.json"), (CLAUDE_SETTINGS, "claude-settings.json")]
        if agent == "claude"
        else [
            (SETTINGS_PATH, "cc-switch-settings.json"),
            (CODEX_AUTH, "codex-auth.json"),
            (CODEX_CONFIG, "codex-config.toml"),
            (CODEX_CATALOG, "codex-model-catalog.json"),
        ]
    )
    for f, backup_name in files:
        if f.exists():
            shutil.copy2(f, dest / backup_name)
    return dest


def _provider_unavailable_reason(agent: str, provider: dict) -> str:
    """仅支持可安全复制的 API-key 配置；官方登录由 GUI 自己管理凭据生命周期。"""
    cfg = provider["config"]
    if provider["category"] == "official":
        return "官方登录凭据需由 cc-switch GUI 管理"
    if agent == "claude":
        env = cfg.get("env") if isinstance(cfg, dict) else None
        if not isinstance(env, dict) or not (
            env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        ):
            return "缺少 Claude API 凭据"
        return ""
    auth = cfg.get("auth") if isinstance(cfg, dict) else None
    if not isinstance(auth, dict) or auth.get("auth_mode") == "chatgpt":
        return "Codex 官方登录需由 cc-switch GUI 管理"
    if not auth.get("OPENAI_API_KEY"):
        return "缺少 Codex API 凭据"
    if not isinstance(cfg.get("config"), str) or not cfg["config"].strip():
        return "缺少 Codex config.toml 配置"
    return ""


def _proxy_takeover_active(agent: str) -> bool:
    try:
        with closing(_open_db()) as con:
            return con.execute(
                "select 1 from proxy_live_backup where app_type=?", (agent,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def preflight_switch(agent: str, index: int) -> str:
    """只读预检。返回空串表示可以切换，否则返回可直接展示给用户的错误。"""
    if agent not in CURRENT_KEY:
        return f"❌ 不支持的 agent：{agent}"
    try:
        providers = list_providers(agent)
    except (CcSwitchError, sqlite3.Error) as exc:
        return f"❌ 读取 cc-switch 供应商失败：{exc}"
    if not 1 <= index <= len(providers):
        return f"❌ 编号超出范围（1-{len(providers)}），/api 查看列表"
    p = providers[index - 1]
    reason = _provider_unavailable_reason(agent, p)
    if reason:
        return f"❌ 「{p['name']}」{reason}，请在 cc-switch GUI 中切换"
    if _proxy_takeover_active(agent):
        return (
            f"❌ {agent} 的 live 配置正被 cc-switch 本地代理接管，"
            "请在 cc-switch GUI 中关闭代理接管后再用 /api 切换"
        )
    try:
        if agent == "claude":
            _build_claude_settings(p)
        else:
            _build_codex_settings(p)
    except (CcSwitchError, json.JSONDecodeError, tomllib.TOMLDecodeError, TypeError) as exc:
        return f"❌ 供应商「{p['name']}」配置无效：{exc}"
    return ""


def switch_provider(agent: str, index: int) -> str:
    """把编号对应的供应商写入 CLI 配置文件，返回给用户的回复文本。"""
    error = preflight_switch(agent, index)
    if error:
        return error
    try:
        p = list_providers(agent)[index - 1]
        backup_dir = _backup_live(agent)
        if agent == "claude":
            _apply_claude(p)
        else:
            _apply_codex(p)
        _sync_current_marker(agent, p["id"])
    except (CcSwitchError, OSError, sqlite3.Error, json.JSONDecodeError, tomllib.TOMLDecodeError, TypeError) as exc:
        return f"❌ 切换失败：{exc}"

    return (
        f"✅ {agent} API 已切换为「{p['name']}」｜{p['base_url'] or '—'}\n"
        f"💾 原配置已备份到 {backup_dir.name}"
    )


def _uses_common_config(provider: dict) -> bool:
    return bool((provider.get("meta") or {}).get("commonConfigEnabled"))


def _build_claude_settings(provider: dict) -> dict:
    merged = json.loads(json.dumps(provider["config"]))
    if _uses_common_config(provider):
        merged = _deep_merge(merged, _load_common_config("claude"))
    for key in CLAUDE_INTERNAL_KEYS:
        merged.pop(key, None)
    return merged


def _apply_claude(provider: dict) -> None:
    merged = _build_claude_settings(provider)
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")


def _build_codex_settings(provider: dict) -> tuple[dict, dict, object | None]:
    provider_cfg = provider["config"]
    auth = provider_cfg.get("auth")
    if auth is None:
        raise CcSwitchError("该供应商缺少 auth 配置，无法应用（请在 cc-switch GUI 中切换）")
    config_text = provider_cfg.get("config") or ""
    merged = tomllib.loads(config_text) if config_text.strip() else {}
    if _uses_common_config(provider):
        merged = _deep_merge(merged, _load_common_config("codex"))
    return auth, merged, provider_cfg.get("modelCatalog")


def _apply_codex(provider: dict) -> None:
    auth, merged, catalog = _build_codex_settings(provider)
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_AUTH.write_text(json.dumps(auth, ensure_ascii=False, indent=2), "utf-8")
    CODEX_CONFIG.write_text(tomli_w.dumps(merged), "utf-8")
    if catalog is not None:
        CODEX_CATALOG.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), "utf-8"
        )


def _sync_current_marker(agent: str, provider_id: str) -> None:
    """尽力同步 ~/.cc-switch/settings.json 的当前供应商标记，失败不影响切换。"""
    try:
        data = json.loads(SETTINGS_PATH.read_text("utf-8"))
        data[CURRENT_KEY[agent]] = provider_id
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except (OSError, json.JSONDecodeError, KeyError):
        pass
