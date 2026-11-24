"""pipework3 (21.10.2026): живучесть дуо-пайплайна по итогам боевого инцидента.

Что фиксируют тесты:
  * граница итерации: лимит итераций/бюджет проверяются ПОСЛЕ ревью и синтеза председателя
    (6 из 6 прежних остановок по лимиту имели форму «доработка сделана → стоп до её ревью»);
  * halt: любая ошибка (транспорт, бюджет, лимит, артефакт) — чистая остановка с записью в
    state и логом, а не молчаливая смерть процесса;
  * `run --continue` возобновляет остановку с места (доработка по последнему ревью, откат
    версии при «артефакт не записан»), с преамбулой для прерванного шага;
  * Executor: повторы транспортного сбоя с resume той же сессии, «дожимающий» ход при
    ненаписанном артефакте; DuoReviewer: повторы вызова ревьюера/председателя;
  * автоперенос эррат председателя в принятый план (детерминированно, идемпотентно, fail-closed);
  * надзор: lock_held / classify_task / wait / supervise_tick.
Паттерн test_duo_pipework2.py: git-песочницы в tmp_path, скриптованный транспорт, без LLM.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
from pathlib import Path

import pytest

from tests.unit.test_duo_pipev3 import (
    CHAIR_REPORT,
    ScriptedTransport,
    _cfg,
    _init_repo,
    _mk_task,
    duo,
)
from tests.unit.test_duo_pipework2 import AcceptingTransport, _set_state

CHAIR_REVISE = CHAIR_REPORT                                   # VERDICT: REVISE
CHAIR_ACCEPT_ERRATA = CHAIR_REPORT.replace(
    "## 3. Сводка для исполнителя\n- поправить Н1.\n\nVERDICT: REVISE\n",
    "## 3. Сводка для исполнителя\n\nПринимать.\n\n## Эрраты к плану\n\n"
    "- **Е1 (высокая)** — Координаты: §3 Ш2. РЕШЕНИЕ: порт слушателя берётся из переменной окружения CALLER_PORT_ZZ.\n"
    "- **Е2 (лёгкая)** — Координаты: §5. РЕШЕНИЕ: добавить тест на пустой каталог ZZ_EMPTY_DIR.\n\n"
    "VERDICT: ACCEPT\n",
)


def _cfg3(tmp_path: Path, repo: Path, **limits) -> dict:
    lim = {"max_plan_iterations": 4, "max_impl_iterations": 4, "budget_usd": 100.0}
    lim.update(limits)
    cfg = _cfg(tmp_path, repo, limits=lim)
    cfg["auto_proceed_to_impl"] = False
    return cfg


def _plan_task(repo: Path, **state) -> Path:
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n", encoding="utf-8")
    _set_state(td, plan_version=1, plan_iteration=0, **state)
    return td


def _impl_task(repo: Path, **state) -> Path:
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    (td / "impl_report_v1.md").write_text("# отчёт\n", encoding="utf-8")
    _set_state(td, phase="impl", plan_version=1, impl_version=1, impl_iteration=0,
               impl_base_head=_head(repo), **state)
    return td


def _head(repo: Path) -> str:
    rc, out = duo.sh(["git", "rev-parse", "HEAD"], cwd=repo)
    assert rc == 0, out
    return out.strip()


def _state(td: Path) -> dict:
    return duo.load_json(td / "state.json")


def _revs(td: Path, phase: str) -> list[str]:
    return sorted(p.name for p in td.glob(f"rev*_{phase}.md"))


# ---------------------------------------------------------------------------
# Граница итерации
# ---------------------------------------------------------------------------


def test_plan_limit_stops_after_review_not_before(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    p = duo.Pipeline(_cfg3(tmp_path, repo, max_plan_iterations=2), "demo")
    p.t = ScriptedTransport()                                  # председатель всегда REVISE
    p.run(force_continue=True)
    st = _state(td)
    assert st["phase"] == "needs_human"
    assert st["halt"]["kind"] == "limit" and st["halt"]["phase"] == "plan" and st["halt"]["resumable"]
    # обе версии дошли до ревью + синтеза; третья версия плана НЕ создавалась
    assert _revs(td, "plan") == ["rev001_plan.md", "rev002_plan.md"]
    assert st["plan_iteration"] == 2 and st["plan_version"] == 2
    assert not (td / "plan_v3.md").exists()
    assert st["rework"]["rev"] == "rev002_plan.md"             # доработка по последнему ревью ждёт
    assert "НЕ начата" in st["stop_reason"]


def test_accept_on_last_allowed_iteration_is_not_a_limit_stop(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    p = duo.Pipeline(_cfg3(tmp_path, repo, max_plan_iterations=1), "demo")
    p.t = AcceptingTransport()
    p.run()
    st = _state(td)
    assert st["phase"] == "impl_pending" and "halt" not in st


def test_impl_limit_stops_after_review_without_revision(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _impl_task(repo)
    p = duo.Pipeline(_cfg3(tmp_path, repo, max_impl_iterations=1), "demo")
    t = ScriptedTransport()
    p.t = t
    p.run(force_continue=True)
    st = _state(td)
    assert st["phase"] == "needs_human" and st["halt"]["kind"] == "limit" and st["halt"]["phase"] == "impl"
    assert st["impl_version"] == 1 and not (td / "impl_report_v2.md").exists()
    assert _revs(td, "impl") == ["rev001_impl.md"]
    assert not [c for c in t.calls if "текущий дифф" in c]     # revise_impl не вызывался


def test_continue_after_limit_requires_raised_limit_then_revises_without_rereview(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    cfg = _cfg3(tmp_path, repo, max_plan_iterations=1)
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport()
    p.run(force_continue=True)
    assert _state(td)["halt"]["kind"] == "limit"
    # без поднятого лимита --continue ничего не делает, состояние не портится
    p2 = duo.Pipeline(cfg, "demo")
    t2 = ScriptedTransport()
    p2.t = t2
    p2.run(resume=True)
    st = _state(td)
    assert st["phase"] == "needs_human" and t2.calls == []
    # --add-iterations 1: доработка по rev001 (а не повторное ревью v1), затем ревью v2 и новая граница
    p3 = duo.Pipeline(cfg, "demo")
    t3 = ScriptedTransport()
    p3.t = t3
    p3.run(resume=True, add_iterations=1)
    st = _state(td)
    assert st["iter_extra"] == {"plan": 1}
    assert st["plan_version"] == 2 and st["plan_iteration"] == 2
    assert _revs(td, "plan") == ["rev001_plan.md", "rev002_plan.md"]
    assert "Н1 тестовое" in t3.calls[0] and "участник структурированного" not in t3.calls[0]   # 1-й вызов — доработка по rev001, не ревью
    assert st["halt"]["kind"] == "limit"                        # на новой границе — снова чистая остановка


# ---------------------------------------------------------------------------
# halt: транспорт, бюджет, артефакт
# ---------------------------------------------------------------------------


class _FailOnce(AcceptingTransport):
    """Исполнитель падает транспортной ошибкой на первом шаге, где в промпте есть needle."""

    def __init__(self, needle: str, **kw):
        super().__init__(**kw)
        self.needle = needle
        self.armed = True

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        if self.armed and mode == "full" and self.needle in prompt:
            self.armed = False
            raise duo.ClaudeTransportError("idle-kill: молчание 1200с", session_id="sid-x", cost=0.5)
        return super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                            resume_session=resume_session, tag=tag)


class _ReviseThenAccept(_FailOnce):
    """Ревью v1 — REVISE; на доработке v2 — транспортный сбой; потом всё принимается."""

    def __init__(self, needle: str):
        super().__init__(needle)
        self.first_review_done = False

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        if "Председатель консилиума" in prompt and not self.first_review_done and not self.responses:
            self.first_review_done = True
            self.responses = [CHAIR_REVISE]
        return super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                            resume_session=resume_session, tag=tag)


def test_transport_failure_halts_cleanly_and_resumes_in_place(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    cfg = _cfg3(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    # ревью v1 → REVISE, доработка v2 падает транспортной ошибкой
    p.t = _ReviseThenAccept("plan_v2.md")
    p.run(force_continue=True)
    st = _state(td)
    assert st["phase"] == "needs_human"                         # не молчаливая смерть с phase=plan
    assert st["halt"]["kind"] == "transport" and st["halt"]["resumable"]
    assert "idle-kill" in st["stop_reason"]
    assert st["inflight"]["step"] == "revise_plan" and st["inflight"]["version"] == 2
    assert st["rework"]["rev"] == "rev001_plan.md" and st["rework"]["source"] == "auto-resume"
    assert st["executor_session"] == "sid-x"                    # сессия сохранена для resume
    assert st["spent_usd"] >= 0.5                               # стоимость упавшего вызова учтена
    # --continue: тот же шаг в той же сессии, с преамбулой «сверь состояние»; затем ревью v2 → ACCEPT
    p2 = duo.Pipeline(cfg, "demo")
    t2 = AcceptingTransport()
    p2.t = t2
    p2.run(resume=True)
    st = _state(td)
    assert t2.calls[0].startswith(duo.EXECUTOR_RESUMED_PREFACE)
    assert st["phase"] == "impl_pending" and st["plan_version"] == 2
    assert "halt" not in st and "inflight" not in st and "rework" not in st


def test_review_failure_does_not_burn_rev_number(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)

    class Boom(ScriptedTransport):
        def call(self, prompt, *, mode, **kw):
            raise duo.ClaudeTransportError("claude завершился без result", session_id=None, cost=0.0)

    p = duo.Pipeline(_cfg3(tmp_path, repo), "demo")
    p.t = Boom()
    p.run(force_continue=True)
    st = _state(td)
    assert st["halt"]["kind"] == "transport" and st["phase"] == "needs_human"
    assert st.get("rev_counter_plan", 0) == 0 and _revs(td, "plan") == []   # rev001 не «сгорел»
    # возобновление: ревью снова rev001 (без дыры в нумерации)
    p2 = duo.Pipeline(_cfg3(tmp_path, repo), "demo")
    p2.t = AcceptingTransport()
    p2.run(resume=True)
    assert _revs(td, "plan") == ["rev001_plan.md"] and _state(td)["phase"] == "impl_pending"


def test_budget_hard_check_at_boundary_keeps_review_and_resumes_with_add_budget(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo, spent_usd=102.0)                      # бюджет 100, grace 5
    cfg = _cfg3(tmp_path, repo)
    cfg["limits"]["budget_grace_usd"] = 5.0
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport()
    p.run(force_continue=True)
    st = _state(td)
    # ревью доведено до конца в пределах grace; стоп — на границе, перед работой исполнителя
    assert _revs(td, "plan") == ["rev001_plan.md"]
    assert st["halt"]["kind"] == "budget" and st["plan_version"] == 1
    assert st["rework"]["rev"] == "rev001_plan.md"
    # без добавки бюджета возобновление отказывает; с добавкой — идёт доработка по rev001
    p2 = duo.Pipeline(cfg, "demo")
    t2 = ScriptedTransport()
    p2.t = t2
    p2.run(resume=True)
    assert t2.calls == [] and _state(td)["phase"] == "needs_human"
    p3 = duo.Pipeline(cfg, "demo")
    t3 = ScriptedTransport()
    p3.t = t3
    p3.run(resume=True, add_budget=50.0)
    st = _state(td)
    assert st["budget_extra_usd"] == 50.0 and st["plan_version"] >= 2
    assert "Н1 тестовое" in t3.calls[0] and "участник структурированного" not in t3.calls[0]


def test_budget_exhausted_before_any_review_is_clean_halt(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo, spent_usd=100.5)
    p = duo.Pipeline(_cfg3(tmp_path, repo), "demo")
    t = ScriptedTransport()
    p.t = t
    p.run(force_continue=True)
    st = _state(td)
    assert st["halt"]["kind"] == "budget" and st["phase"] == "needs_human" and t.calls == []
    assert "--add-budget" in st["stop_reason"] and "--resume" in st["stop_reason"]


def test_missing_artifact_halt_resumes_by_rolling_version_back(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, plan_version=1, plan_iteration=0)            # plan_v1.md на диске НЕТ
    cfg = _cfg3(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    p.run(force_continue=True)
    st = _state(td)
    assert st["halt"]["kind"] == "artifact_missing" and st["halt"]["phase"] == "plan"
    assert t.calls == []                                        # ревью не тратилось
    p2 = duo.Pipeline(cfg, "demo")
    p2.t = AcceptingTransport()
    p2.run(resume=True)
    st = _state(td)
    # шаг исполнителя повторён (план записан заново), затем ревью и принятие
    assert (td / "plan_v1.md").exists() and st["phase"] == "impl_pending"


def test_unexpected_exception_becomes_crash_halt(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    p = duo.Pipeline(_cfg3(tmp_path, repo), "demo")
    p.t = ScriptedTransport()

    def boom(*a, **k):
        raise ValueError("неожиданное")

    monkeypatch.setattr(duo.DuoReviewer, "review", boom)
    p.run(force_continue=True)
    st = _state(td)
    assert st["phase"] == "needs_human" and st["halt"]["kind"] == "crash" and st["halt"]["resumable"]
    assert "ValueError" in st["stop_reason"]


def test_set_verdict_after_halt_uses_halt_phase_and_clears_it(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    cfg = _cfg3(tmp_path, repo, max_plan_iterations=1)
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport()
    p.run(force_continue=True)
    assert duo.needs_human_phase(_state(td)) == "plan"
    duo.cmd_set_verdict(cfg, argparse.Namespace(task="demo", verdict="ACCEPT", note="ок", phase=None))
    st = _state(td)
    assert st["phase"] == "impl_pending" and "halt" not in st and "rework" not in st


# ---------------------------------------------------------------------------
# Executor / DuoReviewer
# ---------------------------------------------------------------------------


class _Seq:
    """Транспорт-стаб: по очереди отдаёт ответы или бросает исключения; пишет файлы по запросу."""

    def __init__(self, items, write_on: dict[int, Path] | None = None):
        self.items = list(items)
        self.calls: list[dict] = []
        self.write_on = write_on or {}

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        i = len(self.calls)
        self.calls.append({"prompt": prompt, "resume": resume_session, "tag": tag})
        if i in self.write_on:
            self.write_on[i].parent.mkdir(parents=True, exist_ok=True)
            self.write_on[i].write_text("# артефакт\n", encoding="utf-8")
        it = self.items.pop(0)
        if isinstance(it, Exception):
            raise it
        return it


def _ex(tmp_path: Path, t, **limits):
    cfg = {"timeouts": {"executor_sec": 5, "api_retry_wait_sec": 30}, "limits": limits}
    state = {"executor_session": "sid-0", "spent_usd": 0.0, "artifacts_rel": "pipeline/demo"}
    return duo.Executor(cfg, t, tmp_path, tmp_path, state), state


def test_executor_retries_transport_error_with_resume_of_failed_session(tmp_path: Path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    t = _Seq([duo.ClaudeTransportError("idle", session_id="sid-1", cost=0.4), ("готово", "sid-1", 0.1)])
    ex, state = _ex(tmp_path, t, executor_api_retries=2)
    assert ex._send("БОЛЬШОЙ ПРОМПТ", "executor_impl_v2") == "готово"
    assert len(t.calls) == 2 and sleeps == [30.0]
    assert t.calls[1]["resume"] == "sid-1" and "БОЛЬШОЙ" not in t.calls[1]["prompt"]
    assert state["spent_usd"] == 0.5 and state["executor_session"] == "sid-1"


def test_executor_transport_error_without_retries_reraises_and_keeps_cost(tmp_path: Path):
    t = _Seq([duo.ClaudeTransportError("idle", session_id="sid-9", cost=0.7)])
    ex, state = _ex(tmp_path, t, executor_api_retries=0)
    with pytest.raises(duo.ClaudeTransportError):
        ex._send("p", "executor_impl_v2")
    assert state["spent_usd"] == 0.7 and state["executor_session"] == "sid-9"
    tr = next((tmp_path / "transcripts").glob("*_executor_impl_v2.md")).read_text(encoding="utf-8")
    assert "сбой исполнителя" in tr


def test_executor_continue_turn_when_artifact_not_written(tmp_path: Path):
    art = tmp_path / "pipeline/demo/impl_report_v2.md"
    # ход 0 — «ответ оборвался» (файла нет); ход 1 (дожимающий) файл создаёт
    t = _Seq([("Исправление логики и", "sid-1", 0.1), ("записал", "sid-1", 0.1)], write_on={1: art})
    ex, state = _ex(tmp_path, t, executor_continue_retries=2)
    ex.implement("ТЗ", "pipeline/demo/plan_v1.md", 2)
    assert len(t.calls) == 2
    assert "impl_report_v2.md" in t.calls[1]["prompt"] and t.calls[1]["resume"] == "sid-1"
    assert art.is_file()
    assert list((tmp_path / "transcripts").glob("*_executor_impl_v2_continue1.md"))


def test_executor_no_continue_turns_by_default(tmp_path: Path):
    t = _Seq([("оборвалось", "sid-1", 0.1)])
    ex, _ = _ex(tmp_path, t)
    ex.implement("ТЗ", "pipeline/demo/plan_v1.md", 2)
    assert len(t.calls) == 1                                   # прежнее поведение: решает run()


def test_reviewer_transport_retry_then_gives_up(tmp_path: Path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    cfg = {"timeouts": {"api_retry_wait_sec": 10}, "limits": {"reviewer_retries": 2}}
    rv = duo.DuoReviewer.__new__(duo.DuoReviewer)
    rv.cfg = cfg
    rv.task_dir = tmp_path
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 3:
            raise duo.ClaudeTransportError("idle")
        return "ok"

    assert rv._transport_retry(flaky, "R1") == "ok" and sleeps == [10.0, 20.0]
    n["i"] = -10
    with pytest.raises(duo.ClaudeTransportError):
        rv._transport_retry(flaky, "R1")                       # 3 попытки (1+2 повтора) → пробрасывает


# ---------------------------------------------------------------------------
# Эрраты: перенос из ревью председателя в план
# ---------------------------------------------------------------------------

REV_OK = (
    "# rev\n\n## 3. Сводка для исполнителя\n\nПринимать.\n\n## Эрраты к плану\n\n"
    "1. **Е1 (высокая)** — Координаты: §3. РЕШЕНИЕ: порт из CALLER_PORT_ZZ.\n"
    "2. **Е2 (лёгкая)** — Координаты: §5. РЕШЕНИЕ: тест на пустой каталог ZZ_EMPTY_DIR.\n\n"
    "VERDICT: ACCEPT\n"
)


def test_extract_errata_basic_and_trimming():
    body = duo.extract_chairman_errata(REV_OK)
    assert body.startswith("1. **Е1") and body.endswith("ZZ_EMPTY_DIR.")
    assert "VERDICT" not in body
    assert duo.extract_chairman_errata("# rev\n\nнет блока\n\nVERDICT: ACCEPT\n") is None


def test_extract_errata_ignores_fenced_heading_and_stops_at_next_heading():
    text = ("# rev\n\n```\n## Эрраты к плану\nпример в коде\n```\n\n"
            "## Эрраты к плану\n\n- пункт один про ZZ_ONE\n\n## 4. Хвост\n\nне эрраты\n\nVERDICT: ACCEPT\n")
    body = duo.extract_chairman_errata(text)
    assert body == "- пункт один про ZZ_ONE"


def test_extract_errata_keeps_subheadings_of_deeper_level():
    text = "## Эрраты к плану\n\n- Е1 текст про ZZ\n\n### К владельцу\n\n1. вопрос\n\nVERDICT: ACCEPT\n"
    body = duo.extract_chairman_errata(text)
    assert "### К владельцу" in body and body.endswith("1. вопрос")


def test_apply_errata_appends_verbatim_and_is_idempotent(tmp_path: Path):
    rev = tmp_path / "rev001_plan.md"
    plan = tmp_path / "plan_v1.md"
    rev.write_text(REV_OK, encoding="utf-8")
    plan.write_text("# план\n\nтело\n", encoding="utf-8")
    status, info = duo.apply_accept_errata(rev, plan)
    assert status == "applied" and info["items"] == 2
    text = plan.read_text(encoding="utf-8")
    assert "<!-- duo:errata rev001_plan.md sha256:" in text and text.rstrip().endswith("<!-- /duo:errata rev001_plan.md -->")
    assert "порт из CALLER_PORT_ZZ" in duo.extract_errata(text)       # исполнитель увидит блок в промпте impl
    assert plan.read_text(encoding="utf-8").startswith("# план\n\nтело\n")   # тело плана не тронуто
    assert duo.apply_accept_errata(rev, plan)[0] == "present"         # повтор — без изменений
    assert plan.read_text(encoding="utf-8") == text


def test_apply_errata_appends_below_existing_errata_section(tmp_path: Path):
    rev = tmp_path / "rev001_plan.md"
    plan = tmp_path / "plan_v1.md"
    rev.write_text(REV_OK, encoding="utf-8")
    plan.write_text("# план\n\n## Эрраты к плану\n\n- прежняя эрррата про OLD_ZZ\n", encoding="utf-8")
    assert duo.apply_accept_errata(rev, plan)[0] == "applied"
    text = plan.read_text(encoding="utf-8")
    assert len(re.findall(r"^## Эрраты", text, re.M)) == 1 and "### Эрраты приёмки rev001_plan.md" in text
    assert "OLD_ZZ" in text and "CALLER_PORT_ZZ" in text


def test_apply_errata_detects_manual_transfer_and_partial(tmp_path: Path):
    rev = tmp_path / "rev001_plan.md"
    plan = tmp_path / "plan_v1.md"
    rev.write_text(REV_OK, encoding="utf-8")
    plan.write_text("# план\n\n## Эрраты к плану\n\n" + duo.extract_chairman_errata(REV_OK) + "\n", encoding="utf-8")
    assert duo.apply_accept_errata(rev, plan)[0] == "present"         # помощник уже перенёс руками
    partial = "# план\n\n## Эрраты к плану\n\n1. **Е1 (высокая)** — Координаты: §3. РЕШЕНИЕ: порт из CALLER_PORT_ZZ.\n"
    plan.write_text(partial, encoding="utf-8")
    with pytest.raises(duo.ErrataError, match="частично"):
        duo.apply_accept_errata(rev, plan)
    assert plan.read_text(encoding="utf-8") == partial                # при отказе план не тронут


def test_apply_errata_fail_closed_cases(tmp_path: Path):
    plan = tmp_path / "plan_v1.md"
    plan.write_text("# план\n", encoding="utf-8")
    empty = tmp_path / "rev_empty.md"
    empty.write_text("## Эрраты к плану\n\nVERDICT: ACCEPT\n", encoding="utf-8")
    with pytest.raises(duo.ErrataError, match="пуст"):
        duo.apply_accept_errata(empty, plan)
    marked = tmp_path / "rev_marked.md"
    marked.write_text("## 1. Замечания\n\n**Н1 (ЭРРАТА, высокая)** текст\n\nVERDICT: ACCEPT\n", encoding="utf-8")
    with pytest.raises(duo.ErrataError, match="нет"):
        duo.apply_accept_errata(marked, plan)
    clean = tmp_path / "rev_clean.md"
    clean.write_text("## 3. Сводка\n\nправок не требуется\n\nVERDICT: ACCEPT\n", encoding="utf-8")
    assert duo.apply_accept_errata(clean, plan) == ("none", {})
    assert plan.read_text(encoding="utf-8") == "# план\n"


def _triage_cfg(tmp_path: Path, repo: Path) -> dict:
    cfg = _cfg3(tmp_path, repo)
    cfg["review"] = {"plan_policy": "triage"}
    return cfg


def test_pipeline_applies_chairman_errata_at_plan_accept(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    p = duo.Pipeline(_triage_cfg(tmp_path, repo), "demo")

    class Acc(AcceptingTransport):
        def call(self, prompt, *, mode, **kw):
            if "Председатель консилиума" in prompt and not self.responses:
                self.responses = [CHAIR_ACCEPT_ERRATA]
            return super().call(prompt, mode=mode, **kw)

    p.t = Acc()
    p.run()
    st = _state(td)
    assert st["phase"] == "impl_pending"
    plan = (td / "plan_v1.md").read_text(encoding="utf-8")
    assert "CALLER_PORT_ZZ" in plan and "ZZ_EMPTY_DIR" in plan
    assert st["errata_applied"]["rev"] == "rev001_plan.md" and st["errata_applied"]["items"] == 2
    assert "ЭРРАТЫ: блок rev001_plan.md дописан" in (td / "orchestrator.log").read_text(encoding="utf-8")


def test_pipeline_errata_failure_halts_not_resumable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)
    bad = CHAIR_ACCEPT_ERRATA.replace(
        CHAIR_ACCEPT_ERRATA[CHAIR_ACCEPT_ERRATA.index("- **Е1"):CHAIR_ACCEPT_ERRATA.index("VERDICT: ACCEPT")], "")
    p = duo.Pipeline(_triage_cfg(tmp_path, repo), "demo")

    class Acc(AcceptingTransport):
        def call(self, prompt, *, mode, **kw):
            if "Председатель консилиума" in prompt and not self.responses:
                self.responses = [bad]
            return super().call(prompt, mode=mode, **kw)

    p.t = Acc()
    p.run()
    st = _state(td)
    assert st["phase"] == "needs_human" and st["halt"]["kind"] == "errata" and st["halt"]["resumable"] is False
    assert "пуст" in st["stop_reason"]


def test_errata_not_applied_when_policy_is_legacy(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _plan_task(repo)

    class Acc(AcceptingTransport):
        def call(self, prompt, *, mode, **kw):
            if "Председатель консилиума" in prompt and not self.responses:
                self.responses = [CHAIR_ACCEPT_ERRATA]
            return super().call(prompt, mode=mode, **kw)

    p = duo.Pipeline(_cfg3(tmp_path, repo), "demo")         # plan_policy не задан → legacy
    p.t = Acc()
    p.run()
    assert "CALLER_PORT_ZZ" not in (td / "plan_v1.md").read_text(encoding="utf-8")


def test_chairman_prompt_tells_orchestrator_appends_errata():
    assert "оркестратор дословно допишет" in duo.PLAN_POLICY_CHAIRMAN_BLOCK
    assert "эксперт допишет" not in duo.PLAN_POLICY_CHAIRMAN_BLOCK


# ---------------------------------------------------------------------------
# Надзор: lock / classify / wait / supervise
# ---------------------------------------------------------------------------


def _mk_state(arts: Path, name: str, **st) -> Path:
    td = arts / name
    td.mkdir(parents=True, exist_ok=True)
    base = {"task": name, "phase": "plan", "verdicts": []}
    base.update(st)
    duo.save_json(td / "state.json", base)
    return td


def test_lock_held_reflects_flock_and_dies_with_holder(tmp_path: Path):
    td = tmp_path / "t"
    td.mkdir()
    assert duo.lock_held(td) is False                          # .lock ещё нет
    with open(td / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert duo.lock_held(td) is True
    assert duo.lock_held(td) is False                          # держатель закрыл дескриптор (смерть процесса)


def test_classify_task_kinds(tmp_path: Path):
    arts = tmp_path / "pipeline"
    now = 1_000_000.0
    c = lambda name: duo.classify_task(arts / name, now=now, stale_sec=300, dead_max_age_sec=86400)  # noqa: E731
    _mk_state(arts, "done_t", phase="done", state_updated_at=now - 10)
    _mk_state(arts, "dead_t", phase="impl", state_updated_at=now - 600)
    _mk_state(arts, "fresh_t", phase="impl", state_updated_at=now - 30)
    _mk_state(arts, "never_t", phase="plan")
    _mk_state(arts, "corpse_t", phase="impl", state_updated_at=now - 3 * 86400)
    _mk_state(arts, "halt_t", phase="needs_human", state_updated_at=now - 5,
              halt={"kind": "limit", "phase": "impl", "resumable": True}, stop_reason="лимит")
    _mk_state(arts, "nh_t", phase="needs_human", state_updated_at=now - 5)
    _mk_state(arts, "pend_t", phase="impl_pending", state_updated_at=now - 5)
    live = _mk_state(arts, "live_t", phase="impl", state_updated_at=now - 900)
    assert c("done_t")["kind"] == "done"
    assert c("dead_t")["kind"] == "dead"
    assert c("fresh_t")["kind"] == "starting"
    assert c("never_t")["kind"] == "idle" and c("corpse_t")["kind"] == "idle"
    assert c("halt_t")["kind"] == "halted" and c("nh_t")["kind"] == "idle" and c("pend_t")["kind"] == "idle"
    with open(live / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert c("live_t")["kind"] == "running" and c("live_t")["alive"]          # lock важнее возраста state
    assert duo.classify_task(arts / "nonexistent", now=now)["kind"] == "unknown"


def _wait_ns(task, **kw):
    d = {"task": task, "poll": 0.01, "timeout": 0.0, "quiet": True, "keep_waiting_idle": False}
    d.update(kw)
    return argparse.Namespace(**d)


def _repo_cfg(tmp_path: Path) -> tuple[dict, Path]:
    repo = tmp_path / "repo"
    arts = repo / "pipeline"
    arts.mkdir(parents=True)
    return {"repo": str(repo), "artifacts_dir": "pipeline"}, arts


def test_wait_exit_codes(tmp_path: Path, capsys):
    cfg, arts = _repo_cfg(tmp_path)
    _mk_state(arts, "d", phase="done")
    _mk_state(arts, "h", phase="needs_human", state_updated_at=1.0,
              halt={"kind": "budget", "phase": "plan", "resumable": True}, stop_reason="бюджет исчерпан")
    _mk_state(arts, "p", phase="impl_pending", state_updated_at=1.0)
    assert duo.cmd_wait(cfg, _wait_ns("d")) == duo.WAIT_EXIT["done"] == 0
    assert duo.cmd_wait(cfg, _wait_ns("p")) == duo.WAIT_EXIT["impl_pending"]
    rc = duo.cmd_wait(cfg, _wait_ns("h"))
    out = capsys.readouterr().out
    assert rc == duo.WAIT_EXIT["halted"] == 10
    assert "WAIT-RESULT: halted" in out and "halt=budget" in out and "бюджет исчерпан" in out


def test_wait_dead_and_timeout_on_running(tmp_path: Path):
    import time as _t
    cfg, arts = _repo_cfg(tmp_path)
    _mk_state(arts, "dead", phase="impl", state_updated_at=_t.time() - 4000)
    assert duo.cmd_wait(cfg, _wait_ns("dead")) == duo.WAIT_EXIT["dead"]
    live = _mk_state(arts, "live", phase="impl", state_updated_at=_t.time())
    with open(live / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert duo.cmd_wait(cfg, _wait_ns("live", timeout=0.1)) == duo.WAIT_EXIT["timeout"]


def test_supervise_alerts_once_and_resumes_dead_with_cap(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["supervisor"] = {"auto_resume": True, "max_resumes": 2}
    now = 2_000_000.0
    _mk_state(arts, "dead", phase="impl", state_updated_at=now - 1000)
    _mk_state(arts, "lim", phase="needs_human", state_updated_at=now - 10,
              halt={"kind": "limit", "phase": "impl", "resumable": True, "at": "x"}, stop_reason="лимит")
    _mk_state(arts, "tr", phase="needs_human", state_updated_at=now - 10,
              halt={"kind": "transport", "phase": "impl", "resumable": True, "at": "y"}, stop_reason="сбой")
    _mk_state(arts, "ok", phase="done")
    spawned, alerts = [], []
    sp = tmp_path / "sup.json"

    def tick(t):
        return {a["task"]: a for a in duo.supervise_tick(
            cfg, now=t, state_path=sp, spawn=spawned.append, notify=lambda a, b: alerts.append(a))}

    r1 = tick(now)
    assert r1["dead"]["action"] == "resumed" and r1["tr"]["action"] == "resumed"
    assert r1["lim"]["action"] == "alert" and "ok" not in r1          # limit — решение человека, не авто
    assert sorted(spawned) == ["dead", "tr"]
    r2 = tick(now + 60)                                               # те же события: алерт повторно не шлётся
    assert r2["lim"]["action"] == "none"
    assert r2["dead"]["action"] == "resumed"                          # счётчик 2/2
    r3 = tick(now + 120)
    assert r3["dead"]["action"] == "resume-limit" and spawned.count("dead") == 2


def test_supervise_without_auto_resume_only_alerts(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    now = 3_000_000.0
    _mk_state(arts, "dead", phase="impl", state_updated_at=now - 1000)
    spawned = []
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json", spawn=spawned.append,
                              notify=lambda a, b: None)
    assert acts[0]["action"] == "alert" and spawned == []


def test_supervise_notices_fresh_done_once_and_is_silent_on_baseline(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    now = 4_000_000.0
    _mk_state(arts, "fresh_done", phase="done", state_updated_at=now - 100)
    _mk_state(arts, "old_done", phase="done", state_updated_at=now - 7 * 3600)
    _mk_state(arts, "pend", phase="impl_pending", state_updated_at=now - 100)
    sp = tmp_path / "s.json"
    notes: list[str] = []
    kw = {"state_path": sp, "spawn": lambda t: None, "notify": lambda a, b: notes.append(a)}
    cfg["notify"] = {"desktop": True}
    assert duo.supervise_tick(cfg, now=now, **kw) == []              # первый тик — baseline, молчим
    _mk_state(arts, "fresh_done2", phase="done", state_updated_at=now + 50)
    acts = {a["task"]: a for a in duo.supervise_tick(cfg, now=now + 60, **kw)}
    assert acts["fresh_done2"]["action"] == "notice" and len(notes) == 1
    assert duo.supervise_tick(cfg, now=now + 120, **kw) == []        # повтор того же события не шлётся


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def test_config_json_pipework3_values():
    cfg = duo.load_config(duo.DEFAULT_CONFIG)
    lim = cfg["limits"]
    assert lim["reviewer_retries"] >= 1 and lim["executor_continue_retries"] >= 1
    assert lim["executor_api_retries"] >= 1
    assert lim["max_impl_iterations"] >= 6 and lim["max_plan_iterations"] >= 12
    assert lim["budget_grace_usd"] > 0
    assert cfg["supervisor"]["stale_sec"] >= 120
    json.dumps(cfg)
