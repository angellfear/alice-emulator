"""Установленное локально: каталоги пакетов и общих зависимостей в папке речевых пакетов.

* `<speech>/<id>/` — установка пакета (окружение, модели, лог); каталог без описания пакета —
  «осиротевший» (пакет удалён или расширение выключено), он тоже виден и очищается;
* `<speech>/_shared/<id>/` — общая зависимость; пакет перечисляет такие в `"depends": [...]`.

Пакет «используется», если он применён в настройках, его сервер работает или идёт установка.
Неиспользуемое очищается вместе с общими зависимостями, на которые больше не ссылается ни один
установленный пакет; зависимость, нужная оставшемуся пакету, остаётся.

Размеры каталогов считаются в фоне и кешируются (ключ — отметка installed.json), чтобы вкладка
не обходила десятки тысяч файлов окружения на каждом показе; сброс — при очистке и после установки.
"""
import asyncio
import logging
import os
import shutil
import stat
import sys
import time

log = logging.getLogger(__name__)

SHARED = "_shared"
CACHE_TTL = 600.0


def folder_size(path):
    """Размер каталога в байтах без перехода по ссылкам."""
    total, pending = 0, [str(path)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _writable_retry(function, path, error):
    """rmtree: файл только для чтения (кеши, git-объекты) — снять атрибут и повторить."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_retry)
    else:
        shutil.rmtree(path, onerror=_writable_retry)


class Inventory:
    def __init__(self, stack):
        self.stack = stack
        self.cache = {}  # путь → {"size", "key", "at"}
        self._task = None

    # --- каталоги ----------------------------------------------------------------
    @property
    def root(self):
        return self.stack.local.directory

    def _key(self, path):
        try:
            return (path / "installed.json").stat().st_mtime_ns
        except OSError:
            return 0

    def _fresh(self, path):
        entry = self.cache.get(str(path))
        return entry is not None and entry["key"] == self._key(path) and time.monotonic() - entry["at"] < CACHE_TTL

    def invalidate(self, path=None):
        if path is None:
            self.cache.clear()
        else:
            self.cache.pop(str(path), None)

    def _compute(self, paths):
        for path in paths:
            key = self._key(path)
            self.cache[str(path)] = {"size": folder_size(path), "key": key, "at": time.monotonic()}

    def _schedule(self, paths):
        stale = [p for p in paths if not self._fresh(p)]
        if not stale or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.get_running_loop().create_task(asyncio.to_thread(self._compute, stale))

    def computing(self):
        return self._task is not None and not self._task.done()

    def _size(self, path):
        entry = self.cache.get(str(path))
        return entry["size"] if entry else None

    # --- описание ----------------------------------------------------------------
    async def items(self):
        """Строки блока «Установлено локально» и итоги."""
        stack, local = self.stack, self.stack.local
        packages = {p["id"]: p for items in stack.packages().values() for p in items}
        running = {e["package"]["id"] for e in await asyncio.to_thread(stack.lifecycle.discover)}
        folders = sorted(p for p in local.directory.iterdir() if p.is_dir() and p.name != SHARED) if local.directory.is_dir() else []
        shared_root = local.directory / SHARED
        shared_dirs = sorted(p for p in shared_root.iterdir() if p.is_dir()) if shared_root.is_dir() else []
        self._schedule(folders + shared_dirs)

        present = {f.name for f in folders}
        refs = {}  # общая зависимость → id установленных пакетов, которые на неё ссылаются
        for name in present & set(packages):
            for dep in packages[name].get("depends", []):
                refs.setdefault(dep, []).append(name)

        rows = []
        for folder in folders:
            package = packages.get(folder.name)
            job = local.jobs.get(folder.name)
            if package is None:
                status, reason = "unused", ""
            elif job and job["stage"] not in ("done", "error"):
                status, reason = "installing", "идёт установка"
            elif stack.active(package["service"]) == "k:" + package["id"]:
                status, reason = "active", "применён в настройках"
            elif folder.name in running:
                status, reason = "running", "сервер запущен"
            else:
                status, reason = "unused", ""
            rows.append({"id": folder.name, "kind": "package" if package else "orphan",
                         "title": package["title"] if package else folder.name,
                         "service": package["service"] if package else None,
                         "size": self._size(folder), "status": status, "reason": reason,
                         "removable": status == "unused", "shared": list(package.get("depends", [])) if package else []})
        for folder in shared_dirs:
            users = refs.get(folder.name, [])
            rows.append({"id": SHARED + "/" + folder.name, "kind": "shared", "title": folder.name, "service": None,
                         "size": self._size(folder), "status": "needed" if users else "unused",
                         "reason": ("нужна: " + ", ".join(packages[u]["title"] for u in users)) if users else "",
                         "removable": not users, "shared": []})
        sizes = {r["id"]: r["size"] for r in rows}
        for row in rows:
            row["frees"] = self._frees(row, rows)
            row["freed_size"] = self._total(row["id"], row["frees"], sizes)
        reclaim = self._reclaim(rows, sizes)
        known = [s for s in sizes.values() if s is not None]
        return {"items": rows, "total": sum(known) if len(known) == len(rows) else None,
                "reclaimable": reclaim["size"], "reclaim_ids": reclaim["ids"],
                "computing": self.computing() or any(s is None for s in sizes.values())}

    @staticmethod
    def _frees(row, rows):
        """Общие зависимости, которые уйдут вместе с этим пакетом (на них больше никто не ссылается)."""
        if row["kind"] != "package" or not row["removable"]:
            return []
        others = {d for r in rows if r["kind"] == "package" and r["id"] != row["id"] for d in r["shared"]}
        return [d for d in row["shared"] if d not in others and any(r["id"] == SHARED + "/" + d for r in rows)]

    @staticmethod
    def _total(identifier, frees, sizes):
        parts = [sizes.get(identifier), *[sizes.get(SHARED + "/" + d) for d in frees]]
        return None if any(p is None for p in parts) else sum(parts)

    @staticmethod
    def _reclaim(rows, sizes):
        """Всё неиспользуемое одним действием: пакеты, их ставшие ненужными зависимости и свободные зависимости."""
        gone = {r["id"] for r in rows if r["kind"] in ("package", "orphan") and r["removable"]}
        kept_refs = {d for r in rows if r["kind"] == "package" and r["id"] not in gone for d in r["shared"]}
        ids = set(gone) | {r["id"] for r in rows if r["kind"] == "shared" and r["id"].split("/", 1)[1] not in kept_refs}
        parts = [sizes[i] for i in ids]
        return {"ids": sorted(ids), "size": None if any(p is None for p in parts) else sum(parts)}

    # --- очистка -----------------------------------------------------------------
    async def clean(self, ids=None, everything=False):
        """Удалить неиспользуемые каталоги (и ставшие ненужными общие зависимости). Возвращает
        {"removed": [id], "freed": байты, "errors": [{id, error}]}; используемое — ValueError."""
        data = await self.items()
        rows = {r["id"]: r for r in data["items"]}
        if everything:
            targets = list(data["reclaim_ids"])
        else:
            targets = list(dict.fromkeys(ids or []))
            for identifier in targets:
                row = rows.get(identifier)
                if row is None:
                    raise ValueError(f"«{identifier}» не найден среди установленного")
                if not row["removable"]:
                    raise ValueError(f"«{row['title']}» используется"
                                     + (f" ({row['reason']})" if row["reason"] else "") + " — очистка невозможна")
            for identifier in list(targets):
                for dep in rows[identifier]["frees"]:
                    if SHARED + "/" + dep not in targets:
                        targets.append(SHARED + "/" + dep)
        removed, errors, freed = [], [], 0
        for identifier in targets:
            row = rows.get(identifier)
            if row is None:
                continue
            path = self.root / identifier
            try:
                if row["kind"] == "package":
                    await asyncio.to_thread(self.stack.local.stop, self.stack.package(identifier))
                await asyncio.to_thread(remove_tree, path)
            except (OSError, ValueError) as exc:
                errors.append({"id": identifier, "error": str(exc)})
                continue
            self.invalidate(path)
            self.stack.local.jobs.pop(identifier, None)
            removed.append(identifier)
            freed += row["size"] or 0
        return {"removed": removed, "freed": freed, "errors": errors}
