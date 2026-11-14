"""Охранный тест архива серий (серия archivereloc, 2026-11-01).

Каталоги 54 серий «E-набора» удалены из ``pipeline/`` (тег-архив
``pipeline-archive-20261101``, снимки ``~/pipeline_archive/20261101/series/``,
дайджесты ``pipeline/ARCHIVE-INDEX.md``). После удаления вне ``pipeline/`` не
должно оставаться ни чтений файлов этих каталогов, ни указателей на них:
легальная ссылка — только ``pipeline/ARCHIVE-INDEX.md#<серия>``.

Имена серий читаются из ``pipeline/archive/ARCHIVED.txt`` (не зашиты в тест:
расширение набора = дописывание файла). Обход — ``os.walk`` без git: гейты
прогоняются и в ``git archive``-копии без ``.git``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVED_TXT = ROOT / "pipeline" / "archive" / "ARCHIVED.txt"

# Каталоги, не сканируемые вовсе: сам архив серий с индексом, VCS и
# инфраструктура duo-пайплайна, тяжёлые данные (симлинки), сборки и кэши.
SKIP_DIRS = {
    "pipeline", ".git", ".worktrees", "node_modules", "data", "models", "dist",
    "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "build", "vca_platform.egg-info",
}
# Легальные упоминания: 99-TODO правит владелец; registry-фикстура хранит
# provenance-пути байт-в-байт (S-инвариант серии archivereloc).
EXEMPT_FILES = {
    "doc/99-TODO.md",
    "tests/fixtures/mocalib_a3/a3_registry.json",
}
# Не-текстовые суффиксы пропускаем: литерал в бинарнике — не указатель.
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".bin", ".zst", ".zip", ".tar",
    ".gz", ".ico", ".woff", ".woff2", ".mp4", ".onnx", ".gguf",
}
# Локальный env-конфиг и его бэкапы (.env, .env.bak_*) — файлы машины, не
# источники ссылок репозитория; в dev-дереве бэкапы копят указатели серий
# (прецедент Н1 rev001: 9 файлов красили make unit после слияния). Отслеживаемые
# примеры (.env.example, .env.prod-amd) источниками являются — остаются под надзором.
SKIP_FILENAMES = {".env"}


def _is_local_env(fname: str) -> bool:
    return fname in SKIP_FILENAMES or fname.startswith(".env.bak")
# Легальная ссылка на архив: индекс с якорем серии (вырезаем до поиска).
INDEX_LINK = re.compile(r"pipeline/ARCHIVE-INDEX\.md#\w+")


def archived_names() -> list[str]:
    """Имена заархивированных серий из ARCHIVED.txt (пропуски пустых строк)."""
    lines = ARCHIVED_TXT.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


def stale_refs_pattern(names: list[str]) -> re.Pattern[str]:
    """``pipeline/<имя>/`` или ``pipeline/<имя>`` + граница слова."""
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(rf"pipeline/({alts})(/|\b)")


def scan_violations() -> list[str]:
    """Файлы вне SKIP_DIRS с остаточными указателями на серии E-набора."""
    pattern = stale_refs_pattern(archived_names())
    violations: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(ROOT).as_posix()
            if _is_local_env(fname):
                continue
            if rel in EXEMPT_FILES or fpath.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if pattern.search(INDEX_LINK.sub("", text)):
                violations.append(rel)
    return violations


def test_no_stale_refs_to_archived_series() -> None:
    violations = scan_violations()
    assert not violations, (
        "файлы со ссылками на заархивированные серии вне pipeline/ "
        "(легальная ссылка — pipeline/ARCHIVE-INDEX.md#<серия>): " + ", ".join(violations)
    )
