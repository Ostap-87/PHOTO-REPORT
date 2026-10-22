from __future__ import annotations
# -*- coding: utf-8 -*-
"""Анализ ТЗ ИИ (Claude): «Реестр подтверждений» и «Требуют уточнения» по заданию пользователя.

Задание — config/registry_prompt.md (текст пользователя, без изменений). Приложение добавляет к нему
только техническую часть: пример структуры со скриншота и формат ответа (JSON вместо файла Excel —
Excel собирает само приложение). Анализ идёт в два шага, как требует раздел 8 задания:
черновой реестр, затем повторная сверка всего ТЗ с реестром и исправленный итог.

Ключ API — из настроек приложения (app/secrets_store.py) или переменной ANTHROPIC_API_KEY.
"""
import json
import os
from typing import Callable

from app import secrets_store
from app.store import APP_ROOT

MODEL = "claude-opus-5-5"
PROMPT_PATH = os.path.join(APP_ROOT, "config", "registry_prompt.md")
MAX_CHARS = 1_500_000              # ~ предел контекста модели с запасом; больше — просим разделить ТЗ

ROW = {
    "type": "object",
    "properties": {
        "num": {"type": "string", "description": "A. Пункт ТЗ — точный номер(а) из документа"},
        "section": {"type": "string", "description": "B. Раздел"},
        "name": {"type": "string", "description": "C. Наименование"},
        "description": {"type": "string", "description": "D. Описание"},
        "required": {"type": "string", "description": "E. Что конкретно должно быть приложено, в каком виде"},
        "comment": {"type": "string", "description": "F. Комментарий (пусто, если замечаний нет)"},
    },
    "required": ["num", "section", "name", "description", "required", "comment"],
    "additionalProperties": False,
}
CLARIFY = {
    "type": "object",
    "properties": {
        "num": {"type": "string"}, "section": {"type": "string"}, "name": {"type": "string"},
        "description": {"type": "string"},
        "reason": {"type": "string", "description": "Конкретная причина неопределённости"},
    },
    "required": ["num", "section", "name", "description", "reason"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": ROW, "description": "Лист «Реестр подтверждений» по ходу ТЗ"},
        "clarify": {"type": "array", "items": CLARIFY, "description": "Лист «Требуют уточнения»"},
        "summary": {
            "type": "object",
            "properties": {
                "analyzed": {"type": "string", "description": "Сколько пунктов и подпунктов ТЗ проанализировано"},
                "sections": {"type": "array", "items": {"type": "string"}, "description": "Какие разделы ТЗ охвачены"},
                "issues": {"type": "string", "description": "Обнаруженные противоречия и неоднозначности (кратко)"},
            },
            "required": ["analyzed", "sections", "issues"],
            "additionalProperties": False,
        },
    },
    "required": ["rows", "clarify", "summary"],
    "additionalProperties": False,
}

APP_NOTE = """
---
Технические условия приложения «ФотоОтчёт» (не меняют задание выше):
1. Приложенный скриншот-пример передаётся текстом. Пример структуры строк (не источник требований):
   «6.2.3.1. | Площадка | Оснащение Главной сцены | ноутбук с установленным необходимым программным обеспечением … – 1 (одна) шт.; | Фото одного ноутбука | (пусто)»;
   «6.2.4 | Информационное сопровождение | Рассылка по собственной базе СМИ | Исполнитель должен осуществить e-mail-рассылку приглашений по собственной базе СМИ на 3000 адресов | Скриншот рассылки | (пусто)».
2. Файл Excel формирует приложение. Верни содержимое двух листов строго в формате JSON по заданной схеме:
   rows — лист «Реестр подтверждений» (столбцы A–F: num, section, name, description, required, comment);
   clarify — лист «Требуют уточнения»; summary — сведения для сопроводительного сообщения (раздел 9 задания).
3. Текст ТЗ получен из файла программой: номера автоматической нумерации Word уже подставлены в начало
   абзацев. Таблицы переданы построчно, ячейки разделены « | ».
"""

VERIFY = (
    "Выполни раздел 8 задания «Обязательная повторная проверка»: сверь всё ТЗ, пункт за пунктом, с "
    "составленным реестром. Исправь пропуски, неверные номера, искажения описания, придуманные материалы, "
    "лишние требования к количеству или формату и случайные дубли; проверь лист «Требуют уточнения». "
    "Верни полный итоговый результат (не только изменения) в том же формате JSON."
)


class AIError(RuntimeError):
    """Понятная пользователю ошибка анализа (по-русски)."""


def load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        text = fh.read()
    if text.startswith("<!--"):                      # служебный комментарий в начале файла — не модели
        text = text.split("-->", 1)[-1]
    return text.strip() + "\n" + APP_NOTE


def make_client(api_key: str | None = None):
    import anthropic
    key = api_key or secrets_store.get_api_key()
    return anthropic.Anthropic(api_key=key, max_retries=3) if key else anthropic.Anthropic(max_retries=3)


def _ask(client, system: str, messages: list, effort: str):
    """Один шаг: потоковый запрос (длинный вход и выход), структурированный ответ, запасная модель
    при отказе. -> (message, data)."""
    import anthropic
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AIError("Ключ API не подошёл. Проверьте его в настройках (ключ из console.anthropic.com).") from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIError("У ключа API нет доступа к модели. Проверьте настройки аккаунта Anthropic.") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Слишком много запросов к ИИ. Подождите минуту и повторите.") from exc
    except anthropic.BadRequestError as exc:
        raise AIError(f"ИИ отклонил запрос: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Сервис ИИ временно недоступен (код {exc.status_code}). Повторите позже.") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("Нет связи с сервисом ИИ (api.anthropic.com). Проверьте интернет.") from exc
    if msg.stop_reason == "refusal":
        raise AIError("ИИ отказался обрабатывать этот документ.")
    if msg.stop_reason == "max_tokens":
        raise AIError("ТЗ слишком большое для одного разбора: ответ не поместился. Разделите ТЗ на части.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIError("ИИ вернул ответ не в том формате. Повторите анализ.") from exc
    return msg, data


def _echo(content: list) -> list:
    """Ответ модели для следующего шага — без изменений; если сработала запасная модель, служебные
    блоки до последней точки переключения не возвращаются (правило API для fallback)."""
    last = max((i for i, b in enumerate(content) if getattr(b, "type", "") == "fallback"), default=-1)
    if last < 0:
        return content
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(content) if i > last or getattr(b, "type", "") not in drop]


def _clean(data: dict) -> dict:
    def s(v):
        return str(v or "").strip()
    rows = [{k: s(r.get(k)) for k in ROW["properties"]} for r in data.get("rows") or []]
    rows = [r for r in rows if r["num"] or r["name"] or r["required"]]
    clarify = [{k: s(r.get(k)) for k in CLARIFY["properties"]} for r in data.get("clarify") or []]
    sm = data.get("summary") or {}
    summary = {"analyzed": s(sm.get("analyzed")), "sections": [s(x) for x in sm.get("sections") or [] if s(x)],
               "issues": s(sm.get("issues"))}
    return {"rows": rows, "clarify": clarify, "summary": summary}


def analyze(full_text: str, title: str = "", api_key: str | None = None, client=None,
            progress: Callable[[str], None] | None = None, verify: bool = True, effort: str = "high") -> dict:
    """ТЗ (полный текст с номерами) -> {"rows", "clarify", "summary", "model", "usage"}."""
    text = (full_text or "").strip()
    if not text:
        raise AIError("В ТЗ нет текста для анализа (возможно, это скан без текстового слоя).")
    if len(text) > MAX_CHARS:
        raise AIError("ТЗ слишком большое для одного разбора. Разделите его на части.")
    client = client or make_client(api_key)
    system = load_prompt()
    user = f"Техническое задание{(' «' + title + '»') if title else ''}. Полный текст:\n\n{text}"
    messages = [{"role": "user", "content": user}]
    note = progress or (lambda _s: None)
    note("ИИ читает ТЗ и составляет реестр…")
    msg, data = _ask(client, system, messages, effort)
    usage = [msg.usage]
    if verify:
        note("Повторная сверка реестра со всем ТЗ…")
        messages = messages + [{"role": "assistant", "content": _echo(msg.content)},
                               {"role": "user", "content": VERIFY}]
        msg2, data2 = _ask(client, system, messages, effort)
        usage.append(msg2.usage)
        data = data2
    out = _clean(data)
    out["model"] = getattr(msg, "model", MODEL)
    out["usage"] = {"input_tokens": sum(int(getattr(u, "input_tokens", 0) or 0) for u in usage),
                    "output_tokens": sum(int(getattr(u, "output_tokens", 0) or 0) for u in usage)}
    return out


def test_key(api_key: str | None = None) -> str:
    """Короткая проверка ключа. -> модель, которая ответила."""
    import anthropic
    client = make_client(api_key)
    try:
        msg = client.messages.create(model=MODEL, max_tokens=512, output_config={"effort": "low"},
                                     messages=[{"role": "user", "content": "Ответь одним словом: готов"}])
    except anthropic.AuthenticationError as exc:
        raise AIError("Ключ API не подошёл.") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("Нет связи с api.anthropic.com. Проверьте интернет.") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Сервис ИИ ответил ошибкой {exc.status_code}: {exc.message}") from exc
    return msg.model
