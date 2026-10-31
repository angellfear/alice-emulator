"""Заглушка модели: на любой вопрос — заданный текст, сеть не вызывается."""

TITLE = "Заглушка (фиксированный ответ)"
DESCRIPTION = "Модель не вызывается: на любой вопрос звучит заданный текст."
SERVICES = {
    "llm": {
        "fields": [{"key": "stub_text", "label": "Текст ответа", "type": "textarea"}],
        "defaults": {"stub_text": "Я пока не подключена к модели."},
    },
}


async def complete(runtime, config, payload):
    # runtime.chat отвечает по stub_text раньше вызова адаптера; здесь — для прямых вызовов
    return {"choices": [{"message": {"role": "assistant", "content": config.get("stub_text", "")}}], "usage": {}}
