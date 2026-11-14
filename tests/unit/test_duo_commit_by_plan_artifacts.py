"""devhyg (30.10.2026): группа «артефакты серии» в ## commit_plan не блокирует commit-by-plan.

Прецедент — слияние duo/devhyg 30.10.2026: `commit-by-plan --yes` отказывал целиком
(«групп без изменённых файлов: 1 (4. Артефакты серии)»), хотя пути pipeline/<task>/…
по замыслу оркестратора коммитятся человеком отдельно и исключены из coverage.
Паттерн test_duo_guards (грабли 8): git-песочницы в tmp_path, без LLM и сети.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from tests.unit.test_duo_guards import _cbp, _commit_cfg_repo, _ncommits
from tests.unit.test_duo_pipev3 import _git_out, duo

# План-прецедент: обычная группа A + группа «Артефакты серии», глобы которой лежат
# целиком в pipeline/demo/ (артефактные файлы task.md/state.json/impl_report_v1.md
# в песочнице создаёт сам хелпер _commit_cfg_repo через _mk_task).
ART_TABLE = """
| группа | файлы (glob) | сообщение коммита | rationale |
|---|---|---|---|
| A | `a.py` | feat: a | п.1 |
| Артефакты серии | `pipeline/demo/*.md`, `pipeline/demo/state.json` | chore(pipeline): артефакты | коммитятся человеком отдельно |
"""


def test_artifact_group_does_not_block_yes_and_artifacts_stay_uncommitted(tmp_path: Path, capsys):
    """(а) --yes без --allow-partial проходит; закоммичена только обычная группа."""
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, ART_TABLE, {"a.py": "1"})
    assert _cbp(cfg) == 0
    assert _ncommits(repo) == n0 + 1
    out = capsys.readouterr().out
    assert "ОТКАЗ --yes" not in out
    assert "группа Артефакты серии: артефакты серии — коммитятся отдельно (пропущена)" in out
    assert _git_out(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.py"]
    assert "?? pipeline/demo/task.md" in _git_out(repo, "status", "--porcelain", "--untracked-files=all")


def test_plain_empty_group_still_refuses_and_artifact_group_not_blamed(tmp_path: Path, capsys):
    """(б) обычная пустая группа — отказ, как раньше; артефактная в списке пустых не значится."""
    table = ART_TABLE.replace(
        "| Артефакты серии |", "| B | `b.py` | feat: b | п.2 |\n| Артефакты серии |")
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, table, {"a.py": "1"})
    assert _cbp(cfg) == 1
    out = capsys.readouterr().out
    refusal = out.split("ОТКАЗ --yes: ")[1].split("\n")[0]
    assert "групп без изменённых файлов: 1 (B)" in refusal
    assert "Артефакты серии" not in refusal
    assert _ncommits(repo) == n0


def test_mixed_group_commits_only_normal_paths(tmp_path: Path):
    """(в) смешанная группа: артефактные глобы игнорируются, коммит — только обычные пути."""
    table = """
| группа | файлы (glob) | сообщение коммита | rationale |
|---|---|---|---|
| M | `a.py`, `pipeline/demo/*.md` | fix: m | смешанная |
"""
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, table, {"a.py": "1"})
    assert _cbp(cfg) == 0
    assert _ncommits(repo) == n0 + 1
    assert _git_out(repo, "show", "--name-only", "--format=", "HEAD").split() == ["a.py"]
    assert "?? pipeline/demo/task.md" in _git_out(repo, "status", "--porcelain", "--untracked-files=all")


def test_mixed_group_without_normal_files_is_still_empty(tmp_path: Path, capsys):
    """(в) смешанная группа без обычных файлов — «пустая», отказ (оценка по обычным глобам)."""
    table = """
| группа | файлы (glob) | сообщение коммита | rationale |
|---|---|---|---|
| M | `b.py`, `pipeline/demo/*.md` | fix: m | смешанная |
"""
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, table, {})
    assert _cbp(cfg) == 1
    out = capsys.readouterr().out
    refusal = out.split("ОТКАЗ --yes: ")[1].split("\n")[0]
    assert "групп без изменённых файлов: 1 (M)" in refusal
    assert _ncommits(repo) == n0


def test_dry_run_marks_artifact_group(tmp_path: Path, capsys):
    """(г) dry-run: у артефактной группы пометка «коммитятся отдельно», а не «нет файлов»."""
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, ART_TABLE, {"a.py": "1"})
    ns = types.SimpleNamespace(task="demo", version=1, dry_run=True, yes=False, allow_partial=False)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(cfg, ns)
    assert (ei.value.code or 0) == 0                      # план чист — dry-run без хвостов
    out = capsys.readouterr().out
    assert "(артефакты серии — коммитятся отдельно)" in out
    assert "(нет изменённых файлов под глобами группы)" not in out
    assert _ncommits(repo) == n0


def test_numbered_commit_plan_header_still_parsed():
    """(д) регресс: нумерованный заголовок `## 4. commit_plan` разбирается."""
    report = "## 4. commit_plan\n| файлы | сообщение |\n|---|---|\n| `a.py` | feat: a |\n"
    groups, warnings = duo.parse_commit_plan(report)
    assert warnings == []
    assert [g["globs"] for g in groups] == [["a.py"]]
    assert groups[0]["message"] == "feat: a"


@pytest.mark.parametrize("globs,expected", [
    (["pipeline/demo/plan_v1.md"], True),                 # точный путь артефакта
    (["pipeline/demo/*"], True),                          # глоб внутри артефактов серии
    (["pipeline/demo"], True),                            # сам каталог артефактов
    (["a.py"], False),                                    # обычный путь
    (["pipeline/*"], False),                              # накрывает чужие задачи — не артефактные
    (["pipeline/demo/x.md", "a.py"], False),              # смешанная группа — не artifact-only
    ([], False),                                          # vacuous all() подстрахован
])
def test_artifact_only_group_classification(globs: list[str], expected: bool):
    g = {"name": "G", "globs": globs, "message": "m", "rationale": ""}
    assert duo.artifact_only_group(g, "pipeline/demo") is expected
