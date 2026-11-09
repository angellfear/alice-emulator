"""Финализация AliceResponse по证据 реального трафика (fixtures voice-clock/volume-set).

Реальный ответ сервера, который колонка отображает, содержит:
- Cards[i].CardId — новый идентификатор на каждую карточку (comparator: new_identifier);
- Header.SequenceNumber — монотонный счётчик ответов;
- Header.DialogId = "" (не диалог-Id запроса);
- директиву tts_play_placeholder с Payload {"channel": "Dialog"} при голосе;
- управление громкостью — директива sound_set_level {"new_level": <float>}.

finalize() применяется ЕДИНОЖДЫ к каждому исходящему AliceResponse (голосовой
тракт и сценарный dialog-слой) и доводит отсутствующие поля до реального каркаса.
"""
from __future__ import annotations

import uuid


def _add_directive(response, name, payload=None, analytics=None):
    directive = response.Response.Directives.add()
    directive.Type = "client_action"
    directive.Name = name
    directive.IsLedSilent = True
    if analytics:
        directive.AnalyticsType = analytics
    if payload:
        for key, value in payload.items():
            directive.Payload[key] = value  # Struct: setitem, не kwargs
    return directive


def add_volume_directive(response, level):
    """Реальная директива громкости из перехвата (volume-set fixture): шкала 0..10,
    NewPercentLevel=6 в TSoundSetLevelDirective — проценты (int(level * 10), как у
    Push-действия звука)."""
    return _add_directive(response, "sound_set_level",
                          {"new_level": float(level), "new_percent_level": int(level * 10)})


def finalize(message, sequence, settings=None):
    """Доводит Event.AliceResponse до каркаса реальных ответов. Мутация на месте.
    settings — настройки эмулятора (alice2_settings): служебные поля Alice Pro
    (EffectiveSettings/TrialState) в каждом ответе, как у quasar-server;
    у служебных ответов поле уже стоит пустым — не перетирается."""
    event = message.Event
    if event.WhichOneof("Event") != "AliceResponse":
        return message
    response = event.AliceResponse
    if settings is not None and (settings.get("alice2_settings")
                                 and not response.Response.HasField("Alice2EffectiveSettings")):
        effective = response.Response.Alice2EffectiveSettings
        effective.EffectiveSettings.Mode = 2  # NAlice2Mode.EMode.Pro
        effective.EffectiveSettings.Preset = "test-preset"
        effective.TrialState.LeftCount = 9999
        effective.TrialState.Limit = 9999
        effective.TrialState.TimeLimitSec = 9999
    if not response.Header.ResponseId:
        response.Header.ResponseId = uuid.uuid4().hex
    response.Header.DialogId = ""  # реальный сервер шлёт пустой DialogId
    if sequence is not None and not response.Header.SequenceNumber:
        response.Header.SequenceNumber = sequence
    for card in response.Response.Cards:
        if not card.CardId:
            card.CardId = uuid.uuid4().hex
    if all(d.Name != "tts_play_placeholder" for d in response.Response.Directives):
        _add_directive(response, "tts_play_placeholder", {"channel": "Dialog"},
                       analytics="tts_play_placeholder")
    return message
