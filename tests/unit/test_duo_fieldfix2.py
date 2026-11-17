"""fieldfix2: правки по второму полевому отчёту (прогон pair+worktree на свежем Python-проекте).

Н8   gates --extra: абсолютный путь внутри корня проекта нормализуется, вне корня — отказ.
Н9   commit-by-plan: чужой staging — подсказка `git -C <дерево> reset`.
Н12  commit-by-plan --dry-run предсказывает ровно то, что сделает --yes (чужой staging, пустые группы).
Н10  Python-проект без игнора __pycache__: setup дописывает, init предупреждает.
Н11  сводка «что принято» в `status --task` и перед стартом `run --continue`.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix import _cbp_repo, _gates_one_step, _snap_runner
from tests.unit.test_duo_gates import _gates_ns
from tests.unit.test_duo_pipev3 import _cfg, _git, _init_repo, _mk_task, duo
from tests.unit.test_duo_portable import _repo_at, _setup_ns

# --- Н8 --------------------------------------------------------------------------


def _extra_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    (repo / "lib").mkdir()
    (repo / "lib" / "m.py").write_text("x = 1", encoding="utf-8")
    (repo / "top.py").write_text("t = 1", encoding="utf-8")
    _git(repo, "add", "lib", "top.py")
    _git(repo, "commit", "-qm", "code")
    _mk_task(repo, "demo")
    return repo


def test_gates_extra_absolute_inside_root_is_normalized(tmp_path: Path):
    repo = _extra_repo(tmp_path)
    (repo / "lib" / "m.py").write_text("x = 2", encoding="utf-8")        # изменён → доедет extra'ом
    snap_rel: dict = {}
    snap_abs: dict = {}
    cfg = _gates_one_step(tmp_path, repo)
    assert duo.cmd_gates(cfg, _gates_ns(extra=["lib", "top.py"]), runner=_snap_runner(snap_rel)) == 0
    assert duo.cmd_gates(cfg, _gates_ns(extra=[str(repo / "lib"), str(repo / "top.py")]),
                         runner=_snap_runner(snap_abs)) == 0
    assert snap_abs["text"]["lib/m.py"] == "x = 2"
    assert snap_abs["files"] == snap_rel["files"]                          # тот же результат, что с относительными


def test_gates_extra_absolute_outside_root_and_dotdot_rejected(tmp_path: Path):
    repo = _extra_repo(tmp_path)
    cfg = _gates_one_step(tmp_path, repo)
    with pytest.raises(SystemExit, match="вне корня"):
        duo.cmd_gates(cfg, _gates_ns(extra=[str(tmp_path / "elsewhere.txt")]), runner=_snap_runner({}))
    with pytest.raises(SystemExit, match=r"без '\.\.'"):
        duo.cmd_gates(cfg, _gates_ns(extra=["lib/../../x"]), runner=_snap_runner({}))
    inside_dotdot = str(repo / "lib" / ".." / ".." / "x")                   # абсолютный, но выходит из корня
    with pytest.raises(SystemExit, match="вне корня"):
        duo.cmd_gates(cfg, _gates_ns(extra=[inside_dotdot]), runner=_snap_runner({}))


# --- Н9 / Н12 --------------------------------------------------------------------


def _cbp_ns(**over):
    d = dict(task="demo", version=1, dry_run=False, yes=False, allow_partial=False)
    d.update(over)
    return types.SimpleNamespace(**d)


def test_dry_run_predicts_staging_refusal_and_gives_reset_hint(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    _git(repo, "add", "src/a.py")                                          # «собственный git add -A» оператора
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(dry_run=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1                                              # раньше dry-run отвечал 0 и «обещал» зелёное
    assert "чужой staging" in out and f"git -C {repo} reset" in out
    assert "предсказание для `--yes`: ОТКАЖЕТ" in out and "застейдженные" in out


def test_dry_run_green_when_plan_clean(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(dry_run=True))
    assert ei.value.code == 0
    assert "предсказание для `--yes`: пройдёт" in capsys.readouterr().out


def test_dry_run_predicts_empty_group_refusal(tmp_path: Path, capsys):
    """Группа без изменённых файлов: --yes откажет — dry-run обязан это предсказать (раньше молчал)."""
    repo, td = _cbp_repo(tmp_path)
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | src/* | feat: код | п.1 |\n| G2 | docs/* | docs: нет таких файлов | п.2 |\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(dry_run=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and "ОТКАЖЕТ" in out and "групп без изменённых файлов: 1 (G2)" in out
    # и реальный --yes действительно отказывает по тому же поводу (источник правды один)
    with pytest.raises(SystemExit) as ei2:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(yes=True))
    assert ei2.value.code == 1 and "ОТКАЗ --yes" in capsys.readouterr().out


def test_real_run_refuses_on_staging_with_hint_and_commits_nothing(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    _git(repo, "add", "src/a.py")
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(yes=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and f"git -C {repo} reset" in out
    assert duo.sh(["git", "log", "--oneline"], cwd=repo)[1].count("\n") == 1    # только базовый коммит
    # подсказка рабочая: reset снимает индекс, правка в файле цела, дальше --yes проходит
    _git(repo, "reset", "-q")
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "a"
    with pytest.raises(SystemExit) as ei2:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(yes=True))
    assert ei2.value.code == 0


# --- Н10 -------------------------------------------------------------------------


def _py_repo(tmp_path: Path, gitignore: str | None = None, with_py: bool = True) -> Path:
    repo = _repo_at(tmp_path / "proj")
    if with_py:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    if gitignore is not None:
        (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    return repo


@pytest.mark.parametrize("gi,with_py,expected", [
    (None, True, True),                       # Python без игнора — ловушка
    ("__pycache__/\n", True, False),
    ("*.pyc\n", True, False),
    ("/__pycache__/\n", True, True),          # только корень: tests/__pycache__ не покрыт
    (None, False, False),                     # не Python-проект — не лезем
])
def test_python_junk_unignored(tmp_path: Path, gi, with_py, expected):
    repo = _py_repo(tmp_path, gi, with_py)
    assert duo.python_junk_unignored(repo) is expected


def test_setup_adds_python_junk_block_once(tmp_path: Path, capsys):
    repo = _py_repo(tmp_path)
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(no_register=True), repo=repo)
    gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert duo.GITIGNORE_PY_MARKER in gi and "__pycache__/" in gi
    assert not duo.python_junk_unignored(repo)
    duo.cmd_setup(cfg, _setup_ns(no_register=True), repo=repo)             # повтор — без дублей
    assert (repo / ".gitignore").read_text(encoding="utf-8").count("__pycache__/") == 1
    capsys.readouterr()


def test_setup_leaves_non_python_project_alone(tmp_path: Path):
    repo = _py_repo(tmp_path, with_py=False)
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(no_register=True), repo=repo)
    assert "__pycache__" not in (repo / ".gitignore").read_text(encoding="utf-8")


def test_init_warns_python_project_without_pycache_ignore(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    (repo / "m.py").write_text("x = 1", encoding="utf-8")
    ns = types.SimpleNamespace(task="s1", task_file=None, mode=None, solo=False, isolation=None)
    duo.cmd_init(_cfg(tmp_path, repo), ns)
    err = capsys.readouterr().err
    assert "__pycache__" in err and "ДО серии" in err
    (repo / ".gitignore").write_text(".worktrees/\n__pycache__/\n", encoding="utf-8")
    ns2 = types.SimpleNamespace(task="s2", task_file=None, mode=None, solo=False, isolation=None)
    duo.cmd_init(_cfg(tmp_path, repo), ns2)
    assert "__pycache__" not in capsys.readouterr().err                     # игнор есть — тишина


# --- Н11 -------------------------------------------------------------------------


def _accepted_state(**over) -> dict:
    st = {
        "plan_version": 2, "phase": "impl_pending", "spent_usd": 3.1234,
        "verdicts": [{"rev": "rev001_plan.md", "phase": "plan", "verdict": "REVISE"},
                     {"rev": "rev002_plan.md", "phase": "plan", "verdict": "ACCEPT"}],
        "errata_applied": {"rev": "rev002_plan.md", "plan": "plan_v2.md", "items": 5, "sha": "abc"},
        "claim": {"paths": ["a.py", "b.py", "tests/t.py"], "declared": True},
    }
    st.update(over)
    return st


def test_accepted_plan_summary_content():
    s = duo.accepted_plan_summary(_accepted_state())
    assert "план v2 принят ревью rev002_plan.md" in s            # ACCEPT-ревью, не первое REVISE
    assert "эрраты: 5 п. из rev002_plan.md" in s
    assert "зона записи: 3 путей (a.py, b.py, tests/t.py)" in s
    assert "потрачено $3.12" in s


def test_accepted_plan_summary_variants():
    assert duo.accepted_plan_summary({"plan_version": 0}) is None
    s = duo.accepted_plan_summary(_accepted_state(errata_applied=None, claim={"declared": False}))
    assert "эрраты оркестратором не дописывались" in s and "зона записи НЕ заявлена" in s
    stale = _accepted_state(errata_applied={"rev": "r", "plan": "plan_v1.md", "items": 9})
    assert "эрраты оркестратором не дописывались" in duo.accepted_plan_summary(stale)   # эрраты прошлой версии плана
    shared = _accepted_state()
    shared.pop("claim")
    assert "зона" not in duo.accepted_plan_summary(shared)                              # shared-серия: зоны нет
    many = _accepted_state(claim={"paths": [f"f{i}.py" for i in range(9)], "declared": True})
    assert "9 путей" in duo.accepted_plan_summary(many) and "…" in duo.accepted_plan_summary(many)


def test_status_task_prints_summary_at_impl_pending(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st.update(_accepted_state())
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), types.SimpleNamespace(task="demo", all=False))
    out = capsys.readouterr().out
    assert "принято: план v2 принят ревью rev002_plan.md" in out
    assert "решение за человеком" in out and "run --task demo --continue" in out


def test_status_task_no_summary_before_plan_accepted(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo)                                                          # phase=plan, плана нет
    duo.cmd_status(_cfg(tmp_path, repo), types.SimpleNamespace(task="demo", all=False))
    assert "принято:" not in capsys.readouterr().out


def _run_cli(monkeypatch, cfg_path: Path, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["duo_pipeline.py", "--config", str(cfg_path), *argv])
    duo.main()


def test_run_continue_prints_summary_before_launch(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st.update(_accepted_state())
    duo.save_json(td / "state.json", st)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo)}), encoding="utf-8")
    launched: list = []
    monkeypatch.setattr(duo, "run_detached", lambda cfg, task, argv=None: launched.append(task))
    _run_cli(monkeypatch, cfg_path, "run", "--task", "demo", "--continue")
    out = capsys.readouterr().out
    assert "запуск имплементации: план v2 принят ревью rev002_plan.md" in out
    assert launched == ["demo"]                                             # сводка не мешает запуску
    # без --continue сводки нет; и не impl_pending — тоже
    _run_cli(monkeypatch, cfg_path, "run", "--task", "demo")
    assert "запуск имплементации" not in capsys.readouterr().out
    st["phase"] = "plan"
    duo.save_json(td / "state.json", st)
    _run_cli(monkeypatch, cfg_path, "run", "--task", "demo", "--continue")
    assert "запуск имплементации" not in capsys.readouterr().out


