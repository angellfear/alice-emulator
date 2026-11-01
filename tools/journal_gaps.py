"""Журнал трафика → недостающие обработчики и сценарии (TASK-072).

На вход — путь к КОПИИ журнала (traffic.sqlite3 с -wal/-shm, рабочий журнал только
читается из копии). На выход — отчёт Markdown: разделы HTTP (кто отвечал, unhandled,
счётчики, образец ответа Яндекса без секретов), AAPI клиент→сервер (есть ли обработчик
в реестре), AAPI сервер→клиент (что Яндекс шлёт в ответ на какие входящие, по
ref_message_id — и шлёт ли то же эмулятор), фразы ASR и ответы Алисы (что закрыто
сценариями библиотеки). Разбор тел — тем же декодером, что журнал в UI (zstd +
protobuf AAPI). Секреты в отчёт не выводятся: query адресов обрезается, значения
полей с именами token/auth/sign/uuid/device_id/oauth маскируются, идентификаторы
устройств показываются первыми четырьмя символами.

Запуск: python tools/journal_gaps.py <копия-журнала> [--output reports/journal-gaps-<дата>.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server_emulator.schema import load_type  # noqa: E402
from server_emulator.runtime import ROOT as PACKAGE_ROOT  # noqa: E402

CLIENT = load_type(PACKAGE_ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TClientMessage")
SERVER = load_type(PACKAGE_ROOT / "schemas", "alice-api.pb", "NAlice.NAliceApi.TServerMessage")

SECRET_KEY = re.compile(r"token|auth|sign|uuid|device_id|oauth|cookie|session", re.I)
SECRET_VALUE = re.compile(r"[A-Za-z0-9]{20,}")  # длинные строки — токеноподобные
# Ответы, которые эмулятор отправляет колонке (сверка раздела «сервер → клиент»).
EMULATOR_SENDS = {"InputStartAck", "AsrResult", "AliceResponse", "TtsSpeak", "SpotterValidation",
                  "StreamControl", "SynchronizeStateResponse", "LogAck", "RequestStatAck", "Push",
                  "CancelRequest", "TtsGenerate", "DeferredAliceResponse"}


def mask(text, limit=160):
    """Строка для отчёта: длинные токеноподобные значения маскируются, query адресов — обрезается."""
    text = SECRET_VALUE.sub("***", str(text))
    return text[:limit] + ("…" if len(text) > limit else "")


def body_of(blob):
    import zstandard
    if not blob:
        return b""
    try:
        return zstandard.decompress(blob)
    except Exception:
        return blob


def ws_event(blob, message_type):
    """(имя события, ref_message_id, объект) кадра AAPI или (None, '', None)."""
    data = body_of(blob)
    if not data.startswith(b"AAPI"):
        return None, "", None
    try:
        message = message_type.FromString(data[4:])
    except Exception:
        return None, "", None
    if message.WhichOneof("Message") == "Event":
        return message.Event.WhichOneof("Event"), str(message.Event.Header.RefMessageId or ""), message
    return message.WhichOneof("Message") or "", "", message


def http_rows(db):
    """HTTP-запросы, сгруппированные по (хост, метод, путь без query). Метод, потерянный
    журналом (NULL у части записей), восстанавливается из ключа события."""
    groups = defaultdict(list)
    for row in db.execute("SELECT id, host, method, path, status, source, unhandled, key,"
                          " response_headers, response_body, request_headers FROM exchanges WHERE kind='http'"):
        path = (row["path"] or "").split("?", 1)[0] or "/"
        method = (row["method"] or (row["key"] or "").split(" ")[0] or "?").upper()
        groups[(row["host"] or "", method, path)].append(row)
    return groups


def sample_answer(row):
    """Образец ответа для отчёта: статус, тип, размер и первые поля JSON без секретов."""
    body = body_of(row["response_body"])
    headers = json.loads(row["response_headers"] or "[]")
    ctype = next((v for k, v in headers if str(k).lower() == "content-type"), "")
    head = f"{row['status']} {ctype} {len(body)}Б".strip()
    if not body:
        return head
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return head
    if isinstance(parsed, dict):
        fields = ", ".join(f"{k}={mask(v, 24) if SECRET_KEY.search(str(k)) else mask(v, 24)}"
                           for k, v in list(parsed.items())[:6])
        return f"{head}; поля: {fields}"
    return head


def handler_known(trigger):
    """Есть ли обработчик события в реестре (профиль с включёнными AI-флагами:
   TextInput/VoiceInput отвечают только при aapi_model/aapi_voice)."""
    from server_emulator import aapi_handlers
    return aapi_handlers.has_handler(trigger, {"aapi_model": True, "aapi_voice": True})


def phrase_covered(text):
    """Закрыта ли фраза сценарием библиотеки (матчинг маршрутора, без runtime)."""
    from server_emulator.scenario_engine import library, router
    for scenario, reason in router.candidates(list(library.SCENARIOS.values()), text=text):
        return scenario.get("id", ""), reason
    return "", ""


def build_report(journal_path):
    """Отчёт Markdown по копии журнала: HTTP, AAPI клиент→сервер, сервер→клиент, фразы."""
    db = sqlite3.connect(str(journal_path))  # копия журнала: только чтение по договорённости
    db.row_factory = sqlite3.Row
    lines = [f"# Журнал → пробелы обработчиков ({time.strftime('%Y-%m-%d %H:%M')}, журнал: {Path(journal_path).name})", ""]

    # --- HTTP -------------------------------------------------------------------
    lines += ["## HTTP", "", "| Запрос | Источник | Без обработчика | Записей | Ответ (образец) |", "|---|---|---|---|---|"]
    for (host, method, path), rows in sorted(http_rows(db).items(), key=lambda item: (-len(item[1]), item[0])):
        sources = ",".join(sorted({r["source"] or "?" for r in rows}))
        unhandled = sum(1 for r in rows if r["unhandled"])
        yandex = next((r for r in rows if r["source"] == "upstream"), rows[0])
        lines.append(f"| {method} {host}{path} | {sources} | {'да' if unhandled else '—'} ({unhandled}) "
                     f"| {len(rows)} | {sample_answer(yandex)} |")
    lines.append("")

    # --- AAPI клиент → сервер ------------------------------------------------------
    client_counts, unhandled_events = Counter(), {}
    for row in db.execute("SELECT request_body FROM exchanges WHERE kind='ws' AND request_body IS NOT NULL"):
        name, _ref, _message = ws_event(row["request_body"], CLIENT)
        if name:
            client_counts[name] += 1
            if name not in unhandled_events and not handler_known(name):
                unhandled_events[name] = True
    lines += ["## AAPI: колонка → эмулятор", "", "| Событие | Раз | Обработчик в реестре |", "|---|---|---|"]
    for name, count in client_counts.most_common():
        lines.append(f"| {name} | {count} | {'есть' if not unhandled_events.get(name) else '**НЕТ**'} |")
    lines.append("")

    # --- AAPI сервер → клиент (ответы Яндекса по ref_message_id) ----------------------
    # Сервер отвечает RefMessageId = MessageId входящего события колонки.
    refs = {}
    for row in db.execute("SELECT id, request_body FROM exchanges WHERE kind='ws' AND request_body IS NOT NULL"):
        name, _ref, message = ws_event(row["request_body"], CLIENT)
        if name and message is not None:
            refs[str(message.Event.Header.MessageId or "")] = name
    answers, unanswered = Counter(), Counter()
    for row in db.execute("SELECT id, response_body FROM exchanges WHERE kind='ws' AND response_body IS NOT NULL"):
        name, ref, _message = ws_event(row["response_body"], SERVER)
        if name:
            answers[name] += 1
            unanswered[(refs.get(ref, "?"), name)] += 1
    lines += ["## AAPI: Яндекс → колонка (по ref_message_id)", "",
              "| Ответ Яндекса | Раз | На что отвечает | Эмулятор шлёт |", "|---|---|---|---|"]
    for name, count in answers.most_common():
        targets = ", ".join(f"{target or 'без ref'} ×{n}" for (target, answer), n in unanswered.items()
                            if answer == name) or "—"
        lines.append(f"| {name} | {count} | {targets} | {'да' if name in EMULATOR_SENDS else '**нет**'} |")
    lines.append("")

    # --- фразы и ответы Алисы ---------------------------------------------------------
    phrases = Counter()
    replies = Counter()
    asr_words = {}  # ref VoiceInput → слова последнего AsrResult (финальный — последний)
    for row in db.execute("SELECT request_body, response_body FROM exchanges WHERE kind='ws'"
                          " AND (request_body IS NOT NULL OR response_body IS NOT NULL)"):
        # AsrResult идёт сервером → колонке: тела обеих сторон, свой декодер для каждой
        for column, message_type in (("request_body", CLIENT), ("response_body", SERVER)):
            name, _ref, message = ws_event(row[column], message_type)
            if name == "AsrResult":
                ref = str(message.Event.Header.RefMessageId or "")
                words = " ".join(word.Value for recognition in message.Event.AsrResult.Recognition
                                 for word in recognition.Words if word.Value).strip()
                if words:
                    asr_words[ref] = words
        name, _ref, message = ws_event(row["response_body"], SERVER)
        if name == "AliceResponse":
            directive_names = ",".join(d.Name for d in message.Event.AliceResponse.Response.Directives)
            if directive_names:
                replies[directive_names] += 1
    lines += ["## Фразы и ответы Алисы", "", "| Фраза (ASR, финальная) | Закрыто сценарием |", "|---|---|"]
    for _ref, phrase in asr_words.items():
        scenario, reason = phrase_covered(phrase)
        lines.append(f"| {mask(phrase, 60)} | {count} | {scenario + ' (' + reason + ')' if scenario else '**нет**'} |")
    lines += ["", "| Директивы в ответах Алисы | Раз |", "|---|---|"]
    for directive, count in replies.most_common():
        lines.append(f"| {mask(directive, 80)} | {count} |")
    lines.append("")
    db.close()
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Журнал трафика → пробелы обработчиков (TASK-072)")
    parser.add_argument("journal", help="путь к КОПИИ traffic.sqlite3 (с -wal/-shm рядом)")
    parser.add_argument("--output", default=None, help="куда писать отчёт (по умолчанию reports/journal-gaps-<дата>.md)")
    args = parser.parse_args(argv)
    output = Path(args.output) if args.output else ROOT / "reports" / f"journal-gaps-{time.strftime('%Y-%m-%d')}.md"
    report = build_report(args.journal)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"Отчёт: {output} ({len(report.splitlines())} строк)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
