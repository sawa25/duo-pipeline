#!/usr/bin/env python3
"""archive_cmd — логика подкоманды `archive` оркестратора (серия pipearchive, 01.11.2026).

Правило 5 (pipeline/expert/pipeline_cleanup_plan_20261101.md §11): удаление каталога
завершённой серии pipeline/<task>/ невозможно без (а) снимка tar.zst + строки
`name files raw_bytes tar_bytes sha256` в MANIFEST.tsv архива
~/pipeline_archive/<дата>/series/ и (б) дайджеста `### <task>` без `<заполнить>`
в pipeline/ARCHIVE-INDEX.md. Самодостаточен: не импортирует duo_pipeline (тот лениво
импортирует этот модуль в диспетчере), стандартная библиотека + subprocess для
git/tar/zstd. Ничего не коммитит и не мержит: staged-удаления артефактов серии
остаются человеку («КОММИТ ДЕЛАЕТ ЧЕЛОВЕК»).
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

PLACEHOLDER = "<заполнить>"
DEFAULT_MIN_AGE_DAYS = 14
TASK_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
SUBPROC_TIMEOUT = 1800

# --list: сервисные каталоги pipeline/, не серии (эррата Е3 rev001_plan)
LIST_SKIP_DIRS = {"archive", "expert"}

# Правило 5а — исключения из снимка (дословно ~/pipeline_archive/20261101/make_snapshots.sh);
# только регулярные файлы — симлинки не включаются (см. iter_snapshot_files).
SNAPSHOT_SKIP_DIRS = {"transcripts", "logs"}
SNAPSHOT_SKIP_PATTERNS = ("state.json", "orchestrator*.log", ".lock", ".stand.lock",
                          "*.pid", "untracked_impl_v*.txt")

MANIFEST_NAME = "MANIFEST.tsv"
MANIFEST_HEADER = "name\tfiles\traw_bytes\ttar_bytes\tsha256"
ZSTD_ARGS = ("-T0", "-6", "-q")          # уровень/потоки как в make_snapshots.sh (-T4 -6 -q)

# R6 (duoportable): охранный обход конфигурируем. Встроенный набор — нейтральные
# служебные каталоги любого git-проекта; vca-специфика (data, models, egg-info,
# doc/99-TODO.md, a3_registry.json) живёт в archive.guard_skip_dirs /
# archive.guard_exempt_files конфига ПРОЕКТА (.duo/config.json) и ДОПОЛНЯЕТ builtin.
GUARD_SKIP_DIRS_BUILTIN = {
    ".git", ".worktrees", "node_modules", "dist", "build", "__pycache__",
    ".ruff_cache", ".mypy_cache", ".pytest_cache", ".venv", "venv",
}
GUARD_SKIP_DIRS = set(GUARD_SKIP_DIRS_BUILTIN) | {"pipeline"}   # дефолт прямых вызовов
GUARD_EXEMPT_FILES_BUILTIN: set[str] = set()
GUARD_EXEMPT_FILES = set(GUARD_EXEMPT_FILES_BUILTIN)


def guard_sets(cfg: dict) -> tuple[set[str], set[str]]:
    """Эффективные наборы охранного обхода для репозитория с этим cfg: builtin ∪
    значения конфига ∪ каталог артефактов (сам pipeline/ всегда вне скана)."""
    arc = cfg.get("archive") or {}
    skip = set(GUARD_SKIP_DIRS_BUILTIN) | {str(d) for d in arc.get("guard_skip_dirs") or []}
    arts = str(cfg.get("artifacts_dir") or "pipeline").strip("/")
    skip.add(arts or "pipeline")
    exempt = set(GUARD_EXEMPT_FILES_BUILTIN) | {str(f) for f in arc.get("guard_exempt_files") or []}
    return skip, exempt
GUARD_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".bin", ".zst", ".zip", ".tar",
    ".gz", ".ico", ".woff", ".woff2", ".mp4", ".onnx", ".gguf",
}
GUARD_ENV_FILENAMES = {".env"}           # + .env.bak* — локальные бэкапы машины, не источники
INDEX_LINK_RE = re.compile(r"pipeline/ARCHIVE-INDEX\.md#\w+")   # легальная ссылка на архив
# Куда должна быть слита ветка серии, чтобы её можно было архивировать. Встроенный набор
# нейтрален; ветки конкретного проекта — `archive.merge_target_refs` в .duo/config.json.
MERGE_TARGET_REFS = ("HEAD", "main")


def merge_target_refs(cfg: dict) -> tuple[str, ...]:
    """Эффективные ссылки слияния: builtin ∪ archive.merge_target_refs (без дублей, по порядку)."""
    out = list(MERGE_TARGET_REFS)
    for ref in (cfg.get("archive") or {}).get("merge_target_refs") or []:
        if isinstance(ref, str) and ref and ref not in out:
            out.append(ref)
    return tuple(out)


class ArchiveError(RuntimeError):
    """Сбой снимка/верификации/манифеста. Вызывающий обязан откатиться БЕЗ удаления серии."""


# --- мелкие хелперы -----------------------------------------------------------


def _sh(cmd: list[str], cwd: Path | None = None, timeout: int = SUBPROC_TIMEOUT) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _paths(cfg: dict) -> dict:
    repo = Path(cfg["repo"]).resolve()
    git = cfg.get("git") or {}
    return {
        "repo": repo,
        "arts_rel": cfg.get("artifacts_dir", "pipeline"),
        "arts": repo / cfg.get("artifacts_dir", "pipeline"),
        "index": repo / cfg.get("artifacts_dir", "pipeline") / "ARCHIVE-INDEX.md",
        "archived": repo / cfg.get("artifacts_dir", "pipeline") / "archive" / "ARCHIVED.txt",
        "branch_prefix": git.get("branch_prefix", "duo/"),
        "worktrees_dir": git.get("worktrees_dir", ".worktrees"),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_size(n: int) -> str:
    x = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if x < 1024 or unit == "ГБ":
            return f"{x:.0f}{unit}" if unit == "Б" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}ГБ"


def _is_local_env(fname: str) -> bool:
    return fname in GUARD_ENV_FILENAMES or fname.startswith(".env.bak")


def lock_held(task_dir: Path) -> bool:
    """Держит ли кто-то `.lock` серии (живой оркестратор). Проба LOCK_SH|NB на отдельном
    дескрипторе — как lock_held() в duo_pipeline.py; держателя не беспокоит."""
    lock = Path(task_dir) / ".lock"
    try:
        fd = os.open(str(lock), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --- обход дерева серии / сканирование ссылок ----------------------------------


def iter_snapshot_files(repo: Path, task: str, arts_rel: str = "pipeline") -> list[str]:
    """Свежий обход каталога серии по Правилу 5а: rel-posix-пути регулярных файлов
    (симлинки/каталоги-симлинки не включаются) минус исключения; сортировка для
    детерминизма (сравнимость с членами tar)."""
    base = Path(repo) / arts_rel / task
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SNAPSHOT_SKIP_DIRS)
        for fname in filenames:
            if any(fnmatch.fnmatch(fname, pat) for pat in SNAPSHOT_SKIP_PATTERNS):
                continue
            p = Path(dirpath) / fname
            if p.is_symlink() or not p.is_file():
                continue
            out.append(p.relative_to(repo).as_posix())
    return sorted(out)


def _refs_pattern(names: list[str]) -> re.Pattern[str]:
    """`pipeline/<имя>/` или `pipeline/<имя>` + граница слова — как stale_refs_pattern()
    охранного теста (сравнение паттернов — test_guard_constants_in_sync)."""
    alts = "|".join(re.escape(n) for n in names)
    return re.compile(rf"pipeline/({alts})(/|\b)")


def scan_series_refs(repo: Path, names: list[str], *, skip_dirs: set[str] | None = None,
                     exempt_files: set[str] | None = None) -> dict[str, list[str]]:
    """Указатели `pipeline/<имя>` ВНЕ pipeline/ с семантикой охранного теста: один os.walk
    на все имена сразу (для --list), вырезание легальных ссылок ARCHIVE-INDEX.md#…,
    исключения exempt/.env*/бинарных суффиксов. Наборы каталогов/файлов-исключений —
    R6 (guard_sets(cfg)); по умолчанию — модульные GUARD_SKIP_DIRS/GUARD_EXEMPT_FILES.
    Возвращает {имя: [файлы]}."""
    hits: dict[str, set[str]] = {n: set() for n in names}
    if not names:
        return {n: sorted(v) for n, v in hits.items()}
    skip = GUARD_SKIP_DIRS if skip_dirs is None else skip_dirs
    exempt = GUARD_EXEMPT_FILES if exempt_files is None else exempt_files
    pattern = _refs_pattern(names)
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(repo).as_posix()
            if _is_local_env(fname):
                continue
            if rel in exempt or fpath.suffix.lower() in GUARD_BINARY_SUFFIXES:
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in pattern.finditer(INDEX_LINK_RE.sub("", text)):
                hits[m.group(1)].add(rel)
    return {n: sorted(v) for n, v in hits.items()}


def digest_section(index_path: Path, task: str) -> str | None:
    """Текст записи `### <task>` индекса (до следующего `### `/`## `); None — якоря нет."""
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    anchor = f"### {task}"
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == anchor:
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for ln in lines[start:]:
        if ln.startswith("### ") or ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body)


def _parse_age_days(iso: str) -> int | None:
    """Полных дней от метки времени до сейчас; None — метка не распознана.
    state.merged.at бывает наивным (пишет _finish_merge), а tip %cI у git — ВСЕГДА
    с offset (aware): смешивать aware/naive нельзя (TypeError), поэтому aware
    приводим к наивному локальному; ошибки парсинга гасим здесь (Н1 ревью)."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return int((datetime.now() - dt).total_seconds() // 86400)
    except (TypeError, ValueError):
        return None


def merged_age_days(st: dict, repo: Path, branch: str) -> tuple[int | None, str]:
    """Возраст слияния в полных днях: state.merged.at → дата последнего коммита ветки →
    (None, причина). Время наивное локальное (как пишет _finish_merge)."""
    m = st.get("merged") if isinstance(st.get("merged"), dict) else None
    at = str((m or {}).get("at") or "")
    if at:
        age = _parse_age_days(at)
        if age is not None:
            return age, f"state.merged.at {at[:10]}"
    rc, out = _sh(["git", "log", "-1", "--format=%cI", f"refs/heads/{branch}"], cwd=repo)
    tip = out.strip()
    if rc == 0 and tip:
        age = _parse_age_days(tip)
        if age is not None:
            return age, f"tip ветки {branch} {tip[:10]}"
    return None, "дата слияния не распознана (нет state.merged.at и валидного tip ветки)"


def merged_into(repo: Path, branch: str, refs: tuple[str, ...] | None = None) -> list[str]:
    """Существующие ссылки из refs (по умолчанию HEAD, main), содержащие ветку серии целиком."""
    out = []
    for ref in (refs or MERGE_TARGET_REFS):
        rc, _ = _sh(["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", ref], cwd=repo)
        if rc == 0:
            out.append(ref)
    return out


def _tracked_files(repo: Path, arts_rel: str, task: str) -> list[str]:
    rc, out = _sh(["git", "ls-files", "--", f"{arts_rel}/{task}"], cwd=repo)
    return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []


# --- допуск ---------------------------------------------------------------------


def admission(cfg: dict, repo: Path, task: str, min_age_days: int = DEFAULT_MIN_AGE_DAYS,
              *, force_young: bool = False, refs: list[str] | None = None) -> tuple[dict, list[str]]:
    """Сбор фактов серии и причин-блокеров (используется --list и --task). Пустой список
    причин = «можно архивировать». refs — предвычисленный scan_series_refs (для --list)."""
    p = _paths(cfg)
    task_dir = p["arts"] / task
    branch = f"{p['branch_prefix']}{task}"
    st = _load_json(task_dir / "state.json")
    m = st.get("merged") if isinstance(st.get("merged"), dict) else None
    targets = merge_target_refs(cfg)
    into = merged_into(repo, branch, targets)
    age_days, age_src = merged_age_days(st, repo, branch)
    worktree = (repo / p["worktrees_dir"] / task / ".git").exists()
    busy = lock_held(task_dir)
    dig = digest_section(p["index"], task)
    digest_state = "нет" if dig is None else ("черновик" if PLACEHOLDER in dig else "готов")
    tracked = _tracked_files(repo, p["arts_rel"], task)
    if refs is None:
        skip, exempt = guard_sets(cfg)
        refs = scan_series_refs(repo, [task], skip_dirs=skip, exempt_files=exempt)[task]

    facts = {"phase": st.get("phase") or "—",
             "age_days": age_days, "age_src": age_src, "into": into, "worktree": worktree,
             "lock_busy": busy, "digest": digest_state, "refs": refs, "tracked": len(tracked)}
    reasons: list[str] = []
    if not m and not into:
        reasons.append(f"серия не слита: нет state.merged и ветка {branch} не предок "
                       f"{'/'.join(targets)}")
    if age_days is None:
        reasons.append(f"возраст слияния недоказуем ({age_src})")
    elif age_days < min_age_days and not force_young:
        reasons.append(f"карантин: возраст {age_days} дн < {min_age_days} "
                       f"(--force-young — на свой риск)")
    if not tracked:
        reasons.append("артефакты серии не закоммичены (каталог untracked) — сначала закоммить "
                       "их (commit-by-plan), затем архивируй")
    if worktree:
        reasons.append(f"живой worktree серии: {repo / p['worktrees_dir'] / task} — "
                       f"сначала `merge --task {task}` (он удаляет worktree)")
    if busy:
        reasons.append(".lock серии занят (живой оркестратор)")
    if dig is None:
        reasons.append(f"нет дайджеста `### {task}` в ARCHIVE-INDEX.md (заготовка: "
                       f"`archive --task {task} --print-digest-template`)")
    elif PLACEHOLDER in dig:
        reasons.append(f"дайджест `### {task}` содержит {PLACEHOLDER} — заполни поля-суждения")
    if refs:
        reasons.append(f"указатели pipeline/{task} вне pipeline/: {len(refs)} файл(ов): "
                       f"{refs[:5]}")
    return facts, reasons


# --- снимок / верификация / манифест ---------------------------------------------


def make_snapshot(repo: Path, files: list[str], tar_zst: Path) -> None:
    """Собрать tar.zst по списку rel-путей (cwd=repo → члены архива `pipeline/<task>/…`,
    как в образце make_snapshots.sh). tar во временный файл, затем zstd (-T0 -6 -q):
    без пайпов — rc каждой стороны однозначен. ArchiveError — вызывающий откатывается."""
    if not files:
        raise ArchiveError("в каталоге серии нет файлов под снимок (Правило 5а)")
    with tempfile.TemporaryDirectory(prefix="duo_archive_snap_") as tmp:
        list_path = Path(tmp) / "files.nul"
        list_path.write_bytes(b"\0".join(f.encode("utf-8") for f in files) + b"\0")
        plain = Path(tmp) / "snap.tar"
        rc, out = _sh(["tar", "-cf", str(plain), "--null", "-T", str(list_path)], cwd=repo)
        if rc != 0:
            raise ArchiveError(f"tar не собрался (rc={rc}): {out.strip()[:300]}")
        rc, out = _sh(["zstd", *ZSTD_ARGS, "-f", str(plain), "-o", str(tar_zst)])
        if rc != 0:
            raise ArchiveError(f"zstd не сжал (rc={rc}): {out.strip()[:300]}")


def verify_snapshot(repo: Path, task: str, tar_zst: Path, sha256: str,
                    arts_rel: str = "pipeline") -> None:
    """Правило 5б: zstd -t → повторный sha256 → полная распаковка и сверка со СВЕЖИМ обходом
    (множество путей + размер каждого файла). Ловит порчу байта, пропавший член tar и
    изменение дерева между снимком и удалением. Расхождение → ArchiveError (откат)."""
    rc, out = _sh(["zstd", "-t", str(tar_zst)])
    if rc != 0:
        raise ArchiveError(f"zstd -t не прошёл (rc={rc}): {out.strip()[:300]}")
    actual = _sha256(tar_zst)
    if actual != sha256:
        raise ArchiveError(f"sha256 снимка разошёлся: записан {sha256[:16]}…, "
                           f"прочитан {actual[:16]}…")
    fresh = {rel: (Path(repo) / rel).stat().st_size for rel in iter_snapshot_files(repo, task, arts_rel)}
    with tempfile.TemporaryDirectory(prefix="duo_archive_verify_") as tmp:
        plain = Path(tmp) / "snap.tar"
        untar = Path(tmp) / "x"          # распаковка ОТДЕЛЬНО от промежуточного snap.tar,
        untar.mkdir()                    # иначе он сам попадёт в сверку членов
        rc, out = _sh(["zstd", "-d", "-q", "-f", str(tar_zst), "-o", str(plain)])
        if rc != 0:
            raise ArchiveError(f"zstd -d не прошёл (rc={rc}): {out.strip()[:300]}")
        rc, out = _sh(["tar", "-xf", str(plain), "-C", str(untar)])
        if rc != 0:
            raise ArchiveError(f"tar -xf не прошёл (rc={rc}): {out.strip()[:300]}")
        got: dict[str, int] = {}
        for dirpath, _dirnames, filenames in os.walk(untar):
            for fname in filenames:
                fp = Path(dirpath) / fname
                if fp.is_symlink():
                    raise ArchiveError(f"в снимок попал симлинк: {fp.relative_to(untar).as_posix()}")
                got[fp.relative_to(untar).as_posix()] = fp.stat().st_size
    if got != fresh:
        only_fresh = sorted(set(fresh) - set(got))
        only_got = sorted(set(got) - set(fresh))
        size_diff = [k for k in sorted(set(fresh) & set(got)) if fresh[k] != got[k]]
        raise ArchiveError(f"снимок ≠ дерево (свежий обход): только в дереве {len(only_fresh)} "
                           f"{only_fresh[:4]}, только в снимке {len(only_got)} {only_got[:4]}, "
                           f"иной размер {len(size_diff)} {size_diff[:4]}")


def _manifest_names(mpath: Path) -> list[str]:
    try:
        lines = mpath.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.split("\t", 1)[0] for ln in lines[1:] if ln.strip()]


def append_manifest(archive_root: Path, name: str, files: int, raw_bytes: int,
                    tar_bytes: int, sha256: str) -> None:
    """Дозапись строки в MANIFEST.tsv (никогда не перезапись); заголовок — при создании."""
    archive_root.mkdir(parents=True, exist_ok=True)
    mpath = archive_root / MANIFEST_NAME
    if name in _manifest_names(mpath):
        raise ArchiveError(f"{name}: строка уже есть в {mpath} — имя снимка неуникально")
    row = f"{name}\t{files}\t{raw_bytes}\t{tar_bytes}\t{sha256}\n"
    if mpath.exists():
        with open(mpath, "a", encoding="utf-8") as f:
            f.write(row)
    else:
        mpath.write_text(MANIFEST_HEADER + "\n" + row, encoding="utf-8")


def _archived_names(path: Path) -> list[str]:
    try:
        return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def update_archived_txt(path: Path, name: str) -> None:
    """Дописать имя серии в ARCHIVED.txt: без дублей, лексикографически отсортированно,
    атомарно (tmp + os.replace — как save_json оркестратора)."""
    names = _archived_names(path)
    if name not in names:
        names.append(name)
    names = sorted(set(names))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text("\n".join(names) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- шаблон дайджеста --------------------------------------------------------------


def _branch_dates(repo: Path, branch: str) -> tuple[str, str]:
    """Даты первого/последнего коммита ветки (YYYY-MM-DD); нет ветки — ('', '')."""
    rc_first, first = _sh(["git", "log", "--reverse", "--format=%as", f"refs/heads/{branch}"],
                          cwd=repo)
    rc_last, last = _sh(["git", "log", "-1", "--format=%as", f"refs/heads/{branch}"], cwd=repo)
    if rc_first != 0 or rc_last != 0 or not last.strip():
        return "", ""
    return (first.splitlines() or [""])[0].strip(), last.strip()


def _task_title(task_dir: Path) -> str:
    try:
        for ln in task_dir.joinpath("task.md").read_text(encoding="utf-8").splitlines():
            if ln.startswith("#"):
                return ln.lstrip("# ").strip()
    except OSError:
        pass
    return PLACEHOLDER


def _series_report(task_dir: Path) -> str:
    reports = sorted(task_dir.glob("impl_report_v*.md"),
                     key=lambda p: int(re.search(r"v(\d+)", p.name).group(1)) if re.search(r"v(\d+)", p.name) else 0)
    if reports:
        return reports[-1].name
    return "report.md" if (task_dir / "report.md").is_file() else "—"


def _dir_size(task_dir: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(task_dir):
        for fname in filenames:
            fp = Path(dirpath) / fname
            if not fp.is_symlink():
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    return total


def digest_template(cfg: dict, repo: Path, task: str) -> str:
    """Заготовка записи `### <task>` (6 полей формата ARCHIVE-INDEX) с механической
    подстановкой; поля-суждения — <заполнить>; вставленный сырым шаблон блокирует
    архивацию (PLACEHOLDER в digest_section)."""
    p = _paths(cfg)
    task_dir = p["arts"] / task
    branch = f"{p['branch_prefix']}{task}"
    st = _load_json(task_dir / "state.json")
    m = st.get("merged") if isinstance(st.get("merged"), dict) else None
    first, last = _branch_dates(repo, branch)
    dates = f"{first}–{last}" if first else PLACEHOLDER
    if m:
        src = f"слито (merge {str(m.get('commit', ''))[:10]} в {m.get('into', '?')} {str(m.get('at', ''))[:10]})"
    elif into := merged_into(repo, branch):
        src = f"слита в {', '.join(into)} (state.merged нет — даты из git)"
    else:
        src = PLACEHOLDER
    _skip, _exempt = guard_sets(cfg)
    refs = scan_series_refs(repo, [task], skip_dirs=_skip, exempt_files=_exempt)[task]
    refs_txt = "нет" if not refs else f"{len(refs)} попаданий: {', '.join(refs[:5])}"
    return (f"### {task}\n"
            f"* **Тема / даты:** {_task_title(task_dir)} · {dates}\n"
            f"* **Исход:** {src}\n"
            f"* **Решение:** {PLACEHOLDER}\n"
            f"* **Почему так, а не иначе:** {PLACEHOLDER}\n"
            f"* **Где живёт в проекте:** {PLACEHOLDER}\n"
            f"* **Данные серии:** каталог {_fmt_size(_dir_size(task_dir))}; "
            f"отчёт: {_series_report(task_dir)}\n"
            f"* **Ссылки из кода/доков на файлы серии:** {refs_txt}\n")


# --- подкоманды ----------------------------------------------------------------------


def cmd_archive(cfg: dict, args) -> int:
    """Диспетчер `archive`: --list | --task X --print-digest-template | --task X […].
    Коды возврата: 0 — ок/dry-run/«уже архивирована»; 1 — отказ (серия не тронута)."""
    if getattr(args, "list", False):
        return _cmd_list(cfg, args)
    task = getattr(args, "task", None)
    if not task:
        print("укажи --task NAME или --list")
        return 1
    if getattr(args, "print_digest_template", False):
        return _cmd_template(cfg, task)
    return _cmd_task(cfg, args)


def _min_age(args) -> int | None:
    """--min-age-days → int ≥ 0. 0 — валидное «выключить карантин» (не falsy-подмена!),
    None — невалидное значение (отрицательное/нечисловое)."""
    raw = getattr(args, "min_age_days", None)
    if raw is None:
        return DEFAULT_MIN_AGE_DAYS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val >= 0 else None


def _cmd_list(cfg: dict, args) -> int:
    p = _paths(cfg)
    min_age = _min_age(args)
    if min_age is None:
        print("ОТКАЗ: --min-age-days должно быть целым ≥ 0 (0 — выключить карантин)")
        return 1
    names: list[str] = []
    for child in sorted(p["arts"].iterdir()):
        if child.name in LIST_SKIP_DIRS:
            continue
        if child.is_symlink():          # битые симлинки is_dir() не видит — проверяем первыми
            print(f"{child.name}: симлинк (провижининг worktree-серии) — пропущен")
            continue
        if not child.is_dir():
            continue
        names.append(child.name)
    skip, exempt = guard_sets(cfg)
    refs_map = scan_series_refs(p["repo"], names, skip_dirs=skip, exempt_files=exempt)
    print(f"{'задача':<14} {'фаза':<10} {'дн':>4} {'слита в':<16} {'wt':<4} {'lock':<5} "
          f"{'дайджест':<8} {'ссылки':>6}  вердикт")
    for name in names:
        facts, reasons = admission(cfg, p["repo"], name, min_age, refs=refs_map.get(name, []))
        age = "?" if facts["age_days"] is None else str(facts["age_days"])
        into = ", ".join(facts["into"]) or "—"
        verdict = "МОЖНО" if not reasons else "нельзя: " + "; ".join(reasons)
        if len(verdict) > 110:
            verdict = verdict[:107] + "…"
        print(f"{name:<14} {str(facts['phase']):<10} {age:>4} {into:<16} "
              f"{'есть' if facts['worktree'] else '—':<4} "
              f"{'занят' if facts['lock_busy'] else '—':<5} {facts['digest']:<8} "
              f"{len(facts['refs']):>6}  {verdict}")
    print(f"всего: {len(names)} серий (возраст/карантин: {min_age} дн; архив — только человек)")
    return 0


def _cmd_template(cfg: dict, task: str) -> int:
    if not TASK_NAME_RE.fullmatch(task):
        print(f"ОТКАЗ: --task {task!r}: допустимы буквы/цифры/_/- (без путей и пробелов)")
        return 1
    print(digest_template(cfg, _paths(cfg)["repo"], task), end="")
    return 0


def _cmd_task(cfg: dict, args) -> int:
    p = _paths(cfg)
    repo = p["repo"]
    task = args.task
    if not TASK_NAME_RE.fullmatch(task):
        print(f"ОТКАЗ: --task {task!r}: допустимы буквы/цифры/_/- (без путей и пробелов)")
        return 1
    task_dir = p["arts"] / task
    arts_rel_task = f"{p['arts_rel']}/{task}"
    if not task_dir.exists():
        if task in _archived_names(p["archived"]):
            print(f"{task}: уже архивирована (имя есть в {p['archived']})")
            return 0
        print(f"ОТКАЗ: каталога серии нет: {task_dir} (и имени нет в ARCHIVED.txt)")
        return 1
    if task_dir.is_symlink():
        print(f"ОТКАЗ: {task_dir} — симлинк (провижининг worktree-серии); "
              f"архивируй в основном дереве")
        return 1

    min_age = _min_age(args)
    if min_age is None:
        print("ОТКАЗ: --min-age-days должно быть целым ≥ 0 (0 — выключить карантин)")
        return 1
    facts, reasons = admission(cfg, repo, task, min_age, force_young=args.force_young)
    if reasons:
        for r in reasons:
            print(f"ОТКАЗ: {r}")
        return 1

    if not shutil.which("zstd"):
        print("ОТКАЗ: нет бинарника zstd — снимок не собрать (поставь пакет zstandard/zstd)")
        return 1
    if not shutil.which("tar"):
        print("ОТКАЗ: нет бинарника tar — снимок не собрать")
        return 1
    files = iter_snapshot_files(repo, task, p["arts_rel"])
    if not files:
        print(f"ОТКАЗ: в {task_dir} нет файлов под снимок (Правило 5а) — архивировать нечего")
        return 1
    raw_bytes = sum((repo / f).stat().st_size for f in files)
    root = (Path(args.archive_root).expanduser() if args.archive_root
            else Path.home() / "pipeline_archive" / datetime.now().strftime("%Y%m%d"))
    tar_zst = root / "series" / f"{task}.tar.zst"
    if tar_zst.exists():
        print(f"ОТКАЗ: снимок уже есть: {tar_zst} (имя неуникально — смени --archive-root)")
        return 1
    if task in _manifest_names(root / MANIFEST_NAME):
        print(f"ОТКАЗ: {task} уже есть в {root / MANIFEST_NAME} (имя снимка неуникально)")
        return 1
    probe = root
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < raw_bytes:
        print(f"ОТКАЗ: в {probe} свободно {free} байт < ~{raw_bytes} байт (raw-оценка снимка)")
        return 1

    if args.dry_run:
        print(f"dry-run {task}: все проверки пройдены, НИЧЕГО не изменено")
        print(f"  снимок:    {len(files)} файлов, {raw_bytes} байт (raw) → {tar_zst}")
        print(f"  манифест:  дозапись `{task}\t{len(files)}\t{raw_bytes}\t<tar>\t<sha256>` "
              f"в {root / MANIFEST_NAME}")
        print(f"  удаление:  git rm -r -q -f -- {arts_rel_task} + остаток "
              f"(transcripts/, logs/, state.json, .lock)")
        nxt = sorted(set(_archived_names(p["archived"])) | {task})
        print(f"  {p['archived']}: +{task} (всего {len(nxt)})")
        print(f"  коммит:    человек, например `git commit -m \"chore(pipeline): archive {task}\"`")
        return 0

    if not args.yes:
        try:
            ans = input(f"архивировать {task} (снимок → удаление каталога)? [y/N] ")
        except (EOFError, OSError):
            print("нет tty для подтверждения — повтори с `--yes` (или в интерактивном терминале)")
            return 1
        if ans.strip().lower() not in ("y", "yes", "д", "да"):
            print("отменено")
            return 1

    # Е2: .lock может отсутствовать (gitignored; в чистой копии его нет) — создаём сами
    try:
        lock_fd = os.open(str(task_dir / ".lock"), os.O_RDWR | os.O_CREAT)
    except OSError as e:
        print(f"ОТКАЗ: не открыть {task_dir / '.lock'} ({e}) — серия не тронута")
        return 1
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ОТКАЗ: .lock серии занят (живой оркестратор)")
            return 1

        root.mkdir(parents=True, exist_ok=True)
        (root / "series").mkdir(parents=True, exist_ok=True)
        try:
            make_snapshot(repo, files, tar_zst)
            sha = _sha256(tar_zst)
            verify_snapshot(repo, task, tar_zst, sha, p["arts_rel"])
        except Exception as e:                     # любой сбой → откат без удаления серии
            print(f"ОТКАЗ: снимок/верификация не прошли — ОТКАТ БЕЗ УДАЛЕНИЯ: {e}")
            tar_zst.unlink(missing_ok=True)
            return 1
        tar_bytes = tar_zst.stat().st_size
        try:
            append_manifest(root, task, len(files), raw_bytes, tar_bytes, sha)
        except ArchiveError as e:
            print(f"ОТКАЗ: {e} (снимок {tar_zst} сохранён, каталог серии не тронут)")
            return 1

        rc, out = _sh(["git", "rm", "-r", "-q", "-f", "--", arts_rel_task], cwd=repo)
        if rc != 0:
            print(f"ОТКАЗ: git rm не удался (снимок {tar_zst} сохранён, каталог на месте): "
                  f"{out.strip()[:300]}")
            return 1
        if task_dir.exists():
            shutil.rmtree(task_dir)               # untracked-остаток: transcripts/, logs/, state.json
        update_archived_txt(p["archived"], task)

        print(f"архивирована {task}: {len(files)} файлов, raw {raw_bytes} → tar.zst {tar_bytes} байт")
        print(f"  снимок:    {tar_zst}")
        print(f"  манифест:  {root / MANIFEST_NAME}")
        print(f"  ветка {p['branch_prefix']}{task} сохранена (не тронута)")
        print(f"далее (человек): переведи оставшиеся указатели на серию в коде/доках на "
              f"pipeline/ARCHIVE-INDEX.md#{task}; коммит staged-удалений; гейты (make unit / lint)")
        print("КОММИТ ДЕЛАЕТ ЧЕЛОВЕК: инструмент ничего не коммитит")
        return 0
    finally:
        os.close(lock_fd)
