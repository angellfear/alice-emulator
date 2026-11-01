"""Возможности рабочей копии для самообновления (TASK-040): dulwich, git CLI не требуется.

Отвечает на «можно ли обновить» (git-клон с нужным origin, без правок в отслеживаемых файлах,
не контейнер), выполняет fetch тегов, stash правок и переключение на тег: ветка релизов —
fast-forward (refs/heads/<ветка> = коммит тега + reset --hard), иначе — checkout тега (detached).
Stash — коммит правок в refs/stash (см. stash()); в git stash list он не виден — там нужен reflog,
нам достаточно сохранённых объектов и ссылки.
"""
from pathlib import Path
import time

from dulwich import porcelain
from dulwich.objects import Commit
from dulwich.repo import Repo, NotGitRepository

STASH_MESSAGE = "alice-emulator: local changes before update"


def discover(root):
    """Repo рабочей копии; NotGitRepository — установка из zip без .git."""
    return Repo(str(Path(root)))


def is_clone(root):
    try:
        discover(root)
        return True
    except NotGitRepository:
        return False


def origin_url(repo):
    """URL remote origin (bytes) или None — сравнивается с настройкой self_update.repo."""
    try:
        return repo.get_config().get((b"remote", b"origin"), b"url")
    except KeyError:
        return None


def tracked_changes(repo):
    """Изменённые отслеживаемые файлы (staged + unstaged); untracked (state/ и пр.) — не блокируют."""
    status = porcelain.status(repo)
    files = set()
    for names in status.staged.values():
        files.update(_name(name) for name in names)
    files.update(_name(name) for name in status.unstaged)
    return sorted(files)


def _name(value):
    return (value[0] if isinstance(value, tuple) else value).decode()


def can_update(root, repo_name):
    """(ok, причины, файлы). Причины — для окна обновления; файлы — правки в отслеживаемых."""
    reasons, files = [], []
    if (Path(root) / ".dockerenv").exists():
        reasons.append("эмулятор в контейнере — обновляйте образ")
    if not is_clone(root):
        reasons.append("установка из zip-архива без .git — обновляется заменой архива")
    else:
        repo = discover(root)
        url = origin_url(repo)
        if url is None or not (url.decode().rstrip("/").endswith(f":{repo_name}.git")
                               or url.decode().rstrip("/").endswith(f"/{repo_name}.git")):
            reasons.append("origin не совпадает с " + repo_name)
        files = tracked_changes(repo)
        if files:
            reasons.append("в рабочей копии правки — отложите их или обновите через stash")
    return (not reasons, reasons, files)


def tags(repo):
    """Отслеживаемые semver-теги: {"0.1.0": sha}."""
    result = {}
    for ref, target in repo.get_refs().items():
        name = ref.decode()
        if not name.startswith("refs/tags/v"):
            continue
        number = name.rsplit("/", 1)[1].removeprefix("v")
        if number.replace(".", "").isdigit():
            obj = repo[target]
            if obj.type_name == b"tag":  # аннотированный тег — берём его цель
                target = obj.object[1]
            result[number] = target.decode()
    return result


def fetch_tags(repo, origin):
    """Fetch тегов из origin; сырые ссылки fetch'а записываются в refs/tags рабочей копии."""
    location = origin.decode() if isinstance(origin, bytes) else str(origin)
    result = porcelain.fetch(repo, location, include_tags=True)
    for ref, sha in result.refs.items():
        if ref.startswith(b"refs/tags/"):
            repo.refs[ref] = sha
    return tags(repo)


def descendant(repo, ancestor_sha, commit_sha):
    """commit_sha — потомок ancestor_sha (для выбора fast-forward). Релизная история линейна."""
    wanted, seen = {ancestor_sha}, set()
    stack = [commit_sha]
    while stack:
        sha = stack.pop()
        if sha in wanted:
            return True
        if sha in seen:
            continue
        seen.add(sha)
        try:
            stack.extend(parent.decode() for parent in repo[sha.encode()].parents)
        except KeyError:
            return False
    return False


def switch(repo, tag, tag_sha):
    """Переключение на релиз: ветка HEAD — fast-forward (reset --hard двигает ветку и дерево),
    иначе checkout тега (detached)."""
    follow = repo.refs.follow(b"HEAD")
    branch = follow[0][-1] if follow else None  # цепочка symbolic-ссылок; последний — ветка
    if branch and branch.startswith(b"refs/heads/") and descendant(repo, repo.head().decode(), tag_sha):
        porcelain.reset(repo, "hard", tag_sha)
        return "fast-forward"
    porcelain.checkout(repo, target=tag_sha)
    return "checkout"


def stash(repo):
    """Правки отслеживаемых файлов → коммит в refs/stash, рабочая копия — к HEAD. Возвращает sha."""
    changed = tracked_changes(repo)
    if not changed:
        return None
    porcelain.add(repo)  # рабочее дерево → индекс, дерево коммита собирается из индекса
    tree = repo.open_index().commit(repo.object_store)
    parents = [repo.head()]
    previous = repo.refs.read_loose_ref(b"refs/stash")
    if previous:
        parents.append(previous)  # второй родитель — прежний stash (как у git stash)
    commit = Commit()
    commit.tree = tree
    commit.parents = parents
    commit.commit_time = commit.author_time = int(time.time())
    commit.commit_timezone = commit.author_timezone = 0
    commit.author = commit.committer = b"alice-emulator <alice-emulator@localhost>"
    commit.message = STASH_MESSAGE.encode()
    repo.object_store.add_object(commit)
    repo.refs[b"refs/stash"] = commit.id
    porcelain.reset(repo, "hard")
    return commit.id.decode()


def blob_at(repo, commit_sha, path):
    """Содержимое файла в коммите (сравнение pyproject между релизами) или None."""
    try:
        tree = repo[repo[commit_sha.encode()].tree]
        for part in Path(path).parts:
            mode, sha = tree[part.encode()]
            tree = repo[sha]
        return tree.data if tree.type_name == b"blob" else None
    except KeyError:
        return None
