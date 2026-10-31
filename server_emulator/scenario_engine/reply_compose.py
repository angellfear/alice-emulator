"""Декларативный ответ сценария (compose.reply / compose.raw) — всё из JSON.

reply: карточка + реплика + директивы. Payload директивы:
  proto + fields — типизированный protobuf по дескриптору (proto_build), поля и
    значения (литералы/шаблоны {{…}}/bindings) — в JSON сценария;
  payload — google.protobuf.Struct (директивы со Struct-payload);
  payload_raw — base64, только с "debug": true (реплей перехвата при отладке).
envelope: dialog — каркас диалогового ответа облака (tts_play_placeholder,
  SequenceNumber, CardId добавляет диалоговый слой); service — тихий служебный
  ответ (ResponseId, пустые EffectiveSettings, Timings.SendingTime).
raw (debug): сообщение целиком из fields — отправляется как есть.

HasVoiceResponse = False: голосовой тракт поднимает флаг только когда аудио
реально синтезировано (как облако в капче).
"""
from __future__ import annotations

import base64
import uuid

from ..schema import load_type
from .bindings import expand
from .proto_build import build, encode


def compose_reply(spec, ctx, schemas_root):
    """compose.reply → TServerMessage AliceResponse по декларативному spec."""
    spec = spec or {}
    server_type = load_type(schemas_root, "alice-api.pb", "NAlice.NAliceApi.TServerMessage")
    message = server_type()
    response = message.Event.AliceResponse
    response.Header.RequestId = (ctx.get("input") or {}).get("request_id", "")
    text = expand(spec.get("text"), ctx)
    speech = expand(spec.get("speech"), ctx)
    text = "" if text is None else str(text)
    speech = "" if speech is None else str(speech)
    if not speech and spec.get("speech_parts"):  # части озвучки (кеш TTS) → одна фраза
        speech = " ".join(str(p) for p in expand(spec["speech_parts"], ctx) or [] if str(p or "").strip())
        speech = str(expand(spec.get("speech_prefix"), ctx) or "") + speech if speech else ""
    if not speech and text:
        speech = str(expand(spec.get("speech_prefix"), ctx) or "") + text
    if text and spec.get("card", True):
        response.Response.Cards.add(Type="simple_text", Text=text)
    if speech:
        response.VoiceResponse.OutputSpeech.Text = speech
    response.VoiceResponse.HasVoiceResponse = False
    for item in spec.get("directives") or []:
        if "when" in item:
            from .conditions import check
            if not check(item["when"], ctx):
                continue
        directive = response.Response.Directives.add()
        directive.Type = str(item.get("type") or "client_action")
        directive.Name = str(expand(item["name"], ctx))
        if item.get("analytics_type"):
            directive.AnalyticsType = str(expand(item["analytics_type"], ctx))
        if item.get("is_led_silent") is not None:
            directive.IsLedSilent = bool(item["is_led_silent"])
        if item.get("proto"):
            directive.PayloadRaw = encode(schemas_root, item["proto"], expand(item.get("fields") or {}, ctx),
                                          item.get("schema") or "centaur")
        if item.get("payload") is not None:
            directive.Payload.update(expand(item["payload"], ctx))  # Struct: вложенные dict/list
        if item.get("payload_raw"):
            directive.PayloadRaw = base64.b64decode(expand(item["payload_raw"], ctx))
    if spec.get("envelope") == "service":
        # Тихий служебный ответ облака (capture 2026-10-26): без голосовых добавок,
        # с пустыми EffectiveSettings и серверной меткой времени.
        response.Header.ResponseId = str(uuid.uuid4())
        response.Response.Alice2EffectiveSettings.EffectiveSettings.SetInParent()
        message.Timings.SendingTime.GetCurrentTime()
    return message


def compose_raw(spec, ctx, schemas_root):
    """Отладка: сообщение целиком из полей JSON (schema/proto/fields)."""
    return build(schemas_root, spec.get("proto") or "NAlice.NAliceApi.TServerMessage",
                 expand(spec.get("fields") or {}, ctx), spec.get("schema") or "alice-api")
