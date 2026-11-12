"""Реестр шаблонов экранных директив (divui_show_view, draw_animation_directive).

Встроенные шаблоны — app_cards/*.json (образцы настоящего сервера); пользовательские
и переопределения встроенных — state/screen_templates/<id>.json: переопределение
сильнее встроенного, «сбросить» возвращает встроенный. Тело шаблона — JSON-документ
payload директивы (fields): {"Name": "divui_show_view", "Div2Card": {…}}.

Подстановки {{name}} заменяются значениями при сборке (build); <uuid> — свежий
request_id (в образце ютуба log_id карты == request_id webview — один uuid;
из seeded random — детерминированность эталонов). Переменные шаблона собираются
из тела автоматически. Проверка тела — validate: структура по имени директивы,
ошибки с путём в JSON.
"""
from __future__ import annotations

import json
import random
import re
import uuid
from pathlib import Path

CARDS_DIR = Path(__file__).parent / "app_cards"
STATE_DIRNAME = "screen_templates"
UUID_MARKER = "<uuid>"
VAR = re.compile(r"\{\{([a-z0-9_]+)\}\}")

# Директивы экрана: имя конверта → proto payload (схема станции, "centaur")
SHOW_VIEW_PROTO = "NAlice.TLayeredDivUICapability.TDivUIShowViewDirective"
ANIMATION_PROTO = "NAlice.TAnimationCapability.TDrawAnimationDirective"
HIDE_VIEW_PROTO = "NAlice.TLayeredDivUICapability.TDivUIHideViewDirective"
DIRECTIVE_PROTO = {"divui_show_view": SHOW_VIEW_PROTO,
                   "draw_animation_directive": ANIMATION_PROTO,
                   "divui_hide_view": HIDE_VIEW_PROTO}

DIRECTIVES = set(DIRECTIVE_PROTO)


class TemplateError(ValueError):
    pass


class Registry:
    """Реестр шаблонов: builtin из app_cards/*.json, пользовательские
    и переопределения — state/screen_templates/<id>.json."""

    def __init__(self, cards_dir=CARDS_DIR, state_dir=None):
        self.cards_dir = Path(cards_dir)
        self.state_dir = Path(state_dir) if state_dir else None

    # ── чтение ────────────────────────────────────────────────────────────
    def _builtin(self):
        entries = {}
        if self.cards_dir.is_dir():
            for path in sorted(self.cards_dir.glob("*.json")):
                doc = json.loads(path.read_text(encoding="utf-8"))
                entries[path.stem] = self._normalize(path.stem, doc)
        return entries

    @staticmethod
    def _normalize(id_, doc):
        """Образец настоящего сервера ({"proves", "title"?, "name"?,
        "analytics_type"?, "directive"}) или файл нового формата
        ({"title", "name", "body"}): title — короткое название для вкладки,
        proves — запись-источник образца (в UI не показывается)."""
        if "directive" in doc:
            body = doc["directive"]
            return {"id": id_, "title": doc.get("title") or doc.get("proves", ""),
                    "name": doc.get("name") or body["Name"],
                    "analytics_type": doc.get("analytics_type") or body["Name"],
                    "body": body}
        return {"id": id_, "title": doc.get("title", ""), "name": doc["name"],
                "analytics_type": doc.get("analytics_type") or doc["name"],
                "body": doc["body"]}

    def _state(self, id_):
        if self.state_dir is None:
            return None
        path = self.state_dir / f"{id_}.json"
        if not path.is_file():
            return None
        return self._normalize(id_, json.loads(path.read_text(encoding="utf-8")))

    def builtin_ids(self):
        return sorted(self._builtin())

    def _source(self, id_, builtin):
        if id_ not in builtin:
            return "custom"
        return "override" if self._state(id_) is not None else "builtin"

    def list(self):
        """Все шаблоны с источником и переменными: переопределение встроенного
        сильнее; ломаный файл в state (правится сейчас) список не ломает."""
        builtin = self._builtin()
        result = dict(builtin)
        if self.state_dir is not None and self.state_dir.is_dir():
            for path in sorted(self.state_dir.glob("*.json")):
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                result[path.stem] = self._normalize(path.stem, doc)
        return [dict(entry, source=self._source(entry["id"], builtin),
                     variables=variables(entry["body"]))
                for entry in sorted(result.values(), key=lambda e: e["id"])]

    def read(self, id_):
        """Шаблон целиком (переопределение сильнее встроенного); None — нет."""
        builtin = self._builtin()
        entry = self._state(id_) or builtin.get(id_)
        if entry is None:
            return None
        return dict(entry, source=self._source(id_, builtin),
                    variables=variables(entry["body"]))

    # ── проверка и сохранение ─────────────────────────────────────────────
    def validate(self, body):
        """Структура payload по имени директивы в теле (схема станции);
        ошибки — с путём в JSON."""
        if not isinstance(body, dict):
            raise TemplateError("тело: ожидается объект")
        name = str(body.get("Name") or "")
        if not name:
            raise TemplateError("Name: пустое имя директивы")
        if name == "divui_show_view":
            card = body.get("Div2Card")
            if not isinstance(card, dict):
                raise TemplateError("Div2Card: ожидается объект")
            if not (isinstance(card.get("Id"), dict) and card["Id"].get("CardId")):
                raise TemplateError("Div2Card.Id: нужен объект с CardId")
            self._validate_card_body(card)
        elif name == "draw_animation_directive":
            animations = body.get("Animations")
            if not (isinstance(animations, list) and animations):
                raise TemplateError("Animations: нужен непустой массив")
            for i, animation in enumerate(animations):
                directory = (animation or {}).get("S3Directory") if isinstance(animation, dict) else None
                if not isinstance(directory, dict):
                    raise TemplateError(f"Animations[{i}]: ожидается объект S3Directory")
                bucket, animation_path = str(directory.get("Bucket") or ""), str(directory.get("Path") or "")
                if not bucket.startswith("http") and "{{" not in bucket:  # {{var}} — подстановка при сборке
                    raise TemplateError(f"Animations[{i}].S3Directory.Bucket: URL каталога")
                if not animation_path.strip() and "{{" not in animation_path:
                    raise TemplateError(f"Animations[{i}].S3Directory.Path: путь анимации")
        return None

    @staticmethod
    def _validate_card_body(card):
        string_body = card.get("StringBody")
        if isinstance(string_body, str):
            try:
                body = json.loads(string_body)
            except json.JSONDecodeError as exc:
                raise TemplateError(f"Div2Card.StringBody: JSON {exc}") from exc
        elif isinstance(string_body, dict):
            body = string_body
        else:
            raise TemplateError("Div2Card.StringBody: JSON-объект DivKit")
        card_body = body.get("card") if isinstance(body, dict) else None
        if not isinstance(card_body, dict):
            raise TemplateError("Div2Card.StringBody.card: ожидается объект DivKit")
        if not str(card_body.get("log_id") or "").strip():
            raise TemplateError("Div2Card.StringBody.card.log_id: пустой log_id")
        if not (isinstance(card_body.get("states"), list) and card_body["states"]):
            raise TemplateError("Div2Card.StringBody.card.states: нужен непустой массив")

    def save(self, id_, body, title="", name=None, analytics_type=None):
        """Сохранение в state/screen_templates: встроенный id — переопределение,
        новый — пользовательский шаблон. Валидация — до записи на диск. Имя конверта —
        Name тела или по форме payload (карта с Div2Card — divui_show_view, Animations —
        draw_animation_directive); analytics_type переопределения наследуется от встроенного."""
        self.validate(body)
        name = name or str(body.get("Name") or "")
        if name not in DIRECTIVES:
            if isinstance(body.get("Div2Card"), dict):
                name = "divui_show_view"
            elif isinstance(body.get("Animations"), list):
                name = "draw_animation_directive"
            else:
                raise TemplateError("name: ожидается " + " | ".join(sorted(DIRECTIVES)))
        if not analytics_type:
            builtin = self._builtin().get(id_)
            analytics_type = builtin["analytics_type"] if builtin else name
        entry = {"id": id_, "title": str(title or ""), "name": name,
                 "analytics_type": analytics_type, "body": body}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / f"{id_}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"source": "override" if id_ in self._builtin() else "custom"}

    def reset(self, id_):
        """Сброс к встроенному: удалить переопределение (пользовательский
        шаблон удаляется целиком)."""
        path = self.state_dir / f"{id_}.json" if self.state_dir else None
        if path is None or not path.is_file():
            raise TemplateError(f"«{id_}»: переопределения нет")
        path.unlink()

    # ── сборка директивы ──────────────────────────────────────────────────
    def build(self, id_, values=None):
        """Элемент директивы {"name", "type", "is_led_silent", "analytics_type",
        "proto", "fields"}: тело с подстановками {{name}} и свежим <uuid>;
        Div2Card.StringBody — компактная JSON-строка (формат провода)."""
        entry = self.read(id_)
        if entry is None:
            raise TemplateError(f"Шаблон «{id_}» не найден")
        fields = parse_body(entry["body"], values)
        card = fields.get("Div2Card") if isinstance(fields.get("Div2Card"), dict) else None
        if isinstance(card, dict) and isinstance(card.get("StringBody"), dict):
            card["StringBody"] = json.dumps(card["StringBody"], ensure_ascii=False,
                                            separators=(",", ":"))
        return {"name": entry["name"], "type": "client_action", "is_led_silent": True,
                "analytics_type": entry["analytics_type"],
                "proto": DIRECTIVE_PROTO[entry["name"]], "fields": fields}


def substitute(text, values=None):
    """{{name}} — значениями (строки); <uuid> — свежий uuid (seeded random —
    детерминированность эталонов). Неизвестная переменная — TemplateError
    с её именем: опечатка не уйдёт на провод."""
    used = sorted(set(VAR.findall(text)))
    for name in used:
        if values is None or name not in values:
            raise TemplateError(f"Переменная «{name}» без значения")
        text = text.replace("{{" + name + "}}", str(values[name]))
    return text.replace(UUID_MARKER, str(uuid.UUID(int=random.getrandbits(128), version=4))), used


def parse_body(body, values=None):
    """Тело с подстановками → dict: {{name}} — значениями, <uuid> — свежий uuid.
    Вложенные StringBody пересобираются компактно — как на проводе образца."""
    return json.loads(substitute(json.dumps(body, ensure_ascii=False, separators=(",", ":")), values)[0])


def variables(body):
    """Доступные переменные шаблона — {{name}} из тела (для списка в UI)."""
    return sorted(set(VAR.findall(json.dumps(body, ensure_ascii=False))))


# ── модуль-синглтон (как voice_stack.adapters: configure при старте) ─────────

_registry = Registry()


def configure(state_dir, cards_dir=CARDS_DIR):
    """Каталог пользовательских шаблонов (state/screen_templates); вызывается
    из runtime при старте и из тестов с временной папкой."""
    _registry.cards_dir = Path(cards_dir)
    _registry.state_dir = Path(state_dir) if state_dir else None


def registry():
    return _registry
