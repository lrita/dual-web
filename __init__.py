"""dual-web：把多个 Hermes web search provider 的结果聚合为一次搜索。

默认同时调用 ``deepseek`` 与 ``firecrawl``，按 URL 去重后轮流合并。子 provider
列表可通过 ``plugins.entries.dual-web.settings.providers`` 配置，例如::

    providers: [deepseek, firecrawl]

设计边界：

- 只实现 ``search``，不实现正文抓取；``web.extract_backend`` 仍由用户单独配置。
- 每次调用按配置顺序逐个调用子 provider；单个子 provider 失败不会中断整次搜索。
- 结果按 URL 字符串去重，并在每条命中上标注 ``vendor`` 字段。

兼容性提示：本插件依赖 Hermes 的内部模块
``agent.web_search_provider.WebSearchProvider`` 与
``agent.web_search_registry.get_provider``。这些路径不是稳定的公开 API，
Hermes 升级后若发生变化，日志里会出现 ``[dual-web][HERMES-API]`` 前缀的错误。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("plugins.dual_web")

_DEFAULT_PROVIDERS: tuple[str, ...] = ("deepseek", "firecrawl")
_QUERY_LOG_LIMIT = 500
# 同一个配置/API 问题只完整告警一次；Hermes 的 check_fn 可能每个 turn 都调用 is_available()。
_WARNED_ONCE: set[str] = set()
_API_HINT = (
    "本插件依赖 Hermes 内部 API：agent.web_search_registry.get_provider(name) 返回一个带 "
    "supports_search() 与 search(query, limit) 的 provider。若此错误在 Hermes 升级后出现，"
    "说明内部 API 已变化；请对照当前版本的 agent/web_search_registry.py 与 "
    "agent/web_search_provider.py 修复本插件，或在确认兼容前固定 hermes-agent 版本。"
)

try:
    from agent.web_search_provider import WebSearchProvider
    from agent.web_search_registry import get_provider as _hermes_get_provider

    _IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # 只有 Hermes 内部 API 变化或安装不完整时才会进入
    _IMPORT_ERROR = _exc
    WebSearchProvider = object  # type: ignore[assignment,misc]
    _hermes_get_provider = None  # type: ignore[assignment]


def _clip_query(query: Any) -> str:
    """日志里保留搜索词，但截断超长输入，避免单条日志失控。"""
    text = str(query)
    return text if len(text) <= _QUERY_LOG_LIMIT else text[: _QUERY_LOG_LIMIT - 3] + "..."


def _parse_provider_names(value: Any) -> tuple[str, ...]:
    """把配置值解析成去重后的 provider 名列表；支持列表、元组、逗号或空格分隔的字符串。"""
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,\s]+", value) if part]
    elif isinstance(value, (list, tuple)):
        raw = [str(part) for part in value]
    else:
        raw = []
    names: list[str] = []
    for part in raw:
        name = part.strip().lower()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _hermes_version() -> str:
    """尽力读取当前 Hermes 版本，只用于日志定位；失败不影响功能。"""
    try:
        from hermes_cli.version_info import get_version_info

        return str(get_version_info().base_version)
    except Exception:
        return "unknown"


def _warn_once(key: str, message: str, *args: Any, exc_info: Any = None) -> None:
    """同一个问题只完整告警一次；重复出现降为 DEBUG，避免轮询路径刷屏。"""
    if key in _WARNED_ONCE:
        logger.debug(message, *args)
        return
    _WARNED_ONCE.add(key)
    logger.warning(message, *args, exc_info=exc_info)


def _log_api_error(
    message: str, *args: Any, exc_info: Any = None, once_key: Optional[str] = None
) -> None:
    """Hermes 内部 API 相关错误的统一出口，便于按 ``[dual-web][HERMES-API]`` 过滤。"""
    if once_key and once_key in _WARNED_ONCE:
        logger.debug("[dual-web][HERMES-API] " + message + " " + _API_HINT, *args)
        return
    if once_key:
        _WARNED_ONCE.add(once_key)
    logger.error("[dual-web][HERMES-API] " + message + " " + _API_HINT, *args, exc_info=exc_info)


class DualWebProvider(WebSearchProvider):
    """聚合多个 web search provider 的 ``dual-web`` 后端。"""

    def __init__(self, get_setting: Optional[Callable[..., Any]] = None) -> None:
        self._get_setting = get_setting

    @property
    def name(self) -> str:
        return "dual-web"

    @property
    def display_name(self) -> str:
        return "dual-web"

    def _configured_providers(self) -> tuple[str, ...]:
        """读取 ``plugins.entries.dual-web.settings.providers``，每次调用都读，改配置无需重启。"""
        if self._get_setting is None:
            return _DEFAULT_PROVIDERS
        try:
            value = self._get_setting("providers", _DEFAULT_PROVIDERS)
        except Exception as exc:
            _warn_once(
                "config:read",
                "[dual-web] 读取 settings.providers 失败，回退默认值 %s：%s: %s",
                list(_DEFAULT_PROVIDERS),
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return _DEFAULT_PROVIDERS
        names = _parse_provider_names(value)
        if self.name in names:
            _warn_once(
                "config:self",
                "[dual-web] settings.providers 不能包含聚合器自身 %r，已忽略该项",
                self.name,
            )
            names = tuple(item for item in names if item != self.name)
        if not names:
            _warn_once(
                "config:empty",
                "[dual-web] settings.providers 为空或无效，回退默认值 %s",
                list(_DEFAULT_PROVIDERS),
            )
            return _DEFAULT_PROVIDERS
        return names

    def _resolve(self, name: str) -> tuple[Optional[Any], Optional[str]]:
        """解析一个子 provider；返回 ``(provider, error)``。所有内部 API 异常都会记录关键日志。"""
        if _hermes_get_provider is None:
            _log_api_error(
                "无法导入 agent.web_search_registry.get_provider：%s: %s",
                type(_IMPORT_ERROR).__name__ if _IMPORT_ERROR else "unknown",
                _IMPORT_ERROR,
                exc_info=_IMPORT_ERROR,
                once_key="api:import",
            )
            return None, f"get_provider unavailable: {_IMPORT_ERROR}"

        try:
            provider = _hermes_get_provider(name)
        except Exception as exc:
            _log_api_error(
                "get_provider(%r) 抛异常 %s: %s",
                name,
                type(exc).__name__,
                exc,
                exc_info=True,
                once_key=f"api:get_provider:{name}",
            )
            return None, f"{type(exc).__name__}: {exc}"

        if provider is None:
            _warn_once(
                f"missing:{name}",
                "[dual-web] provider=%s 未注册：可能未安装、未启用（hermes plugins list），"
                "或 settings.providers 里的名字写错。若确认已启用仍然出现，请检查 "
                "agent.web_search_registry.get_provider 是否仍可用。",
                name,
            )
            return None, f"{name} is not registered"
        _WARNED_ONCE.discard(f"missing:{name}")

        supports_search = getattr(provider, "supports_search", None)
        if not callable(supports_search):
            _log_api_error(
                "provider=%s 缺少 supports_search()", name, once_key=f"api:supports_search:{name}"
            )
            return None, f"{name} has no supports_search()"
        try:
            if not supports_search():
                logger.warning("[dual-web] provider=%s 不支持 search，已跳过", name)
                return None, f"{name} does not support search"
        except Exception as exc:
            _log_api_error(
                "provider=%s supports_search() 抛异常 %s: %s",
                name,
                type(exc).__name__,
                exc,
                exc_info=True,
                once_key=f"api:supports_search_call:{name}",
            )
            return None, f"{name} supports_search() failed: {exc}"

        return provider, None

    def _provider_ready(self, provider: Any) -> bool:
        """与 Hermes 自身的 readiness 判定保持一致：keyed 可用或 keyless 可用。"""
        name = getattr(provider, "name", provider)
        is_available = getattr(provider, "is_available", None)
        if callable(is_available):
            try:
                if is_available():
                    return True
            except Exception as exc:
                _warn_once(
                    f"ready:{name}",
                    "[dual-web] provider=%s is_available() 抛异常：%s: %s",
                    name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return False
        else:
            _log_api_error(
                "provider=%s 缺少 is_available()", name, once_key=f"api:is_available:{name}"
            )
            return False

        is_keyless_available = getattr(provider, "is_keyless_available", None)
        if not callable(is_keyless_available):
            return False
        try:
            return bool(is_keyless_available())
        except Exception as exc:
            logger.debug(
                "[dual-web] provider=%s is_keyless_available() 抛异常：%s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            return False

    def is_available(self) -> bool:
        """至少一个子 provider 可用就算可用；两家都缺时由 search() 返回明确错误。"""
        return any(
            provider is not None and self._provider_ready(provider)
            for provider, _error in (self._resolve(name) for name in self._configured_providers())
        )

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "dual-web backend",
            "tag": "同时搜索 DeepSeek 与 Firecrawl，按 URL 去重后合并；只支持搜索，web_extract 请配置为 firecrawl。",
            "env_vars": [],
        }

    def _search_one(
        self, name: str, query: str, safe_query: str, limit: int
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """调用单个子 provider；返回 ``(结果列表, 错误信息)``。"""
        provider, resolve_error = self._resolve(name)
        if provider is None:
            return [], f"{name}: {resolve_error}"

        call_started = time.monotonic()
        try:
            response = provider.search(query, limit)
        except TypeError as exc:
            elapsed_ms = (time.monotonic() - call_started) * 1000
            _log_api_error(
                "provider=%s search(query, limit) 抛 TypeError：%s: %s（耗时 %.1f ms）",
                name,
                type(exc).__name__,
                exc,
                elapsed_ms,
                exc_info=True,
                once_key=f"api:search_type:{name}",
            )
            return [], f"{name}: {type(exc).__name__}: {exc}"
        except Exception as exc:
            elapsed_ms = (time.monotonic() - call_started) * 1000
            logger.warning(
                "[dual-web] provider=%s search 失败 query=%r 耗时=%.1f ms error=%s: %s",
                name,
                safe_query,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            return [], f"{name}: {type(exc).__name__}: {exc}"

        elapsed_ms = (time.monotonic() - call_started) * 1000
        if not isinstance(response, dict):
            _log_api_error(
                "provider=%s search() 返回 %s，预期 dict {'success': bool, 'data': {'web': [...]}}",
                name,
                type(response).__name__,
                once_key=f"api:response_type:{name}",
            )
            return [], f"{name}: unexpected response type {type(response).__name__}"

        if not response.get("success"):
            message = response.get("error", "search failed")
            logger.warning(
                "[dual-web] provider=%s search 未成功 query=%r 耗时=%.1f ms error=%s",
                name,
                safe_query,
                elapsed_ms,
                message,
            )
            return [], f"{name}: {message}"

        data = response.get("data")
        rows = data.get("web") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            _log_api_error(
                "provider=%s search() 的 data.web 不是列表（实际 %s）",
                name,
                type(rows).__name__,
                once_key=f"api:data_web:{name}",
            )
            return [], f"{name}: malformed data.web"

        normalised = [row for row in rows if isinstance(row, dict)]
        if len(normalised) != len(rows):
            logger.warning(
                "[dual-web] provider=%s query=%r 丢弃 %d 条非 dict 结果（原始 %d 条）",
                name,
                safe_query,
                len(rows) - len(normalised),
                len(rows),
            )
        logger.info(
            "[dual-web] provider=%s query=%r results=%d elapsed_ms=%.1f",
            name,
            safe_query,
            len(normalised),
            elapsed_ms,
        )
        return normalised, None

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """依次调用子 provider，记录每次调用的搜索词与返回条数，再合并去重。"""
        started = time.monotonic()
        providers = self._configured_providers()
        safe_query = _clip_query(query)
        logger.info(
            "[dual-web] search start query=%r limit=%d providers=%s",
            safe_query,
            limit,
            list(providers),
        )

        hits: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        counts: dict[str, int] = {}
        for name in providers:
            rows, error = self._search_one(name, query, safe_query, limit)
            hits[name] = rows
            counts[name] = len(rows)
            if error:
                errors.append(error)

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates = 0
        queues = [[(name, row) for row in hits.get(name, [])] for name in providers]
        while len(merged) < limit and any(queues):
            appended = False
            for queue in queues:
                # 每轮每个 provider 最多取一条新结果；重复 URL 直接跳过，不占用本轮名额。
                while queue:
                    vendor, row = queue.pop(0)
                    url = row.get("url")
                    if not isinstance(url, str) or not url:
                        continue
                    if url in seen:
                        duplicates += 1
                        continue
                    seen.add(url)
                    merged.append({**row, "vendor": vendor})
                    appended = True
                    break
                if len(merged) >= limit:
                    break
            if not appended:
                break

        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "[dual-web] search done query=%r counts=%s merged=%d duplicates=%d errors=%d elapsed_ms=%.1f",
            safe_query,
            counts,
            len(merged),
            duplicates,
            len(errors),
            elapsed_ms,
        )
        if not merged:
            message = "; ".join(errors) or "no results"
            logger.warning("[dual-web] search produced no results query=%r reason=%s", safe_query, message)
            return {"success": False, "error": f"dual-web search failed: {message}"}

        payload: dict[str, Any] = {"web": merged}
        if errors:
            payload["errors"] = errors
        return {"success": True, "data": payload}


def register(ctx) -> None:
    """插件入口：注册 ``dual-web`` web search provider。"""
    if _IMPORT_ERROR is not None:
        _log_api_error(
            "插件导入 Hermes 内部模块失败：%s: %s",
            type(_IMPORT_ERROR).__name__,
            _IMPORT_ERROR,
            exc_info=_IMPORT_ERROR,
            once_key="api:import",
        )
        raise RuntimeError(
            "dual-web 无法导入 Hermes 的 web provider API；"
            "请查看带 [dual-web][HERMES-API] 前缀的日志。"
        ) from _IMPORT_ERROR

    registrar = getattr(ctx, "register_web_search_provider", None)
    if not callable(registrar):
        _log_api_error(
            "PluginContext 缺少 register_web_search_provider()", once_key="api:registrar"
        )
        raise RuntimeError(
            "dual-web: PluginContext.register_web_search_provider 不存在；"
            "Hermes 插件 API 可能已变化。"
        )

    reader = getattr(ctx, "get_config", None)
    get_setting = (lambda key, default=None: reader(key, default)) if callable(reader) else None
    provider = DualWebProvider(get_setting)
    registrar(provider)

    names = provider._configured_providers()
    logger.info(
        "[dual-web] registered provider=%s hermes=%s providers=%s settings=%s",
        provider.name,
        _hermes_version(),
        list(names),
        "plugins.entries.dual-web.settings.providers",
    )
    for name in names:
        resolved, error = provider._resolve(name)
        if resolved is None:
            logger.warning("[dual-web] 注册时 provider=%s 不可用：%s", name, error)
        else:
            logger.info(
                "[dual-web] 注册时 provider=%s ready=%s",
                name,
                provider._provider_ready(resolved),
            )
