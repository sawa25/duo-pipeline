"""fieldfix: правки по полевому отчёту боевой проверки (находки Н1–Н6).

Н1  reset серии без потери task.md; init предупреждает о пропавшем task.md; run отказывает на шаблоне.
Н2  закоммиченные артефакты worktree-серии — отказ ДО создания worktree/ветки (и на init).
Н3  worktree_base не переживает удаление worktree+ветки; откат при провале provisioning.
Н4  gates: предупреждение «проверен бейзлайн», --dirty, каталоги в --extra.
Н5  commit-by-plan: артефакты ДРУГИХ серий не блокируют.
Н6  счётчики заглушек/транспортных повторов ревьюеров — в телеметрии и в `status --task`.

Паттерн соседних файлов: модуль — importlib-ом (через test_duo_pipev3), git-песочницы — в tmp_path.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import types
from pathlib import Path

import pytest

import reset_cmd  # noqa: E402 — корень репозитория на sys.path (pyproject: pythonpath/rootdir)
from tests.unit.test_duo_gates import _gates_ns, _gcfg, _last_log_dir
from tests.unit.test_duo_pipev3 import (
    ScriptedTransport,
    _cfg,
    _git,
    _git_out,
    _head,
    _init_repo,
    _mk_task,
    _review,
    duo,
)

# --- общие помощники ------------------------------------------------------------


def _commit_all(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _head(repo)


def _commit_file(repo: Path, name: str, msg: str = "c") -> str:
    """Коммит ОДНОГО файла: `add -A` утащил бы в базу pipeline/<task>/ (ловушка Н2)."""
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", msg)
    return _head(repo)


def _wcfg(tmp_path: Path, repo: Path) -> dict:
    cfg = _cfg(tmp_path, repo)
    cfg["git"]["isolation"] = "worktree"
    return cfg


def _branches(repo: Path) -> str:
    return _git_out(repo, "branch", "--list", "duo/*").strip()


# --- Н3: свежая база и откат создания worktree ----------------------------------


def test_stale_worktree_base_is_refreshed_when_no_worktree_and_no_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    old = _head(repo)
    (repo / "a.txt").write_text("новый коммит", encoding="utf-8")
    new = _commit_file(repo, "a.txt", "ушли вперёд")
    assert old != new
    p = duo.Pipeline(_wcfg(tmp_path, repo), "demo")
    st = p.load_state()
    st["worktree_base"] = old                       # пин прошлой ПРОВАЛЕННОЙ попытки
    p.save_state(st)
    p._ensure_worktree(st)
    assert st["worktree_base"] == new
    assert _git_out(repo, "rev-parse", "duo/demo").strip() == new
    assert "БАЗА" in (p.task_dir / "orchestrator.log").read_text(encoding="utf-8")


def test_pin_kept_when_branch_exists(tmp_path: Path):
    """Ветка с наработками уцелела — пин базы сохраняется, worktree цепляется к ветке."""
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    old = _head(repo)
    _git(repo, "branch", "duo/demo", old)
    (repo / "a.txt").write_text("вперёд", encoding="utf-8")
    _commit_file(repo, "a.txt")
    p = duo.Pipeline(_wcfg(tmp_path, repo), "demo")
    st = p.load_state()
    st["worktree_base"] = old
    p._ensure_worktree(st)
    assert st["worktree_base"] == old
    assert _git_out(p.work_path, "rev-parse", "HEAD").strip() == old


def test_provisioning_failure_rolls_back_fresh_worktree(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    p = duo.Pipeline(_wcfg(tmp_path, repo), "demo")
    st = p.load_state()

    def boom(self, wt):
        sys.exit("provisioning boom")

    monkeypatch.setattr(duo.Pipeline, "_provision_worktree", boom)
    with pytest.raises(SystemExit, match="provisioning boom"):
        p._ensure_worktree(st)
    assert not (repo / ".worktrees" / "demo").exists()
    assert _branches(repo) == ""                    # ветка-сирота не осталась
    assert "worktree_base" not in duo.load_json(p.state_file)   # и пин базы снят
    assert "ОТКАТ" in (p.task_dir / "orchestrator.log").read_text(encoding="utf-8")
    # повтор после починки причины — чистый старт с текущего HEAD
    monkeypatch.undo()
    st2 = p.load_state()
    p._ensure_worktree(st2)
    assert (repo / ".worktrees" / "demo" / ".git").exists()


def test_rollback_keeps_preexisting_branch_and_pin(tmp_path: Path, monkeypatch):
    """Откат снимает worktree, но НЕ чужую (прошлых попыток) ветку и её пин."""
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    old = _head(repo)
    _git(repo, "branch", "duo/demo", old)
    p = duo.Pipeline(_wcfg(tmp_path, repo), "demo")
    st = p.load_state()
    st["worktree_base"] = old
    p.save_state(st)
    monkeypatch.setattr(duo.Pipeline, "_provision_worktree", lambda self, wt: sys.exit("boom"))
    with pytest.raises(SystemExit):
        p._ensure_worktree(st)
    assert not (repo / ".worktrees" / "demo").exists()
    assert "duo/demo" in _branches(repo)
    assert duo.load_json(p.state_file)["worktree_base"] == old


# --- Н2: артефакты серии в коммитах базы ----------------------------------------


def test_tracked_artifacts_refuse_before_worktree_created(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _git(repo, "add", str((td / "task.md").relative_to(repo)))
    _git(repo, "commit", "-qm", "task.md в базе (ловушка Н2)")
    p = duo.Pipeline(_wcfg(tmp_path, repo), "demo")
    with pytest.raises(SystemExit) as ei:
        p._ensure_worktree(p.load_state())
    msg = str(ei.value)
    assert "закоммичен в базу" in msg and "pipeline/demo/task.md" in msg and "ПОСЛЕ merge" in msg
    assert not (repo / ".worktrees" / "demo").exists()
    assert _branches(repo) == ""                    # ветка не создавалась


def test_init_worktree_refuses_when_artifacts_tracked(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "state.json").unlink()
    _git(repo, "add", "pipeline/demo/task.md")
    _git(repo, "commit", "-qm", "task.md в базе")
    cfg = _cfg(tmp_path, repo)
    ns = types.SimpleNamespace(task="demo", task_file=None, mode="duo", solo=False, isolation="worktree")
    with pytest.raises(SystemExit, match="закоммичен в базу"):
        duo.cmd_init(cfg, ns)
    assert not (td / "state.json").exists()         # init ничего не создал
    # shared-серия с тем же закоммиченным task.md — безвредна, init проходит
    duo.cmd_init(cfg, types.SimpleNamespace(task="demo", task_file=None, mode=None, solo=False,
                                            isolation=None))
    assert (td / "state.json").exists()


# --- Н1: шаблон task.md, предупреждение init, reset -----------------------------


@pytest.mark.parametrize("text,expected", [
    ("# Задача: x\n\nОпиши задачу здесь.\n", True),
    ("", True),
    ("  \n\n", True),
    ("# задача demo", False),                       # краткий заголовок — осознанное задание
    ("# Задача: x\n\nОпиши задачу здесь.\n\nСделай X\n", False),
    ("Сделай X", False),
])
def test_task_md_is_placeholder(text, expected):
    assert duo.task_md_is_placeholder(text) is expected


def test_init_template_matches_placeholder_detector():
    assert duo.task_md_is_placeholder(duo.task_template("anything"))


def test_run_refuses_placeholder_task_md(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "task.md").write_text(duo.task_template("demo"), encoding="utf-8")
    p = duo.Pipeline(_cfg(tmp_path, repo), "demo")
    p.acquire_lock()
    with pytest.raises(SystemExit, match="остался шаблоном"):
        p._run_impl("auto", False, 0.0)
    assert "worktree_base" not in duo.load_json(p.state_file)       # побочных эффектов нет


def test_init_warns_when_task_md_missing_but_traces_exist(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "task.md").unlink()                      # rm -rf-подобный сценарий: следы есть, задания нет
    duo.cmd_init(_cfg(tmp_path, repo), types.SimpleNamespace(
        task="demo", task_file=None, mode=None, solo=False, isolation=None))
    err = capsys.readouterr().err
    assert "task.md" in err and "ШАБЛОН" in err and "reset" in err
    assert duo.task_md_is_placeholder((td / "task.md").read_text(encoding="utf-8"))


def test_init_fresh_series_has_no_warning(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    duo.cmd_init(_cfg(tmp_path, repo), types.SimpleNamespace(
        task="fresh", task_file=None, mode=None, solo=False, isolation=None))
    assert "ВНИМАНИЕ" not in capsys.readouterr().err


def _rns(task="demo", yes=True, dry_run=False, force=False):
    return argparse.Namespace(task=task, yes=yes, dry_run=dry_run, force=force)


def _series_with_junk(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "task.md").write_text("# важное задание\n\nсуть на 100 строк\n", encoding="utf-8")
    for name in ("plan_v1.md", "rev001_plan.md", "orchestrator.log"):
        (td / name).write_text("x", encoding="utf-8")
    (td / "transcripts").mkdir()
    (td / "transcripts" / "t.md").write_text("t", encoding="utf-8")
    return repo, td


def test_reset_moves_everything_but_task_md(tmp_path: Path, capsys):
    repo, td = _series_with_junk(tmp_path)
    before = (td / "task.md").read_bytes()
    assert reset_cmd.cmd_reset(_cfg(tmp_path, repo), _rns()) == 0
    assert (td / "task.md").read_bytes() == before            # ЗАДАНИЕ ЦЕЛО
    left = sorted(c.name for c in td.iterdir() if c.name != ".lock")
    backups = [n for n in left if n.startswith("_reset_")]
    assert len(backups) == 1 and "task.md" in left and len(left) == 2
    bk = td / backups[0]
    assert (bk / "state.json").exists() and (bk / "plan_v1.md").exists()
    assert (bk / "transcripts" / "t.md").exists()
    out = capsys.readouterr().out
    assert "task.md: СОХРАНЯЕТСЯ" in out and "duo init --task demo" in out
    # после reset init поднимает серию заново, task.md не затирается
    duo.cmd_init(_cfg(tmp_path, repo), types.SimpleNamespace(
        task="demo", task_file=None, mode=None, solo=False, isolation=None))
    assert (td / "task.md").read_bytes() == before and (td / "state.json").exists()


def test_reset_second_time_keeps_old_backups(tmp_path: Path):
    repo, td = _series_with_junk(tmp_path)
    cfg = _cfg(tmp_path, repo)
    assert reset_cmd.cmd_reset(cfg, _rns()) == 0
    duo.cmd_init(cfg, types.SimpleNamespace(task="demo", task_file=None, mode=None, solo=False,
                                            isolation=None))
    first = [c.name for c in td.iterdir() if c.name.startswith("_reset_")]
    import time as _t
    _t.sleep(1.1)                                   # метка — секунды
    assert reset_cmd.cmd_reset(cfg, _rns()) == 0
    now = [c.name for c in td.iterdir() if c.name.startswith("_reset_")]
    assert len(now) == 2 and first[0] in now        # прежний бэкап не вложен и не тронут


def test_reset_refuses_live_series(tmp_path: Path, capsys):
    repo, td = _series_with_junk(tmp_path)
    fd = open(td / ".lock", "w")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert reset_cmd.cmd_reset(_cfg(tmp_path, repo), _rns(force=True)) == 2   # --force живую не обходит
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    assert (td / "state.json").exists() and "lock занят" in capsys.readouterr().err


def test_reset_refuses_done_unless_force(tmp_path: Path):
    repo, td = _series_with_junk(tmp_path)
    st = duo.load_json(td / "state.json")
    st["phase"] = "done"
    duo.save_json(td / "state.json", st)
    cfg = _cfg(tmp_path, repo)
    assert reset_cmd.cmd_reset(cfg, _rns()) == 3
    assert (td / "state.json").exists()
    assert reset_cmd.cmd_reset(cfg, _rns(force=True)) == 0
    assert not (td / "state.json").exists()


def test_reset_dry_run_and_no_tty_without_yes_change_nothing(tmp_path: Path, monkeypatch):
    repo, td = _series_with_junk(tmp_path)
    cfg = _cfg(tmp_path, repo)
    assert reset_cmd.cmd_reset(cfg, _rns(dry_run=True, yes=False)) == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert reset_cmd.cmd_reset(cfg, _rns(yes=False)) == 3
    assert (td / "state.json").exists() and (td / "plan_v1.md").exists()


def test_reset_bad_name_and_missing_series(tmp_path: Path):
    repo, _ = _series_with_junk(tmp_path)
    cfg = _cfg(tmp_path, repo)
    assert reset_cmd.cmd_reset(cfg, _rns(task="../etc")) == 2
    assert reset_cmd.cmd_reset(cfg, _rns(task="nope")) == 2


def _worktree_series(tmp_path: Path):
    repo, td = _series_with_junk(tmp_path)
    cfg = _wcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    st["isolation"] = "worktree"
    st["mode"] = "duo"
    p.save_state(st)
    p._ensure_worktree(st)
    return repo, td, cfg, p


def test_reset_removes_worktree_and_branch_but_not_series_dir(tmp_path: Path):
    """Симлинк артефактов в worktree не должен утащить каталог серии основного дерева."""
    repo, td, cfg, p = _worktree_series(tmp_path)
    assert (repo / ".worktrees" / "demo" / "pipeline" / "demo").is_symlink()
    assert reset_cmd.cmd_reset(cfg, _rns()) == 0
    assert not (repo / ".worktrees" / "demo").exists() and _branches(repo) == ""
    assert (td / "task.md").read_text(encoding="utf-8").startswith("# важное задание")
    assert any(c.name.startswith("_reset_") for c in td.iterdir())


def test_reset_refuses_unmerged_branch_commits_unless_force(tmp_path: Path, capsys):
    repo, td, cfg, p = _worktree_series(tmp_path)
    wt = repo / ".worktrees" / "demo"
    (wt / "feat.txt").write_text("наработка", encoding="utf-8")
    _git(wt, "add", "feat.txt")
    _git(wt, "commit", "-qm", "work")
    assert reset_cmd.cmd_reset(cfg, _rns()) == 3
    assert (wt / ".git").exists() and "duo/demo" in _branches(repo) and (td / "state.json").exists()
    assert "не слитых" in capsys.readouterr().out
    assert reset_cmd.cmd_reset(cfg, _rns(force=True)) == 0
    assert not wt.exists() and _branches(repo) == ""


def test_reset_refuses_dirty_worktree_but_ignores_symlinks(tmp_path: Path):
    repo, td, cfg, p = _worktree_series(tmp_path)
    wt = repo / ".worktrees" / "demo"
    (wt / "wip.txt").write_text("не закоммичено", encoding="utf-8")
    assert reset_cmd.cmd_reset(cfg, _rns()) == 3
    assert wt.exists()
    (wt / "wip.txt").unlink()
    assert reset_cmd.cmd_reset(cfg, _rns()) == 0     # симлинк артефактов грязью не считается


def test_reset_merged_branch_is_not_work(tmp_path: Path):
    repo, td, cfg, p = _worktree_series(tmp_path)
    wt = repo / ".worktrees" / "demo"
    (wt / "feat.txt").write_text("x", encoding="utf-8")
    _git(wt, "add", "feat.txt")
    _git(wt, "commit", "-qm", "work")
    _git(repo, "merge", "-q", "--ff-only", "duo/demo")     # слита в HEAD → не наработка
    assert reset_cmd.cmd_reset(cfg, _rns()) == 0


# --- Н4: gates ------------------------------------------------------------------


def _gates_one_step(tmp_path: Path, repo: Path) -> dict:
    cfg = _gcfg(tmp_path, repo)
    cfg["gates"]["steps"] = [{"name": "x", "argv": ["true"], "pp": False, "timeout": 60}]
    return cfg


def _snap_runner(store: dict):
    def run(argv, *, cwd, env, timeout):
        cwd = Path(cwd)
        store["files"] = sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file())
        store["text"] = {str(p.relative_to(cwd)): p.read_text(encoding="utf-8", errors="replace")
                         for p in cwd.rglob("*") if p.is_file() and ".git" not in p.parts}
        return 0, "ok"
    return run


def _dirty_shared_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("v1", encoding="utf-8")
    (repo / "old.py").write_text("old", encoding="utf-8")
    (repo / ".gitignore").write_text(".worktrees/\n*.log\n", encoding="utf-8")
    _commit_all(repo, "base")
    _mk_task(repo, "demo")
    (repo / "app.py").write_text("v2", encoding="utf-8")          # изменён
    (repo / "new.py").write_text("новый", encoding="utf-8")       # untracked
    (repo / "old.py").unlink()                                    # удалён
    (repo / "junk.log").write_text("ignored", encoding="utf-8")   # игнорируется git
    (repo / "pipeline" / "other").mkdir()
    (repo / "pipeline" / "other" / "x.md").write_text("чужой", encoding="utf-8")   # артефакты — не код
    return repo


def test_gates_warns_that_baseline_is_checked_when_tree_dirty(tmp_path: Path, capsys):
    repo = _dirty_shared_repo(tmp_path)
    snap: dict = {}
    assert duo.cmd_gates(_gates_one_step(tmp_path, repo), _gates_ns(), runner=_snap_runner(snap)) == 0
    out = capsys.readouterr().out
    assert "НЕ содержит 3 незакоммиченных" in out and "--dirty" in out and "БЕЙЗЛАЙН" in out
    assert "⚠ результат относится к ref=HEAD" in out           # и в финальной строке
    assert snap["text"]["app.py"] == "v1" and "old.py" in snap["files"]       # проверен бейзлайн
    assert "WARN" in (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")


def test_gates_dirty_overlay_applies_changes_and_deletions(tmp_path: Path, capsys):
    repo = _dirty_shared_repo(tmp_path)
    snap: dict = {}
    ns = _gates_ns()
    ns.dirty = True
    assert duo.cmd_gates(_gates_one_step(tmp_path, repo), ns, runner=_snap_runner(snap)) == 0
    out = capsys.readouterr().out
    assert "--dirty:" in out and "ВНИМАНИЕ" not in out
    assert snap["text"]["app.py"] == "v2" and snap["text"]["new.py"] == "новый"
    assert "old.py" not in snap["files"]                       # удаление применено
    assert "junk.log" not in snap["files"]                     # игнорируемое не едет
    assert not any(f.startswith("pipeline/other") for f in snap["files"])
    assert "dirty_overlay=+2/-1" in (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")


def test_gates_clean_tree_no_warning(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo, "demo")
    assert duo.cmd_gates(_gates_one_step(tmp_path, repo), _gates_ns(),
                         runner=_snap_runner({})) == 0
    assert "ВНИМАНИЕ" not in capsys.readouterr().out


def test_gates_extra_accepts_directory_respecting_gitignore(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "dash").mkdir()
    (repo / "dash" / "a.js").write_text("a", encoding="utf-8")
    (repo / ".gitignore").write_text(".worktrees/\n*.log\n", encoding="utf-8")
    _commit_all(repo, "dash")
    (repo / "dash" / "b.js").write_text("b", encoding="utf-8")       # untracked
    (repo / "dash" / "sub").mkdir()
    (repo / "dash" / "sub" / "c.js").write_text("c", encoding="utf-8")
    (repo / "dash" / "x.log").write_text("log", encoding="utf-8")    # игнорируется
    (repo / "dash" / "a.js").write_text("a2", encoding="utf-8")      # изменён → доедет extra'ом
    _mk_task(repo, "demo")
    snap: dict = {}
    ns = _gates_ns(extra=["dash"])
    assert duo.cmd_gates(_gates_one_step(tmp_path, repo), ns, runner=_snap_runner(snap)) == 0
    assert {"dash/a.js", "dash/b.js", "dash/sub/c.js"} <= set(snap["files"])
    assert "dash/x.log" not in snap["files"] and snap["text"]["dash/a.js"] == "a2"


def test_gates_extra_rejects_missing_and_empty_dir(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo, "demo")
    (repo / "emptydir").mkdir()
    for bad in ("nope", "emptydir"):
        with pytest.raises(SystemExit, match="--extra"):
            duo.cmd_gates(_gates_one_step(tmp_path, repo), _gates_ns(extra=[bad]),
                          runner=_snap_runner({}))


def test_gates_worktree_series_dirty_taken_from_worktree(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _wcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    st["isolation"] = "worktree"
    p.save_state(st)
    p._ensure_worktree(st)
    wt = repo / ".worktrees" / "demo"
    (wt / "impl.py").write_text("правка исполнителя", encoding="utf-8")   # не закоммичено в ветке
    gcfg = _gates_one_step(tmp_path, repo)
    gcfg["git"] = cfg["git"]
    snap: dict = {}
    assert duo.cmd_gates(gcfg, _gates_ns(), runner=_snap_runner(snap)) == 0
    assert "ВНИМАНИЕ" in capsys.readouterr().out and "impl.py" not in snap["files"]
    ns = _gates_ns()
    ns.dirty = True
    assert duo.cmd_gates(gcfg, ns, runner=_snap_runner(snap)) == 0
    assert snap["text"]["impl.py"] == "правка исполнителя"


# --- Н5: чужие артефакты в commit-by-plan ---------------------------------------

PLAN = ("## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | src/* | feat: код | п.1 |\n")


def _cbp_repo(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo, "demo")
    (td / "impl_report_v1.md").write_text(PLAN, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a", encoding="utf-8")
    return repo, td


def test_foreign_series_artifacts_not_uncovered(tmp_path: Path):
    repo, td = _cbp_repo(tmp_path)
    other = _mk_task(repo, "next")                               # подготовленная чужая серия
    snap = duo.collect_commit_groups(repo, td, 1, "pipeline/demo")
    assert snap["uncovered"] == []
    assert any(f.startswith("pipeline/next/") for f in snap["foreign_artifacts"])
    assert other.is_dir()


def test_non_series_files_under_pipeline_still_uncovered(tmp_path: Path):
    """Только каталоги с task.md/state.json — «чужие серии»; прочее под pipeline/ — по-прежнему uncovered."""
    repo, td = _cbp_repo(tmp_path)
    (repo / "pipeline" / "NOTES.md").write_text("n", encoding="utf-8")
    (repo / "pipeline" / "scratch").mkdir()
    (repo / "pipeline" / "scratch" / "f.txt").write_text("f", encoding="utf-8")
    snap = duo.collect_commit_groups(repo, td, 1, "pipeline/demo")
    assert "pipeline/NOTES.md" in snap["uncovered"] and "pipeline/scratch/f.txt" in snap["uncovered"]
    assert snap["foreign_artifacts"] == []


def test_commit_by_plan_yes_not_blocked_by_foreign_and_does_not_commit_it(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    _mk_task(repo, "next")
    ns = types.SimpleNamespace(task="demo", version=1, dry_run=False, yes=True, allow_partial=False)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), ns)
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "артефакты ДРУГИХ серий" in out and "ОТКАЗ" not in out
    tracked = _git_out(repo, "ls-files").split()
    assert "src/a.py" in tracked and not any(t.startswith("pipeline/next") for t in tracked)


# --- Н6: счётчики транспорта ревью ----------------------------------------------


def test_review_telemetry_counts_stub_retries(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)
    _, _, tel_ok = _review(tmp_path, repo, cfg, ScriptedTransport())
    assert tel_ok["stub_retries"] == 0 and tel_ok["transport_retries"] == 0
    sub = tmp_path / "second"
    sub.mkdir()
    _, _, tel = _review(sub, repo, cfg, ScriptedTransport(responses=[""]))   # первый ответ — заглушка
    assert tel["stub_retries"] == 1 and tel["transport_retries"] == 0


def test_status_task_shows_review_transport_degradation(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["review_telemetry"] = [{"stub_retries": 2, "transport_retries": 1}, {"stub_retries": 0}]
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), types.SimpleNamespace(task="demo", all=False))
    out = capsys.readouterr().out
    assert "ответов-заглушек 2" in out and "транспортных повторов 1" in out and "по 2 ревью" in out


def test_status_task_silent_when_no_degradation(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["review_telemetry"] = [{"stub_retries": 0, "transport_retries": 0}]
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), types.SimpleNamespace(task="demo", all=False))
    assert "транспорт ревью" not in capsys.readouterr().out
