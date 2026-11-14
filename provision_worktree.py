#!/usr/bin/env python3
"""provision_worktree.py — provisioning git-worktree серии (pipev3 P2/B1).

Симлинкает тяжёлые untracked-ресурсы основного дерева (data/, models/,
node_modules/) в свежий worktree: git их не переносит, а серия ожидает
окружение основного дерева. `.env` НЕ симлинкуется (секреты; изоляция
тестов — conftest, а не общий .env).

Контракт fail-closed: цель, которую нельзя разрешить (путь в worktree занят
не-симлинком, симлинк указывает не туда, worktree не git-worktree), —
nonzero exit, и вызывающий дуо-пайплайн НЕ стартует серию. Отсутствие
ресурса в основном дереве — не ошибка (в этой среде его может не быть).
Идемпотентен: повторный прогон по готовому worktree ничего не меняет.

Вызов: duo_pipeline.py Pipeline._ensure_worktree при каждом запуске серии
(git.isolation=worktree). Юниты: tests/unit/test_duo_pipev3.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Тяжёлые untracked-ресурсы dev-клона (см. CLAUDE.md: data/, models/ —
# симлинки в dev, маунт в prod; node_modules — сборка UI). Расширение —
# правкой списка, новые ресурсы появляются редко.
LINK_TARGETS = ("data", "models", "node_modules")


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        print("использование: provision_worktree.py <основное-дерево> <worktree>", file=sys.stderr)
        return 2
    repo = Path(sys.argv[1]).resolve()
    wt = Path(sys.argv[2]).resolve()
    if not repo.is_dir():
        return fail(f"основное дерево не найдено: {repo}")
    if not (wt / ".git").exists():
        return fail(f"{wt} — не git-worktree (нет .git)")
    if repo == wt:
        return fail("worktree совпадает с основным деревом")
    linked: list[str] = []
    for name in LINK_TARGETS:
        src = repo / name
        dst = wt / name
        if not src.exists():
            continue  # нет ресурса в этой среде — не ошибка
        if dst.is_symlink():
            if dst.resolve() != src.resolve():
                return fail(f"{dst} уже указывает на {dst.resolve()}, ожидался {src}")
            continue  # идемпотентность: уже слинковано
        if dst.exists():
            return fail(f"{dst} существует и не симлинк — не трогаю")
        try:
            dst.symlink_to(src, target_is_directory=True)
        except OSError as e:
            return fail(f"симлинк {dst} -> {src} не создан: {e}")
        linked.append(name)
    detail = ", ".join(linked) if linked else "нечего линковать"
    print(f"OK: provisioning worktree {wt.name}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
