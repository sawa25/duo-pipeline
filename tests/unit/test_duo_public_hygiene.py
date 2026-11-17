"""Публичная чистота репозитория: в отслеживаемых файлах нет следов конкретных проектов и
хостов владельца (имена проектов-потребителей, домашние пути, адреса стендов).

Репозиторий открытый: проектная специфика живёт в `.duo/config.json` ПРОЕКТА-потребителя, а не
здесь. Тест ловит возврат таких следов (копипаста из боевого проекта) на этапе `make check`.
Запрещённые слова собраны в DENY ниже — сам этот файл из проверки исключён.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SELF = Path(__file__).resolve()

DENY = re.compile(
    r"vca|speedlim|mocalib|amdtdlc|m2group|ingests\w*|segreuse|grpspeed|tdlcguard|wdlag|a3_registry|99-TODO"
    r"|ydav|panelspeed|\boura\b"
    r"|/home/sawa|aspek@|minisserver|sawaTitan|minis128",
    re.IGNORECASE)


def _tracked_files() -> list[Path]:
    r = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, timeout=60)
    if r.returncode != 0:                                       # распаковка без .git: обходим дерево
        return [p for p in REPO_ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [REPO_ROOT / n for n in r.stdout.decode().split("\0") if n]


def test_no_owner_or_project_traces_in_tracked_files():
    bad: list[str] = []
    for f in _tracked_files():
        if f.resolve() == SELF or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                                            # бинарные файлы не проверяем
        for i, line in enumerate(text.splitlines(), 1):
            m = DENY.search(line)
            if m:
                bad.append(f"{f.relative_to(REPO_ROOT)}:{i}: «{m.group(0)}» → {line.strip()[:90]}")
    assert not bad, "следы проектов/хостов владельца в публичном репозитории:\n" + "\n".join(bad)
