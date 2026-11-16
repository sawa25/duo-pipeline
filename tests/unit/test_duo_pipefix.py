"""pipefix (30.10.2026): правки оркестратора по итогам боевой проверки pair+worktree.

Что фиксируют тесты (план pipeline/pipefix/plan_v1.md, грабли 1/4/11 + статус):
  * грабля 1: симлинк pipeline/<task> не мешает fast-forward базы worktree, когда основное
    дерево закоммитило pipeline/<task>/…; симлинк восстановлен после успеха и после
    искусственной ошибки слияния; merge после такой подтяжки удаляет worktree
    (принесённые артефакты возвращаются в индекс перед `git worktree remove`);
  * грабля 11: секции `## 12. files_touch` / `## 5) commit_plan` (номер, скобка, регистр,
    пробелы) разбираются; files_touch_notes / commit_plan_notes и заголовки другого смысла —
    не секция; промпты просят заголовок без номера;
  * грабля 4: merge без tty печатает подсказку про `--yes` (rc=1), явный отказ — «отменено»;
  * status --all: колонка «зона» помечает несвитую done-серию («не слита, зона держится N ч»),
    слитая и shared — без пометки.
Паттерн test_duo_pipemodes: git-песочницы в tmp_path, скриптованный транспорт, без LLM.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from tests.unit.test_duo_pipemodes import (
    _done_series,
    _executor,
    _gate,
    _git,
    _git_out,
    _head,
    _init_repo,
    _log_text,
    _merge_ns,
    _mk_task,
    _pcfg,
    _state,
    _wt_series,
    _zone,
    duo,
)
from tests.unit.test_duo_pipework2 import _set_state

# ---------------------------------------------------------------------------
# Грабля 1: fast-forward базы мимо симлинка pipeline/<task>
# ---------------------------------------------------------------------------


def _arts_link(p: duo.Pipeline) -> Path:
    return p.work_path / "pipeline" / p.task


def _commit_task_artifact(repo: Path, task: str) -> str:
    """Человек коммитит артефакт серии в основное дерево (сценарий грабли 1)."""
    _git(repo, "add", f"pipeline/{task}/task.md")
    _git(repo, "commit", "-qm", f"chore(pipeline): {task} task")
    return _head(repo)


def test_sync_base_fast_forward_survives_artifacts_symlink(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    assert _arts_link(a).is_symlink()                                  # симлинк артефактов на месте
    new_head = _commit_task_artifact(repo, "a")
    st = a.load_state()
    a._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == new_head                             # база подтянута, не «прежняя»
    assert _head(a.work_path) == new_head
    assert "БАЗА: worktree подтянут" in _log_text(a)
    link = _arts_link(a)                                               # симлинк восстановлен и ведёт в серию
    assert link.is_symlink() and link.resolve() == a.task_dir.resolve()
    assert (link / "state.json").exists()                              # артефакты видны через симлинк


def test_sync_base_restores_symlink_when_ff_fails(tmp_path: Path, monkeypatch):
    """Регрессионный (эррата Е1): до правки симлинк никто не снимает, при rc=1 он и так цел;
    тест фиксирует восстановление в finally после снятия."""
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    base = _state(a.task_dir)["worktree_base"]
    _commit_task_artifact(repo, "a")
    orig_sh = duo.sh

    def fake_sh(cmd, *args, **kw):
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            return 1, "искусственная ошибка слияния"
        return orig_sh(cmd, *args, **kw)

    monkeypatch.setattr(duo, "sh", fake_sh)
    st = a.load_state()
    a._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == base                                 # база не сдвинута
    assert _head(a.work_path) == base
    assert "fast-forward не удался" in _log_text(a)
    link = _arts_link(a)                                               # симлинк возвращён даже при ошибке
    assert link.is_symlink() and link.resolve() == a.task_dir.resolve()


def test_merge_removes_worktree_after_sync_brought_artifacts(tmp_path: Path, capsys):
    """Сценарий грабли 1 целиком: ff принёс pipeline/a/task.md → серия закоммитила работу →
    merge чист: зона отпущена, принесённые артефакты восстановлены, worktree удалён."""
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("feature.txt"))
    assert _gate(a) is True                                            # зона занята до подтяжки базы
    new_head = _commit_task_artifact(repo, "a")
    st = a.load_state()
    a._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == new_head                             # до правки падает здесь
    (a.work_path / "feature.txt").write_text("фича", encoding="utf-8")
    _git(a.work_path, "add", "feature.txt")
    _git(a.work_path, "commit", "-qm", "feat: a")
    st = a.load_state()
    st["phase"] = "done"
    a.save_state(st)
    assert duo.cmd_merge(cfg, _merge_ns()) == 0
    out = capsys.readouterr().out
    assert "worktree удалён" in out and "НЕ удалён" not in out
    assert not a.work_path.exists()
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "фича"
    assert _state(a.task_dir)["claim"]["released"] is True


# ---------------------------------------------------------------------------
# Грабля 11: нумерованные заголовки files_touch / commit_plan
# ---------------------------------------------------------------------------


NUMBERED_HEADING_ZONE = ["pkg/detector.py", "tests/unit/test_detector.py"]


def test_parse_files_touch_numbered_heading_variants():
    """Вид из боевого плана: заголовок с номером (`## 12. files_touch`)."""
    plan = ("# план\n\n## 12. files_touch\n\n"
            "- `pkg/detector.py`\n"
            "- `tests/unit/test_detector.py`\n\n## Риски\n")
    assert duo.parse_files_touch(plan) == NUMBERED_HEADING_ZONE
    for heading in ("## 5) files_touch", "## 12.files_touch", "## FILES_TOUCH",
                    "## Files_Touch (зона записи)", "### files_touch"):
        assert duo.parse_files_touch(f"# план\n\n{heading}\n\n- `a/b.py`\n") == ["a/b.py"], heading
    for heading in ("## 12. files_touch_notes", "## files_touch_notes",
                    "## 12. Зона записи files_touch", "## 12 . files_touch"):
        assert duo.parse_files_touch(f"# план\n\n{heading}\n\n- `a/b.py`\n") is None, heading


_CP_TABLE = ("\n| группа | файлы (glob) | сообщение коммита | rationale |\n"
             "|---|---|---|---|\n| A | `a.py` | feat: a | п.1 |\n")


def test_parse_commit_plan_numbered_heading():
    for heading in ("## 12. commit_plan", "## 5) commit_plan", "## 12.commit_plan",
                    "## COMMIT_PLAN", "## commit_plan:"):
        groups, warnings = duo.parse_commit_plan(f"# отчёт\n\n{heading}\n{_CP_TABLE}")
        assert warnings == [] and [g["name"] for g in groups] == ["A"], heading
        assert groups[0]["globs"] == ["a.py"] and groups[0]["message"] == "feat: a"
    for heading in ("## 12. commit_plan_notes", "## commit_plan — таблица",
                    "### commit_plan", "## commit_plan:ref"):
        groups, warnings = duo.parse_commit_plan(f"# отчёт\n\n{heading}\n{_CP_TABLE}")
        assert groups == [] and any("не найдена" in w for w in warnings), heading


def test_plan_prompt_asks_heading_without_number(tmp_path: Path):
    repo = _init_repo(tmp_path)
    ex, t = _executor(tmp_path, repo, isolation="worktree")
    ex.make_plan("задача", 1)
    assert "## files_touch" in t.calls[0]                              # литерал заголовка — как раньше
    assert "без номера" in t.calls[0]
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    ex.implement("задача", "pipeline/demo/plan_v1.md", 1)
    assert "ровно `## commit_plan`" in t.calls[-1]                     # заголовок отчёта — без номера


# ---------------------------------------------------------------------------
# Грабля 4: merge без tty
# ---------------------------------------------------------------------------


def test_merge_without_tty_hints_yes(tmp_path: Path, capsys, monkeypatch):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)

    def no_tty(*_args):
        raise EOFError                                                  # stdin закрыт: неинтерактивный вызов

    monkeypatch.setattr("builtins.input", no_tty)
    assert duo.cmd_merge(cfg, _merge_ns(yes=False)) == 1
    out = capsys.readouterr().out
    assert "нет tty" in out and "--yes" in out
    assert a.work_path.exists()                                        # ничего не слито
    assert "duo/a" not in _git_out(repo, "branch", "--merged", "HEAD")

    monkeypatch.setattr("builtins.input", lambda *_args: "n")          # явный отказ — «отменено»
    assert duo.cmd_merge(cfg, _merge_ns(yes=False)) == 1
    assert "отменено" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status --all: несвитые done-серии держат зону
# ---------------------------------------------------------------------------


def test_status_all_marks_unmerged_done_series(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)                              # done, ветка не слита, claim держит
    st = a.load_state()
    st["claim"]["at"] = (datetime.now() - timedelta(hours=3, minutes=10)).isoformat(timespec="seconds")
    a.save_state(st)
    ns = argparse.Namespace(task=None, all=True)
    duo.cmd_status(cfg, ns)
    line_a = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("a "))
    assert "не слита" in line_a and "зона держится 3 ч" in line_a

    _set_state(_mk_task(repo, "sh"), phase="done")                     # shared done — без пометки
    duo.cmd_status(cfg, ns)
    line_sh = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("sh "))
    assert "не слита" not in line_sh

    assert duo.cmd_merge(cfg, _merge_ns()) == 0                        # после слияния пометки нет
    duo.cmd_status(cfg, ns)
    assert "не слита" not in capsys.readouterr().out
