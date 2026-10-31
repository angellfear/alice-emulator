"""Версия эмулятора (TASK-039): git-описание рабочей копии + manifest release-please.

Источники, по убыванию точности:
1. git-репозиторий (dulwich, git CLI не требуется): последний semver-тег `vX.Y.Z`;
   коммиты после тега — `X.Y.Z+N.g<sha>` (как `git describe --tags`); изменённые
   отслеживаемые файлы — dirty=True (ignored-файлы, например `state/`, не считаются).
2. `.release-please-manifest.json` в корне репозитория — установки из zip без `.git`.

current() → {"version", "commit", "dirty"}; любая ошибка git — fallback на manifest,
без manifest — "0.0.0". Функция никогда не бросает исключений: версия — витринная
сводка, а не критичный путь.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # корень репозитория
MANIFEST = ROOT / ".release-please-manifest.json"


def _manifest_version():
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data.get(".", "") or "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


def current(path=None):
    """{"version": str, "commit": str, "dirty": bool} — см. докстринг модуля."""
    result = {"version": _manifest_version(), "commit": "", "dirty": False}
    try:
        from dulwich.repo import Repo
    except ImportError:  # dulwich не установлен (минимальная установка) — manifest
        return result
    try:
        repo = Repo.discover(start=str(Path(path or Path(__file__).parent)))
        head = repo.head().decode()
        result["commit"] = head[:7]  # есть и без тегов: пока release-please не создал первый релиз
        # semver-теги v*; аннотированный тег — берём его цель
        versions = []
        for ref, target in repo.get_refs().items():
            name = ref.decode()
            if not name.startswith("refs/tags/v"):
                continue
            obj = repo[target]
            if obj.type_name == b"tag":
                target = obj.object[1]
            number = name.rsplit("/", 1)[1].removeprefix("v")
            if number.replace(".", "").isdigit():
                versions.append((tuple(map(int, number.split("."))), number, target.decode()))
        if versions:
            versions.sort()
            # последний тег, достижимый от HEAD (HEAD может отставать от max-тега), и коммиты после него
            walk = [entry.commit.id.decode() for entry in repo.get_walker([repo.head()])]
            position = {sha: index for index, sha in enumerate(walk)}
            base = next(((number, sha) for _, number, sha in reversed(versions) if sha in position), None)
            if base:
                number, tagged_sha = base
                version = f"{number}+{position[tagged_sha]}.g{head[:7]}" if position[tagged_sha] else number
            result["version"] = version
        from dulwich import porcelain
        status = porcelain.status(repo)  # GitStatus: staged/unstaged/untracked; ignored (state/) не входит
        result["dirty"] = bool(any(status.staged.values()) or status.unstaged or status.untracked)
    except Exception:  # noqa: BLE001 — не git-репозиторий, битые refs: остаётся manifest
        pass
    return result
