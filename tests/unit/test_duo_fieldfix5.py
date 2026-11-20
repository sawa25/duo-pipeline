"""fieldfix5: правки по полевому отчёту solo-серий на живом оборудовании (20.11.2026).

Н25  `duo audit`: что исполнитель реально вызывал — по журналу сессии CLI (правила `audit`);
     история `executor_sessions` в state.json; `--archive` — только по явному флагу.
Н26  `executor_deny` → `--disallowedTools` (только исполнитель; вариадическая опция не съедает промпт).
Н27  `duo accept`: шаги `acceptance.steps` в реальном дереве серии под замками ресурсов, вердикт
     PASS/FAIL/INCONCLUSIVE в state.json; `phase` не меняется.
Н28  `resources`: машинные замки общих ресурсов (отказ с именем держателя / ожидание).
Н29  в логе и `selftest` — во что отображается алиас модели профиля CLI (без секретов из settings.json).
+    пометка прогресса в строках инструментов; подсказка про бриф при первом `init`.
Паттерн test_duo_fieldfix4: git-песочницы в tmp_path, importlib-загрузка модуля, без LLM.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix4 import _HeldLock, _SlowProc
from tests.unit.test_duo_pipemodes import _log_text, _pcfg, _state
from tests.unit.test_duo_pipev3 import (
    REPO_ROOT,
    ScriptedTransport,
    _cfg,
    _git,
    _init_repo,
    _mk_task,
    duo,
)


def _load_audit():
    spec = importlib.util.spec_from_file_location("audit_cmd_under_test", REPO_ROOT / "audit_cmd.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()

# --- Н26: executor_deny -------------------------------------------------------------------


def _capture_argv(tmp_path: Path, monkeypatch, cfg_extra: dict, mode: str) -> list[str]:
    captured: list[list[str]] = []
    events = [{"type": "result", "session_id": "s", "total_cost_usd": 0.0, "result": "ok"}]

    def fake_popen(cmd, **kw):
        captured.append(list(cmd))
        return _SlowProc(events, 0.0)

    monkeypatch.setattr(duo.subprocess, "Popen", fake_popen)
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m", **cfg_extra})
    glm.call("ПРОМПТ-ЗАДАНИЯ", mode=mode, timeout=30)
    return captured[0]


def test_executor_deny_flag_is_followed_by_a_flag(tmp_path: Path, monkeypatch):
    # --disallowedTools вариадическая: без следующего флага «съест» позиционный промпт (проверено на
    # CLI 2.1.215, ловушка полевого разбора). Порядок: правила, затем --model; промпт — последний.
    argv = _capture_argv(tmp_path, monkeypatch, {"executor_deny": ["Bash(adb:*)", "Read(./.env)"]}, "full")
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "Bash(adb:*),Read(./.env)"
    assert argv[i + 2].startswith("--")
    assert argv[-1] == "ПРОМПТ-ЗАДАНИЯ" and "--dangerously-skip-permissions" in argv


def test_executor_deny_off_by_default_and_never_for_reviewers(tmp_path: Path, monkeypatch):
    assert "--disallowedTools" not in _capture_argv(tmp_path, monkeypatch, {}, "full")
    assert "--disallowedTools" not in _capture_argv(
        tmp_path, monkeypatch, {"executor_deny": ["Bash(adb:*)"]}, "plan")        # ревьюеры и так read-only


def test_executor_deny_rules_skip_blank_entries():
    assert duo.executor_deny_rules({"executor_deny": ["  Bash(adb:*) ", "", "  "]}) == ["Bash(adb:*)"]
    assert duo.executor_deny_rules({}) == []


@pytest.mark.parametrize("over,needle", [
    ({"executor_deny": "Bash(adb:*)"}, "executor_deny должен быть списком"),
    ({"executor_deny": ["Bash(a:*),Bash(b:*)"]}, "запятая"),
    ({"executor_deny": [""]}, "пустое"),
    ({"resources": [3]}, "resources"),
    ({"resources": "adb"}, "resources должен быть списком"),
    ({"audit": []}, "audit должен быть объектом"),
    ({"audit": {"deny_bash": ["("]}}, "некорректный regex"),
    ({"audit": {"write_zones": "pipeline/"}}, "audit.write_zones должен быть списком"),
])
def test_guard_config_problems_reports_bad_values(over, needle):
    assert any(needle in p for p in duo.guard_config_problems(over)), over


def test_guard_config_default_and_valid_are_clean():
    assert duo.guard_config_problems({}) == []
    ok = {"executor_deny": ["Bash(adb:*)"], "resources": ["adb:phone"],
          "audit": {"deny_bash": [r"\badb\b"], "write_zones": ["pipeline/{task}/"]}}
    assert duo.guard_config_problems(ok) == []
    assert duo.guard_config_problems(duo.DEFAULTS) == []


def test_run_refuses_bad_guard_config_before_taking_lock(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo, executor_deny=["a,b"]), "demo")
    before = (td / "state.json").read_bytes()
    with pytest.raises(SystemExit) as ei:
        p.run()
    assert "executor_deny" in str(ei.value) and "запятая" in str(ei.value)
    assert not (td / ".lock").exists() and (td / "state.json").read_bytes() == before


def test_run_logs_executor_deny_summary(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo, executor_deny=["Bash(adb:*)", "Read(./.env)"]), "demo")
    monkeypatch.setattr(p, "_run_impl", lambda *a, **k: None)
    p.run()
    log = _log_text(p)
    assert "технических запретов 2" in log and "Bash(adb:*)" in log
    q = duo.Pipeline(_cfg(tmp_path, repo), "demo")                    # без правил — строки нет (нет шума)
    monkeypatch.setattr(q, "_run_impl", lambda *a, **k: None)
    (q.task_dir / "orchestrator.log").unlink()
    q.run()
    assert "технических запретов" not in _log_text(q)


# --- Н25: история сессий ---------------------------------------------------------------------


def test_finalize_keeps_history_of_executor_sessions(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st: dict = {}
    ex = duo.Executor(_cfg(tmp_path, repo), None, td, repo, st)
    ex._finalize("p", "t1", "r", 0.1, "s-new1", None, [])             # первая сессия
    ex._finalize("p", "t2", "r", 0.1, "s-new2", "s-new1", [])         # resume породил новый id
    ex._finalize("p", "t3", "r", 0.1, "s-new2", "s-new2", [])         # тот же id — без дублей
    ex._finalize("p", "t4", "r", 0.1, None, "s-new2", ["сбой"])       # сбой без нового id: последняя остаётся
    assert st["executor_sessions"] == ["s-new1", "s-new2"] and st["executor_session"] == "s-new2"


def test_finalize_adopts_pre_history_session_and_failed_resume_id(tmp_path: Path):
    # серия начата ДО появления истории: в state есть executor_session, списка нет — прежний id не теряется
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st: dict = {"executor_session": "legacy"}
    ex = duo.Executor(_cfg(tmp_path, repo), None, td, repo, st)
    ex._finalize("p", "t1", "r", 0.1, "n1", "legacy", [])
    assert st["executor_sessions"] == ["legacy", "n1"]
    st2: dict = {}
    ex2 = duo.Executor(_cfg(tmp_path, repo), None, td, repo, st2)
    ex2._finalize("p", "t2", "r", 0.1, None, "orphan", ["API Error"])    # сбой без нового id: известный id учтён
    assert st2["executor_sessions"] == ["orphan"] and st2["executor_session"] == "orphan"


# --- Н25: duo audit --------------------------------------------------------------------------


def _tu(uid: str, name: str, inp: dict, ts: str = "2026-11-20T10:00:00.000Z") -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": "делаю"},
                                    {"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def _session(cdir: Path, sid: str, events: list[dict], slug: str = "-some-project") -> Path:
    f = cdir / "projects" / slug / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    lines.insert(1, "это не json — строка журнала с мусором")           # битые строки не роняют разбор
    lines.append(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "SECRET=abc"}]}}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


RULES = {"deny_bash": [r"\badb\b", r"curl .*--upload"], "deny_read": [r"(^|/)\.env$"],
         "deny_script": [r"subprocess|os\.system"], "write_zones": ["pipeline/{task}/", "runs"]}


def _audit_cfg(tmp_path: Path, repo: Path, **over) -> dict:
    return _cfg(tmp_path, repo, audit=RULES, **over)


def _audit_ns(**over):
    d = dict(task="demo", json=True, archive=False)
    d.update(over)
    return types.SimpleNamespace(**d)


def _run_audit(cfg: dict, td: Path, root: Path, state: dict, capsys, **over):
    rc = audit.cmd_audit(cfg, _audit_ns(**over), td, root, state)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if over.get("json", True) else out)


def test_audit_finds_each_kind_and_ignores_clean_calls(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    cdir = Path(cfg["claude_config_dir"])
    events = [
        _tu("1", "Bash", {"command": "ls -la && adb shell input tap 1 2"}),         # deny_bash
        _tu("2", "Bash", {"command": "pytest -q"}),                                 # чисто
        _tu("3", "Read", {"file_path": str(repo / "app" / ".env")}),                # deny_read
        _tu("4", "Read", {"file_path": str(repo / "README.md")}),                   # чисто
        _tu("5", "Write", {"file_path": str(repo / "pipeline/demo/notes.md"),
                           "content": "прямого adb не было; subprocess не нужен"}),  # .md: тело не сканируется
        _tu("6", "Write", {"file_path": "src/app.py", "content": "print(1)"}),      # вне write_zones
        _tu("7", "Write", {"file_path": "runs/run.py", "content": "import subprocess"}),   # зона ок, deny_script
        _tu("8", "Edit", {"file_path": "../outside.txt", "new_string": "x"}),       # вне дерева серии
        _tu("9", "MultiEdit", {"file_path": "runs/ok.sh", "edits": [
            {"new_string": "echo ok"}, {"new_string": "os.system('x')"}]}),         # deny_script в MultiEdit
        _tu("10", "Glob", {"pattern": "*.py"}),
    ]
    _session(cdir, "sess-1", events)
    rc, res = _run_audit(cfg, td, repo, {"executor_session": "sess-1"}, capsys)
    assert rc == 1 and res["rc"] == 1 and res["checked"] is True
    got = sorted((f["kind"], f["tool"]) for f in res["findings"])
    assert got == [("bash", "Bash"), ("read", "Read"), ("script", "MultiEdit"), ("script", "Write"),
                   ("write", "Edit"), ("write", "Write")]
    by_kind = {(f["kind"], f["tool"]): f for f in res["findings"]}
    assert "adb shell" in by_kind[("bash", "Bash")]["subject"]
    assert by_kind[("write", "Edit")]["rule"] == "вне дерева серии"
    assert by_kind[("write", "Write")]["rule"] == "вне write_zones"
    assert res["events"] == 10 and res["tools"]["Bash"] == 2 and res["tools"]["Write"] == 3


def test_audit_subject_of_multiline_command_is_one_line(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    _session(Path(cfg["claude_config_dir"]), "s", [
        _tu("1", "Bash", {"command": "python3 - <<'EOF'\nimport os\nos.system('adb devices')\nEOF"})])
    rc, text = _run_audit(cfg, td, repo, {"executor_session": "s"}, capsys, json=False)
    line = next(ln for ln in text.splitlines() if ln.startswith("  [bash]"))
    assert rc == 1 and "python3 - <<'EOF' import os os.system('adb devices') EOF" in line


def test_audit_clean_series_returns_zero_and_prose_mentions_limits(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    _session(Path(cfg["claude_config_dir"]), "s", [
        _tu("1", "Bash", {"command": "pytest -q"}), _tu("2", "Write", {"file_path": "runs/a.txt", "content": "x"})])
    rc, text = _run_audit(cfg, td, repo, {"executor_session": "s"}, capsys, json=False)
    assert rc == 0 and "НАРУШЕНИЙ: 0" in text and "ограничение:" in text and "Bash 1" in text


def test_audit_dedups_tool_calls_across_resumed_sessions(tmp_path: Path, capsys):
    # resume копирует историю прошлой сессии в новый журнал: тот же tool_use.id — один вызов, не два
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    cdir = Path(cfg["claude_config_dir"])
    old = [_tu("a", "Bash", {"command": "adb devices"})]
    _session(cdir, "s-old", old)
    _session(cdir, "s-new", [*old, _tu("b", "Bash", {"command": "echo hi"})])
    st = {"executor_sessions": ["s-old", "s-new"], "executor_session": "s-new"}
    rc, res = _run_audit(cfg, td, repo, st, capsys)
    assert rc == 1 and res["events"] == 2 and len(res["findings"]) == 1


def test_audit_cannot_check_is_exit_2_not_clean(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    rc, res = _run_audit(cfg, td, repo, {}, capsys)                                 # серия не вызывала исполнителя
    assert rc == 2 and "нет id сессии" in res["error"]
    rc, res = _run_audit(cfg, td, repo, {"executor_session": "gone"}, capsys)       # журнал вычищен CLI
    assert rc == 2 and "gone" in res["error"] and "НЕ означает" in res["error"]
    bad = _cfg(tmp_path, repo, audit={"deny_bash": ["("]})
    rc, res = _run_audit(bad, td, repo, {"executor_session": "s"}, capsys)          # битый regex в правилах
    assert rc == 2 and "regex" in res["error"]
    notdict = _cfg(tmp_path, repo, audit=["deny_bash"])                             # не объект — не AttributeError
    rc, res = _run_audit(notdict, td, repo, {"executor_session": "s"}, capsys)
    assert rc == 2 and "объектом" in res["error"]


def test_audit_reports_partially_missing_sessions_but_checks_the_rest(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    _session(Path(cfg["claude_config_dir"]), "have", [_tu("1", "Bash", {"command": "pytest"})])
    rc, res = _run_audit(cfg, td, repo, {"executor_sessions": ["lost", "have"]}, capsys)
    assert rc == 0 and res["missing"] == ["lost"] and [s["id"] for s in res["sessions"]] == ["have"]


def test_audit_without_rules_counts_only_and_says_so(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo)                                                      # правил нет
    _session(Path(cfg["claude_config_dir"]), "s", [_tu("1", "Bash", {"command": "adb devices"})])
    rc, res = _run_audit(cfg, td, repo, {"executor_session": "s"}, capsys)
    assert rc == 0 and res["checked"] is False and res["tools"] == {"Bash": 1} and res["findings"] == []
    rc, text = _run_audit(cfg, td, repo, {"executor_session": "s"}, capsys, json=False)
    assert "нарушения НЕ проверялись" in text


def test_audit_write_zones_resolve_against_series_tree_root(tmp_path: Path, capsys):
    # для worktree-серии корень — её дерево, а не основной репозиторий
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    wt = tmp_path / "wt"
    cfg = _audit_cfg(tmp_path, repo)
    _session(Path(cfg["claude_config_dir"]), "s", [
        _tu("1", "Write", {"file_path": str(wt / "runs" / "x.txt"), "content": "x"}),        # в зоне worktree
        _tu("2", "Write", {"file_path": str(repo / "runs" / "x.txt"), "content": "x"})])    # основное дерево: вне
    rc, res = _run_audit(cfg, td, wt, {"executor_session": "s"}, capsys)
    assert rc == 1 and [f["rule"] for f in res["findings"]] == ["вне дерева серии"]


def test_audit_archive_is_opt_in_private_and_git_ignored(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    src = _session(Path(cfg["claude_config_dir"]), "sid1", [_tu("1", "Bash", {"command": "pytest"})])
    rc, res = _run_audit(cfg, td, repo, {"executor_session": "sid1"}, capsys)
    assert rc == 0 and res["archived"] == [] and not (td / "audit").exists()        # по умолчанию — не копируем
    rc, res = _run_audit(cfg, td, repo, {"executor_session": "sid1"}, capsys, archive=True)
    dst = td / "audit" / "session_sid1.jsonl.gz"
    assert res["archived"] == [str(dst)] and dst.exists()
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600                                # в журнале — результаты инструментов
    assert gzip.decompress(dst.read_bytes()) == src.read_bytes()
    assert (td / "audit" / ".gitignore").read_text(encoding="utf-8").strip() == "*"
    rc, text = _run_audit(cfg, td, repo, {"executor_session": "sid1"}, capsys, json=False, archive=True)
    assert "МОЖЕТ СОДЕРЖАТЬ СЕКРЕТЫ" in text


def test_audit_command_wiring_exit_codes_and_pure_json(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cdir = tmp_path / "claude-cfg"
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo), "claude_config_dir": str(cdir), "audit": RULES}),
                        encoding="utf-8")
    st = duo.load_json(td / "state.json")
    st["executor_session"] = "wired"
    duo.save_json(td / "state.json", st)
    _session(cdir, "wired", [_tu("1", "Bash", {"command": "adb devices"})])
    before = (td / "state.json").read_bytes()

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["duo_pipeline.py", "--config", str(cfg_path), *argv])
        with pytest.raises(SystemExit) as ei:
            duo.main()
        return ei.value.code

    assert cli("audit", "--task", "demo", "--json") == 1
    obj = json.loads(capsys.readouterr().out)                                       # ровно один JSON-объект
    assert obj["findings"][0]["kind"] == "bash"
    assert (td / "state.json").read_bytes() == before                               # read-only
    assert "Задача не найдена" in str(cli("audit", "--task", "typo"))              # не плодит pipeline/<опечатка>
    assert not (repo / "pipeline" / "typo").exists()


def test_session_ids_merge_history_with_legacy_single_id():
    assert audit.session_ids({"executor_sessions": ["a", "b"], "executor_session": "b"}) == ["a", "b"]
    assert audit.session_ids({"executor_session": "legacy"}) == ["legacy"]            # серия старше Н25
    assert audit.session_ids({"executor_sessions": ["a"], "executor_session": "c"}) == ["a", "c"]
    assert audit.session_ids({}) == []


# --- Н28: resources --------------------------------------------------------------------------


def test_resources_hold_info_release_and_alphabetical_order(tmp_path: Path):
    info = {"project": "p", "task": "t", "pid": 4242, "since": "10:00"}
    fds = duo.acquire_resources(["b:res", "a:res", "b:res"], info)
    assert len(fds) == 2                                                            # дубль имени — один замок
    h = duo.resource_holder("a:res")
    assert h == info and "p/t" in duo.format_holder(h) and "pid 4242" in duo.format_holder(h)
    duo.release_resources(fds)
    assert duo.resource_holder("a:res") is None and duo.resource_holder("never-used") is None


def test_resource_busy_refusal_names_holder_and_releases_partial_locks(tmp_path: Path):
    other = duo.acquire_resources(["b:res"], {"project": "other", "task": "t2", "pid": 7, "since": "09:00"})
    try:
        with pytest.raises(RuntimeError) as ei:
            duo.acquire_resources(["a:res", "b:res"], {"project": "me"})            # a свободен, b занят
        msg = str(ei.value)
        assert "'b:res'" in msg and "other/t2" in msg and "--wait-resources" in msg
        assert duo.resource_holder("a:res") is None                                 # взятый a отпущен при отказе
    finally:
        duo.release_resources(other)


def test_resources_are_taken_in_alphabetical_order_whatever_the_config_order(tmp_path: Path):
    # две серии с пересекающимися наборами в разном порядке не должны заклинить друг друга: первым
    # всегда берётся «меньшее» имя, поэтому при двух занятых ресурсах отказ называет именно его
    other = duo.acquire_resources(["a:res", "b:res"], {"project": "other", "task": "t2"})
    try:
        with pytest.raises(RuntimeError) as ei:
            duo.acquire_resources(["b:res", "a:res"], {"project": "me"})
        assert "'a:res'" in str(ei.value) and "'b:res'" not in str(ei.value)
    finally:
        duo.release_resources(other)


def test_resource_wait_succeeds_when_holder_releases(tmp_path: Path):
    other = duo.acquire_resources(["r"], {"project": "other", "task": "t2"})
    threading.Timer(0.3, duo.release_resources, args=(other,)).start()
    waits: list[str] = []
    fds = duo.acquire_resources(["r"], {"project": "me"}, wait_sec=5, on_wait=lambda n, h: waits.append(n),
                                poll_sec=0.05)
    try:
        assert waits == ["r"] and duo.resource_holder("r") == {"project": "me"}
    finally:
        duo.release_resources(fds)


def test_resource_wait_times_out_with_holder_in_message(tmp_path: Path):
    other = duo.acquire_resources(["r"], {"project": "other", "task": "t2"})
    try:
        t0 = time.time()
        with pytest.raises(RuntimeError, match="other/t2"):
            duo.acquire_resources(["r"], {"project": "me"}, wait_sec=0.3, poll_sec=0.05)
        assert 0.25 <= time.time() - t0 < 3
    finally:
        duo.release_resources(other)


def test_resource_held_by_foreign_flock_is_reported_as_external(tmp_path: Path):
    import fcntl
    path = duo.resource_lock_path("ext")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)                                  # как shell-`flock` без записи
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert duo.resource_holder("ext") == {}
        assert "внешний процесс" in duo.format_holder({})
        with pytest.raises(RuntimeError, match="внешний процесс"):
            duo.acquire_resources(["ext"], {"project": "me"})
    finally:
        os.close(fd)


def test_released_resource_leaves_no_stale_holder_record(tmp_path: Path):
    # запись держателя — содержимое файла замка; после release она не должна пережить держателя, иначе
    # следующий ВНЕШНИЙ владелец замка (shell-`flock` без записи) выглядел бы прежней серией
    import fcntl
    fds = duo.acquire_resources(["r"], {"project": "gone", "task": "old", "pid": 1, "since": "08:00"})
    duo.release_resources(fds)
    fd = os.open(str(duo.resource_lock_path("r")), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert duo.resource_holder("r") == {}                                         # «внешний», а не серия gone/old
    finally:
        os.close(fd)


def test_resource_name_maps_to_a_safe_path(tmp_path: Path):
    p = duo.resource_lock_path("adb:phone/1 ../x")
    assert p.parent == duo.resources_dir() and "/" not in p.name and p.name.endswith(".lock")


def test_run_holds_resources_for_its_lifetime_and_releases(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo, resources=["adb:phone", "gpu:0"]), "demo")
    seen: list = []
    monkeypatch.setattr(p, "_run_impl", lambda *a, **k: seen.append(
        (duo.resource_holder("adb:phone"), duo.resource_holder("gpu:0"))))
    p.run()
    holder = seen[0][0]
    assert holder["task"] == "demo" and holder["pid"] == os.getpid() and seen[0][1] == holder
    assert duo.resource_holder("adb:phone") is None and duo.resource_holder("gpu:0") is None
    assert "РЕСУРСЫ: захвачены adb:phone, gpu:0" in (td / "orchestrator.log").read_text(encoding="utf-8")


def test_run_refusal_on_busy_resource_leaves_series_untouched(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    other = duo.acquire_resources(["adb:phone"], {"project": "other", "task": "t2", "pid": 7, "since": "09:00"})
    try:
        p = duo.Pipeline(_cfg(tmp_path, repo, resources=["adb:phone"]), "demo")
        monkeypatch.setattr(p, "_run_impl", lambda *a, **k: pytest.fail("серия не должна стартовать"))
        before = (td / "state.json").read_bytes()
        with pytest.raises(SystemExit) as ei:
            p.run()
        assert "ОТКАЗ" in str(ei.value) and "other/t2" in str(ei.value)
        assert (td / "state.json").read_bytes() == before                           # ни байта в state
        duo.Pipeline(_cfg(tmp_path, repo), "demo").acquire_lock()                   # .lock отпущен после отказа
    finally:
        duo.release_resources(other)


def test_run_without_resources_does_not_touch_the_state_dir(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    p = duo.Pipeline(_cfg(tmp_path, repo), "demo")
    monkeypatch.setattr(p, "_run_impl", lambda *a, **k: None)
    p.run()
    assert not duo.resources_dir().exists()                                          # нет ключа — нет побочных эффектов


def test_run_cli_passes_wait_resources_to_pipeline(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo)}), encoding="utf-8")
    got: dict = {}
    monkeypatch.setattr(duo.Pipeline, "run", lambda self, **kw: got.update(kw))
    argv = ["duo_pipeline.py", "--config", str(cfg_path), "run", "--task", "demo", "--fg"]
    monkeypatch.setattr(sys, "argv", [*argv, "--wait-resources", "90"])
    duo.main()
    assert got["wait_resources"] == 90.0
    monkeypatch.setattr(sys, "argv", argv)
    duo.main()
    assert got["wait_resources"] == 0.0


def test_status_shows_guard_and_resources(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    plain = _cfg(tmp_path, repo)
    ns = types.SimpleNamespace(task="demo", all=False, json=False)
    duo.cmd_status(plain, ns)
    assert "технические запреты исполнителя: нет" in capsys.readouterr().out
    cfg = _cfg(tmp_path, repo, executor_deny=["Bash(adb:*)"], resources=["adb:phone"])
    duo.cmd_status(cfg, ns)
    out = capsys.readouterr().out
    assert "технические запреты исполнителя (executor_deny): 1" in out and "ресурс adb:phone: свободен" in out
    held = duo.acquire_resources(["adb:phone"], {"project": "other", "task": "t2", "pid": 7, "since": "09:00"})
    try:
        duo.cmd_status(cfg, types.SimpleNamespace(task="demo", all=False, json=True))
        g = json.loads(capsys.readouterr().out)["guard"]
        assert g["executor_deny"] == ["Bash(adb:*)"] and g["resources"]["adb:phone"]["task"] == "t2"
        duo.cmd_status(cfg, ns)
        assert "ресурс adb:phone: ЗАНЯТ — серия other/t2" in capsys.readouterr().out
    finally:
        duo.release_resources(held)


# --- Н29: подпись модели ---------------------------------------------------------------------


def _settings(cdir: Path, env: dict) -> None:
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "settings.json").write_text(json.dumps({"env": env}), encoding="utf-8")


def test_model_label_maps_alias_without_leaking_secrets(tmp_path: Path):
    cdir = tmp_path / "prof"
    _settings(cdir, {"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3-flash", "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3",
                     "ANTHROPIC_AUTH_TOKEN": "sekret-token-123"})
    cfg = {"claude_config_dir": str(cdir), "model": "opus"}
    assert duo.model_label(cfg) == "opus → glm-5.3"
    assert duo.model_label(cfg, "sonnet") == "sonnet → glm-5.3-flash"
    assert duo.model_label(cfg, "haiku") == "haiku"                                  # маппинга нет — голый алиас
    assert "sekret" not in duo.model_label(cfg, "sonnet")


@pytest.mark.parametrize("content", [None, "{не json", '{"env": null}', "[]"])
def test_model_label_survives_missing_or_broken_settings(tmp_path: Path, content):
    cdir = tmp_path / "prof"
    cdir.mkdir()
    if content is not None:
        (cdir / "settings.json").write_text(content, encoding="utf-8")
    assert duo.model_label({"claude_config_dir": str(cdir), "model": "sonnet"}) == "sonnet"


def test_solo_log_line_names_real_model_and_profile(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    cfg["solo"] = {"model": "sonnet"}
    _settings(Path(cfg["claude_config_dir"]), {"ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3-flash"})
    p = duo.Pipeline(cfg, "demo", solo=True)
    p.t = ScriptedTransport(["сделано\nSOLO_DONE"])
    p.run()
    log = _log_text(p)
    assert "модель: sonnet → glm-5.3-flash" in log and cfg["claude_config_dir"] in log
    assert _state(p.task_dir)["phase"] == "done"


# --- прогресс и подсказка при первом init ------------------------------------------------------


def test_tool_lines_carry_elapsed_and_counter(tmp_path: Path, monkeypatch, capsys):
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                       "input": {"command": "pytest -q"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read",
                                                       "input": {"file_path": "a.py"}}]}},
        {"type": "result", "session_id": "s", "total_cost_usd": 0.0, "result": "ok"},
    ]
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _SlowProc(events, 0.0))
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    glm.call("p", mode="plan", timeout=30)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "· [" in ln]
    assert re.fullmatch(r"    · \[00:0\d #1\] Bash\(pytest -q\)", lines[0]), lines
    assert re.fullmatch(r"    · \[00:0\d #2\] Read\(a\.py\)", lines[1]), lines


def _init_ns(task: str):
    return types.SimpleNamespace(task=task, task_file=None, mode=None, worktree=False, isolation=None)


def test_first_init_prints_brief_pointer_once(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)
    duo.cmd_init(cfg, _init_ns("first"))
    out = capsys.readouterr().out
    assert "Первая серия проекта" in out and "FIELD-TEST-BRIEF.md" in out and "CHEATSHEET-CONTROL.md" in out
    assert "оборудовании" in out
    duo.cmd_init(cfg, _init_ns("second"))                                            # вторая серия — без подсказки
    assert "Первая серия проекта" not in capsys.readouterr().out
    duo.cmd_init(cfg, _init_ns("first"))                                             # повторный init той же — тоже
    assert "Первая серия проекта" not in capsys.readouterr().out


# --- Н27: duo accept ---------------------------------------------------------------------------

accept = None


def _accept_mod():
    global accept
    if accept is None:
        spec = importlib.util.spec_from_file_location("accept_cmd_under_test", REPO_ROOT / "accept_cmd.py")
        assert spec is not None and spec.loader is not None
        accept = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = accept
        spec.loader.exec_module(accept)
    return accept


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_acceptance_steps_validation_normalizes_and_reports_problems():
    m = _accept_mod()
    steps, problems = m.validate_steps({"steps": [{"argv": ["echo", "x"]},
                                                  {"name": "n", "argv": ["a"], "inconclusive_rc": 2, "timeout_sec": 5}]})
    assert problems == [] and steps[0]["name"] == "step1" and steps[0]["timeout_sec"] == 600.0
    assert steps[1]["inconclusive_rc"] == 2 and steps[1]["timeout_sec"] == 5.0
    assert m.validate_steps(None) == ([], []) and m.validate_steps({}) == ([], [])
    for bad, needle in [
        ([], "acceptance должен быть объектом"),
        ({"steps": "x"}, "списком шагов"),
        ({"steps": ["x"]}, "шаг должен быть объектом"),
        ({"steps": [{"argv": []}]}, "argv"),
        ({"steps": [{"argv": "ls -la"}]}, "argv"),                                    # строка вместо списка (shell нет)
        ({"steps": [{"argv": ["a"], "inconclusive_rc": 0}]}, "inconclusive_rc"),    # rc 0 — это PASS
        ({"steps": [{"argv": ["a"], "inconclusive_rc": True}]}, "inconclusive_rc"),
        ({"steps": [{"argv": ["a"], "timeout_sec": 0}]}, "timeout_sec"),
    ]:
        assert any(needle in p for p in m.validate_steps(bad)[1]), bad


def test_step_verdicts_and_aggregate_precedence():
    m = _accept_mod()
    codes = {"ok": 0, "bad": 1, "nodev": 2}

    def runner(argv, *, cwd, env, timeout):
        if argv[0] == "hang":
            return None, "", "таймаут 1 с"
        return codes[argv[0]], "line1\nline2\nпоследняя", None

    steps = [{"name": n, "argv": [n], "inconclusive_rc": 2, "timeout_sec": 1.0} for n in ("ok", "bad", "nodev", "hang")]
    res = m.run_steps(steps, cwd=".", env={}, runner=runner)
    assert [r["verdict"] for r in res] == ["PASS", "FAIL", "INCONCLUSIVE", "INCONCLUSIVE"]   # не останавливается на провале
    assert res[0]["tail"] == "" and "последняя" in res[1]["tail"]                           # tail — только у не-PASS
    assert "таймаут" in res[3]["reason"] and res[3]["rc"] is None
    assert m.aggregate(res) == "FAIL"
    assert m.aggregate([r for r in res if r["verdict"] != "FAIL"]) == "INCONCLUSIVE"
    assert m.aggregate([res[0]]) == "PASS" and m.aggregate([]) == "INCONCLUSIVE"
    nodev_plain = m.run_steps([{"name": "n", "argv": ["nodev"], "inconclusive_rc": None, "timeout_sec": 1.0}],
                              cwd=".", env={}, runner=runner)
    assert nodev_plain[0]["verdict"] == "FAIL"                                              # без inconclusive_rc rc=2 — провал


def test_default_runner_real_processes(tmp_path: Path):
    m = _accept_mod()
    steps = [{"name": "fail", "argv": _py("import sys; print('boom'); sys.exit(3)"), "inconclusive_rc": None,
              "timeout_sec": 30.0},
             {"name": "slow", "argv": _py("import time; time.sleep(5)"), "inconclusive_rc": None, "timeout_sec": 0.3},
             {"name": "missing", "argv": ["no-such-binary-xyz"], "inconclusive_rc": None, "timeout_sec": 5.0}]
    t0 = time.time()
    res = m.run_steps(steps, cwd=str(tmp_path), env=dict(os.environ))
    assert time.time() - t0 < 4
    assert (res[0]["verdict"], res[0]["rc"], res[0]["tail"]) == ("FAIL", 3, "boom")
    assert res[1]["verdict"] == "INCONCLUSIVE" and "таймаут" in res[1]["reason"]
    assert res[2]["verdict"] == "INCONCLUSIVE" and "нет 'no-such-binary-xyz'" in res[2]["reason"]


def _accept_setup(tmp_path: Path, steps, phase: str = "done", **cfg_over):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["phase"] = phase
    duo.save_json(td / "state.json", st)
    cfg = _cfg(tmp_path, repo, acceptance={"steps": steps}, **cfg_over)
    return repo, td, cfg


def _accept(cfg: dict, capsys, **over):
    ns = types.SimpleNamespace(task="demo", json=False, dry_run=False, wait_resources=0.0)
    for k, v in over.items():
        setattr(ns, k, v)
    rc = duo.cmd_accept(cfg, ns)
    return rc, capsys.readouterr().out


def test_accept_pass_records_verdict_without_output_and_keeps_phase(tmp_path: Path, capsys):
    steps = [{"name": "marker", "argv": _py("import os,sys; print('SECRET=hunter2'); "
                                              "sys.exit(0 if os.path.exists('marker.txt') else 1)")}]
    repo, td, cfg = _accept_setup(tmp_path, steps, phase="needs_human")
    (repo / "marker.txt").write_text("x", encoding="utf-8")                              # шаг идёт в дереве серии
    rc, out = _accept(cfg, capsys)
    assert rc == 0 and "ACCEPT-RESULT: verdict=PASS steps=1 pass=1 fail=0 inconclusive=0 rc=0" in out
    st = duo.load_json(td / "state.json")
    assert st["phase"] == "needs_human"                                                  # phase приёмкой не меняется (даже PASS не «закрывает» серию)
    acc = st["acceptance"]
    assert acc["verdict"] == "PASS" and acc["phase"] == "needs_human" and acc["head"]
    assert acc["steps"][0]["name"] == "marker" and "tail" not in acc["steps"][0]
    assert "hunter2" not in (td / "state.json").read_text(encoding="utf-8")              # вывод шага в state не попадает
    assert "ПРИЁМКА: PASS" in (td / "orchestrator.log").read_text(encoding="utf-8")


def test_accept_fail_and_inconclusive_exit_codes(tmp_path: Path, capsys):
    steps = [{"name": "marker", "argv": _py("import os,sys; print('нет метки'); sys.exit(0 if os.path.exists('m') else 1)")}]
    repo, td, cfg = _accept_setup(tmp_path, steps)
    rc, out = _accept(cfg, capsys)                                                       # метки нет → FAIL
    assert rc == 1 and "[FAIL] marker" in out and "нет метки" in out and "verdict=FAIL" in out
    assert duo.load_json(td / "state.json")["acceptance"]["verdict"] == "FAIL"
    cfg2 = _cfg(tmp_path, repo, acceptance={"steps": [
        {"name": "dev", "argv": _py("import sys; sys.exit(7)"), "inconclusive_rc": 7}]})
    rc, out = _accept(cfg2, capsys)
    assert rc == 2 and "[INCONCLUSIVE] dev" in out and "устройство недоступно" in out
    assert duo.load_json(td / "state.json")["acceptance"]["verdict"] == "INCONCLUSIVE"   # последний вердикт перезаписан


def test_accept_not_configured_or_broken_config_is_rc2_and_touches_nothing(tmp_path: Path, capsys):
    repo, td, cfg = _accept_setup(tmp_path, [])
    before = (td / "state.json").read_bytes()
    rc, out = _accept(cfg, capsys)
    assert rc == 2 and "приёмка не настроена" in out
    cfg_bad = _cfg(tmp_path, repo, acceptance={"steps": [{"argv": "ls"}]})
    rc, out = _accept(cfg_bad, capsys)
    assert rc == 2 and "приёмка невозможна" in out and "argv" in out
    rc, out = _accept(_cfg(tmp_path, repo, acceptance={"steps": [{"argv": ["true"]}]}, executor_deny=["a,b"]), capsys)
    assert rc == 2 and "запятая" in out
    assert (td / "state.json").read_bytes() == before and "acceptance" not in duo.load_json(td / "state.json")


def test_accept_refuses_live_series_with_rc2_not_fail(tmp_path: Path, capsys):
    steps = [{"name": "s", "argv": _py("open('ran.txt','w').close()")}]
    repo, td, cfg = _accept_setup(tmp_path, steps)
    with _HeldLock(td):
        rc, out = _accept(cfg, capsys)
    assert rc == 2 and "приёмка невозможна" in out and "уже выполняется" in out         # rc=1 читался бы как FAIL
    assert not (repo / "ran.txt").exists()


def test_accept_holds_resource_locks_during_steps_and_releases_after(tmp_path: Path, capsys):
    lock = duo.resource_lock_path("adb:phone")
    probe = ("import fcntl,os,sys\n"
             f"fd=os.open({str(lock)!r}, os.O_RDWR)\n"
             "try:\n    fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n    sys.exit(0)\n"
             "sys.exit(1)")                                                              # замок держит приёмка → PASS
    repo, td, cfg = _accept_setup(tmp_path, [{"name": "locked", "argv": _py(probe)}], resources=["adb:phone"])
    rc, out = _accept(cfg, capsys)
    assert rc == 0, out
    assert duo.resource_holder("adb:phone") is None                                      # после приёмки замок свободен


def test_accept_busy_resource_refuses_before_running_any_step(tmp_path: Path, capsys):
    steps = [{"name": "s", "argv": _py("open('ran.txt','w').close()")}]
    repo, td, cfg = _accept_setup(tmp_path, steps, resources=["adb:phone"])
    other = duo.acquire_resources(["adb:phone"], {"project": "other", "task": "t2", "pid": 7, "since": "09:00"})
    try:
        before = (td / "state.json").read_bytes()
        rc, out = _accept(cfg, capsys)
        assert rc == 2 and "ОТКАЗ" in out and "other/t2" in out
        assert not (repo / "ran.txt").exists() and (td / "state.json").read_bytes() == before
        duo.Pipeline(cfg, "demo").acquire_lock()                                         # .lock отпущен
    finally:
        duo.release_resources(other)


def test_accept_dry_run_prints_plan_and_runs_nothing(tmp_path: Path, capsys):
    steps = [{"name": "s", "argv": _py("open('ran.txt','w').close()"), "inconclusive_rc": 2}]
    repo, td, cfg = _accept_setup(tmp_path, steps, resources=["adb:phone"])
    before = (td / "state.json").read_bytes()
    rc, out = _accept(cfg, capsys, dry_run=True)
    assert rc == 0 and "dry-run" in out and "шаг s:" in out and "adb:phone" in out and "rc=2 = INCONCLUSIVE" in out
    assert not (repo / "ran.txt").exists() and (td / "state.json").read_bytes() == before
    assert duo.resource_holder("adb:phone") is None


def test_accept_runs_in_worktree_of_a_worktree_series(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo, acceptance={"steps": [
        {"name": "where", "argv": _py("import os,sys; sys.exit(0 if os.path.basename(os.getcwd()) == 'demo' else 1)")}]})
    cfg["git"]["isolation"] = "worktree"
    p = duo.Pipeline(cfg, "demo")
    p._ensure_worktree(p.load_state())
    rc, out = _accept(cfg, capsys)
    assert rc == 0, out
    assert f"дерево {p.work_path}" in out
    assert p.work_path != repo and duo.load_json(td / "state.json")["acceptance"]["verdict"] == "PASS"


def test_accept_json_is_one_object_and_cli_wiring(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo), "acceptance": {"steps": [
        {"name": "ok", "argv": _py("print('hi')")}, {"name": "bad", "argv": _py("import sys; sys.exit(1)")}]}}),
        encoding="utf-8")

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["duo_pipeline.py", "--config", str(cfg_path), *argv])
        with pytest.raises(SystemExit) as ei:
            duo.main()
        return ei.value.code

    assert cli("accept", "--task", "demo", "--json") == 1
    obj = json.loads(capsys.readouterr().out)
    assert obj["verdict"] == "FAIL" and obj["rc"] == 1 and [s["verdict"] for s in obj["steps"]] == ["PASS", "FAIL"]
    assert cli("accept", "--task", "demo", "--dry-run", "--json") == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert "Задача не найдена" in str(cli("accept", "--task", "typo"))
    assert not (repo / "pipeline" / "typo").exists() and (td / "state.json").exists()


def test_status_shows_acceptance_and_marks_it_stale_after_new_commit(tmp_path: Path, capsys):
    steps = [{"name": "s", "argv": _py("pass")}]
    repo, td, cfg = _accept_setup(tmp_path, steps)
    _accept(cfg, capsys)
    ns = types.SimpleNamespace(task="demo", all=False, json=False)
    duo.cmd_status(cfg, ns)
    out = capsys.readouterr().out
    assert "живая приёмка: PASS" in out and "УСТАРЕЛА" not in out and "`done` приёмкой не менялся" in out
    (repo / "later.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later")
    duo.cmd_status(cfg, ns)
    assert "УСТАРЕЛА: HEAD с тех пор изменился" in capsys.readouterr().out
