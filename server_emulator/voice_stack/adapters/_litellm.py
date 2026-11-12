"""Общий транспорт LiteLLM для встроенных LLM-адаптеров (openai, anthropic).

litellm.acompletion — выбранный владельцем транспорт. Здесь он приглушается от
телеметрии (панель — локальный сервис, наружу ничего не отправляется), ошибки
провайдера приводятся к ValueError с HTTP-статусом, ответ модели — словарь в
формате OpenAI (как раньше на проводе конвейера). litellm импортируется лениво:
адаптеры без LLM-сервиса не должны платить за его загрузку.
"""

_prepared = False


def prepare():
    """Разовые настройки litellm: без телеметрии и стартового вывода."""
    global _prepared
    if _prepared:
        return
    import litellm
    litellm.suppress_debug_info = True
    litellm.suppress_startup_output = True
    _prepared = True


def sampling_models(names):
    """Каталог litellm считает новые Claude-модели не принимающими temperature —
    разрешить параметры семплирования явно (поля адаптера не меняются)."""
    prepare()
    import litellm
    litellm.register_model({name: {"supports_sampling_params": True} for name in names})


def provider_error(kind, exc):
    """Исключение litellm → ValueError с HTTP-статусом и деталями провайдера."""
    status = getattr(exc, "status_code", None)
    detail = ""
    for pattern in ("'message': '([^']+)'", '"message": "([^"]+)"'):
        import re
        found = re.search(pattern, str(exc))
        if found:
            detail = found.group(1)
            break
    if status:
        raise ValueError(f"{kind}: HTTP {status} от провайдера {detail}".strip()) from exc
    raise ValueError(f"{kind}: {str(exc)[:160]}") from exc


def openai_response(response):
    """ModelResponse LiteLLM → словарь OpenAI (choices/usage) — формат конвейера."""
    data = response.model_dump()
    for message in (choice.get("message") or {} for choice in data.get("choices") or []):
        message.pop("function_call", None)
        message.pop("provider_specific_fields", None)
        for call in message.get("tool_calls") or []:
            call.pop("index", None)
    return data


def api_base(url, suffix):
    """Полный адрес запроса из поля URL → база для litellm (он добавляет suffix сам)."""
    base = (url or "").rstrip("/")
    return base[: -len(suffix)] if base.endswith(suffix) else base
