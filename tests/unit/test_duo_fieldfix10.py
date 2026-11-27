"""fieldfix10: полевые волны 2–3 (Н44–Н59).

Н44  блок эррат: послесловие ревьюера («самопроверка», «где я могу ошибаться») не переносится в план.
Н45  diff_impl_vN.patch видит новые (untracked) файлы; дельта итерации.
Н46  квота провайдера (429 [1308]): класс `quota`, время сброса, остановка без пауз-повторов.
Н49  оборванные ревью видны в review_transport.
Н50  окружение: claude/node недоступны → halt kind=env, а не crash; надзор их не возобновляет.
Н47  R1 дуэта переживают остановку.
Н48  сводка сходимости в остановке по лимиту итераций; правила приёмки в промптах.
Н51  изоляция оркестратора в собственной cgroup-области.
Н53  claim-gate: корень каталога против подкаталога.
Н54  второй канал уведомлений; Н55 — счётчик неучтённых вызовов.
Н59  шаг гейтов: idle-timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

from tests.unit.test_duo_chairman_validation import _mini_report
from tests.unit.test_duo_fieldfix8 import API_529, MetaTransport, _pair_run, _rv
from tests.unit.test_duo_pipemodes import _pcfg, _state
from tests.unit.test_duo_pipev3 import ScriptedTransport, _cfg, _init_repo, _mk_task, _review, duo
from tests.unit.test_duo_pipework3 import _mk_state, _repo_cfg, _wait_ns

# --- Н44 -----------------------------------------------------------------------------------------

# структура реального ревью волны 2: после нумерованного списка эррат — ЖИРНЫЕ абзацы (не заголовки)
# с маркированными пунктами, затем машинная строка
_REV_AFTERWORD = (
    "# rev — ревью плана\n\n## 3. Сводка для исполнителя\n\nПравок-блокеров нет.\n\n## Эрраты к плану\n\n"
    "1. **Е1 (высокая)**: §4.1. РЕШЕНИЕ: опции читать после Build() ZZ_OPT_ONE.\n"
    "   Продолжение пункта с отступом ZZ_CONT_ONE.\n"
    "2. **Е2 (средняя)**: §4.2. РЕШЕНИЕ: якоря regex ZZ_ANCHOR_TWO.\n"
    "ленивое продолжение пункта 2 без отступа ZZ_LAZY_TWO.\n"
    "3. **Е3 (косметика)**: §6. РЕШЕНИЕ: MapFallback ZZ_FALLBACK_THREE.\n\n"
    "**Самопроверка приёмки.** Три самых рискованных места и почему они не блокеры:\n"
    "- Е1: требование C2 не меняется ZZ_AFTER_ONE\n"
    "- Пределы тела: арифметика сходится ZZ_AFTER_TWO\n\n"
    "**Где я могу ошибаться.**\n"
    "- Вывод сделан без прогона ZZ_AFTER_THREE\n\n"
    "VERDICT: ACCEPT\n"
)


def test_errata_afterword_is_not_part_of_block():
    body = duo.extract_chairman_errata(_REV_AFTERWORD)
    assert body is not None
    assert "ZZ_OPT_ONE" in body and "ZZ_FALLBACK_THREE" in body
    assert "ZZ_CONT_ONE" in body and "ZZ_LAZY_TWO" in body        # продолжения пунктов остаются
    assert "Самопроверка" not in body and "ZZ_AFTER" not in body and "Где я могу" not in body
    assert "VERDICT" not in body


def test_apply_errata_counts_only_real_errata_and_reports_afterword(tmp_path: Path):
    rev, plan = tmp_path / "rev001_plan.md", tmp_path / "plan_v1.md"
    rev.write_text(_REV_AFTERWORD, encoding="utf-8")
    plan.write_text("# план\n", encoding="utf-8")
    status, info = duo.apply_accept_errata(rev, plan)
    assert status == "applied"
    assert info["top_items"] == 3                                  # было бы 3 + 3 строки послесловия
    assert info["afterword_lines"] >= 4
    text = plan.read_text(encoding="utf-8")
    assert "ZZ_AFTER_THREE" not in text and "ZZ_FALLBACK_THREE" in text
    # исполнитель получает в промпте только эрраты
    assert "ZZ_AFTER" not in duo.extract_errata(text)
    # повтор идемпотентен и по отсечённому тексту тоже
    assert duo.apply_accept_errata(rev, plan)[0] == "present"


def test_split_afterword_keeps_numbered_groups_and_headings():
    # нумерация продолжается ниже абзаца-группировки — это сами эрраты, резать нельзя
    lines = ("1. первая ZZ_A\n\n**Низкие:**\n\n2. вторая ZZ_B\n").splitlines()
    kept, tail = duo.split_errata_afterword(lines)
    assert tail == [] and kept == lines
    # заголовок глубже (допустим по промпту) не завершает блок
    lines = "- Е1 текст ZZ_A\n\n### К владельцу\n\n- вопрос ZZ_Q\n".splitlines()
    assert duo.split_errata_afterword(lines)[1] == []


def test_split_afterword_prose_block_and_fence_are_untouched():
    prose = "Е1: правка ZZ_A.\n\nЕ2: правка ZZ_B.\n".splitlines()           # списка нет — не трогаем
    assert duo.split_errata_afterword(prose) == (prose, [])
    fenced = "1. пункт ZZ_A\n\n```\nкод в колонке 0\n\nещё код\n```\n\n2. пункт ZZ_B\n".splitlines()
    assert duo.split_errata_afterword(fenced) == (fenced, [])


def test_split_afterword_cuts_at_first_paragraph_after_blank():
    lines = ["1. раз ZZ_A", "", "2. два ZZ_B", "", "Итого: всё проверено.", "- маркер ZZ_T"]
    kept, tail = duo.split_errata_afterword(lines)
    assert kept == ["1. раз ZZ_A", "", "2. два ZZ_B"]
    assert tail == ["Итого: всё проверено.", "- маркер ZZ_T"]


def test_chairman_prompt_says_errata_block_is_last():
    assert "ПОСЛЕДНИЙ раздел отчёта" in duo.PLAN_POLICY_CHAIRMAN_BLOCK


# --- Н45 -----------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    import subprocess
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return r.stdout


def _snap_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("print('old')\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_diff_patch_includes_new_untracked_files(tmp_path: Path):
    repo = _snap_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "old.py").write_text("print('changed')\n", encoding="utf-8")        # tracked-правка
    (repo / "src" / "newdir").mkdir()
    (repo / "src" / "newdir" / "fresh.py").write_text("FRESH_ZZ = 1\n", encoding="utf-8")  # новый файл в новом каталоге
    (repo / "build").mkdir()
    (repo / "build" / "junk.bin").write_text("IGNORED_ZZ\n", encoding="utf-8")            # игнорируется
    (repo / "pipeline" / "t").mkdir(parents=True)
    (repo / "pipeline" / "t" / "plan_v1.md").write_text("ARTIFACT_ZZ\n", encoding="utf-8")  # артефакт серии
    (repo / "data.bin").write_bytes(b"x" * 2048)                                          # крупнее лимита 1 КиБ
    out = tmp_path / "d.patch"
    guard = duo.GitGuard(repo)
    tree = guard.diff_patch(base, out, "pipeline/t", 1)
    text = out.read_text(encoding="utf-8")
    assert tree and "print('changed')" in text                       # tracked-правка видна, как раньше
    assert "FRESH_ZZ" in text and "src/newdir/fresh.py" in text      # Н45: новый файл виден
    assert "IGNORED_ZZ" not in text and "ARTIFACT_ZZ" not in text    # игнор и артефакты серии — нет
    assert "data.bin" not in text                                     # крупнее snapshot_max_file_kb — нет
    # настоящий индекс и дерево не тронуты: untracked остаются untracked, ничего не застейджено
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    assert "?? src/newdir/" in _git(repo, "status", "--porcelain")


def test_diff_patch_without_artifacts_rel_keeps_old_behavior(tmp_path: Path):
    repo = _snap_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "old.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "fresh.py").write_text("FRESH_ZZ = 1\n", encoding="utf-8")
    out = tmp_path / "d.patch"
    assert duo.GitGuard(repo).diff_patch(base, out) == ""
    text = out.read_text(encoding="utf-8")
    assert "print('changed')" in text and "FRESH_ZZ" not in text


def test_diff_delta_between_two_snapshots(tmp_path: Path):
    repo = _snap_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    guard = duo.GitGuard(repo)
    (repo / "a.py").write_text("A_ONE_ZZ\n", encoding="utf-8")
    t1 = guard.diff_patch(base, tmp_path / "v1.patch", "pipeline/t", 512)
    (repo / "a.py").write_text("A_TWO_ZZ\n", encoding="utf-8")
    (repo / "b.py").write_text("B_NEW_ZZ\n", encoding="utf-8")
    t2 = guard.diff_patch(base, tmp_path / "v2.patch", "pipeline/t", 512)
    assert t1 and t2 and t1 != t2
    delta = tmp_path / "delta.patch"
    assert guard.diff_delta(t1, t2, delta)
    d = delta.read_text(encoding="utf-8")
    assert "-A_ONE_ZZ" in d and "+A_TWO_ZZ" in d and "B_NEW_ZZ" in d
    assert "old.py" not in d                                          # то, что не менялось, в дельте нет
    assert not guard.diff_delta("0" * 40, t2, tmp_path / "x.patch")    # снимка нет в базе — дельты нет


def test_record_snapshot_writes_delta_and_prompt_phrase(tmp_path: Path):
    repo = _snap_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").strip()
    task_dir = tmp_path / "td"
    task_dir.mkdir()
    pl = duo.Pipeline.__new__(duo.Pipeline)
    pl.cfg, pl.task_dir, pl.guard = {}, task_dir, duo.GitGuard(repo)
    st: dict = {}
    (repo / "a.py").write_text("ONE\n", encoding="utf-8")
    t1 = pl.guard.diff_patch(base, task_dir / "diff_impl_v1.patch", "pipeline/t", 512)
    assert pl._record_snapshot(st, 1, t1, "pipeline/t") == ""         # первая версия — дельты нет
    (repo / "a.py").write_text("TWO\n", encoding="utf-8")
    t2 = pl.guard.diff_patch(base, task_dir / "diff_impl_v2.patch", "pipeline/t", 512)
    note = pl._record_snapshot(st, 2, t2, "pipeline/t")
    assert "pipeline/t/diff_delta_v2.patch" in note and "v1→v2" in note
    assert (task_dir / "diff_delta_v2.patch").is_file() and st["impl_trees"] == {"1": t1, "2": t2}
    assert pl._record_snapshot(st, 3, "", "pipeline/t") == ""         # снимок не удался — молча без дельты
    assert pl._record_snapshot(st, 3, t2, "pipeline/t") == ""         # дерево не менялось — дельта пуста
    assert pl._snapshot_kb() == 512


# --- Н46 / Н58: квота провайдера -------------------------------------------------------------------

API_QUOTA = ("API Error: 429 [1308] Usage limit reached for 5 hour. Your limit will reset at 2026-11-25 21:39:28")
RESET_UTC = 1_795_613_968.0                                           # 2026-11-25 13:39:28 UTC (= 21:39:28 в UTC+8)


def test_reset_constant_is_the_provider_time_in_utc_plus_8():
    from datetime import UTC, datetime
    assert datetime(2026, 11, 25, 13, 39, 28, tzinfo=UTC).timestamp() == RESET_UTC


@pytest.mark.parametrize("text,meta", [
    (API_QUOTA, None),
    (API_QUOTA, {"api_error_status": 429}),
    ("API Error: 429 {\"error\":{\"message\":\"Usage limit reached for 5 hour\"}}", None),
])
def test_classify_quota_is_not_transient(text, meta):
    assert duo.classify_failure(text, meta) == "quota" and duo.is_quota_text(text)
    assert "quota" in duo.FAILURE_CLASSES


def test_overload_and_quote_in_prose_are_not_quota():
    assert duo.classify_failure(API_529, None) == "transient" and not duo.is_quota_text(API_529)
    # цитата кода в середине нормального ответа — не сбой API (узкая сигнатура: только начало ответа)
    assert not duo.is_quota_text("Разбор: в логе был «[1308] Usage limit reached», но ревью завершено.")


def test_parse_quota_reset_uses_provider_timezone():
    assert duo.parse_quota_reset(API_QUOTA) == RESET_UTC                    # по умолчанию UTC+8
    assert duo.parse_quota_reset(API_QUOTA, 0) == RESET_UTC + 8 * 3600      # пояс настраивается
    assert duo.parse_quota_reset("API Error: 429 [1308] Usage limit reached") is None
    assert duo.parse_quota_reset("reset at 2026-13-45 99:00:00") is None
    assert duo.parse_quota_reset(None) is None and duo.parse_quota_reset("") is None


class _OneShot:
    """Транспорт исполнителя: отдаёт заранее заданные ответы (text, sid, cost) и считает вызовы."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        self.calls += 1
        return self.responses.pop(0)


def test_executor_quota_raises_without_retries_and_charges_cost(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    cfg = {"timeouts": {"executor_sec": 5, "api_retry_wait_sec": 30}, "limits": {"executor_api_retries": 3}}
    state = {"executor_session": "sid-0", "spent_usd": 1.0, "artifacts_rel": "pipeline/demo"}
    t = _OneShot([(API_QUOTA, "sid-1", 0.4)])
    ex = duo.Executor(cfg, t, tmp_path, tmp_path, state)
    with pytest.raises(duo.QuotaExhaustedError) as ei:
        ex._send("ПРОМПТ", "executor_impl_v1")
    assert t.calls == 1 and sleeps == []                                # ни повторов, ни пауз
    assert ei.value.kind == "transport" and ei.value.reset_at == RESET_UTC
    assert state["spent_usd"] == 1.4 and state["executor_session"] == "sid-1"
    assert list((tmp_path / "transcripts").glob("*_executor_impl_v1.md"))     # след остался


def test_chairman_quota_stops_without_pause_and_keeps_cost(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [(API_QUOTA, 0.2, {}), (_mini_report(), 0.1, {})], monkeypatch)
    with pytest.raises(duo.QuotaExhaustedError) as ei:
        rv._chairman_with_retry("БАЗА", persona="gamma")
    assert len(t.calls) == 1 and sleeps == []
    assert rv.call_cost == pytest.approx(0.2) and ei.value.reset_at == RESET_UTC


@pytest.mark.parametrize("script,calls,cost", [
    ([(API_QUOTA, 0.1, {})], 1, 0.1),                                   # квота на первой попытке
    ([("", 0.1, {}), (API_QUOTA, 0.15, {})], 2, 0.25),                  # заглушка, затем квота на повторе
])
def test_reviewer_call_quota_has_no_retry_cascade(tmp_path: Path, monkeypatch, script, calls, cost):
    rv, t, sleeps = _rv(tmp_path, script, monkeypatch)
    with pytest.raises(duo.QuotaExhaustedError):
        rv._reviewer_call("ПРОМПТ", "r1_alpha", persona="alpha")
    assert len(t.calls) == calls and sleeps == []
    assert rv.call_cost == pytest.approx(cost)


def test_pipeline_quota_halts_transport_with_cause_and_reset(tmp_path: Path, monkeypatch):
    st, td, t = _pair_run(tmp_path, monkeypatch, API_QUOTA, 0.7)
    assert t.reviewer_calls == 1                                       # одна попытка, без «ретрая с уточнением»
    h = st["halt"]
    assert h["kind"] == "transport" and h["cause"] == "quota" and h["reset_at"] == RESET_UTC
    assert h["resumable"] is True and st["quota_halts"] == 1
    assert st["spent_usd"] == pytest.approx(0.7)                        # цена попытки в бюджете (Н42)
    assert not list(td.glob("rev*_plan.md")) and st.get("verdicts", []) == []
    assert "Сброс квоты" in st["stop_reason"] and "--wait-quota" in st["stop_reason"]
    assert st["review_aborted"]["count"] == 1                          # Н49: оборванное ревью учтено


def test_wait_result_and_status_show_quota_cause(tmp_path: Path, capsys):
    cfg, arts = _repo_cfg(tmp_path)
    _mk_state(arts, "q", phase="needs_human", state_updated_at=1.0, stop_reason="квота",
              halt={"kind": "transport", "phase": "impl", "resumable": True, "cause": "quota",
                    "reset_at": RESET_UTC})
    assert duo.cmd_wait(cfg, _wait_ns("q")) == duo.WAIT_EXIT["halted"]
    out = capsys.readouterr().out
    assert "halt=transport cause=quota reset=2026-11-25T13:39Z" in out


# --- Н49: счётчики оборванных ревью ---------------------------------------------------------------


def test_aborted_review_retries_are_charged_and_shown_in_status(tmp_path: Path, monkeypatch, capsys):
    st, td, t = _pair_run(tmp_path, monkeypatch, API_529, 0.0, overload_retries=1)
    ab = st["review_aborted"]
    assert ab["count"] == 1 and ab["overload_retries"] == 1      # пауза на 529 до оборванного ревью
    assert st.get("review_telemetry", []) == []                        # ревью не завершилось — телеметрии нет
    repo = td.parent.parent
    capsys.readouterr()                                                # лог серии уже в буфере — до status
    duo.cmd_status(_pcfg(tmp_path, repo), argparse.Namespace(task="demo", json=True, all=False))
    obj = json.loads(capsys.readouterr().out)
    rt = obj["review_transport"]
    assert rt["aborted_reviews"] == 1 and rt["reviews"] == 0
    assert rt["overload_retries"] == ab["overload_retries"] == 1 and rt["stub_retries"] == ab["stub_retries"]


def test_review_transport_totals_sums_completed_and_aborted():
    st = {"review_telemetry": [{"stub_retries": 1, "empty_retries": 1}, {"overload_retries": 2}],
          "review_aborted": {"count": 2, "stub_retries": 3, "overload_retries": 1}, "quota_halts": 1}
    rt = duo.review_transport_totals(st)
    assert rt["stub_retries"] == 4 and rt["overload_retries"] == 3 and rt["empty_retries"] == 1
    assert rt["reviews"] == 2 and rt["aborted_reviews"] == 2 and rt["quota_halts"] == 1
    zero = duo.review_transport_totals({})
    assert zero["reviews"] == 0 and zero["aborted_reviews"] == 0 and zero["stub_retries"] == 0


def test_review_attaches_events_to_exception(tmp_path: Path, monkeypatch):
    rv, _t, _s = _rv(tmp_path, [], monkeypatch)

    def boom(**_kw):
        rv._bump("stub_retries")
        rv._bump("overload_retries")
        raise RuntimeError("сбой")

    monkeypatch.setattr(rv, "_review_impl", boom)
    with pytest.raises(RuntimeError) as ei:
        rv.review(artifact_rel="x")
    assert ei.value.review_events["stub_retries"] == 1 and ei.value.review_events["overload_retries"] == 1


# --- Н46: ожидание сброса (`run --resume --wait-quota`) -----------------------------------------------


def _quota_pipeline(tmp_path: Path, halt: dict | None, **cfg_over):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["phase"] = "needs_human"
    if halt is not None:
        st["halt"] = halt
    duo.save_json(td / "state.json", st)
    cfg = _pcfg(tmp_path, repo)
    cfg.update(cfg_over)
    return duo.Pipeline(cfg, "demo", mode="pair")


class _Clock:
    def __init__(self, now: float):
        self.now, self.slept = now, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def _fake_time(monkeypatch, clock: _Clock):
    monkeypatch.setattr(duo, "time", types.SimpleNamespace(time=clock.time, sleep=clock.sleep))


def test_wait_quota_sleeps_until_reset_plus_margin_in_chunks(tmp_path: Path, monkeypatch):
    p = _quota_pipeline(tmp_path, {"kind": "transport", "phase": "plan", "resumable": True, "cause": "quota",
                                   "reset_at": RESET_UTC})
    clock = _Clock(RESET_UTC - 200)
    _fake_time(monkeypatch, clock)
    p._wait_quota_reset()
    assert clock.now >= RESET_UTC + 90                                 # метка + resume_margin_sec
    assert all(s <= 60 for s in clock.slept) and len(clock.slept) >= 5  # короткие куски (замок виден как живой)
    assert clock.now - (RESET_UTC + 90) < 60


def test_wait_quota_does_not_wait_for_other_halts_or_missing_reset(tmp_path: Path, monkeypatch):
    cases = ({"kind": "transport", "phase": "plan", "resumable": True},                        # не по квоте
             {"kind": "transport", "phase": "plan", "resumable": True, "cause": "quota"})      # нет метки
    for i, halt in enumerate(cases):
        (tmp_path / f"c{i}").mkdir()
        p = _quota_pipeline(tmp_path / f"c{i}", halt)
        clock = _Clock(RESET_UTC - 1000)
        _fake_time(monkeypatch, clock)
        p._wait_quota_reset()
        assert clock.slept == []


def test_wait_quota_refuses_when_reset_is_too_far(tmp_path: Path, monkeypatch):
    p = _quota_pipeline(tmp_path, {"kind": "transport", "phase": "plan", "resumable": True, "cause": "quota",
                                   "reset_at": RESET_UTC}, quota={"max_wait_sec": 600})
    clock = _Clock(RESET_UTC - 7200)
    _fake_time(monkeypatch, clock)
    with pytest.raises(SystemExit) as ei:
        p._wait_quota_reset()
    assert "ОТКАЗ" in str(ei.value) and "max_wait_sec" in str(ei.value) and clock.slept == []


# --- Н46: надзор -------------------------------------------------------------------------------------


def _quota_halted(arts: Path, name: str = "q", reset_at: float | None = RESET_UTC) -> None:
    halt = {"kind": "transport", "phase": "impl", "resumable": True, "cause": "quota", "at": "2026-11-25T10:00:00"}
    if reset_at is not None:
        halt["reset_at"] = reset_at
    _mk_state(arts, name, phase="needs_human", state_updated_at=1.0, halt=halt, stop_reason="квота")


def _tick(cfg: dict, tmp_path: Path, now: float, spawned: list, sp: str = "s.json") -> dict:
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / sp, auto_resume=True,
                              spawn=spawned.append, notify=lambda t, b: None)
    return {a["task"]: a for a in acts}


def test_supervisor_waits_for_quota_reset_then_resumes_without_spending_max_resumes(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    _quota_halted(arts)
    spawned: list = []
    _tick(cfg, tmp_path, RESET_UTC - 1000, spawned)                       # baseline-тик
    a = _tick(cfg, tmp_path, RESET_UTC - 600, spawned)["q"]
    assert a["action"] == "quota-wait" and spawned == [] and "сброс" in a["reason"]
    a = _tick(cfg, tmp_path, RESET_UTC + 30, spawned)["q"]                 # метка прошла, запас (90 с) — нет
    assert a["action"] == "quota-wait" and spawned == []
    a = _tick(cfg, tmp_path, RESET_UTC + 100, spawned)["q"]
    assert a["action"] == "resumed" and spawned == ["q"]
    sstate = duo.load_json(tmp_path / "s.json")
    rec = next(iter(sstate.values()))
    assert rec.get("quota_resumes") and not rec.get("resumes")             # max_resumes не тронут


def test_supervisor_quota_without_reset_label_does_not_resume(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    _quota_halted(arts, reset_at=None)
    spawned: list = []
    a = _tick(cfg, tmp_path, RESET_UTC + 10_000, spawned)["q"]
    assert a["action"] == "quota-no-reset" and spawned == []


def test_supervisor_quota_has_its_own_cap_and_regular_halts_keep_theirs(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["supervisor"] = {"max_quota_resumes": 1}
    _quota_halted(arts)
    spawned: list = []
    now = RESET_UTC + 500
    assert _tick(cfg, tmp_path, now, spawned)["q"]["action"] == "resumed"
    assert _tick(cfg, tmp_path, now + 60, spawned)["q"]["action"] == "resume-limit"     # петля «метка врёт»
    # обычный transport-halt без cause: прежнее поведение (max_resumes=3 по умолчанию)
    _mk_state(arts, "plain", phase="needs_human", state_updated_at=1.0, stop_reason="сеть",
              halt={"kind": "transport", "phase": "impl", "resumable": True, "at": "x"})
    assert _tick(cfg, tmp_path, now + 120, spawned, "s2.json")["plain"]["action"] == "resumed"


@pytest.mark.parametrize("over,needle", [
    ({"quota": {"provider_utc_offset_hours": "восемь"}}, "quota.provider_utc_offset_hours"),
    ({"quota": {"provider_utc_offset_hours": 99}}, "quota.provider_utc_offset_hours"),
    ({"quota": {"resume_margin_sec": -1}}, "quota.resume_margin_sec"),
    ({"quota": ["x"]}, "quota должен быть объектом"),
    ({"supervisor": {"max_quota_resumes": -1}}, "max_quota_resumes"),
])
def test_guard_config_problems_for_quota(over, needle):
    assert any(needle in p for p in duo.guard_config_problems(over)), over
    assert duo.guard_config_problems(duo.DEFAULTS) == []
    assert duo.DEFAULTS["quota"]["provider_utc_offset_hours"] == 8 and duo.DEFAULTS["supervisor"]["max_quota_resumes"] >= 1



# --- Н50: окружение (claude/node вне PATH) -----------------------------------------------------------


def _fake_claude(bin_dir: Path, shebang: str | None) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    f = bin_dir / "claude"
    f.write_text((shebang + "\n" if shebang else "\x7fELF-binary\n") + "echo hi\n", encoding="utf-8")
    f.chmod(0o755)
    return f


def test_env_problem_claude_missing_from_path(tmp_path: Path):
    msg = duo.claude_env_problem({}, path_env=str(tmp_path / "empty"))
    assert msg and "`claude` не найден в PATH" in msg and "claude_bin" in msg


def test_env_problem_script_with_missing_interpreter(tmp_path: Path):
    """Поле: npm-`claude` — скрипт `#!/usr/bin/env node`; у службы в PATH нашёлся claude, но не node."""
    _fake_claude(tmp_path / "bin", "#!/usr/bin/env node")
    msg = duo.claude_env_problem({}, path_env=str(tmp_path / "bin"))
    assert msg and "интерпретатор `node`" in msg and "shebang" in msg
    nodedir = tmp_path / "nodebin"
    nodedir.mkdir()
    (nodedir / "node").write_text("#!/bin/sh\n", encoding="utf-8")
    (nodedir / "node").chmod(0o755)
    assert duo.claude_env_problem({}, path_env=f"{tmp_path / 'bin'}:{nodedir}") is None
    _fake_claude(tmp_path / "sbin", "#!/usr/bin/env -S node --flag")        # опции env пропускаются
    assert "`node`" in duo.claude_env_problem({}, path_env=str(tmp_path / "sbin"))


def test_env_problem_binary_and_absolute_claude_bin(tmp_path: Path):
    exe = _fake_claude(tmp_path / "x", None)                                # не скрипт — shebang не проверяем
    assert duo.claude_env_problem({"claude_bin": str(exe)}, path_env="") is None
    msg = duo.claude_env_problem({"claude_bin": str(tmp_path / "нет" / "claude")}, path_env="/usr/bin")
    assert msg and "не найден" in msg
    assert duo.claude_executable({}) == "claude" and duo.claude_executable({"claude_bin": "/o/claude"}) == "/o/claude"


def test_popen_file_not_found_is_environment_problem_not_crash(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    with pytest.raises(duo.EnvironmentProblem) as ei:
        glm.call("p", mode="plan", timeout=5)
    assert "не найден в PATH" in str(ei.value) and not isinstance(ei.value, duo.ClaudeTransportError)


def test_missing_cwd_is_not_reported_as_environment(tmp_path: Path, monkeypatch):
    exe = _fake_claude(tmp_path / "bin", None)
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m", "claude_bin": str(exe)})
    with pytest.raises(FileNotFoundError):                                   # каталога нет — другая поломка
        glm.call("p", mode="plan", timeout=5, cwd=tmp_path / "нет-такого")


def test_environment_problem_is_not_retried_by_reviewer(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [duo.EnvironmentProblem("claude не найден"), (_mini_report(), 0.1, {})],
                        monkeypatch, limits={"reviewer_retries": 3, "overload_retries": 2})
    with pytest.raises(duo.EnvironmentProblem):
        rv._chairman_with_retry("БАЗА", persona="gamma")
    assert len(t.calls) == 1 and sleeps == []                               # ни ретраев, ни пауз, ни «двойного таймаута»


def test_executor_environment_problem_propagates_without_resume_fallback(tmp_path: Path):
    class Boom:
        calls = 0

        def call(self, *a, **kw):
            Boom.calls += 1
            raise duo.EnvironmentProblem("resume: claude не найден")        # слово resume — не повод для «новой сессии»

    cfg = {"timeouts": {"executor_sec": 5}, "limits": {"executor_api_retries": 3}}
    ex = duo.Executor(cfg, Boom(), tmp_path, tmp_path, {"executor_session": "sid", "spent_usd": 0.0})
    with pytest.raises(duo.EnvironmentProblem):
        ex._send("p", "executor_impl_v1")
    assert Boom.calls == 1


def test_pipeline_environment_problem_halts_env_not_crash(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")

    def boom(*a, **kw):
        raise duo.EnvironmentProblem("`claude` не найден в PATH")

    monkeypatch.setattr(p, "_run_impl", boom)
    p.run()
    st = _state(td)
    assert st["halt"]["kind"] == "env" and st["halt"]["cause"] == "env" and st["halt"]["resumable"] is True
    assert "не найден в PATH" in st["stop_reason"] and "--resume" in st["stop_reason"]
    assert "env" not in duo.DEFAULTS["supervisor"]["auto_resume_kinds"]    # надзор такие остановки не возобновляет


def test_run_preflight_refuses_before_touching_state(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    before = (td / "state.json").read_text(encoding="utf-8")
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")            # настоящий ClaudeGlm, claude нет
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))                     # git нужен только до этой строки
    with pytest.raises(SystemExit) as ei:
        p.run()
    assert "ОТКАЗ" in str(ei.value) and "не найден в PATH" in str(ei.value)
    assert (td / "state.json").read_text(encoding="utf-8") == before        # серия не тронута (не crash)


def test_run_preflight_is_skipped_for_substituted_transport(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    p.t = object()
    p._preflight_env()                                                      # не бросает: вызывать нечего


def test_supervisor_does_not_resume_into_broken_environment(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    now = 2_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)          # dead: оркестратор умер без halt
    spawned: list = []
    kw = dict(state_path=tmp_path / "s.json", auto_resume=True, spawn=spawned.append, notify=lambda t, b: None)
    acts = duo.supervise_tick(cfg, now=now, env_check=lambda c: "claude не найден в PATH", **kw)
    a = {x["task"]: x for x in acts}["t"]
    assert a["action"] == "env-broken" and spawned == [] and "не найден" in a["reason"]
    rec = next(iter(duo.load_json(tmp_path / "s.json").values()))
    assert not rec.get("resumes")                                           # max_resumes не потрачен
    acts = duo.supervise_tick(cfg, now=now + 120, env_check=lambda c: None, **kw)
    assert {x["task"]: x for x in acts}["t"]["action"] == "resumed" and spawned == ["t"]


def test_supervisor_alert_mentions_broken_environment(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["notify"] = {"desktop": True}
    _mk_state(arts, "t", phase="impl", state_updated_at=2_000_000.0 - 1000)
    sent: list = []
    duo.supervise_tick(cfg, now=2_000_000.0, state_path=tmp_path / "s.json", auto_resume=True,
                       spawn=lambda t: None, notify=lambda title, body: sent.append(body),
                       env_check=lambda c: "`claude` не найден в PATH (/usr/bin)")
    assert sent and "авто-возобновление невозможно" in sent[0] and "не найден в PATH" in sent[0]


def test_supervisor_default_spawn_runs_real_preflight_only_when_spawn_is_real(tmp_path: Path, monkeypatch):
    cfg, arts = _repo_cfg(tmp_path)
    _mk_state(arts, "t", phase="impl", state_updated_at=2_000_000.0 - 1000)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    spawned: list = []
    # подставной spawn — проверка PATH не вмешивается (тесты надзора не зависят от машины)
    acts = duo.supervise_tick(cfg, now=2_000_000.0, state_path=tmp_path / "a.json", auto_resume=True,
                              spawn=spawned.append, notify=lambda t, b: None)
    assert {x["task"]: x for x in acts}["t"]["action"] == "resumed"
    # настоящий spawn (по умолчанию): PATH службы без claude → env-broken, процесс не запускается
    started: list = []
    monkeypatch.setattr(duo, "run_detached", lambda *a, **kw: started.append(a))
    acts = duo.supervise_tick(cfg, now=2_000_000.0, state_path=tmp_path / "b.json", auto_resume=True,
                              notify=lambda t, b: None)
    assert {x["task"]: x for x in acts}["t"]["action"] == "env-broken" and started == []


# --- Н47: раунды дуэта переживают остановку ----------------------------------------------------------


class _Flaky(ScriptedTransport):
    """Дуэт на стабах; ответы с маркером `fail_marker` в промпте — квота провайдера, пока `broken` выставлен."""

    def __init__(self, fail_marker: str):
        super().__init__()
        self.fail_marker, self.broken = fail_marker, True

    def call(self, prompt, **kw):
        if self.broken and self.fail_marker in prompt:
            self.calls.append(prompt)
            return API_QUOTA, None, 0.3
        return super().call(prompt, **kw)


def _duo_td(tmp_path: Path) -> tuple[Path, Path, dict]:
    repo = _init_repo(tmp_path)
    td = tmp_path / "td"
    td.mkdir()
    return repo, td, _cfg(tmp_path, repo)


def test_duo_resume_reuses_finished_rounds_after_chairman_quota(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    repo, td, cfg = _duo_td(tmp_path)
    t = _Flaky("Председатель консилиума")
    with pytest.raises(duo.QuotaExhaustedError):
        _review(tmp_path, repo, cfg, t, task_dir=td)
    assert len(t.calls) == 7                                          # R1×2, R2×2, R3×2 и один вызов председателя
    cache = duo.load_json(td / "transcripts" / duo.ROUNDS_CACHE_NAME)
    assert {"r1", "r2", "r3"} <= set(cache) and not (td / "rev001_plan.md").exists()
    t.broken = False
    verdict, cost, tel = _review(tmp_path, repo, cfg, t, task_dir=td)
    assert len(t.calls) == 8                                          # resume: ТОЛЬКО председатель, раунды не пересчитаны
    assert verdict == "REVISE" and tel["rounds_restored"] == ["r1", "r2", "r3"]
    assert not (td / "transcripts" / duo.ROUNDS_CACHE_NAME).exists()   # ревью завершено — кэш убран
    assert (td / "rev001_plan.md").is_file()


def test_duo_resume_reuses_only_finished_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    repo, td, cfg = _duo_td(tmp_path)
    t = _Flaky("раунд 2 дуо-консилиума")                                # квота на R2: R1 завершён, R2 — нет
    with pytest.raises(duo.QuotaExhaustedError):
        _review(tmp_path, repo, cfg, t, task_dir=td)
    cache = duo.load_json(td / "transcripts" / duo.ROUNDS_CACHE_NAME)
    assert "r1" in cache and "r2" not in cache and "r3" not in cache
    before = len(t.calls)
    t.broken = False
    _v, _c, tel = _review(tmp_path, repo, cfg, t, task_dir=td)
    assert tel["rounds_restored"] == ["r1"]
    assert len(t.calls) - before == 5                                 # R2×2 + R3×2 + председатель; R1 не звали


def test_duo_cache_is_ignored_when_artifact_changed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    repo, td, cfg = _duo_td(tmp_path)
    t = _Flaky("Председатель консилиума")
    with pytest.raises(duo.QuotaExhaustedError):
        _review(tmp_path, repo, cfg, t, task_dir=td)
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("ПЕРЕДЕЛАННЫЙ план\n", encoding="utf-8")
    t.broken = False
    before = len(t.calls)
    _v, _c, tel = _review(tmp_path, repo, cfg, t, task_dir=td)
    assert "rounds_restored" not in tel and len(t.calls) - before == 7  # вход другой — честный полный цикл


def test_duo_cache_is_ignored_when_task_or_members_changed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    repo, td, cfg = _duo_td(tmp_path)
    t = _Flaky("Председатель консилиума")
    with pytest.raises(duo.QuotaExhaustedError):
        _review(tmp_path, repo, cfg, t, task_dir=td)
    key_a = duo.load_json(td / "transcripts" / duo.ROUNDS_CACHE_NAME)["key"]
    rv = duo.DuoReviewer(cfg, t, td, repo)
    A, B = duo.load_member(cfg, "alpha"), duo.load_member(cfg, "beta")
    same = rv._rounds_key("plan", "pipeline/demo/plan_v1.md", "текст задачи", A, B, 3, "", "", None)
    assert same == key_a
    for kw in ({"task_text": "ДРУГАЯ задача"}, {"rounds": 2}, {"rejected": "х"}, {"pack": "пак"}, {"errata": "э"}):
        args = {"phase": "plan", "artifact_rel": "pipeline/demo/plan_v1.md", "task_text": "текст задачи",
                "rounds": 3, "rejected": "", "errata": "", "pack": None, **kw}
        assert rv._rounds_key(args["phase"], args["artifact_rel"], args["task_text"], A, B, args["rounds"],
                              args["rejected"], args["errata"], args["pack"]) != key_a, kw
    assert rv._rounds_key("plan", "pipeline/demo/plan_v1.md", "текст задачи", B, A, 3, "", "", None) != key_a


def test_duo_stub_round_is_not_cached(tmp_path: Path):
    repo, td, cfg = _duo_td(tmp_path)
    rv = duo.DuoReviewer(cfg, ScriptedTransport(), td, repo)
    rv._rounds_load("k", 3)
    rv._rounds_save("k", "r1", "", "нормальный ответ")                 # пустышка — не кэшируем
    rv._rounds_save("k", "r1", "API Error: 529 перегрузка", "нормальный")
    assert not (td / "transcripts" / duo.ROUNDS_CACHE_NAME).exists()
    rv._rounds_save("k", "r1", "ответ A", "ответ B")
    assert rv._rounds_load("k", 3) == {"r1": ("ответ A", "ответ B")}
    assert rv._rounds_load("другой-ключ", 3) == {}


def test_duo_cache_ignores_gaps_and_garbage(tmp_path: Path):
    repo, td, cfg = _duo_td(tmp_path)
    rv = duo.DuoReviewer(cfg, ScriptedTransport(), td, repo)
    path = td / "transcripts" / duo.ROUNDS_CACHE_NAME
    path.parent.mkdir(exist_ok=True)
    duo.save_json(path, {"v": 1, "key": "k", "r2": ["a", "b"], "r3": ["a", "b"]})      # без r1 — дыра
    assert rv._rounds_load("k", 3) == {}
    duo.save_json(path, {"v": 1, "key": "k", "r1": ["a", "b"], "r2": ["a", ""], "r3": ["a", "b"]})
    assert rv._rounds_load("k", 3) == {"r1": ("a", "b")}                                # пустой ответ обрывает префикс
    path.write_text("{не json", encoding="utf-8")
    assert rv._rounds_load("k", 3) == {}
    duo.save_json(path, {"v": 2, "key": "k", "r1": ["a", "b"]})
    assert rv._rounds_load("k", 3) == {}                                                # чужая версия формата


def test_duo_two_rounds_config_ignores_cached_r3(tmp_path: Path):
    repo, td, cfg = _duo_td(tmp_path)
    rv = duo.DuoReviewer(cfg, ScriptedTransport(), td, repo)
    path = td / "transcripts" / duo.ROUNDS_CACHE_NAME
    path.parent.mkdir(exist_ok=True)
    duo.save_json(path, {"v": 1, "key": "k", "r1": ["a", "b"], "r2": ["c", "d"], "r3": ["e", "f"]})
    assert set(rv._rounds_load("k", 2)) == {"r1", "r2"}


# --- Н48: сводка сходимости и правила приёмки --------------------------------------------------------

_REV_FINDINGS = """# rev — ревью кода

## 0. Вердикт
Доработать.

## 1. Замечания
1. Н1 (src/a.py:10) — блокер — потеря данных при сбое ротации.
2. Н2 (src/b.py:5) — существенное — fsync-сбой в `TryRotate`; на Linux ветка недостижима, потерь данных нет.
3. Н3 (README) — косметика — опечатка.
   - вложенный подпункт — существенное (не считается: отступ)
- Блокеров нет в тестах — это не замечание.
- Н4 — существенное — тест не различает фикс и дефект.

## 2. Что проверено
- 1. не считается — другой раздел, блокер/существенное.

VERDICT: REVISE
"""


def test_rev_findings_counts_must_fix_and_latent_items():
    f = duo.rev_findings(_REV_FINDINGS)
    assert f == {"blocker": 1, "major": 2, "cosmetic": 1, "latent": 1}
    assert duo.rev_findings("# rev\n\nбез раздела замечаний\n") is None
    assert duo.rev_findings("") is None


def _conv_state(td: Path, rows: list[tuple[str, str, str, float]]) -> dict:
    st = {"verdicts": [], "review_telemetry": []}
    for name, verdict, body, cost in rows:
        (td / name).write_text(body, encoding="utf-8")
        st["verdicts"].append({"rev": name, "phase": "impl", "verdict": verdict})
        st["review_telemetry"].append({"phase": "impl", "cost_usd": cost})
    return st


def test_convergence_summary_lists_rounds_cost_trend_and_latent(tmp_path: Path):
    many = "## 1. Замечания\n" + "".join(f"{i}. Н{i} — блокер — x\n" for i in range(1, 4))
    one_latent = "## 1. Замечания\n1. Н1 — существенное — латентно, потерь данных нет\n"
    st = _conv_state(tmp_path, [("rev001_impl.md", "REVISE", many, 11.87),
                                ("rev002_impl.md", "REVISE", one_latent, 11.97)])
    text = duo.convergence_summary(tmp_path, st, "impl")
    assert "rev001 REVISE 3 $11.87 → rev002 REVISE 1 $11.97" in text
    assert "число обязательных убывает" in text and "rev002: 1" in text and "эвристика" in text
    st = _conv_state(tmp_path, [("rev001_impl.md", "REVISE", one_latent, 1.0),
                                ("rev002_impl.md", "REVISE", many, 2.0)])
    assert "НЕ убывает" in duo.convergence_summary(tmp_path, st, "impl")


def test_convergence_summary_is_empty_without_data_and_survives_garbage(tmp_path: Path):
    assert duo.convergence_summary(tmp_path, {}, "impl") == ""
    one = {"verdicts": [{"rev": "rev001_impl.md", "phase": "impl", "verdict": "REVISE"}]}
    assert duo.convergence_summary(tmp_path, one, "impl") == ""                   # мало данных
    broken = {"verdicts": [{"rev": "rev001_impl.md", "phase": "impl", "verdict": "REVISE"},
                           {"rev": "rev002_impl.md", "phase": "impl", "verdict": "REVISE"}]}
    assert duo.convergence_summary(tmp_path, broken, "impl") == ""                # файлов нет — не падаем
    mixed = {"verdicts": [{"rev": "manual", "phase": "manual", "verdict": "ACCEPT"}]}
    assert duo.convergence_summary(tmp_path, mixed, "impl") == ""


def test_limit_halt_message_carries_convergence_summary(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    st = p.load_state()
    body = "## 1. Замечания\n1. Н1 — блокер — x\n"
    st.update(_conv_state(td, [("rev001_impl.md", "REVISE", body, 5.0), ("rev002_impl.md", "REVISE", body, 6.0)]))
    st["impl_iteration"] = 99
    assert p._boundary_stop(st, "impl", td / "rev002_impl.md", "pipeline/demo") is True
    reason = _state(td)["stop_reason"]
    assert "Подними limits.max_impl_iterations" in reason and "Сходимость" in reason and "rev001 REVISE 1 $5.00" in reason
    assert _state(td)["halt"]["kind"] == "limit"


def test_impl_policy_prompts_carry_backlog_rules_only_when_strict():
    block = duo.IMPL_POLICY_CHAIRMAN_BLOCK
    assert "BACKLOG" in block and "латентно" in block and "НАБЛЮДАЕМЫМ эффектом" in block
    assert "Косметику не включай в «Сводку для исполнителя»" in block and "номеров строк" in block
    assert "не по номерам строк" in duo.IMPL_POLICY_EXECUTOR_REPORT
    assert duo.impl_strict_enabled({"review": {"impl_policy": "strict"}}) is True
    assert duo.impl_strict_enabled({"review": {"impl_policy": "legacy"}}) is False       # legacy — промпты прежние


# --- Н51 / Н52: собственная область systemd и гигиена прогонов -----------------------------------------


def _cg_cfg(tmp_path: Path, **par) -> dict:
    return {"repo": str(tmp_path / "myrepo"), "parallel": {"isolate_cgroup": True, **par}}


def test_cgroup_wrap_off_by_default_keeps_command():
    cmd = ["python", "duo.py", "run"]
    assert duo.cgroup_wrap({}, "t", cmd) == (cmd, "")
    assert duo.cgroup_wrap({"parallel": {"isolate_cgroup": False}}, "t", cmd) == (cmd, "")
    assert duo.DEFAULTS["parallel"]["isolate_cgroup"] is False


def test_cgroup_wrap_builds_systemd_run_scope_with_limits(tmp_path: Path):
    cmd = ["python", "duo.py", "--fg"]
    wrapped, note = duo.cgroup_wrap(_cg_cfg(tmp_path, memory_max="8G", memory_swap_max="2G"), "ser 1", cmd,
                                    which=lambda n: f"/bin/{n}", environ={"XDG_RUNTIME_DIR": "/run/user/1"},
                                    now=1234567.0)
    assert wrapped[:3] == ["/bin/systemd-run", "--user", "--scope"]
    assert "--unit=duo-myrepo-ser_1-1234567" in wrapped                 # имя юнита очищено от пробела
    assert wrapped[wrapped.index("-p") + 1] == "MemoryMax=8G"
    assert "MemorySwapMax=2G" in wrapped
    assert wrapped[wrapped.index("--") + 1:] == cmd                     # исходная команда — после `--`, без изменений
    assert "duo-myrepo-ser_1-1234567.scope" in note and "MemoryMax=8G" in note


def test_cgroup_wrap_without_limits_has_no_property_flags(tmp_path: Path):
    wrapped, note = duo.cgroup_wrap(_cg_cfg(tmp_path), "t", ["x"], which=lambda n: "/usr/bin/systemd-run",
                                    environ={"XDG_RUNTIME_DIR": "/run/user/1"}, now=1.0)
    assert "-p" not in wrapped and "без лимитов памяти" in note


def test_cgroup_wrap_unavailable_systemd_warns_and_keeps_command(tmp_path: Path):
    cmd = ["python", "duo.py"]
    same, note = duo.cgroup_wrap(_cg_cfg(tmp_path), "t", cmd, which=lambda n: None,
                                 environ={"XDG_RUNTIME_DIR": "/run/user/1"})
    assert same == cmd and "systemd-run не найден" in note and "ВНИМАНИЕ" in note
    same, note = duo.cgroup_wrap(_cg_cfg(tmp_path), "t", cmd, which=lambda n: "/bin/systemd-run", environ={})
    assert same == cmd and "XDG_RUNTIME_DIR" in note                    # не молчим: изоляции нет


def test_run_detached_launches_orchestrator_through_systemd_run(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = {"repo": str(repo), "artifacts_dir": "pipeline", "parallel": {"isolate_cgroup": True, "memory_max": "4G"}}
    monkeypatch.setattr(duo.shutil, "which", lambda n: "/bin/systemd-run" if n == "systemd-run" else None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(duo, "DETACHED_CONFIRM_SEC", 0.0)
    seen: list = []

    class _Alive:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(duo.subprocess, "Popen", lambda cmd, **kw: seen.append((list(cmd), kw)) or _Alive())
    duo.run_detached(cfg, "demo", argv=["--config", "c.json", "run", "--task", "demo"])
    cmd, kw = seen[0]
    assert cmd[:3] == ["/bin/systemd-run", "--user", "--scope"] and "MemoryMax=4G" in cmd
    assert cmd[cmd.index("--") + 1] == duo.sys.executable and cmd[-1] == "--fg"
    assert kw["start_new_session"] is True                              # прежнее отсоединение от терминала сохранено
    assert "cgroup: оркестратор в области" in capsys.readouterr().out


def test_run_detached_default_command_is_unchanged(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = {"repo": str(repo), "artifacts_dir": "pipeline"}
    monkeypatch.setattr(duo, "DETACHED_CONFIRM_SEC", 0.0)
    seen: list = []

    class _Alive:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(duo.subprocess, "Popen", lambda cmd, **kw: seen.append(list(cmd)) or _Alive())
    duo.run_detached(cfg, "demo", argv=["run", "--task", "demo"])
    assert seen[0][0] == duo.sys.executable and "systemd-run" not in " ".join(seen[0])


@pytest.mark.parametrize("par,needle", [
    ({"isolate_cgroup": "yes"}, "parallel.isolate_cgroup"),
    ({"memory_max": "много"}, "parallel.memory_max"),
    ({"memory_swap_max": "8 GB"}, "parallel.memory_swap_max"),
    ({"memory_max": True}, "parallel.memory_max"),
])
def test_guard_config_problems_for_cgroup(par, needle):
    assert any(needle in p for p in duo.guard_config_problems({"parallel": par})), par


@pytest.mark.parametrize("v", ["8G", "512M", "1024", 2048, "50%", "infinity", "1.5G", ""])
def test_guard_config_accepts_systemd_sizes(v):
    assert duo.guard_config_problems({"parallel": {"isolate_cgroup": True, "memory_max": v}}) == []


def test_test_hygiene_block_reaches_executor_prompts_only_in_strict_mode(tmp_path: Path):
    h = duo.IMPL_POLICY_EXECUTOR_HYGIENE
    assert "timeout 300" in h and "≤ 40 строк" in h and "assert" in h and "/tmp" in h

    class _Cap:
        def __init__(self):
            self.prompts: list[str] = []

        def call(self, prompt, **kw):
            self.prompts.append(prompt)
            return "ok", "sid", 0.0

    def run(strict: bool) -> str:
        sub = tmp_path / ("s" if strict else "l")
        sub.mkdir()
        (sub / "pipeline" / "demo").mkdir(parents=True)
        (sub / "pipeline" / "demo" / "impl_report_v1.md").write_text("# r\n", encoding="utf-8")
        cfg = {"timeouts": {"executor_sec": 5}, "review": {"impl_policy": "strict" if strict else "legacy"}}
        t = _Cap()
        ex = duo.Executor(cfg, t, sub / "pipeline" / "demo", sub, {"artifacts_rel": "pipeline/demo", "spent_usd": 0.0})
        ex.implement("задача", "pipeline/demo/plan_v1.md", 1)
        return t.prompts[0]

    assert "Гигиена прогонов тестов" in run(True) and "Гигиена прогонов тестов" not in run(False)


# --- Н53: claim-gate — корень каталога против подкаталога ---------------------------------------------


@pytest.mark.parametrize("a,b,expected", [
    # поле: `БЛОК: … src/X.Cli/*.cs ⇄ src/X.Cli/Burst/*` при непересекающихся файлах
    ("src/Cli/*.cs", "src/Cli/Burst/*", False),
    ("src/Cli/*.cs", "src/Cli/Burst/*.cs", False),
    ("tests/Cli.Tests/*.cs", "tests/Cli.Tests/Sub/*.cs", False),
    ("*.md", "docs/*", False),                       # корень репо против подкаталога
    # прежнее поведение там, где пересечение возможно
    ("src/Cli/*", "src/Cli/Burst/*", True),          # голая `*` на хвосте (так нормализуется `dir/`) — поддерево
    ("src/Cli/*.cs", "src/Cli/*.js", True),          # один каталог: консервативно пересекаются
    ("src/Cli/*.cs", "src/Cli/*", True),
    ("src/Cli/*.cs", "src/Cli/Burst/x.cs", True),    # КОНКРЕТНЫЙ файл подкаталога под глобом — fnmatch, как прежде
    ("src/Cli/*.cs", "src/Cli", True),
    ("src/*/x.cs", "src/Cli/Burst/*", True),         # глоб не на хвосте — не «плоский», консервативно
    ("api/*.py", "web/*.js", False),
])
def test_claim_overlap_flat_glob_vs_subdirectory(a, b, expected):
    assert duo.patterns_overlap(a, b) is expected
    assert duo.patterns_overlap(b, a) is expected                       # симметрия


def test_claim_gate_does_not_block_root_against_subdirectory_but_blocks_real_files():
    mine = ["src/Cli/*.cs"]
    holder = {"task": "other", "phase": "impl", "declared": ["src/Cli/Burst/*"], "actual": []}
    assert duo.find_claim_conflicts(mine, [holder]) == []               # ложной блокировки больше нет
    holder["actual"] = ["src/Cli/Burst/Run.cs"]                         # держатель РЕАЛЬНО изменил файл подкаталога
    found = duo.find_claim_conflicts(mine, [holder])
    assert found and ("src/Cli/*.cs", "src/Cli/Burst/Run.cs") in found[0]["pairs"]   # настоящее пересечение ловится


# --- Н54: второй канал уведомлений и причина `dead` ------------------------------------------------


class _Run:
    """Подмена subprocess.run для канала-команды: фиксирует вызовы, отдаёт заданный итог."""

    def __init__(self, rc=0, stderr=b"", exc=None):
        self.rc, self.stderr, self.exc, self.calls = rc, stderr, exc, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(returncode=self.rc, stderr=self.stderr)


def test_notify_command_substitutes_title_body_without_shell(tmp_path: Path):
    cfg = {"notify": {"command": ["/opt/send", "--subject", "{title}", "--text", "{body}"], "command_timeout_sec": 7}}
    run = _Run()
    out = duo.run_notify_command(cfg, "duo p/t: halted", "потрачено $1; rm -rf / ; $(x)", run=run)
    assert out == "доставлено (команда `/opt/send` rc=0)"
    argv, kw = run.calls[0]
    assert argv == ["/opt/send", "--subject", "duo p/t: halted", "--text", "потрачено $1; rm -rf / ; $(x)"]
    assert kw["timeout"] == 7.0 and "shell" not in kw                     # тело — данные в argv, не код
    assert kw["env"]["DUO_NOTIFY_TITLE"] == "duo p/t: halted" and "rm -rf" in kw["env"]["DUO_NOTIFY_BODY"]


@pytest.mark.parametrize("run,expect", [
    (_Run(rc=3, stderr=b"smtp refused"), "НЕ доставлено: команда `/opt/send` rc=3 smtp refused"),
    (_Run(exc=FileNotFoundError("x")), "НЕ доставлено: команда `/opt/send` не найдена"),
    (_Run(exc=duo.subprocess.TimeoutExpired("x", 5)), "не завершилась за"),
    (_Run(exc=RuntimeError("boom")), "НЕ доставлено: RuntimeError: boom"),
])
def test_notify_command_failures_are_reported_not_swallowed(run, expect):
    out = duo.run_notify_command({"notify": {"command": ["/opt/send"]}}, "t", "b", run=run)
    assert expect in out and out.startswith("НЕ доставлено")


def test_notify_command_unconfigured_or_garbage_is_silent():
    run = _Run()
    for cfg in ({}, {"notify": {}}, {"notify": {"command": []}}, {"notify": {"command": "send"}},
                {"notify": {"command": ["", "x"]}}, {"notify": {"command": [1]}}):
        assert duo.run_notify_command(cfg, "t", "b", run=run) == ""
    assert run.calls == []


def _dead_cfg(tmp_path: Path, **notify) -> tuple[dict, Path]:
    cfg, arts = _repo_cfg(tmp_path)
    cfg["notify"] = notify
    return cfg, arts


def test_supervisor_sends_alert_through_both_channels(tmp_path: Path):
    cfg, arts = _dead_cfg(tmp_path, desktop=True, command=["/opt/send", "{title}", "{body}"])
    now = 2_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)
    desk: list = []
    cmd: list = []
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json", spawn=lambda t: None,
                              notify=lambda t, b: desk.append((t, b)),
                              notify_cmd=lambda c, t, b: cmd.append((t, b)) or "доставлено (тест)")
    a = {x["task"]: x for x in acts}["t"]
    assert desk and cmd and desk[0] == cmd[0]                             # один и тот же текст в оба канала
    assert "принято демоном" in a["notify"] and "команда: доставлено (тест)" in a["notify"]


def test_supervisor_command_only_channel_works_without_desktop(tmp_path: Path):
    cfg, arts = _dead_cfg(tmp_path, command=["/opt/send"])                # desktop не включён
    now = 2_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)
    desk: list = []
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json", spawn=lambda t: None,
                              notify=lambda t, b: desk.append(1), notify_cmd=lambda c, t, b: "доставлено (тест)")
    a = {x["task"]: x for x in acts}["t"]
    assert desk == [] and a["notify"] == "команда: доставлено (тест)"


def test_supervisor_without_any_channel_keeps_old_message(tmp_path: Path):
    cfg, arts = _dead_cfg(tmp_path, desktop=False)
    now = 2_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json", spawn=lambda t: None,
                              notify=lambda t, b: None)
    assert {x["task"]: x for x in acts}["t"]["notify"] == "выключено (notify.desktop=false)"


def test_orchestrator_halt_notifies_through_command_and_marks_notified(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    cfg["notify"] = {"desktop": False, "command": ["/opt/send", "{title}"]}
    p = duo.Pipeline(cfg, "demo", mode="pair")
    run = _Run()
    monkeypatch.setattr(duo.subprocess, "run", run)
    p.halt(p.load_state(), "crash", "тестовая остановка", phase="plan")
    assert run.calls and "остановка (crash)" in run.calls[0][0][1]
    assert _state(td)["halt"]["notified"] is True                         # надзор своё не продублирует
    assert "УВЕДОМЛЕНИЕ (команда): доставлено" in (td / "orchestrator.log").read_text(encoding="utf-8")
    run2 = _Run(rc=1, stderr=b"nope")
    monkeypatch.setattr(duo.subprocess, "run", run2)
    p.halt(p.load_state(), "crash", "ещё одна", phase="plan")
    assert _state(td)["halt"]["notified"] is False                        # канал не принял — не врём


def test_dead_reason_names_traceback_or_external_kill_and_inflight(tmp_path: Path):
    td = tmp_path / "t"
    td.mkdir()
    (td / "orchestrator.log").write_text("[10:00:00] PLAN ревью\n", encoding="utf-8")
    killed = duo.dead_hint(td, {"inflight": {"step": "revise_impl", "at": "2026-11-26T01:04:00"},
                                "inflight_review": {"rev": "rev002_impl.md", "at": "2026-11-26T01:33:00"}})
    assert "убит снаружи" in killed and "OOM" in killed and "последний признак жизни" in killed
    assert "revise_impl" in killed and "rev002_impl.md" in killed and "не вошла" in killed
    (td / "orchestrator_bg.log").write_text("x\nTraceback (most recent call last):\n  ...\nFileNotFoundError: boom\n",
                                            encoding="utf-8")
    tb = duo.dead_hint(td, {})
    assert "исключение в оркестраторе" in tb and "FileNotFoundError: boom" in tb and "убит снаружи" not in tb
    assert "убит снаружи" in duo.dead_hint(tmp_path / "нет-каталога", None)       # нет файлов — не падаем


def test_supervisor_dead_alert_carries_the_diagnosis(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    now = 2_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000, inflight={"step": "impl", "at": "x"})
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json", spawn=lambda t: None,
                              notify=lambda t, b: None)
    a = {x["task"]: x for x in acts}["t"]
    assert a["kind"] == "dead" and "убит снаружи" in a["reason"] and "в полёте был шаг исполнителя impl" in a["reason"]


@pytest.mark.parametrize("notify,needle", [
    ({"command": "send"}, "notify.command"),
    ({"command": ["ok", ""]}, "notify.command"),
    ({"command": ["ok"], "command_timeout_sec": 0}, "notify.command_timeout_sec"),
    ({"command": ["ok"], "command_timeout_sec": "20"}, "notify.command_timeout_sec"),
])
def test_guard_config_problems_for_notify_command(notify, needle):
    assert any(needle in p for p in duo.guard_config_problems({"notify": notify})), notify
    assert duo.guard_config_problems({"notify": {"command": ["ok", "{title}"], "command_timeout_sec": 5}}) == []


# --- Н55: неучтённые вызовы -------------------------------------------------------------------------


def _recover(tmp_path: Path, st: dict) -> tuple[dict, Path]:
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    p._recover_inflight(st)
    return st, td


def test_dead_process_inflight_step_and_review_are_counted_as_unrecorded(tmp_path: Path):
    st = {"phase": "impl", "inflight": {"step": "impl", "version": 1, "at": "2026-11-26T01:04:00", "phase": "impl"},
          "inflight_review": {"phase": "impl", "rev": "rev002_impl.md", "at": "2026-11-26T01:33:00"}}
    st, td = _recover(tmp_path, st)
    assert st["unrecorded_count"] == 2 and "inflight_review" not in st
    kinds = [c["kind"] for c in st["unrecorded_calls"]]
    assert kinds == ["executor", "review"] and st["unrecorded_calls"][1]["step"] == "rev002_impl.md"
    assert "УЧЁТ:" in (td / "orchestrator.log").read_text(encoding="utf-8")


def test_clean_halt_does_not_count_inflight_as_unrecorded(tmp_path: Path):
    st = {"phase": "needs_human", "halt": {"kind": "transport", "phase": "impl", "resumable": True},
          "inflight": {"step": "impl", "version": 1, "at": "2026-11-26T01:04:00", "phase": "impl"},
          "inflight_review": {"phase": "impl", "rev": "rev002_impl.md", "at": "x"}}
    st, _td = _recover(tmp_path, st)
    assert "unrecorded_count" not in st and "inflight_review" not in st          # цена учтена при halt


def test_unrecorded_minutes_use_console_activity_and_list_is_capped(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    bg = td / "orchestrator_bg.log"
    bg.write_text("x", encoding="utf-8")
    start = time.time() - 3000
    os.utime(bg, (time.time(), time.time()))
    st: dict = {}
    p._note_unrecorded(st, "executor", "impl", datetime.fromtimestamp(start).isoformat())
    assert 49 <= st["unrecorded_calls"][0]["minutes"] <= 51
    for i in range(30):
        p._note_unrecorded(st, "review", f"r{i}", None)
    assert len(st["unrecorded_calls"]) == 20 and st["unrecorded_count"] == 31     # счётчик полный, список ограничен


def test_review_marker_is_set_and_cleared_around_a_successful_review(tmp_path: Path, monkeypatch):
    from tests.unit.test_duo_pipemodes import PairTransport
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    p.t = PairTransport(verdicts=["REVISE"])
    seen: list = []
    orig = duo.DuoReviewer.review

    def spy(self, **kw):
        seen.append(duo.load_json(td / "state.json").get("inflight_review"))      # маркер уже записан на диск
        return orig(self, **kw)

    monkeypatch.setattr(duo.DuoReviewer, "review", spy)
    monkeypatch.setattr(p, "_boundary_stop", lambda *a, **k: True)                # остановить цикл после 1-го ревью
    p.run()
    assert seen and seen[0] and seen[0]["rev"] == "rev001_plan.md" and seen[0]["phase"] == "plan"
    assert "inflight_review" not in _state(td)                                    # после ревью маркер снят


def test_status_json_and_text_show_unrecorded(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["unrecorded_count"] = 2
    st["unrecorded_calls"] = [{"kind": "executor", "step": "impl", "minutes": 40.0},
                              {"kind": "review", "step": "rev002_impl.md", "minutes": 11.0}]
    duo.save_json(td / "state.json", st)
    cfg = _pcfg(tmp_path, repo)
    duo.cmd_status(cfg, argparse.Namespace(task="demo", json=True, all=False))
    b = json.loads(capsys.readouterr().out)["budget"]
    assert b["unrecorded_calls"] == 2 and b["unrecorded_minutes"] == 51.0
    duo.cmd_status(cfg, argparse.Namespace(task="demo", json=False, all=False))
    assert "неучтённых оборванных вызовов модели: 2 (≈ 51 мин работы)" in capsys.readouterr().out


# --- Н59: шаг гейтов — отсечка по тишине и пульс ----------------------------------------------------

_PY = sys.executable


def _stream(script: str, tmp_path: Path, *, idle: float, timeout: float | None = 30, heartbeat: float | None = 0):
    return duo._gates_stream_runner([_PY, "-u", "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=timeout,
                                    idle_timeout=idle, label="net", heartbeat=heartbeat)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]     # зомби не жив
    except OSError:
        return False


def test_silent_step_is_killed_by_idle_timeout_with_partial_output_and_group(tmp_path: Path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "print('начало прогона', flush=True)\n"
        "time.sleep(300)\n"
    )
    t0 = time.time()
    rc, out = _stream(script, tmp_path, idle=2.0)
    assert time.time() - t0 < 20                                         # не ждали 300 с и не общий таймаут
    assert rc == 124 and "начало прогона" in out and "IDLE-TIMEOUT" in out and "idle_timeout=2" in out
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)                                               # убита вся группа, не только родитель


def test_chatty_step_longer_than_idle_timeout_is_not_killed(tmp_path: Path):
    script = "import time\nfor i in range(6):\n    print('тик', i, flush=True)\n    time.sleep(0.5)\n"
    rc, out = _stream(script, tmp_path, idle=2.0)                         # шаг идёт 3 с > idle 2 с, но не молчит
    assert rc == 0 and out.count("тик") == 6 and "IDLE" not in out


def test_stream_runner_merges_stderr_keeps_rc_and_reports_missing_bin(tmp_path: Path):
    rc, out = _stream("import sys\nprint('o')\nprint('e', file=sys.stderr)\nsys.exit(3)\n", tmp_path, idle=5.0)
    assert rc == 3 and "o" in out and "e" in out
    rc, out = duo._gates_stream_runner(["/нет/такого/бина"], cwd=tmp_path, env={}, timeout=5, idle_timeout=1.0)
    assert rc == 127 and "BIN MISSING" in out


def test_stream_runner_total_timeout_still_applies_and_is_not_labelled_idle(tmp_path: Path):
    script = "import time\nwhile True:\n    print('x', flush=True)\n    time.sleep(0.2)\n"
    rc, out = _stream(script, tmp_path, idle=30.0, timeout=2.0)
    assert rc == 124 and "TIMEOUT после 2.0s" in out and "IDLE-TIMEOUT" not in out


def test_stream_runner_prints_heartbeat_while_step_is_silent(tmp_path: Path, capsys):
    rc, out = _stream("import time\nprint('a', flush=True)\ntime.sleep(2.6)\n", tmp_path, idle=30.0, heartbeat=1.0)
    assert rc == 0
    beats = [ln for ln in capsys.readouterr().out.splitlines() if "идёт" in ln]
    assert beats and "net: идёт" in beats[0] and "вывод 2 Б" in beats[0] and "отсечка по тишине 30 с" in beats[0]


def test_default_runner_without_idle_timeout_keeps_old_path(tmp_path: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(duo, "_gates_stream_runner", lambda *a, **k: calls.append(1) or (0, ""))
    rc, out = duo._gates_default_runner([_PY, "-c", "print('ok')"], cwd=tmp_path, env=dict(os.environ), timeout=20)
    assert (rc, out.strip()) == (0, "ok") and calls == []
    duo._gates_default_runner([_PY, "-c", "pass"], cwd=tmp_path, env=dict(os.environ), timeout=20, idle_timeout=5.0)
    assert calls == [1]                                                  # с idle_timeout — потоковый раннер


def test_gates_resolve_carries_idle_timeout_only_when_valid():
    steps = duo.gates_steps({"gates": {"steps": [
        {"name": "a", "argv": ["x"], "idle_timeout": 120},
        {"name": "b", "argv": ["x"]},
        {"name": "c", "argv": ["x"], "idle_timeout": 0},
        {"name": "d", "argv": ["x"], "idle_timeout": True},
        {"name": "e", "argv": ["x"], "idle_timeout": "5"},
    ]}})
    assert steps[0]["idle_timeout"] == 120.0
    assert all("idle_timeout" not in s for s in steps[1:])               # мусор не включает отсечку молча


def test_guard_config_problems_flags_bad_idle_timeout():
    bad = [{"name": "a", "argv": ["x"], "idle_timeout": 0}, {"name": "b", "argv": ["x"], "idle_timeout": "5"}]
    probs = duo.guard_config_problems({"gates": {"steps": bad}})
    assert len(probs) == 2 and "steps[0] (a).idle_timeout" in probs[0] and "steps[1] (b).idle_timeout" in probs[1]
    assert duo.guard_config_problems({"gates": {"steps": [{"name": "a", "argv": ["x"], "idle_timeout": 90}]}}) == []


def test_cmd_gates_passes_idle_timeout_and_label_only_to_steps_that_set_it(tmp_path: Path):
    from tests.unit.test_duo_fieldfix import _gates_one_step
    from tests.unit.test_duo_gates import _gates_ns
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _gates_one_step(tmp_path, repo)
    cfg["gates"]["steps"] = [{"name": "plain", "argv": ["true"], "timeout": 60},
                             {"name": "watched", "argv": ["true"], "timeout": 60, "idle_timeout": 45}]
    seen: dict = {}

    def run(argv, *, cwd, env, timeout, **kw):
        seen[Path(argv[0]).name] = (timeout, kw)
        return 0, "ok"

    assert duo.cmd_gates(cfg, _gates_ns(), runner=run) == 0
    assert seen["true"] == (60, {"idle_timeout": 45.0, "label": "watched"})    # последний вызов перезаписал ключ
    calls: list = []

    def run2(argv, *, cwd, env, timeout, **kw):
        calls.append(kw)
        return 0, "ok"

    duo.cmd_gates(cfg, _gates_ns(), runner=run2)
    assert calls == [{}, {"idle_timeout": 45.0, "label": "watched"}]


# --- добавлено по мутационной проверке: пробелы, которые не ловила нумерация/«мусорные» ветки ---------


def test_split_afterword_lazy_continuation_of_last_item_stays_in_block():
    # ленивое продолжение ПОСЛЕДНЕГО пункта (без пустой строки и без нумерации ниже): не абзац-послесловие
    lines = ["1. раз ZZ_A", "2. два ZZ_B", "ленивое продолжение пункта 2 ZZ_LAZY", "", "**Самопроверка.** ZZ_T"]
    kept, tail = duo.split_errata_afterword(lines)
    assert kept == lines[:3] and tail == ["**Самопроверка.** ZZ_T"]


def test_split_afterword_fence_in_last_item_hides_column_zero_text():
    lines = ["1. раз ZZ_A", "   ```", "   код", "", "текст в колонке 0 внутри ограждения ZZ_IN_FENCE", "   ```",
             "", "**Самопроверка.** ZZ_T"]
    kept, tail = duo.split_errata_afterword(lines)
    assert "текст в колонке 0 внутри ограждения ZZ_IN_FENCE" in kept and tail == ["**Самопроверка.** ZZ_T"]


def test_diff_patch_excludes_tracked_changes_of_series_artifacts(tmp_path: Path):
    repo = _snap_repo(tmp_path)
    (repo / "pipeline" / "t").mkdir(parents=True)
    (repo / "pipeline" / "t" / "plan_v1.md").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "artifact")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "pipeline" / "t" / "plan_v1.md").write_text("ARTIFACT_TRACKED_ZZ\n", encoding="utf-8")   # tracked-правка
    (repo / "src" / "old.py").write_text("print('changed')\n", encoding="utf-8")
    out = tmp_path / "d.patch"
    assert duo.GitGuard(repo).diff_patch(base, out, "pipeline/t", 512)
    text = out.read_text(encoding="utf-8")
    assert "print('changed')" in text and "ARTIFACT_TRACKED_ZZ" not in text    # артефакты — не часть диффа кода


def test_wait_quota_ignores_reset_of_a_non_quota_halt(tmp_path: Path, monkeypatch):
    p = _quota_pipeline(tmp_path, {"kind": "transport", "phase": "plan", "resumable": True, "cause": "overload",
                                   "reset_at": RESET_UTC})
    clock = _Clock(RESET_UTC - 1000)
    _fake_time(monkeypatch, clock)
    p._wait_quota_reset()
    assert clock.slept == []                                           # метка есть, но остановка не по квоте
    assert "остановка не по квоте" in (p.task_dir / "orchestrator.log").read_text(encoding="utf-8")


def test_convergence_summary_equal_counts_are_not_decreasing_and_single_review_is_empty(tmp_path: Path):
    two = "## 1. Замечания\n1. Н1 — блокер — x\n2. Н2 — существенное — y\n"
    st = _conv_state(tmp_path, [("rev001_impl.md", "REVISE", two, 1.0), ("rev002_impl.md", "REVISE", two, 1.0)])
    assert "НЕ убывает" in duo.convergence_summary(tmp_path, st, "impl")          # 2 → 2: не сходится
    st = _conv_state(tmp_path, [("rev001_impl.md", "REVISE", two, 1.0)])
    assert duo.convergence_summary(tmp_path, st, "impl") == ""                    # одно ревью — тренда нет


def test_rev_findings_latent_mark_on_cosmetic_item_is_not_counted():
    f = duo.rev_findings("## 1. Замечания\n1. Н5 — косметика — латентно, не воспроизводится\n"
                         "2. Н6 — существенное — латентно, потерь данных нет\n")
    assert f == {"blocker": 0, "major": 1, "cosmetic": 1, "latent": 1}       # «латентно» у косметики — не «обязательное»


def test_halt_itself_clears_review_marker(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    st = p.load_state()
    st["inflight_review"] = {"phase": "plan", "rev": "rev001_plan.md", "at": "2026-11-26T01:00:00"}
    p.save_state(st)
    p.halt(st, "crash", "тестовая остановка", phase="plan")
    after = _state(td)
    assert "inflight_review" not in after and after["halt"]["kind"] == "crash"   # чистая остановка: маркер снят сразу


def test_cmd_gates_refuses_bad_idle_timeout_instead_of_silently_dropping_protection(tmp_path: Path):
    from tests.unit.test_duo_fieldfix import _gates_one_step
    from tests.unit.test_duo_gates import _gates_ns
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _gates_one_step(tmp_path, repo)
    cfg["gates"]["steps"] = [{"name": "watched", "argv": ["true"], "timeout": 60, "idle_timeout": "45"}]
    calls: list = []

    def run(argv, *, cwd, env, timeout, **kw):
        calls.append(argv)
        return 0, "ok"

    with pytest.raises(SystemExit) as ei:
        duo.cmd_gates(cfg, _gates_ns(), runner=run)
    assert "ОТКАЗ" in str(ei.value) and "idle_timeout" in str(ei.value) and calls == []
