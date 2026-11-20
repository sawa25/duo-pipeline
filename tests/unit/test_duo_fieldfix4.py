"""fieldfix4: правки по трём полевым отчётам 19.11.2026 (серии pair/duo/solo, shared и worktree).

Н17  результат доставки десктоп-уведомления больше не теряется: `halt.notified` — факт принятого демоном
     сообщения (не «ключ включён»), надзор пишет итог доставки в журнал (`| уведомление: …`).
Н18  `commit-by-plan`: итоговая строка `COMMIT-RESULT:` и вердикт «код 1 — хвосты, а не сбой коммита»;
     сам код возврата не менялся.
Н19  `merge` отказывает, если HEAD основного дерева — ветка ЖИВОЙ другой серии (`duo/<другая>`).
Н21  отказ `.lock` и умерший при старте ребёнок `run_detached` говорят «серия уже идёт», а не
     изображают сбой; в момент запуска печатается команда ожидания события.
Н24  пульс транспорта при молчании модели и возраст консоли в `status` (человек видит: процесс жив).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix import PLAN, _cbp_repo
from tests.unit.test_duo_fieldfix2 import _cbp_ns
from tests.unit.test_duo_pipemodes import (
    _done_series,
    _git_out,
    _human_commit,
    _merge_ns,
    _pcfg,
    _state,
)
from tests.unit.test_duo_pipev3 import _cfg, _git, _init_repo, _mk_task, duo
from tests.unit.test_duo_pipework3 import _mk_state, _repo_cfg

# --- Н17 -------------------------------------------------------------------------


def _desktop_cfg(tmp_path: Path, repo: Path) -> dict:
    cfg = _cfg(tmp_path, repo)
    cfg["notify"] = {"desktop": True}
    return cfg


def _fake_notify_send(monkeypatch, outcome):
    """Подмена ТОЛЬКО notify-send (git и прочее идут настоящим subprocess.run)."""
    real = subprocess.run

    def fake(argv, *a, **kw):
        if argv and argv[0] == "notify-send":
            if isinstance(outcome, Exception):
                raise outcome
            return types.SimpleNamespace(returncode=outcome[0], stderr=outcome[1])
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake)


@pytest.mark.parametrize("outcome,notified,logged", [
    ((0, b""), True, None),
    ((1, b"Cannot autolaunch D-Bus"), False, "notify-send rc=1 Cannot autolaunch D-Bus"),
    (FileNotFoundError("notify-send"), False, "FileNotFoundError"),
])
def test_halt_notified_flag_reflects_real_delivery(tmp_path: Path, monkeypatch, outcome, notified, logged):
    # раньше halt.notified = «ключ desktop включён» ставилось ДО отправки и глоталось любое исключение:
    # при сбое notify-send надзор считал «оркестратор уже уведомил» и не слал своё — остановка без звука
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_desktop_cfg(tmp_path, repo), "demo")
    _fake_notify_send(monkeypatch, outcome)
    p.halt(p.load_state(), "crash", "тестовая остановка", phase="plan")
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "needs_human" and st["halt"]["notified"] is notified
    log = (td / "orchestrator.log").read_text(encoding="utf-8")
    if logged:
        assert "УВЕДОМЛЕНИЕ:" in log and logged in log                  # причина не потеряна
    else:
        assert "УВЕДОМЛЕНИЕ:" not in log


def test_halt_without_desktop_is_not_marked_notified(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo), "demo")                      # notify.desktop выключен
    p.halt(p.load_state(), "crash", "x", phase="plan")
    assert duo.load_json(td / "state.json")["halt"]["notified"] is False


def _tick_notify(cfg: dict, tmp_path: Path, notify, now: float, sp: str = "s.json", **kw) -> dict:
    # у каждого сценария свой файл дедупликации: повторный тик на том же файле молчит («уже оповещали»)
    acts = duo.supervise_tick(cfg, now=now, state_path=tmp_path / sp, spawn=lambda t: None,
                              notify=notify, **kw)
    return {a["task"]: a for a in acts}


@pytest.mark.parametrize("notify,expect", [
    (lambda t, b: None, "принято демоном"),                            # подмена без результата = отправлено
    (lambda t, b: types.SimpleNamespace(returncode=0, stderr=b""), "принято демоном"),
    (lambda t, b: types.SimpleNamespace(returncode=1, stderr=b"no session bus"), "НЕ доставлено: notify-send rc=1 no session bus"),
])
def test_supervise_records_delivery_outcome(tmp_path: Path, notify, expect):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["notify"] = {"desktop": True}
    now = 8_000_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)     # dead → alert
    act = _tick_notify(cfg, tmp_path, notify, now)["t"]
    assert act["action"] == "alert" and expect in act["notify"]


@pytest.mark.parametrize("exc,expect", [
    (FileNotFoundError("notify-send"), "notify-send не установлен"),
    (subprocess.TimeoutExpired("notify-send", 5), "завис"),
    (RuntimeError("boom"), "RuntimeError: boom"),
])
def test_supervise_delivery_exceptions_are_reported_not_swallowed(tmp_path: Path, exc, expect):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["notify"] = {"desktop": True}
    now = 8_100_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)

    def boom(t, b):
        raise exc

    act = _tick_notify(cfg, tmp_path, boom, now)["t"]
    assert act["action"] == "alert"                                    # исключение не ломает тик
    assert act["notify"].startswith("НЕ доставлено") and expect in act["notify"]


def test_supervise_notify_states_off_dry_run_and_already_notified(tmp_path: Path):
    cfg, arts = _repo_cfg(tmp_path)
    now = 8_200_000.0
    _mk_state(arts, "t", phase="impl", state_updated_at=now - 1000)
    calls: list[str] = []

    def spy(t, b):
        calls.append(t)

    assert "выключено" in _tick_notify(cfg, tmp_path, spy, now)["t"]["notify"]      # notify.desktop=false
    cfg["notify"] = {"desktop": True}
    dry = _tick_notify(cfg, tmp_path, spy, now, "dry.json", dry_run=True)["t"]
    assert "dry-run" in dry["notify"] and calls == []                   # dry-run ничего не шлёт
    # остановка, о которой оркестратор УЖЕ уведомил: надзор не дублирует и говорит почему
    _mk_state(arts, "h", phase="needs_human", state_updated_at=now - 100, stop_reason="лимит",
              halt={"kind": "limit", "phase": "impl", "resumable": False, "notified": True})
    h = _tick_notify(cfg, tmp_path, spy, now, "halt.json", dry_run=False)["h"]
    assert h["action"] == "alert" and "уже принято демоном" in h["notify"]
    assert all("/h:" not in c for c in calls)


def test_cmd_supervise_prints_delivery_outcome(tmp_path: Path, monkeypatch, capsys):
    cfg, arts = _repo_cfg(tmp_path)
    cfg["notify"] = {"desktop": True}
    _mk_state(arts, "t", phase="impl", state_updated_at=time.time() - 5000)
    monkeypatch.setattr(duo, "supervise_tick", lambda *a, **k: [
        {"task": "t", "project": "p", "kind": "dead", "action": "alert", "reason": "мёртв",
         "notify": "НЕ доставлено: notify-send rc=1 no bus"}])
    ns = types.SimpleNamespace(config=str(tmp_path / "tool.json"), resume=False, dry_run=False, loop=False, interval=1)
    (tmp_path / "tool.json").write_text("{}", encoding="utf-8")
    duo.cmd_supervise(cfg, ns, single=True)
    assert "[p] t dead → alert :: мёртв | уведомление: НЕ доставлено: notify-send rc=1 no bus" in capsys.readouterr().out


# --- Н18 -------------------------------------------------------------------------


def _cbp(repo: Path, tmp_path: Path, **over) -> int:
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(_cfg(tmp_path, repo), _cbp_ns(**over))
    return ei.value.code


def test_commit_result_clean_run(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    assert _cbp(repo, tmp_path, yes=True) == 0
    out = capsys.readouterr().out
    assert "ИТОГ: все группы закоммичены, хвостов нет" in out
    assert ("COMMIT-RESULT: committed=1 skipped=0 failed=0 empty=0 uncovered=0 overlaps=0 "
            "warnings=0 rc=0") in out


def test_commit_result_explains_rc1_from_uncovered_not_commit_failure(tmp_path: Path, capsys):
    # источник путаницы полевого отчёта: группа закоммичена, а код 1 — из-за файла вне плана
    repo, td = _cbp_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "extra.md").write_text("вне плана", encoding="utf-8")
    assert _cbp(repo, tmp_path, yes=True, allow_partial=True) == 1
    out = capsys.readouterr().out
    assert "коммиты групп прошли (1), код 1 — из-за хвостов, а не из-за сбоя коммита: uncovered=1" in out
    assert "COMMIT-RESULT: committed=1 skipped=0 failed=0 empty=0 uncovered=1 overlaps=0 warnings=0 rc=1" in out
    assert "НЕ удавшиеся" not in out
    assert _git_out(repo, "log", "--oneline").count("\n") == 2          # базовый + feat: код


def test_commit_result_reports_real_commit_failure(tmp_path: Path, capsys, monkeypatch):
    repo, td = _cbp_repo(tmp_path)
    monkeypatch.setattr(duo, "commit_group", lambda *a, **k: (False, "pre-commit hook failed"))
    assert _cbp(repo, tmp_path, yes=True) == 1
    out = capsys.readouterr().out
    assert "ИТОГ: есть НЕ удавшиеся коммиты групп (G1)" in out
    assert "COMMIT-RESULT: committed=0 skipped=0 failed=1" in out and "rc=1" in out


def test_commit_result_dry_run_line(tmp_path: Path, capsys):
    repo, td = _cbp_repo(tmp_path)
    assert _cbp(repo, tmp_path, dry_run=True) == 0
    assert "COMMIT-RESULT: dry-run yes=pass uncovered=0 overlaps=0 warnings=0 rc=0" in capsys.readouterr().out
    (td / "impl_report_v1.md").write_text(PLAN + "| G2 | docs/* | docs | п.2 |\n", encoding="utf-8")
    assert _cbp(repo, tmp_path, dry_run=True) == 1
    assert "COMMIT-RESULT: dry-run yes=refuse" in capsys.readouterr().out


# --- Н19 -------------------------------------------------------------------------


def _head_on_series_branch(repo: Path, other: str = "live") -> None:
    """Основное дерево переключено на ветку `duo/<other>` (как у shared-серии с auto_branch)."""
    _git(repo, "switch", "-q", "-c", f"duo/{other}")


def test_merge_refuses_into_live_series_branch_even_in_dry_run(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)
    _head_on_series_branch(repo)
    _mk_state(repo / "pipeline", "live", phase="impl")                  # серия «live» ещё работает
    head_before = _git_out(repo, "rev-parse", "HEAD")
    for dry in (True, False):
        assert duo.cmd_merge(cfg, _merge_ns(dry_run=dry)) == 1
        out = capsys.readouterr().out
        assert "ОТКАЗ" in out and "duo/live" in out and "«live»" in out and "phase=impl" in out
        assert "duo wait --task live" in out and "--force" in out      # что делать — в сообщении
    assert _git_out(repo, "rev-parse", "HEAD") == head_before and not (repo / "feature.txt").exists()
    assert _state(a.task_dir)["claim"].get("released") is None          # серию не тронули: зона держится


def test_merge_force_overrides_live_series_guard_with_warning(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    _done_series(tmp_path, repo, cfg)
    _head_on_series_branch(repo)
    _mk_state(repo / "pipeline", "live", phase="impl")
    assert duo.cmd_merge(cfg, _merge_ns(force=True)) == 0
    out = capsys.readouterr().out
    assert "ВНИМАНИЕ (--force)" in out and (repo / "feature.txt").exists()


def test_merge_into_done_or_unknown_series_branch_only_warns(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    _done_series(tmp_path, repo, cfg)
    _head_on_series_branch(repo, "finished")
    _mk_state(repo / "pipeline", "finished", phase="done")
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 0
    assert "принятой серии «finished»" in capsys.readouterr().out
    _git(repo, "switch", "-q", "-c", "duo/ghost")                       # ветка с префиксом, состояния серии нет
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 0
    assert "состояния серии «ghost» нет" in capsys.readouterr().out


def test_merge_into_ordinary_branch_has_no_series_warning(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    _done_series(tmp_path, repo, cfg)
    _human_commit(repo)
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "ВНИМАНИЕ" not in out and "ОТКАЗ" not in out


def test_head_series_status_treats_held_lock_as_live_even_if_phase_done(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    td = _mk_state(repo / "pipeline", "x", phase="done")
    assert duo._head_series_status(cfg, repo, "duo/x") == ("x", "done", "done")
    assert duo._head_series_status(cfg, repo, "main") is None
    fd = os.open(str(td / ".lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                 # «живой оркестратор»
        other, kind, detail = duo._head_series_status(cfg, repo, "duo/x")
        assert (other, kind) == ("x", "live") and "оркестратор жив" in detail
    finally:
        os.close(fd)


# --- Н21 -------------------------------------------------------------------------


class _HeldLock:
    """Держит `.lock` задачи как живой оркестратор (flock на отдельном дескрипторе)."""

    def __init__(self, task_dir: Path):
        self.fd = os.open(str(task_dir / ".lock"), os.O_CREAT | os.O_RDWR)

    def __enter__(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def test_lock_refusal_says_series_is_already_running(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo), "demo")
    with _HeldLock(td), pytest.raises(SystemExit) as ei:
        p.acquire_lock()
    msg = str(ei.value)
    assert "уже выполняется" in msg                                     # прежняя часть (на неё опираются тесты/скрипты)
    assert "серия уже идёт" in msg and "duo status --task demo" in msg and "duo wait --task demo" in msg


class _FakeChild:
    pid = 4242

    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _detached(tmp_path: Path, monkeypatch, child_rc):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    monkeypatch.setattr(duo, "DETACHED_CONFIRM_SEC", 0)
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _FakeChild(child_rc))
    return _cfg(tmp_path, repo), td


def test_run_detached_duplicate_start_is_reported_as_already_running(tmp_path: Path, monkeypatch, capsys):
    cfg, td = _detached(tmp_path, monkeypatch, 1)                       # ребёнок отбит .lock'ом и умер
    with _HeldLock(td), pytest.raises(SystemExit) as ei:
        duo.run_detached(cfg, "demo", ["run", "--task", "demo"])
    out = capsys.readouterr().out
    assert ei.value.code == 1                                           # код выхода прежний
    assert "УЖЕ выполняется" in out and "не сбой оркестратора" in out and "duo status --task demo" in out
    assert "умер при старте" not in out


def test_run_detached_real_startup_death_keeps_tail_diagnostics(tmp_path: Path, monkeypatch, capsys):
    cfg, td = _detached(tmp_path, monkeypatch, 3)                       # lock свободен — настоящая гибель
    (td / "orchestrator_bg.log").write_text("Traceback: boom\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        duo.run_detached(cfg, "demo", ["run", "--task", "demo"])
    out = capsys.readouterr().out
    assert ei.value.code == 3 and "оркестратор умер при старте" in out and "Traceback: boom" in out
    assert "УЖЕ выполняется" not in out


def test_run_detached_prints_wait_hint_on_success(tmp_path: Path, monkeypatch, capsys):
    cfg, td = _detached(tmp_path, monkeypatch, None)                    # ребёнок жив
    duo.run_detached(cfg, "demo", ["run", "--task", "demo"])
    out = capsys.readouterr().out
    assert "PID 4242" in out and "duo wait --task demo --timeout 3600 --quiet" in out and "WAIT-RESULT" in out
    assert "СТОРОЖ" in out and "В ФОНЕ" in out                          # блок, а не строка, которую пролистывают


# --- Н24 -------------------------------------------------------------------------


class _SlowStdout:
    """Поток stream-json, который «думает» перед ответом: события приходят только после паузы."""

    def __init__(self, events, delay):
        self._lines = ["".join([json.dumps(e), "\n"]) for e in events]
        self._delay = delay

    def __iter__(self):
        time.sleep(self._delay)
        yield from self._lines


class _SlowProc:
    def __init__(self, events, delay):
        self.stdout = _SlowStdout(events, delay)
        self.stderr = types.SimpleNamespace(read=lambda: "")
        self._born = time.time()
        self._delay = delay

    def poll(self):
        return None if time.time() - self._born < self._delay + 0.2 else 0

    def kill(self):
        pass


def test_transport_prints_heartbeat_while_model_is_silent(tmp_path: Path, monkeypatch, capsys):
    events = [{"type": "result", "session_id": "s", "total_cost_usd": 0.01, "result": "готово"}]
    monkeypatch.setattr(duo, "TRANSPORT_POLL_SEC", 0.05)
    monkeypatch.setattr(duo, "TRANSPORT_HEARTBEAT_SEC", 0.2)
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _SlowProc(events, 0.7))
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    text, sid, cost = glm.call("промпт", mode="plan", timeout=60)
    out = capsys.readouterr().out
    assert text == "готово"                                             # пульс не мешает результату
    beats = [ln for ln in out.splitlines() if "жду ответ модели" in ln]
    assert len(beats) >= 2 and "тишина" in beats[0] and "отсечка по тишине 60 с" in beats[0]


def test_transport_prints_no_heartbeat_when_events_flow(tmp_path: Path, monkeypatch, capsys):
    events = [{"type": "result", "session_id": "s", "total_cost_usd": 0.0, "result": "ok"}]
    monkeypatch.setattr(duo, "TRANSPORT_POLL_SEC", 0.05)
    monkeypatch.setattr(duo, "TRANSPORT_HEARTBEAT_SEC", 5.0)            # быстрый ответ — пульса нет
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _SlowProc(events, 0.0))
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    assert glm.call("p", mode="plan", timeout=60)[0] == "ok"
    assert "жду ответ модели" not in capsys.readouterr().out


class _PacedProc:
    """События приходят ровно, с паузой МЕНЬШЕ интервала пульса: тишины, достойной пульса, нет."""

    def __init__(self, events, gap):
        self._done = False
        lines = ["".join([json.dumps(e), "\n"]) for e in events]

        def gen():
            for ln in lines:
                time.sleep(gap)
                yield ln
            self._done = True

        self.stdout = gen()
        self.stderr = types.SimpleNamespace(read=lambda: "")

    def poll(self):
        return 0 if self._done else None

    def kill(self):
        pass


def test_transport_heartbeat_timer_resets_on_every_event(tmp_path: Path, monkeypatch, capsys):
    # без сброса таймера после события пульс «просрочивался» бы за время потока и печатал «тишина 0 с»
    # посреди работающей модели — шум, который учит оператора игнорировать пульс
    events = [{"type": "assistant", "session_id": "s", "message": {"content": [{"type": "text", "text": "…"}]}}
              for _ in range(8)]
    events.append({"type": "result", "session_id": "s", "total_cost_usd": 0.0, "result": "ok"})
    monkeypatch.setattr(duo, "TRANSPORT_POLL_SEC", 0.02)
    monkeypatch.setattr(duo, "TRANSPORT_HEARTBEAT_SEC", 0.25)
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _PacedProc(events, 0.1))   # поток ≈0.9 с > 0.25 с
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    assert glm.call("p", mode="plan", timeout=60)[0] == "ok"
    assert "жду ответ модели" not in capsys.readouterr().out


def _status_ns(**over):
    d = dict(task="demo", all=False, json=False)
    d.update(over)
    return types.SimpleNamespace(**d)


def test_status_shows_console_age_in_json_and_text(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    duo.cmd_status(cfg, _status_ns(json=True))
    assert json.loads(capsys.readouterr().out)["process"]["console_age_sec"] is None      # консоли ещё нет
    bg = td / "orchestrator_bg.log"
    bg.write_text("· Read(x)\n", encoding="utf-8")
    old = time.time() - 90
    os.utime(bg, (old, old))
    duo.cmd_status(cfg, _status_ns(json=True))
    assert 89 <= json.loads(capsys.readouterr().out)["process"]["console_age_sec"] <= 100
    duo.cmd_status(cfg, _status_ns())                                   # процесс не жив — строки консоли нет
    assert "консоль (orchestrator_bg.log)" not in capsys.readouterr().out
    with _HeldLock(td):
        duo.cmd_status(cfg, _status_ns())
    out = capsys.readouterr().out
    assert "консоль (orchestrator_bg.log): последний вывод" in out and "занятый lock" in out
