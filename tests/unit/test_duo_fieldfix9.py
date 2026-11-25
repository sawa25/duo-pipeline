"""fieldfix9: вторичные находки полевого отчёта волны 1, Н34 / Н35 / Н41 / Н43.

Н34  число эррат: «21 пунктов» при 9 пронумерованных эрратах (считались и вложенные подпункты).
Н35  solo/worktree: исполнитель по заданию пайплайна пишет `impl_report_v1.md` с `commit_plan`, а
     `commit-by-plan` без `--version` находит этот отчёт, хотя `impl_version` у solo остаётся 0.
Н41  audit: запись по РЕАЛЬНОМУ пути каталога артефактов (симлинк в worktree) — не нарушение.
Н43  commit-by-plan: пробел в glob (несколько путей в одном бэктик-спане) — подсказка, а не немое «пусто».
(Н40 — пульс по времени последнего вывода — в test_duo_fieldfix4.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from audit_cmd import _zone_verdict, audit_files, compile_rules
from tests.unit.test_duo_fieldfix import _cbp_repo
from tests.unit.test_duo_fieldfix2 import _cbp_ns
from tests.unit.test_duo_pipev3 import _cfg, duo
from tests.unit.test_duo_pipework3 import _ex, _Seq

# --- Н41 -----------------------------------------------------------------------------------------


def _symlinked_series(tmp_path: Path) -> tuple[Path, Path]:
    """main/pipeline/t — реальный каталог артефактов; wt/pipeline/t — симлинк на него (как у worktree-серии)."""
    main, wt = tmp_path / "main", tmp_path / "wt"
    (main / "pipeline" / "t").mkdir(parents=True)
    (main / "pipeline" / "other").mkdir()
    (main / "src").mkdir()
    (wt / "pipeline").mkdir(parents=True)
    (wt / "src").mkdir()
    os.symlink(main / "pipeline" / "t", wt / "pipeline" / "t")
    return main, wt


def test_zone_verdict_accepts_real_path_of_symlinked_artifacts_dir(tmp_path: Path):
    main, wt = _symlinked_series(tmp_path)
    zones = ["pipeline/t"]
    # путь основного дерева к артефакту серии — ровно то, что в поле давало ложные «нарушения»
    assert _zone_verdict(str(main / "pipeline" / "t" / "impl_report_v1.md"), wt, zones) is None
    assert _zone_verdict(str(main / "pipeline" / "t" / "sub" / "x.md"), wt, zones) is None
    assert _zone_verdict(str(main / "pipeline" / "t"), wt, zones) is None            # сам каталог
    # путь через симлинк в worktree — как и раньше
    assert _zone_verdict("pipeline/t/solo_report.md", wt, zones) is None


@pytest.mark.parametrize("rel", [
    "src/app.py",                        # код основного дерева — не зона артефактов
    "pipeline/other/x.md",               # чужая серия
    "pipeline/t/../other/x.md",          # выход из зоны через `..`
    "pipeline/tt/x.md",                  # префикс-двойник имени зоны
])
def test_zone_verdict_still_flags_writes_into_main_tree_outside_zone(tmp_path: Path, rel: str):
    main, wt = _symlinked_series(tmp_path)
    assert _zone_verdict(str(main / rel), wt, ["pipeline/t"]) == "вне дерева серии"


def test_audit_files_symlinked_artifacts_write_is_not_a_finding(tmp_path: Path):
    main, wt = _symlinked_series(tmp_path)

    def tu(uid: str, path: Path) -> dict:
        return {"type": "assistant", "timestamp": "2026-11-25T06:00:00.000Z", "message": {"content": [
            {"type": "tool_use", "id": uid, "name": "Write", "input": {"file_path": str(path), "content": "x"}}]}}

    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (
        tu("1", main / "pipeline" / "t" / "impl_report_v1.md"),
        tu("2", main / "pipeline" / "t" / "solo_report.md"),
        tu("3", main / "src" / "app.py"))) + "\n", encoding="utf-8")
    rules = compile_rules({"write_zones": ["pipeline/{task}/"]}, "t")
    res = audit_files([("s", log)], rules, wt)
    assert [(f["kind"], f["subject"].endswith("app.py")) for f in res["findings"]] == [("write", True)]


# --- Н34 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("info,expected", [
    ({"items": 5}, "5 п."),                                            # старые state — без top_items
    ({"items": 9, "top_items": 9}, "9 п."),                            # плоский список — одно число
    ({"items": 21, "top_items": 9}, "9 п. (строк списка с вложенными: 21)"),
    ({"items": 4, "top_items": 0}, "4 п."),                            # весь блок с отступом — верхнего уровня нет
])
def test_errata_count_text(info: dict, expected: str):
    assert duo.errata_count_text(info) == expected


def test_apply_errata_counts_top_level_items_apart_from_nested(tmp_path: Path):
    rev, plan = tmp_path / "rev001_plan.md", tmp_path / "plan_v1.md"
    rev.write_text(
        "# ревью\n\n## Эрраты к плану\n\n"
        "1. Порт сервиса брать из переменной CALLER_PORT_ZZ, а не из константы.\n"
        "   - подпункт: проверить значение по умолчанию в конфиге ZZ_ONE.\n"
        "   - подпункт: добавить тест на пустое значение ZZ_TWO.\n"
        "2. Журнал писать построчно с немедленным сбросом буфера на диск ZZ_THREE.\n"
        "   - подпункт: проверить права на каталог журнала ZZ_FOUR.\n\nVERDICT: ACCEPT\n", encoding="utf-8")
    plan.write_text("# план\n", encoding="utf-8")
    status, info = duo.apply_accept_errata(rev, plan)
    assert status == "applied" and info["items"] == 5 and info["top_items"] == 2
    assert duo.errata_count_text(info) == "2 п. (строк списка с вложенными: 5)"


def test_accepted_plan_summary_shows_both_errata_numbers():
    st = {"plan_version": 1, "verdicts": [{"phase": "plan", "verdict": "ACCEPT", "rev": "rev001_plan.md"}],
          "errata_applied": {"rev": "rev001_plan.md", "plan": "plan_v1.md", "items": 21, "top_items": 9},
          "spent_usd": 1.0}
    s = duo.accepted_plan_summary(st)
    assert "эрраты: 9 п. (строк списка с вложенными: 21) из rev001_plan.md" in s


# --- Н43 -----------------------------------------------------------------------------------------


def test_spaced_globs_hint_only_for_globs_with_whitespace():
    assert duo.spaced_globs_hint({"globs": ["src/*", "a/b.py"]}) == ""
    assert duo.spaced_globs_hint({"globs": []}) == "" and duo.spaced_globs_hint({}) == ""
    hint = duo.spaced_globs_hint({"globs": ["ops/a.sh ops/b.sh", "ok.py"]})
    assert "'ops/a.sh ops/b.sh'" in hint and "`a.sh`, `b.sh`" in hint and "ok.py" not in hint
    assert duo.spaced_globs_hint({"globs": ["ops/a.sh\tops/b.sh"]}) != ""         # любой пробельный символ


def test_dry_run_explains_empty_group_with_spaced_glob(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | `src/a.py src/b.py` | feat: код | п.1 |\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(dry_run=True))
    out = capsys.readouterr().out
    assert "нет изменённых файлов под глобами группы" in out and "в glob есть пробел" in out


def test_real_run_explains_empty_group_with_spaced_glob(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | `src/a.py src/b.py` | feat: код | п.1 |\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(yes=True, allow_partial=True))
    out = capsys.readouterr().out
    assert "пустой коммит не создаём" in out and "в glob есть пробел" in out


def test_dry_run_has_no_spaced_hint_for_regular_empty_group(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | `docs/*` | docs: x | п.1 |\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(dry_run=True))
    out = capsys.readouterr().out
    assert "нет изменённых файлов под глобами группы" in out and "в glob есть пробел" not in out


# --- Н35 -----------------------------------------------------------------------------------------


def _reports(td: Path, *names: str) -> None:
    for n in names:
        (td / n).write_text("x", encoding="utf-8")


@pytest.mark.parametrize("files,default,expected", [
    ((), 0, 0),                                                       # отчётов нет — прежнее «не найден»
    (("impl_report_v1.md",), 0, 1),                                   # solo: impl_version=0, отчёт v1
    (("impl_report_v0.md", "impl_report_v1.md"), 0, 0),               # заданная версия существует — её и берём
    (("impl_report_v1.md", "impl_report_v3.md", "impl_report_v10.md"), 0, 10),   # самый свежий по числу, не по строке
    (("impl_report_v1.md.bak", "impl_report_vX.md", "impl_report_v2.md.orig"), 0, 0),   # посторонние имена
    (("impl_report_v1.md", "impl_report_v5_old.md"), 0, 1),                            # суффикс в имени — не версия
])
def test_pick_report_version(tmp_path: Path, files, default, expected):
    _reports(tmp_path, *files)
    assert duo.pick_report_version(tmp_path, default) == expected


def test_commit_by_plan_without_version_finds_solo_report(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)                                    # отчёт impl_report_v1.md, state.impl_version нет (=0)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(version=None, dry_run=True))
    out = capsys.readouterr().out
    assert ei.value.code == 0
    assert "impl_report_v0.md нет — беру самый свежий impl_report_v1.md" in out
    assert "commit_plan: pipeline/demo/impl_report_v1.md, групп: 1" in out


def test_commit_by_plan_explicit_version_never_falls_back(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(version=3, dry_run=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and "файл отчёта не найден" in out and "impl_report_v3.md" in out
    assert "беру самый свежий" not in out


@pytest.mark.parametrize("isolation,expected", [("worktree", True), ("shared", False), (None, False)])
def test_solo_prompt_asks_for_commit_plan_report_only_in_worktree(tmp_path: Path, isolation, expected):
    t = _Seq([("готово\nSOLO_DONE", "sid-1", 0.1)])
    ex, state = _ex(tmp_path, t)
    state["mode"] = "solo"
    if isolation:
        state["isolation"] = isolation
    ex.solo_turn("задача: сделай x", 1)
    prompt = t.calls[0]["prompt"]
    assert ("pipeline/demo/impl_report_v1.md" in prompt and "## commit_plan" in prompt) is expected
    if expected:
        assert "| группа | файлы (glob) | сообщение коммита (1 строка) | rationale |" in prompt
        assert "НЕ `a.sh b.sh` в одном спане" in prompt
        assert prompt.index("## commit_plan") < prompt.index("Когда ВСЁ по DoD")   # просьба — до команды завершения
    assert "SOLO_DONE" in prompt                                             # протокол завершения не потерян
