"""pipearchive (01.11.2026): подкоманда `archive` — безопасная уборка завершённых серий.

Что фиксируют тесты (план pipeline/pipearchive/plan_v1.md §6 + эрраты Е1–Е3 из rev001_plan):
  * допуск/отказ по каждому условию (не слита, карантин/--force-young, живой worktree,
    занятый `.lock`, нет дайджеста, `<заполнить>` в дайджесте, указатели вне pipeline/,
    незакоммиченный каталог — эррата Е1);
  * `--dry-run` не меняет ни байта; happy path: снимок → верификация → git rm + остаток →
    дозапись ARCHIVED.txt/MANIFEST.tsv; без единого нового коммита (коммит делает человек);
  * порча снимка (подмена байта / член пропал из tar) → отказ и каталог на месте;
    симлинки не попадают в снимок; идемпотентность; отказы окружения (нет zstd, мало
    места, неуникальное имя снимка, неинтерактив без --yes, плохое имя --task).
R6 (duoportable): охрана архивации конфигурируема — tests/arch удалён, песочница
симулирует проектный слой .duo/config.json (archive.guard_exempt_files).
Паттерн test_duo_pipev3: git-песочницы в tmp_path, importlib-загрузка модуля, без LLM.
Снимки требуют бинарники zstd/tar (на машине без них соответствующие тесты skip).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.test_duo_pipev3 import _cfg, _git, _git_out, _init_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_CMD_PATH = REPO_ROOT / "archive_cmd.py"
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"


def _load_archive_module():
    spec = importlib.util.spec_from_file_location("duo_archive_under_test", ARCHIVE_CMD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


arc = _load_archive_module()

HAS_SNAPSHOT_BINS = shutil.which("zstd") is not None and shutil.which("tar") is not None
need_snap = pytest.mark.skipif(not HAS_SNAPSHOT_BINS, reason="нет zstd/tar — снимок не собрать")

DIGEST_FULL = """### demo
* **Тема / даты:** демо-серия для тестов archive · 01.11.2026
* **Исход:** слито (тестовый merge)
* **Решение:** демонстрационное решение.
* **Почему так, а не иначе:** требования плана pipearchive.
* **Где живёт в проекте:** нигде — песочница теста
* **Данные серии:** каталог 1КБ; отчёт: impl_report_v1.md
* **Ссылки из кода/доков на файлы серии:** нет
"""


# --- песочница -----------------------------------------------------------------


def _series_files(repo: Path, task: str) -> None:
    """Минимальная структура pipeline/<task>/…: tracked-артефакты + runtime-мусор + симлинк."""
    td = repo / "pipeline" / task
    (td / "transcripts").mkdir(parents=True)
    (td / "logs").mkdir()
    (td / "task.md").write_text(f"# Демо-серия {task}\n\nзадание\n", encoding="utf-8")
    (td / "plan_v1.md").write_text("# plan_v1\nплан\n", encoding="utf-8")
    (td / "impl_report_v1.md").write_text("# impl_report_v1\nотчёт\n", encoding="utf-8")
    (td / "diff_impl_v1.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (td / "transcripts" / "rev001_plan.md").write_text("транскрипт\n", encoding="utf-8")
    (td / "logs" / "gates_x.txt").write_text("OK\n", encoding="utf-8")
    (td / "orchestrator.log").write_text("12:00 log\n", encoding="utf-8")
    (td / "untracked_impl_v1.txt").write_text("untracked\n", encoding="utf-8")
    (td / "watcher.pid").write_text("4242\n", encoding="utf-8")
    (td / "link.md").symlink_to(td / "task.md")


def _add_series(repo: Path, task: str, *, merged_days_ago: int | None = 20,
                with_merged: bool = True, commit: str = "branch",
                digest: str | None = DIGEST_FULL) -> Path:
    """Создать серию в готовом репо. commit: 'branch' (ветка duo/<task> + merge --no-ff),
    'plain' (коммит в текущую ветку, ветки нет), False (каталог untracked — эррата Е1)."""
    _series_files(repo, task)
    td = repo / "pipeline" / task
    st = {"task": task, "phase": "done", "mode": "pair", "isolation": "worktree",
          "state_updated_at": 1.0}
    if with_merged:
        at = (datetime.now() - timedelta(days=merged_days_ago)).isoformat(timespec="seconds")
        st["merged"] = {"into": "base", "commit": _git_out(repo, "rev-parse", "HEAD"), "at": at,
                        "new_commits": True}
    (td / "state.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    if digest is not None:
        idx = repo / "pipeline" / "ARCHIVE-INDEX.md"
        body = idx.read_text(encoding="utf-8") if idx.exists() else "# ARCHIVE-INDEX (песочница)\n\n"
        idx.write_text(body + digest.replace("demo", task) + "\n", encoding="utf-8")
    if commit == "branch":
        base = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", f"duo/{task}")
        _git(repo, "add", "-f", "--", f"pipeline/{task}/task.md", f"pipeline/{task}/plan_v1.md",
             f"pipeline/{task}/impl_report_v1.md", f"pipeline/{task}/diff_impl_v1.patch")
        _git(repo, "commit", "-qm", f"{task}: артефакты")
        _git(repo, "checkout", "-q", base)
        _git(repo, "merge", "-q", "--no-ff", "-m", f"merge: duo/{task} ({task})", f"duo/{task}")
    elif commit == "plain":
        _git(repo, "add", "-f", "--", f"pipeline/{task}")
        _git(repo, "commit", "-qm", f"{task}: артефакты без ветки")
    return td


def _series_repo(tmp_path: Path, task: str = "demo", **kw) -> tuple[Path, dict, Path, Path]:
    """Песочница: репо + одна зелёная серия + cfg + корень архива в tmp_path.
    cfg несёт проектный слой охраны (R6): doc/99-TODO.md — легальное исключение
    этого проекта (раньше было hardcoded, теперь — .duo/config.json)."""
    repo = _init_repo(tmp_path)
    (repo / "pipeline" / "archive").mkdir(parents=True)
    (repo / "pipeline" / "archive" / "ARCHIVED.txt").write_text("aaa\nzzz\n", encoding="utf-8")
    _add_series(repo, task, **kw)
    cfg = _cfg(tmp_path, repo)
    cfg["archive"] = {"guard_exempt_files": ["doc/99-TODO.md"]}
    return repo, cfg, repo / "pipeline" / task, tmp_path / "archroot"


def _args(task: str | None = None, **over) -> argparse.Namespace:
    ns = argparse.Namespace(list=False, task=task, dry_run=False, yes=False, min_age_days=14,
                            archive_root=None, force_young=False, print_digest_template=False)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _run(cfg: dict, task: str | None = None, **over) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = arc.cmd_archive(cfg, _args(task=task, **over))
    return rc, buf.getvalue()


def _tree_state(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).digest()
    return out


def _tar_members(tar_zst: Path) -> list[str]:
    plain = tar_zst.with_suffix(".tar")
    subprocess.run(["zstd", "-d", "-q", "-f", str(tar_zst), "-o", str(plain)], check=True)
    out = subprocess.run(["tar", "-tf", str(plain)], check=True, capture_output=True, text=True)
    plain.unlink()
    return sorted(ln for ln in out.stdout.splitlines() if ln.strip())


# --- --list ---------------------------------------------------------------------


def test_list_rows_and_verdict(tmp_path, capsys):
    """--list: таблица серий с вердиктом; сервисные каталоги archive/expert не входят (Е3),
    симлинк-каталог пропускается с пометкой."""
    repo, cfg, td, _ = _series_repo(tmp_path)
    (repo / "pipeline" / "expert").mkdir()
    (repo / "pipeline" / "expert" / "note.md").write_text("заметка\n", encoding="utf-8")
    (repo / "pipeline" / "wtlink").symlink_to(tmp_path / "elsewhere")
    rc = arc.cmd_archive(cfg, _args(list=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo" in out and "вердикт" in out
    assert "МОЖНО" in out                      # зелёная серия — можно архивировать
    assert "expert" not in out                 # Е3: скип-лист {"archive", "expert"}
    assert "wtlink" in out and "симлинк" in out


def test_list_verdict_refuses_not_merged(tmp_path, capsys):
    repo, cfg, td, _ = _series_repo(tmp_path, with_merged=False, commit="plain")
    rc = arc.cmd_archive(cfg, _args(list=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "нельзя" in out and "не слита" in out


# --- допуск/отказ по каждому условию ---------------------------------------------


def test_refusal_not_merged(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path, with_merged=False, commit="plain")
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "не слита" in out
    assert td.is_dir() and not (archroot / "MANIFEST.tsv").exists()


def test_refusal_young__force_young(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path, merged_days_ago=3)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "карантин" in out and "3" in out
    rc, out = _run(cfg, "demo", dry_run=True, force_young=True, archive_root=str(archroot))
    assert rc == 0 and "dry-run" in out


def test_min_age_zero_disables_quarantine(tmp_path):
    """Н3: --min-age-days 0 — валидное «выключить карантин», НЕ подмена на 14;
    отрицательное значение — явный отказ."""
    repo, cfg, td, archroot = _series_repo(tmp_path, merged_days_ago=3)
    rc, out = _run(cfg, "demo", dry_run=True, min_age_days=0, archive_root=str(archroot))
    assert rc == 0 and "dry-run" in out, f"0 должен отключать карантин: {out}"
    rc, out = _run(cfg, "demo", min_age_days=0, list=True, archive_root=str(archroot))
    assert rc == 0 and "МОЖНО" in out
    rc, out = _run(cfg, "demo", dry_run=True, min_age_days=-1, archive_root=str(archroot))
    assert rc == 1 and "--min-age-days" in out
    rc, out = _run(cfg, None, list=True, min_age_days=-1)
    assert rc == 1 and "--min-age-days" in out


def test_age_fallback_branch_tip_no_crash(tmp_path, capsys):
    """Н1: серия с веткой duo/<task>, но без state.merged (все 5 таких на боевом дереве) —
    фолбэк на tip %cI (aware-offset) не должен ронять --list/--task TypeError'ом:
    контролируемый вердикт «нельзя» с причиной-возрастом от tip ветки."""
    repo, cfg, td, archroot = _series_repo(tmp_path, with_merged=False, commit="branch")
    facts, reasons = arc.admission(cfg, repo, "demo")
    assert facts["age_days"] is not None and facts["age_src"].startswith("tip ветки duo/demo")
    assert any("карантин" in r for r in reasons)            # возраст от tip = «сейчас»
    rc = arc.cmd_archive(cfg, _args(list=True))
    out = capsys.readouterr().out
    assert rc == 0 and "нельзя" in out and "TypeError" not in out
    rc, out = _run(cfg, "demo", dry_run=True, archive_root=str(archroot))
    assert rc == 1 and "ОТКАЗ" in out and "TypeError" not in out
    assert td.is_dir()
    # age от tip — «сейчас», т.е. карантин; --force-young открывает dry-run
    rc, out = _run(cfg, "demo", dry_run=True, force_young=True, archive_root=str(archroot))
    assert rc == 0 and "dry-run" in out


def test_merged_age_days_offset_and_garbage(tmp_path):
    """Н1 (прямая проверка _parse_age_days через merged_age_days): state.merged.at
    с offset (aware) и без (naive) дают возраст; мусор → (None, причина), не краш."""
    repo, cfg, _, _ = _series_repo(tmp_path)
    for at in ("2026-10-01T10:00:00", "2026-10-01T10:00:00+03:00"):
        age, src = arc.merged_age_days({"merged": {"at": at}}, repo, "duo/demo")
        assert age is not None and age >= 0, f"{at}: ожидался возраст, получено {age},{src}"
        assert "state.merged.at" in src
    age, src = arc.merged_age_days({"merged": {"at": "не-дата"}}, repo, "duo/demo")
    assert age is not None and "tip ветки" in src          # мусор в at → фолбэк на ветку
    age, src = arc.merged_age_days({}, repo, "duo/no-such-branch")
    assert age is None and "не распознана" in src


def test_refusal_live_worktree(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    wt = repo / ".worktrees" / "demo"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "worktree" in out
    assert td.is_dir()


def test_refusal_lock_held(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    lock = td / ".lock"
    lock.write_text("", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys\n"
         "fd = open(sys.argv[1], 'w')\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "sys.stdin.read()\n", str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
        assert rc == 1 and ".lock" in out and "занят" in out
        assert td.is_dir()
    finally:
        holder.stdin.close()
        holder.wait(timeout=15)


def test_refusal_no_digest(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path, digest=None)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "дайджест" in out and "### demo" in out
    assert td.is_dir()


def test_refusal_digest_placeholder(tmp_path):
    digest = DIGEST_FULL.replace("* **Решение:** демонстрационное решение.",
                                 "* **Решение:** <заполнить>")
    repo, cfg, td, archroot = _series_repo(tmp_path, digest=digest)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "<заполнить>" in out
    assert td.is_dir()


def test_refusal_refs_outside(tmp_path):
    """Указатель pipeline/demo вне pipeline/ блокирует; doc/99-TODO.md — легальное
    исключение ИЗ КОНФИГА проекта (R6): без archive.guard_exempt_files он блокирует."""
    repo, cfg, td, archroot = _series_repo(tmp_path)
    todo = repo / "doc" / "99-TODO.md"
    todo.parent.mkdir()
    todo.write_text("п.3: см. pipeline/demo/plan_v1.md\n", encoding="utf-8")
    rc, out = _run(cfg, "demo", dry_run=True, archive_root=str(archroot))
    assert rc == 0, f"99-TODO — исключение из cfg песочницы, блокировать нельзя: {out}"
    notes = repo / "doc" / "notes.md"
    notes.write_text("см. также pipeline/demo/impl_report_v1.md\n", encoding="utf-8")
    rc, out = _run(cfg, "demo", dry_run=True, archive_root=str(archroot))
    assert rc == 1 and "указатели" in out and "doc/notes.md" in out
    # без проектного исключения (стирли ключ) тот же 99-TODO — уже указатель-блокер
    cfg["archive"] = {}
    rc, out = _run(cfg, "demo", dry_run=True, archive_root=str(archroot))
    assert rc == 1 and "doc/99-TODO.md" in out


def test_refusal_untracked_dir(tmp_path):
    """Е1: ни одного закоммиченного файла (git ls-files пуст) → отказ, каталог цел, манифеста нет."""
    repo, cfg, td, archroot = _series_repo(tmp_path, commit=False)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "не закоммичены" in out and "commit-by-plan" in out
    assert td.is_dir() and not (archroot / "MANIFEST.tsv").exists()


def test_task_name_validation(tmp_path):
    repo, cfg, _, _ = _series_repo(tmp_path)
    for bad in ("../x", "a b", "demo;rm"):
        rc, out = _run(cfg, bad, yes=True)
        assert rc == 1 and "ОТКАЗ" in out, f"--task {bad!r} должен отказывать"


# --- dry-run / happy path --------------------------------------------------------


def test_dry_run_changes_nothing(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    before_tree = _tree_state(repo)
    before_porcelain = _git_out(repo, "status", "--porcelain")
    rc, out = _run(cfg, "demo", dry_run=True, archive_root=str(archroot))
    assert rc == 0
    assert _tree_state(repo) == before_tree
    assert _git_out(repo, "status", "--porcelain") == before_porcelain
    assert not archroot.exists()


@need_snap
def test_success_flow(tmp_path):
    """Снимок → верификация → удаление → манифест/ARCHIVED.txt; .lock не существовал (Е2)."""
    repo, cfg, td, archroot = _series_repo(tmp_path)
    assert not (td / ".lock").exists()          # Е2: lock-файла нет — команда обязана работать
    eligible = arc.iter_snapshot_files(repo, "demo")
    assert eligible == [f"pipeline/demo/{n}" for n in
                        ["diff_impl_v1.patch", "impl_report_v1.md", "plan_v1.md", "task.md"]]
    raw = sum((repo / f).stat().st_size for f in eligible)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 0, out
    tar = archroot / "series" / "demo.tar.zst"
    assert tar.is_file()
    assert subprocess.run(["zstd", "-t", str(tar)]).returncode == 0
    rows = (archroot / "MANIFEST.tsv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "name\tfiles\traw_bytes\ttar_bytes\tsha256"
    assert rows[1].split("\t")[0] == "demo"
    assert int(rows[1].split("\t")[1]) == len(eligible)         # И1: files == свежий обход
    assert int(rows[1].split("\t")[2]) == raw                   # И1: raw_bytes ==
    assert rows[1].split("\t")[4] == hashlib.sha256(tar.read_bytes()).hexdigest()
    assert _tar_members(tar) == eligible                        # И2: члены tar == обход
    assert not td.exists()                                      # каталог удалён целиком
    assert not (repo / "pipeline" / "demo").exists()
    names = (repo / "pipeline" / "archive" / "ARCHIVED.txt").read_text(encoding="utf-8").split()
    assert names == sorted(set(names) | {"demo"})               # И3: сортировано, без дублей
    assert "КОММИТ ДЕЛАЕТ ЧЕЛОВЕК" in out
    assert "снимок" in out and str(archroot / "series" / "demo.tar.zst") in out


@need_snap
def test_success_makes_no_commits(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    heads_before = int(_git_out(repo, "rev-list", "--count", "HEAD"))
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 0
    assert int(_git_out(repo, "rev-list", "--count", "HEAD")) == heads_before   # И5
    staged = _git_out(repo, "status", "--porcelain")
    assert "D  pipeline/demo/task.md" in staged                  # deletions — staged, не коммиты
    assert _git_out(repo, "branch", "--list", "duo/demo").strip().split()[-1:] == ["duo/demo"]  # ветка сохранена


@need_snap
def test_corrupted_byte_rollback(tmp_path):
    """Подмена байта в готовом снимке → верификация падает → откат: tar удалён, каталог цел."""
    repo, cfg, td, archroot = _series_repo(tmp_path)
    real = arc.make_snapshot

    def corrupt(repo_, files, tar_zst):
        real(repo_, files, tar_zst)
        data = bytearray(tar_zst.read_bytes())
        data[len(data) // 2] ^= 0xFF
        tar_zst.write_bytes(bytes(data))

    arc.make_snapshot = corrupt               # модуль загружен importlib — патчим атрибут
    try:
        rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    finally:
        arc.make_snapshot = real
    assert rc == 1 and "ОТКАЗ" in out and "откат" in out.lower()
    assert td.is_dir()                                          # И6
    assert not (archroot / "series" / "demo.tar.zst").exists()   # битый снимок убран
    assert not (archroot / "MANIFEST.tsv").exists()


@need_snap
def test_missing_member_rollback(tmp_path):
    """Файл пропал из tar (снимок собран не в полном составе) → сверка с обходом ловит → откат."""
    repo, cfg, td, archroot = _series_repo(tmp_path)
    real = arc.make_snapshot

    def drop_last(repo_, files, tar_zst):
        real(repo_, files[:-1], tar_zst)

    arc.make_snapshot = drop_last
    try:
        rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    finally:
        arc.make_snapshot = real
    assert rc == 1 and "ОТКАЗ" in out
    assert td.is_dir() and not (archroot / "MANIFEST.tsv").exists()


@need_snap
def test_manifest_append_two(tmp_path):
    repo, cfg, _, archroot = _series_repo(tmp_path)
    _add_series(repo, "beta")
    rc, _ = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 0
    rc, _ = _run(cfg, "beta", yes=True, archive_root=str(archroot))
    assert rc == 0
    lines = (archroot / "MANIFEST.tsv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[0].startswith("name\tfiles")     # дозапись, заголовок один
    assert [ln.split("\t")[0] for ln in lines[1:]] == ["demo", "beta"]


@need_snap
def test_symlinks_excluded(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 0
    members = _tar_members(archroot / "series" / "demo.tar.zst")
    assert "pipeline/demo/link.md" not in members
    assert "pipeline/demo/task.md" in members


@need_snap
def test_idempotent_already_archived(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    assert _run(cfg, "demo", yes=True, archive_root=str(archroot))[0] == 0
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 0 and "уже архивирована" in out


# --- окружение -------------------------------------------------------------------


def test_noninteractive_refuses(tmp_path, monkeypatch):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    rc, out = _run(cfg, "demo", archive_root=str(archroot))      # без --yes
    assert rc == 1 and "--yes" in out
    assert td.is_dir() and not archroot.exists()


def test_zstd_missing_message(tmp_path, monkeypatch):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "zstd" else real_which(name))
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "zstd" in out and "ОТКАЗ" in out
    assert td.is_dir() and not archroot.exists()


def test_disk_space_refusal(tmp_path, monkeypatch):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(100, 99, 1))
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "свободно" in out
    assert td.is_dir() and not archroot.exists()


def test_snapshot_name_conflict(tmp_path):
    repo, cfg, td, archroot = _series_repo(tmp_path)
    (archroot / "series").mkdir(parents=True)
    (archroot / "series" / "demo.tar.zst").write_bytes(b"old")
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "снимок уже есть" in out
    (archroot / "series" / "demo.tar.zst").unlink()
    (archroot / "MANIFEST.tsv").write_text(
        "name\tfiles\traw_bytes\ttar_bytes\tsha256\ndemo\t1\t1\t1\t0" + "0" * 64 + "\n",
        encoding="utf-8")
    rc, out = _run(cfg, "demo", yes=True, archive_root=str(archroot))
    assert rc == 1 and "MANIFEST" in out
    assert td.is_dir()


# --- дайджест-шаблон ----------------------------------------------------------------


def test_digest_template_fields(tmp_path, capsys):
    repo, cfg, _, _ = _series_repo(tmp_path)
    rc = arc.cmd_archive(cfg, _args(task="demo", print_digest_template=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("### demo\n")
    assert "**Тема / даты:** Демо-серия demo · " in out          # L1 task.md + даты ветки
    assert "**Исход:** слито (merge " in out
    assert "**Данные серии:** каталог " in out and "impl_report_v1.md" in out
    assert "**Ссылки из кода/доков на файлы серии:** нет" in out
    assert out.count("<заполнить>") == 3                          # Решение / Почему / Где живёт


def test_template_pasted_blocks(tmp_path):
    import io
    from contextlib import redirect_stdout
    repo, cfg, _, _ = _series_repo(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        arc.cmd_archive(cfg, _args(task="demo", print_digest_template=True))
    idx = repo / "pipeline" / "ARCHIVE-INDEX.md"
    idx.write_text(buf.getvalue(), encoding="utf-8")              # вставили сырой шаблон
    rc, out = _run(cfg, "demo", dry_run=True)
    assert rc == 1 and "<заполнить>" in out                       # незаполненный дайджест блокирует


def test_archived_txt_sorted_unique(tmp_path):
    p = tmp_path / "ARCHIVED.txt"
    p.write_text("b\nb\na\n", encoding="utf-8")
    arc.update_archived_txt(p, "b")
    arc.update_archived_txt(p, "c")
    assert p.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_guard_sets_builtin_plus_config():
    """R6: эффективные наборы охраны = нейтральный builtin ∪ значения конфига проекта ∪
    каталог артефактов; vca-специфика (data/models/99-TODO) в коде больше не живёт.
    Паттерн указателя — дословно как в удалённом охранном тесте tests/arch."""
    assert "node_modules" in arc.GUARD_SKIP_DIRS_BUILTIN
    assert not ({"data", "models", "vca_platform.egg-info"} & arc.GUARD_SKIP_DIRS_BUILTIN)
    assert arc.GUARD_EXEMPT_FILES_BUILTIN == set()
    skip, exempt = arc.guard_sets({"artifacts_dir": "pipeline",
                                   "archive": {"guard_skip_dirs": ["data", "models"],
                                               "guard_exempt_files": ["doc/99-TODO.md"]}})
    assert {"pipeline", "data", "models", ".git", "node_modules"} <= skip
    assert exempt == {"doc/99-TODO.md"}
    # дефолт прямых вызовов scan_series_refs — builtin + каталог артефактов по умолчанию
    assert arc.GUARD_SKIP_DIRS == arc.GUARD_SKIP_DIRS_BUILTIN | {"pipeline"}
    assert arc.GUARD_EXEMPT_FILES == set()
    # семантика patterns охранного теста (дрейф недопустим)
    assert arc.INDEX_LINK_RE.pattern == r"pipeline/ARCHIVE-INDEX\.md#\w+"
    assert arc._refs_pattern(["x1", "x 2"]).pattern == r"pipeline/(x1|x\ 2)(/|\b)"


def test_merge_target_refs_builtin_plus_config(tmp_path: Path):
    """Ветки слияния: нейтральный builtin (HEAD, main) ∪ archive.merge_target_refs проекта;
    vca-ветка dev/speedlim в коде больше не живёт. merged_into учитывает переданный набор."""
    assert arc.MERGE_TARGET_REFS == ("HEAD", "main")
    assert arc.merge_target_refs({}) == ("HEAD", "main")
    assert arc.merge_target_refs({"archive": {"merge_target_refs": ["dev/x", "main", "", 7]}}) == (
        "HEAD", "main", "dev/x")
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        _git(repo, *args)
    (repo / "f").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "duo/t")
    _git(repo, "branch", "dev/x")                  # указывает на тот же коммит — «содержит» ветку серии
    assert arc.merged_into(repo, "duo/t") == ["HEAD", "main"]
    assert arc.merged_into(repo, "duo/t", arc.merge_target_refs(
        {"archive": {"merge_target_refs": ["dev/x"]}})) == ["HEAD", "main", "dev/x"]


def test_cli_help_smoke():
    """Врезка в main(): подкоманда видна в --help (argparse выходит до чтения config/repo)."""
    p = subprocess.run([sys.executable, str(DUO_PIPELINE_PATH), "archive", "--help"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    assert "archive" in p.stdout and "--print-digest-template" in p.stdout
