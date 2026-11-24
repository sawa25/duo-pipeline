"""pipework2: доработка дуо-оркестратора — баги P1–P12/P15 и rework/hold.

Паттерн test_duo_pipev3.py: оркестратор грузится importlib-ом (общий модуль
`duo` из test_duo_pipev3), git-песочницы — в tmp_path, живой transport заменён
скриптованным, реальные repo/ и pipeline/ не читаются, реальных LLM-вызовов нет.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import os
import re
from pathlib import Path

import pytest

from tests.unit.test_duo_pipev3 import (
    PROVISION_PATH,
    ScriptedTransport,
    _cfg,
    _git,
    _head,
    _init_repo,
    _mk_task,
    duo,
)


def _ns(task: str = "demo", verdict: str = "ACCEPT", note: str = "тест", phase: str | None = None):
    return argparse.Namespace(task=task, verdict=verdict, note=note, phase=phase)


def _set_state(td: Path, **fields) -> dict:
    st = duo.load_json(td / "state.json")
    st.update(fields)
    duo.save_json(td / "state.json", st)
    return st


# --- P4: атомарный save_json ----------------------------------------------------


def test_save_json_atomic_keeps_old_on_failed_replace(tmp_path: Path, monkeypatch):
    target = tmp_path / "state.json"
    duo.save_json(target, {"a": 1})

    def boom(src, dst):
        raise OSError("диск отвалился посреди замены")

    monkeypatch.setattr(duo.os, "replace", boom)
    with pytest.raises(OSError):
        duo.save_json(target, {"a": 2})
    assert duo.load_json(target) == {"a": 1}                       # старый файл цел
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # tmp-хвостов нет


def test_save_json_leaves_no_tmp_files(tmp_path: Path):
    target = tmp_path / "state.json"
    duo.save_json(target, {"ключ": "значение"})
    duo.save_json(target, {"ключ": "значение2"})
    assert duo.load_json(target) == {"ключ": "значение2"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# --- P3: set-verdict под .lock --------------------------------------------------


def test_set_verdict_refused_under_live_lock(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="needs_human", stop_reason="эскалация: plan не сходится 2 итераций — см. escalation.md")
    before = (td / "state.json").read_text(encoding="utf-8")
    cfg = _cfg(tmp_path, repo)
    with open(td / ".lock", "w") as held:         # «живой оркестратор»
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as ei:
            duo.cmd_set_verdict(cfg, _ns(verdict="ACCEPT"))
    assert "уже выполняется" in str(ei.value)
    assert (td / "state.json").read_text(encoding="utf-8") == before   # state не тронут


# --- P9/P2: set-verdict из needs_human ------------------------------------------


@pytest.mark.parametrize("reason, esc_phase, expect_phase", [
    ("эскалация: plan не сходится 2 итераций — см. escalation.md", "plan", "impl_pending"),
    ("эскалация: impl не сходится 2 итераций — см. escalation.md", "impl", "done"),
    ("исчерпан лимит итераций плана (4)", None, "impl_pending"),
    ("исчерпан лимит итераций имплементации (4)", None, "done"),
    ("артефакт ревью фазы plan не найден — ревью не запускалось (x)", None, "impl_pending"),
    ("артефакт ревью фазы impl не найден — ревью не запускалось (x)", None, "done"),
])
def test_set_verdict_accept_from_needs_human(tmp_path: Path, reason, esc_phase, expect_phase):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    extra = {"escalated_phase": esc_phase} if esc_phase else {}
    _set_state(td, phase="needs_human", stop_reason=reason, **extra)
    duo.cmd_set_verdict(_cfg(tmp_path, repo), _ns(verdict="ACCEPT"))
    st = duo.load_json(td / "state.json")
    assert st["phase"] == expect_phase
    assert st["verdicts"][-1]["verdict"] == "ACCEPT"


def test_escalated_phase_field_beats_stop_reason_substring(tmp_path: Path):
    """P9: stop_reason без слов plan/impl не ломает определение фазы."""
    st = {"phase": "needs_human", "stop_reason": "эскалация: серия не сходится", "escalated_phase": "plan"}
    assert duo.needs_human_phase(st) == "plan"
    st2 = {"phase": "needs_human", "stop_reason": "эскалация: impl не сходится"}   # старый state
    assert duo.needs_human_phase(st2) == "impl"


def test_set_verdict_unknown_reason_requires_phase(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="needs_human", stop_reason="solo: исчерпан лимит вызовов (24)")
    before = (td / "state.json").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        duo.cmd_set_verdict(_cfg(tmp_path, repo), _ns(verdict="ACCEPT"))
    assert "--phase" in str(ei.value)
    assert (td / "state.json").read_text(encoding="utf-8") == before
    duo.cmd_set_verdict(_cfg(tmp_path, repo), _ns(verdict="ACCEPT", phase="impl"))
    assert duo.load_json(td / "state.json")["phase"] == "done"


def test_escalation_writes_escalated_phase(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo, limits={"max_plan_iterations": 9, "max_impl_iterations": 4,
                                        "budget_usd": 100.0, "escalation_after": 1})
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport()
    _set_state(td, plan_version=1, plan_iteration=0)
    p.run(force_continue=True)
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "needs_human" and st["escalated_phase"] == "plan"


# --- P5: путь диффа в revise_impl -----------------------------------------------


def test_revise_impl_prompt_diff_path_exists(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    (td / "impl_report_v1.md").write_text("# отчёт\n", encoding="utf-8")
    # pipework3: лимит проверяется на границе ПОСЛЕ ревью — при лимите 2 исполнитель правит
    # ровно один раз (после rev001), rev002 доводится до конца, стоп — до второй правки
    cfg = _cfg(tmp_path, repo, limits={"max_plan_iterations": 4, "max_impl_iterations": 2,
                                        "budget_usd": 100.0})
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    _set_state(td, phase="impl", plan_version=1, impl_version=1, impl_iteration=0,
               impl_base_head=_head(repo))
    p.run(force_continue=True)
    revise = [c for c in t.calls if "текущий дифф" in c]
    assert len(revise) == 1
    m = re.search(r"текущий дифф `([^`]+)`", revise[0])
    assert m, revise[0][-300:]
    rel = m.group(1)
    assert rel == "pipeline/demo/diff_impl_v1.patch"      # без удвоенного <task>
    assert (repo / rel).exists()


# --- P12: нет артефакта ревью -> needs_human, ревью не тратится -----------------


def test_missing_plan_halts_to_needs_human_without_review(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    _set_state(td, plan_version=3, plan_iteration=2, rev_counter_plan=2)   # plan_v3.md нет
    p.run(force_continue=True)
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "needs_human"
    assert "фазы plan не найден" in st["stop_reason"]
    assert t.calls == []                         # ни одного вызова модели
    assert st["rev_counter_plan"] == 2           # номер ревью возвращён
    assert not list(td.glob("rev003_plan.md"))


def test_missing_impl_report_halts_to_needs_human(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    _set_state(td, phase="impl", plan_version=1, impl_version=2, impl_iteration=1,
               rev_counter_impl=1, impl_base_head=_head(repo))
    p.run(force_continue=True)
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "needs_human" and "фазы impl не найден" in st["stop_reason"]
    assert t.calls == []
    assert st["rev_counter_impl"] == 1
    # и ручной ACCEPT опознаёт фазу по этой причине
    assert duo.needs_human_phase(st) == "impl"


# --- P8: симлинки provisioning не попадают в uncovered --------------------------


def test_provision_script_has_no_hardcoded_targets():
    """R4 (duoportable): набор имён уехал в argv (git.worktree_links конфига) —
    в скрипте больше НЕТ константы LINK_TARGETS, модуль оркестратора тоже её не знает."""
    src = PROVISION_PATH.read_text(encoding="utf-8")
    assert "LINK_TARGETS" not in src
    assert not hasattr(duo, "PROVISION_LINK_TARGETS")


def test_collect_commit_groups_ignores_provisioning_symlinks(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pipeline")
    heavy = tmp_path / "heavy"
    heavy.mkdir()
    for name in ("data", "models", "node_modules"):
        (repo / name).symlink_to(heavy, target_is_directory=True)
    (repo / "README.md").write_text("modified", encoding="utf-8")
    (repo / "models_notes.txt").write_text("реальный untracked-файл", encoding="utf-8")
    td = tmp_path / "td"
    td.mkdir()
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | README.md | fix: правка | п.1 |\n", encoding="utf-8")
    # R4: имена передаются параметром (раньше — глобальная константа в duo_pipeline)
    links = ("data", "models", "node_modules")
    snap = duo.collect_commit_groups(repo, td, 1, "pipeline/demo", links)
    assert snap["uncovered"] == ["models_notes.txt"]           # реальный файл виден
    for name in links:
        assert name not in snap["changed"]
    # БЕЗ имён те же корневые симлинки — обычный untracked (фильтр не слеп)
    snap2 = duo.collect_commit_groups(repo, td, 1, "pipeline/demo")
    assert set(links) <= set(snap2["changed"])
    # настоящий каталог с таким именем (не симлинк) фильтром НЕ скрывается
    (repo / "docs_real").mkdir()
    assert not duo.is_provisioned_symlink(repo, "docs_real", links)
    assert duo.is_provisioned_symlink(repo, "models", links)
    assert not os.path.islink(repo / "README.md")


# --- P6: задание в промптах без молчаливой обрезки ------------------------------

TAIL_MARK = "ХВОСТ-ЗАДАНИЯ-УНИКАЛЬНАЯ-МЕТКА"


def _long_task(n: int = 12000) -> str:
    return ("начало задания\n" + "х" * n + f"\n## Аддендум\n{TAIL_MARK}\n")


def _impl_artifacts(repo: Path, v: int = 1) -> str:
    for name in (f"impl_report_v{v}.md", f"diff_impl_v{v}.patch", f"untracked_impl_v{v}.txt"):
        f = repo / "pipeline" / "demo" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("stub\n", encoding="utf-8")
    return (f"pipeline/demo/impl_report_v{v}.md и дифф pipeline/demo/diff_impl_v{v}.patch")


def _run_review(tmp_path: Path, repo: Path, t: ScriptedTransport, *, phase: str, task_text: str,
                plan_rel: str = "", name: str = "td"):
    td = tmp_path / name
    td.mkdir(exist_ok=True)
    cfg = _cfg(tmp_path, repo)
    if phase == "impl":
        artifact = _impl_artifacts(repo)
    else:
        artifact = "pipeline/demo/plan_v1.md"
        (repo / "pipeline" / "demo").mkdir(parents=True, exist_ok=True)
        (repo / artifact).write_text("# план\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    return reviewer.review(
        artifact_rel=artifact, phase=phase, task_text=task_text,
        members=["alpha", "beta"], rounds=3, rev_file=td / "rev001.md",
        guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo", plan_rel=plan_rel,
    )


@pytest.mark.parametrize("phase", ["plan", "impl"])
def test_long_task_visible_in_r1_and_chairman(tmp_path: Path, phase: str):
    repo = _init_repo(tmp_path)
    t = ScriptedTransport()
    _run_review(tmp_path, repo, t, phase=phase, task_text=_long_task())
    r1 = [c for c in t.calls if "участник структурированного дуо-консилиума" in c]
    chair = [c for c in t.calls if "Председатель консилиума" in c]
    assert len(r1) == 2 and len(chair) >= 1
    assert all(TAIL_MARK in c for c in r1 + chair)
    assert all("текст задания обрезан" not in c for c in r1 + chair)


def test_long_task_visible_to_executor_implement(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = ScriptedTransport()
    (tmp_path / "td").mkdir()
    ex = duo.Executor(_cfg(tmp_path, repo), t, tmp_path / "td", repo, {"artifacts_rel": "pipeline/demo"})
    ex.implement(_long_task(), "pipeline/demo/plan_v1.md", 1)
    assert TAIL_MARK in t.calls[0] and "текст задания обрезан" not in t.calls[0]


def test_task_for_prompt_safeguard_marks_truncation():
    short = "x" * 100
    assert duo.task_for_prompt(short) == short                       # байты не меняются
    huge = "y" * (duo.TASK_TEXT_SAFEGUARD + 500)
    out = duo.task_for_prompt(huge, path_hint="pipeline/demo/task.md")
    assert out.startswith("y" * duo.TASK_TEXT_SAFEGUARD)
    assert "текст задания обрезан" in out and "pipeline/demo/task.md" in out
    assert str(len(huge)) in out


# --- P7: эрраты плана в промптах impl-фазы --------------------------------------

PLAN_WITH_ERRATA = """# План v1

## Шаги
1. сделать А.

## Эрраты эксперта (приёмка 13.10) — ОБЯЗАТЕЛЬНЫ к реализации

| Э | суть |
|---|---|
| Э1 | счётчик Z считать по ключу ERRATA_MARK_E1 |
| Э2 | не удалять ключ legacy |
"""

PLAN_NO_ERRATA = "# План v1\n\n## Шаги\n1. сделать А.\n\n## Риски\nнет эррат как таковых.\n"


def test_extract_errata_variants():
    assert duo.extract_errata(PLAN_NO_ERRATA) == ""
    got = duo.extract_errata(PLAN_WITH_ERRATA)
    assert got.startswith("## Эрраты эксперта") and got.rstrip().endswith("не удалять ключ legacy |")
    assert "## Шаги" not in got                                      # только раздел до конца файла
    assert duo.extract_errata("текст\n## ЭРРАТЫ ЭКСПЕРТА (13.10)\nЭ1 — x\n").startswith("## ЭРРАТЫ ЭКСПЕРТА")
    assert duo.extract_errata("### эрраты\nЭ1\n").startswith("### эрраты")
    assert duo.extract_errata("## Эрратум\nнет\n") == ""             # другое слово
    assert duo.extract_errata("в тексте слово Эрраты, но не заголовок\n") == ""


def _plan_in_repo(repo: Path, text: str) -> str:
    d = repo / "pipeline" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan_v1.md").write_text(text, encoding="utf-8")
    return "pipeline/demo/plan_v1.md"


def test_impl_review_prompts_contain_errata_and_checklist(tmp_path: Path):
    repo = _init_repo(tmp_path)
    plan_rel = _plan_in_repo(repo, PLAN_WITH_ERRATA)
    t = ScriptedTransport()
    _run_review(tmp_path, repo, t, phase="impl", task_text="задача", plan_rel=plan_rel)
    r1 = [c for c in t.calls if "участник структурированного дуо-консилиума" in c]
    chair = [c for c in t.calls if "Председатель консилиума" in c]
    for c in r1 + chair:
        assert "ERRATA_MARK_E1" in c and plan_rel in c
        assert "Чек-лист эррат" in c
    assert all("БЛОКЕР" in c for c in r1)
    assert all("таблица «эрраты → реализация»" in c for c in r1)       # правило эксперта 13.10
    assert "таблицы «эрраты → реализация»" in chair[0]
    assert all("REVISE" in c and "нереализованная эррата" in c.lower() for c in chair[:1])


def test_impl_review_prompts_unchanged_without_errata(tmp_path: Path):
    """Без раздела «Эрраты» байты промптов те же, передан plan_rel или нет."""
    repo = _init_repo(tmp_path)
    plan_rel = _plan_in_repo(repo, PLAN_NO_ERRATA)
    t1, t2 = ScriptedTransport(), ScriptedTransport()
    _run_review(tmp_path, repo, t1, phase="impl", task_text="задача", plan_rel=plan_rel, name="td1")
    _run_review(tmp_path, repo, t2, phase="impl", task_text="задача", plan_rel="", name="td2")
    assert sorted(t1.calls) == sorted(t2.calls)
    assert all("Чек-лист эррат" not in c for c in t1.calls)


def test_plan_phase_review_ignores_errata(tmp_path: Path):
    repo = _init_repo(tmp_path)
    plan_rel = _plan_in_repo(repo, PLAN_WITH_ERRATA)
    t = ScriptedTransport()
    _run_review(tmp_path, repo, t, phase="plan", task_text="задача", plan_rel=plan_rel)
    assert all("Чек-лист эррат" not in c for c in t.calls)


def test_executor_implement_and_revise_carry_errata(tmp_path: Path):
    repo = _init_repo(tmp_path)
    plan_rel = _plan_in_repo(repo, PLAN_WITH_ERRATA)
    cfg = _cfg(tmp_path, repo)
    state = {"artifacts_rel": "pipeline/demo"}
    (tmp_path / "td").mkdir()
    t = ScriptedTransport()
    ex = duo.Executor(cfg, t, tmp_path / "td", repo, state)
    ex.implement("задача", plan_rel, 1)
    rev = tmp_path / "rev001_impl.md"
    rev.write_text("# rev\n\nVERDICT: REVISE\n", encoding="utf-8")
    ex.revise_impl("задача", plan_rel, 2, rev, "pipeline/demo/diff_impl_v1.patch")
    impl_p, revise_p = t.calls[0], t.calls[1]
    for p in (impl_p, revise_p):
        assert "ERRATA_MARK_E1" in p and "эрраты → реализация" in p
    assert "заново прочитай план" in impl_p                            # resume-сессия не помнит эрраты
    # без эррат — ни слова про таблицу
    plan2 = _plan_in_repo(repo, PLAN_NO_ERRATA)
    t2 = ScriptedTransport()
    duo.Executor(cfg, t2, tmp_path / "td", repo, state).implement("задача", plan2, 1)
    assert "эрраты → реализация" not in t2.calls[0] and "ЭРРАТЫ ПЛАНА" not in t2.calls[0]


# --- этап 1: rework / set-verdict REVISE / P15 ----------------------------------

from tests.unit.test_duo_pipev3 import CHAIR_REPORT  # noqa: E402

CHAIR_ACCEPT = CHAIR_REPORT.replace("VERDICT: REVISE", "VERDICT: ACCEPT")
EXT_REVIEW = "# Экспертное ревью\n\n## 3. Сводка для исполнителя\n- Исправить EXT_MARK_ZZ.\n"


class AcceptingTransport(ScriptedTransport):
    """Как ScriptedTransport, но председатель выдаёт ACCEPT."""

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        if "Председатель консилиума" in prompt and not self.responses:
            self.responses = [CHAIR_ACCEPT]
        out = super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                           resume_session=resume_session, tag=tag)
        if mode == "full" and cwd:
            # исполнитель «пишет» ВСЕ упомянутые в промпте артефакты (план/отчёт новой версии)
            for rel in re.findall(r"(pipeline/[\w/]+/(?:plan_v\d+|impl_report_v\d+)\.md)", prompt):
                f = Path(cwd) / rel
                if not f.exists():
                    f.write_text(f"# stub {rel}\n", encoding="utf-8")
        return out


def _cfg2(tmp_path: Path, repo: Path, **limits) -> dict:
    lim = {"max_plan_iterations": 4, "max_impl_iterations": 4, "budget_usd": 100.0}
    lim.update(limits)
    cfg = _cfg(tmp_path, repo, limits=lim)
    cfg["auto_proceed_to_impl"] = False
    return cfg


def _rw_ns(task="demo", phase="plan", review="", keep_session=False, run=False, force=False,
           config="/nonexistent/cfg.json"):
    return argparse.Namespace(task=task, phase=phase, review=str(review), keep_session=keep_session,
                              run=run, force=force, config=config)


def _task_with_reviews(repo: Path, phase: str, n: int = 2) -> Path:
    td = _mk_task(repo)
    for i in range(1, n + 1):
        (td / f"rev{i:03d}_{phase}.md").write_text(f"# rev{i}\n\nREV{i}_MARK\n\nVERDICT: REVISE\n", encoding="utf-8")
    return td


def test_last_review_name_numeric_order(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _task_with_reviews(repo, "plan", 3)
    (td / "rev004_impl.md").write_text("x", encoding="utf-8")
    (td / "rev005_custom.md").write_text("x", encoding="utf-8")
    assert duo.last_review_name(td, "plan") == "rev003_plan.md"
    assert duo.last_review_name(td, "impl") == "rev004_impl.md"
    assert duo.last_review_name(td, "nope") is None


@pytest.mark.parametrize("reason, esc_phase, phase", [
    ("эскалация: plan не сходится 2 итераций — см. escalation.md", "plan", "plan"),
    ("эскалация: impl не сходится 2 итераций — см. escalation.md", "impl", "impl"),
    ("исчерпан лимит итераций плана (4)", None, "plan"),
    ("исчерпан лимит итераций имплементации (4)", None, "impl"),
])
def test_set_verdict_revise_from_needs_human_enters_rework(tmp_path: Path, reason, esc_phase, phase):
    repo = _init_repo(tmp_path)
    td = _task_with_reviews(repo, phase)
    (td / "escalation.md").write_text("# эскалация", encoding="utf-8")
    extra = {"escalated_phase": esc_phase} if esc_phase else {}
    _set_state(td, phase="needs_human", stop_reason=reason, plan_iteration=2, impl_iteration=3,
               executor_session="SESS-1", **extra)
    duo.cmd_set_verdict(_cfg2(tmp_path, repo), _ns(verdict="REVISE", note="доработать"))
    st = duo.load_json(td / "state.json")
    assert st["phase"] == phase
    assert st["rework"]["phase"] == phase and st["rework"]["rev"] == f"rev002_{phase}.md"
    assert st["iter_base"]["plan"] == 2 and st["iter_base"]["impl"] == 3
    assert "executor_session" not in st                          # свежая сессия
    assert "escalated_phase" not in st
    assert not (td / "escalation.md").exists()                   # старая эскалация архивирована
    assert len(list(td.glob("escalation_*.md"))) == 1


def test_set_verdict_revise_needs_human_without_rev_refused(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="needs_human", stop_reason="исчерпан лимит итераций плана (4)")
    before = (td / "state.json").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        duo.cmd_set_verdict(_cfg2(tmp_path, repo), _ns(verdict="REVISE"))
    assert "rework" in str(ei.value)
    assert (td / "state.json").read_text(encoding="utf-8") == before


def test_set_verdict_revise_outside_needs_human_only_records(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _task_with_reviews(repo, "plan")
    _set_state(td, phase="impl_pending", plan_version=1)
    duo.cmd_set_verdict(_cfg2(tmp_path, repo), _ns(verdict="REVISE"))
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "impl_pending" and "rework" not in st
    assert st["verdicts"][-1]["verdict"] == "REVISE"
    assert "rework --task demo" in capsys.readouterr().out


def test_run_in_needs_human_does_not_call_models(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="needs_human", stop_reason="исчерпан лимит итераций плана (4)", plan_version=1)
    p = duo.Pipeline(_cfg2(tmp_path, repo), "demo")
    p.t = ScriptedTransport()
    p.run(force_continue=True)
    assert p.t.calls == []
    assert "needs_human" in (td / "orchestrator.log").read_text(encoding="utf-8")
    assert duo.load_json(td / "state.json")["phase"] == "needs_human"


def test_rework_plan_executor_first_then_review_relative_counters(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n", encoding="utf-8")
    # лимит 4 уже «исчерпан» сырым счётчиком — без iter_base ревью бы не началось
    _set_state(td, phase="impl_pending", plan_version=1, plan_iteration=4, rev_counter_plan=4,
               executor_session="SESS-OLD")
    ext = tmp_path / "expert_review.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    cfg = _cfg2(tmp_path, repo)
    duo.cmd_rework(cfg, _rw_ns(phase="plan", review=ext))
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "plan" and st["rework"]["rev"] == "rev005_plan.md"
    assert st["iter_base"]["plan"] == 4 and "executor_session" not in st
    reg = (td / "rev005_plan.md").read_text(encoding="utf-8")
    assert "EXT_MARK_ZZ" in reg and reg.rstrip().endswith("VERDICT: REVISE")   # строка вердикта дописана
    assert st["verdicts"][-1] == {"rev": "rev005_plan.md", "phase": "plan", "verdict": "REVISE",
                                  "note": "rework: внешнее ревью expert_review.md"}

    p = duo.Pipeline(cfg, "demo")
    t = AcceptingTransport()
    p.t = t
    p.run(force_continue=False)
    st = duo.load_json(td / "state.json")
    # первым вызовом — исполнитель с текстом внешнего ревью, не ревьюер
    assert "Отчёт ревью надсмотрщика по твоему плану" in t.calls[0] and "EXT_MARK_ZZ" in t.calls[0]
    assert all("участник структурированного дуо-консилиума" not in t.calls[0] for _ in [0])
    assert st["plan_version"] == 2 and (td / "plan_v2.md").exists()
    assert "rework" not in st                                    # маркер снят
    assert st["phase"] == "impl_pending"                         # ревью новой версии → ACCEPT
    assert st["plan_iteration"] == 5                             # сырой счётчик монотонен
    reviewers = [c for c in t.calls if "участник структурированного дуо-консилиума" in c]
    assert reviewers and all("plan_v2.md" in c for c in reviewers)


def test_rework_impl_executor_first_with_diff_and_new_report(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    (td / "impl_report_v2.md").write_text("# отчёт v2\n", encoding="utf-8")
    _set_state(td, phase="needs_human", stop_reason="исчерпан лимит итераций имплементации (4)",
               plan_version=1, impl_version=2, impl_iteration=4, rev_counter_impl=4,
               impl_base_head=_head(repo))
    ext = tmp_path / "expert_impl_review.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    cfg = _cfg2(tmp_path, repo)
    duo.cmd_rework(cfg, _rw_ns(phase="impl", review=ext))
    p = duo.Pipeline(cfg, "demo")
    t = AcceptingTransport()
    p.t = t
    p.run()
    st = duo.load_json(td / "state.json")
    first = t.calls[0]
    assert "Отчёт ревью надсмотрщика по твоей имплементации" in first and "EXT_MARK_ZZ" in first
    assert "pipeline/demo/diff_impl_v2.patch" in first and (td / "diff_impl_v2.patch").exists()
    assert "pipeline/demo/impl_report_v3.md" in first
    assert st["impl_version"] == 3 and "rework" not in st
    assert st["phase"] == "done"                                 # ревью v3 → ACCEPT


def test_rework_keep_session_flag(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    _set_state(td, phase="impl_pending", plan_version=1, executor_session="SESS-KEEP")
    ext = tmp_path / "e.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    duo.cmd_rework(_cfg2(tmp_path, repo), _rw_ns(phase="plan", review=ext, keep_session=True))
    assert duo.load_json(td / "state.json")["executor_session"] == "SESS-KEEP"


def test_rework_refusals(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg2(tmp_path, repo)
    ext = tmp_path / "e.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    accept = tmp_path / "acc.md"
    accept.write_text("# ok\n\nVERDICT: ACCEPT\n", encoding="utf-8")

    def attempt(**kw):
        before = (td / "state.json").read_text(encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            duo.cmd_rework(cfg, _rw_ns(**kw))
        assert (td / "state.json").read_text(encoding="utf-8") == before   # state не тронут
        return str(ei.value)

    assert "plan_version=0" in attempt(phase="plan", review=ext)           # плана нет (контейнер)
    assert "impl_version=0" in attempt(phase="impl", review=ext)
    _set_state(td, phase="done", plan_version=1, impl_version=1)
    assert "phase=done" in attempt(phase="impl", review=ext)
    assert "не найден" in attempt(phase="impl", review=tmp_path / "нет.md", force=True)
    assert "ACCEPT" in attempt(phase="impl", review=accept, force=True)
    with open(td / ".lock", "w") as held:                                  # живой оркестратор
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "уже выполняется" in attempt(phase="impl", review=ext, force=True)
    # force разрешает done
    duo.cmd_rework(cfg, _rw_ns(phase="impl", review=ext, force=True))
    assert duo.load_json(td / "state.json")["phase"] == "impl"


def test_rework_run_flag_releases_lock_and_passes_own_argv(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    _set_state(td, phase="impl_pending", plan_version=1)
    ext = tmp_path / "e.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    seen = {}

    def fake_detached(cfg, task, argv=None):
        seen["argv"] = argv
        with open(td / ".lock", "w") as probe:       # блокировка ДОЛЖНА быть уже снята
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setattr(duo, "run_detached", fake_detached)
    duo.cmd_rework(_cfg2(tmp_path, repo), _rw_ns(phase="plan", review=ext, run=True, config="/x/cfg.json"))
    assert seen["argv"] == ["--config", "/x/cfg.json", "run", "--task", "demo"]


def test_stale_rework_marker_for_other_phase_is_dropped(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    _set_state(td, phase="plan", plan_version=1,
               rework={"phase": "impl", "rev": "rev001_impl.md", "source": "x"})
    p = duo.Pipeline(_cfg2(tmp_path, repo), "demo")
    t = AcceptingTransport()
    p.t = t
    p.run()
    st = duo.load_json(td / "state.json")
    assert "rework" not in st
    # плана не переписывали (маркер чужой фазы): первым вызовом идёт ревью v1
    assert "участник структурированного дуо-консилиума" in t.calls[0]


# --- P15: эскалация не гасит цикл молча -----------------------------------------


def test_p15_existing_escalation_md_does_not_silently_halt(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    (td / "escalation.md").write_text("# прежняя эскалация\n", encoding="utf-8")
    cfg = _cfg2(tmp_path, repo, escalation_after=1, max_plan_iterations=3)
    _set_state(td, plan_version=1, plan_iteration=0)
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()                              # ревьюеры всегда REVISE
    p.t = t
    p.run(force_continue=True)
    st = duo.load_json(td / "state.json")
    # цикл дошёл до ЛИМИТА итераций (а не встал молча после первой итерации с phase=plan)
    assert st["phase"] == "needs_human" and "лимит итераций плана" in st["stop_reason"]
    # pipework3: стоп — на границе ПОСЛЕ ревью: v4 не создаётся (раньше v4 делался и умирал неревьюированным)
    assert st["plan_iteration"] == 3 and st["plan_version"] == 3
    assert st["halt"]["kind"] == "limit" and st["halt"]["phase"] == "plan"
    assert (td / "escalation.md").read_text(encoding="utf-8") == "# прежняя эскалация\n"   # не перезаписан
    assert "эскалация фазы plan пропущена" in (td / "orchestrator.log").read_text(encoding="utf-8")


def test_after_rework_new_escalation_after_relative_iterations(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    cfg = _cfg2(tmp_path, repo, escalation_after=2, max_plan_iterations=9)
    # серия уже эскалировала на 2-й итерации; человек делает REVISE
    (td / "rev001_plan.md").write_text("# r\n\nVERDICT: REVISE\n", encoding="utf-8")
    (td / "escalation.md").write_text("# старая эскалация\n", encoding="utf-8")
    _set_state(td, phase="needs_human", stop_reason="эскалация: plan не сходится 2 итераций — см. escalation.md",
               escalated_phase="plan", plan_version=1, plan_iteration=5, rev_counter_plan=1)
    duo.cmd_set_verdict(cfg, _ns(verdict="REVISE"))
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    p.run()
    st = duo.load_json(td / "state.json")
    # плановая итерация после rework: 6 (eff 1) — без эскалации; 7 (eff 2) — эскалация
    assert st["plan_iteration"] == 7
    assert st["phase"] == "needs_human" and st["escalated_phase"] == "plan"
    assert (td / "escalation.md").exists()                       # НОВАЯ эскалация (старая в архиве)
    assert "старая эскалация" not in (td / "escalation.md").read_text(encoding="utf-8")
    assert len(list(td.glob("escalation_*.md"))) == 1


def test_old_accept_does_not_suppress_escalation_after_rework(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_cfg2(tmp_path, repo, escalation_after=1), "demo")
    st = {"plan_iteration": 3, "verdicts": [{"phase": "plan", "verdict": "ACCEPT"}],
          "iter_base": {"plan": 2, "impl": 0, "verdicts": 1}}
    rev = td / "rev001_plan.md"
    rev.write_text("# r\n", encoding="utf-8")
    p.t = ScriptedTransport()
    # ACCEPT до iter_base.verdicts не гасит: эскалация обязана выполниться
    assert p._escalate_if_due(st, "plan", rev) is True
    # без iter_base — прежняя семантика: ACCEPT фазы гасит эскалацию
    st2 = {"plan_iteration": 3, "verdicts": [{"phase": "plan", "verdict": "ACCEPT"}]}
    assert p._escalate_if_due(st2, "plan", rev) is False


# --- P11: hold / release ----------------------------------------------------------


def _hold_ns(task="demo"):
    return argparse.Namespace(task=task)


def test_hold_release_touch_only_flag(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="impl_pending", plan_version=2, state_updated_at=123.0)
    cfg = _cfg2(tmp_path, repo)
    duo.cmd_hold(cfg, _hold_ns(), True)
    st = duo.load_json(td / "state.json")
    assert st["hold_impl"] is True
    assert st["phase"] == "impl_pending" and st["plan_version"] == 2
    assert st["state_updated_at"] == 123.0                       # heartbeat не подделывается
    duo.cmd_hold(cfg, _hold_ns(), False)
    assert "hold_impl" not in duo.load_json(td / "state.json")
    duo.cmd_hold(cfg, _hold_ns(), False)                         # идемпотентно


def test_hold_works_under_live_lock(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg2(tmp_path, repo)
    with open(td / ".lock", "w") as held:                        # «живой оркестратор»
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        duo.cmd_hold(cfg, _hold_ns(), True)
    assert duo.load_json(td / "state.json")["hold_impl"] is True


def test_hold_survives_orchestrator_save_state(tmp_path: Path):
    """Оркестратор прочитал state ДО hold и пишет ПОСЛЕ — флаг не затирается."""
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg2(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()                                          # в памяти hold_impl нет
    duo.cmd_hold(cfg, _hold_ns(), True)                          # человек ставит hold
    st["plan_iteration"] = 3
    p.save_state(st)
    disk = duo.load_json(td / "state.json")
    assert disk["hold_impl"] is True and disk["plan_iteration"] == 3


def test_release_survives_orchestrator_save_state(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg2(tmp_path, repo)
    duo.cmd_hold(cfg, _hold_ns(), True)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()                                          # в памяти hold_impl=True
    duo.cmd_hold(cfg, _hold_ns(), False)                         # человек снимает
    p.save_state(st)
    assert "hold_impl" not in duo.load_json(td / "state.json")   # не воскресает из памяти оркестратора


def test_state_write_lock_serializes_writers(tmp_path: Path):
    """hold и save_state из разных потоков: ни одна запись не теряется."""
    import threading
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg2(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            st["counter"] = i
            p.save_state(st)

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(30):
            duo.cmd_hold(cfg, _hold_ns(), True)
            duo.cmd_hold(cfg, _hold_ns(), True)
            assert duo.load_json(td / "state.json").get("hold_impl") is True
            duo.cmd_hold(cfg, _hold_ns(), False)
            assert "hold_impl" not in duo.load_json(td / "state.json")
    finally:
        stop.set()
        th.join()
    final = duo.load_json(td / "state.json")                     # файл не побит (json читается)
    assert final["counter"] >= 1
