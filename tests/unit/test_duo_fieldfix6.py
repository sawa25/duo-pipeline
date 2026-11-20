"""fieldfix6: правки по двум полевым отчётам 20.11.2026 (вечер; solo-серии на оборудовании и pair на NAS-проекте).

Н30  «серия закончилась, а оператор не заметил»: проверка по журналу надзора показала, что уведомление
     доставлено (надзор), но слалось по 2–4 раза на серию — ключ события зависел от heartbeat state, а `accept`
     (Н27) пишет state. Теперь ключ — отпечаток события (`phase_event_key`); `status` говорит «ЖДЁТ ВАС N мин»
     (`waiting_since`); подсказка `run` — блок «СТОРОЖ».
Н31  токен `@plan` в `audit.write_zones` — зона записи из `## files_touch` принятого плана.
Н32  `acceptance.steps[].inconclusive_match` — regex по выводу при rc != 0 → INCONCLUSIVE (никогда не PASS).
Паттерн test_duo_fieldfix5: git-песочницы в tmp_path, без LLM.
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix4 import _detached, _status_ns
from tests.unit.test_duo_fieldfix5 import (
    _accept,
    _accept_mod,
    _accept_setup,
    _audit_cfg,
    _audit_ns,
    _py,
    _session,
    _tu,
    audit,
)
from tests.unit.test_duo_pipemodes import PairTransport, _pcfg, _state
from tests.unit.test_duo_pipev3 import ScriptedTransport, _cfg, _init_repo, _mk_task, duo

# --- Н30: ключ события надзора -----------------------------------------------------------------


def test_phase_event_key_ignores_heartbeat_and_follows_real_completion():
    base = {"phase": "done", "spent_usd": 4.74, "plan_version": 0, "impl_version": 0, "updated_at": 100.0}
    assert duo.phase_event_key(base) == duo.phase_event_key({**base, "updated_at": 999.0})   # heartbeat не событие
    assert duo.phase_event_key(base) != duo.phase_event_key({**base, "spent_usd": 6.1})      # доработка — новое
    assert duo.phase_event_key(base) != duo.phase_event_key({**base, "phase": "impl_pending"})
    assert duo.phase_event_key(base) != duo.phase_event_key({**base, "impl_version": 2})
    assert duo.phase_event_key({"phase": "done"}) == "done:0:0:0"                            # поля могут отсутствовать


def test_supervisor_notices_done_once_even_after_accept_rewrites_state(tmp_path: Path, capsys):
    # воспроизведение инцидента: SOLO_DONE → уведомление; `duo accept` пишет state (heartbeat) → раньше
    # надзор слал «ГОТОВО» повторно (по одному на каждый accept)
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo, acceptance={"steps": [{"name": "ok", "argv": _py("pass")}]})
    cfg["notify"] = {"desktop": True}
    notes: list[str] = []
    kw = {"state_path": tmp_path / "sup.json", "spawn": lambda t: None,
          "notify": lambda title, body: notes.append(title)}
    (repo / "pipeline" / "bystander").mkdir()
    duo.save_json(repo / "pipeline" / "bystander" / "state.json", {"task": "bystander", "phase": "plan"})
    assert duo.supervise_tick(cfg, now=time.time(), **kw) == []                  # первый тик — фон (baseline)

    st = duo.load_json(td / "state.json")
    st.update(phase="done", spent_usd=4.74, state_updated_at=time.time())
    duo.save_json(td / "state.json", st)
    acts = duo.supervise_tick(cfg, now=time.time(), **kw)
    assert [a["action"] for a in acts] == ["notice"] and len(notes) == 1

    rc, _ = _accept(cfg, capsys)                                                 # state переписан, heartbeat новый
    assert rc == 0 and duo.load_json(td / "state.json")["acceptance"]["verdict"] == "PASS"
    assert duo.supervise_tick(cfg, now=time.time() + 120, **kw) == [] and len(notes) == 1

    st = duo.load_json(td / "state.json")                                        # доработка и новый done — событие
    st.update(spent_usd=6.10, state_updated_at=time.time())
    duo.save_json(td / "state.json", st)
    acts = duo.supervise_tick(cfg, now=time.time() + 240, **kw)
    assert [a["action"] for a in acts] == ["notice"] and len(notes) == 2


def test_supervisor_migrates_legacy_event_key_silently(tmp_path: Path):
    # состояние надзора со старым ключом `<фаза>:<heartbeat>`: первый тик нового кода НЕ шлёт повтор
    # по уже отмеченной свежей серии; настоящее новое завершение после миграции уведомляется как обычно
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    cfg["notify"] = {"desktop": True}
    st = duo.load_json(td / "state.json")
    st.update(phase="done", spent_usd=2.0, state_updated_at=time.time() - 100)
    duo.save_json(td / "state.json", st)
    sup = tmp_path / "sup.json"
    duo.save_json(sup, {f"{Path(cfg['repo']).resolve()}::demo": {"noticed": f"done:{int(st['state_updated_at'])}"}})
    notes: list[str] = []
    kw = {"state_path": sup, "spawn": lambda t: None, "notify": lambda title, body: notes.append(title)}
    assert duo.supervise_tick(cfg, now=time.time(), **kw) == [] and notes == []         # миграция — молча
    rec = duo.load_json(sup)[f"{Path(cfg['repo']).resolve()}::demo"]
    assert rec["noticed"] == "done:200:0:0"                                              # ключ переписан
    assert duo.supervise_tick(cfg, now=time.time() + 60, **kw) == []
    st.update(spent_usd=3.5, state_updated_at=time.time())
    duo.save_json(td / "state.json", st)
    assert [a["action"] for a in duo.supervise_tick(cfg, now=time.time() + 120, **kw)] == ["notice"]
    # чужая фаза в старом ключе — не миграция: impl_pending после done уведомляется
    duo.save_json(sup, {f"{Path(cfg['repo']).resolve()}::demo": {"noticed": f"impl_pending:{int(time.time())}"}})
    st.update(phase="done", spent_usd=9.0, state_updated_at=time.time())
    duo.save_json(td / "state.json", st)
    assert [a["action"] for a in duo.supervise_tick(cfg, now=time.time() + 180, **kw)] == ["notice"]


# --- Н30: «ждёт вас» ---------------------------------------------------------------------------


def test_waiting_since_is_set_at_each_end_of_phase(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    p.t = PairTransport(verdicts=["ACCEPT"])
    t0 = time.time()
    p.run()
    st = _state(td)
    assert st["phase"] == "impl_pending" and t0 <= st["waiting_since"] <= time.time()   # план принят — ждём человека
    plan_wait = st["waiting_since"]
    time.sleep(0.02)
    q = duo.Pipeline(cfg, "demo")
    q.t = PairTransport(verdicts=["ACCEPT"])
    q.run(force_continue=True)
    st = _state(td)
    assert st["phase"] == "done" and st["waiting_since"] > plan_wait                   # код принят — новая точка


def test_waiting_since_is_set_on_solo_done(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", solo=True)
    p.t = ScriptedTransport(["сделано\nSOLO_DONE"])
    p.run()
    st = _state(td)
    assert st["phase"] == "done" and st["waiting_since"] > 0


def test_accept_does_not_move_waiting_since(tmp_path: Path, capsys):
    # heartbeat двигается любой записью state, а «с какого момента ждём» — нет
    repo, td, cfg = _accept_setup(tmp_path, [{"name": "ok", "argv": _py("pass")}])
    st = duo.load_json(td / "state.json")
    st["waiting_since"] = time.time() - 600
    duo.save_json(td / "state.json", st)
    rc, _ = _accept(cfg, capsys)
    after = duo.load_json(td / "state.json")
    assert rc == 0 and after["waiting_since"] == st["waiting_since"]
    assert after["state_updated_at"] > st["waiting_since"] + 500


def test_waiting_info_per_phase_and_fallbacks():
    now = 1_800_000_000.0                                   # позже halt.at из 2026 года
    assert duo.waiting_info({"phase": "impl"}, now) is None
    assert duo.waiting_info({"phase": "plan"}, now) is None
    w = duo.waiting_info({"phase": "impl_pending", "waiting_since": now - 125 * 60}, now)
    assert w == {"phase": "impl_pending", "since": now - 7500, "min": 125}
    legacy = duo.waiting_info({"phase": "done", "state_updated_at": now - 600}, now)    # серия до Н30
    assert legacy is not None and legacy["min"] == 10
    halted = duo.waiting_info({"phase": "needs_human", "state_updated_at": now - 5,
                               "halt": {"at": "2026-11-20T10:00:00"}}, now)
    assert halted is not None and halted["since"] < now - 3600                           # отсчёт от halt.at, не heartbeat
    broken = duo.waiting_info({"phase": "needs_human", "state_updated_at": now - 60, "halt": {"at": "не дата"}}, now)
    assert broken is not None and broken["min"] == 1
    assert duo.waiting_info({"phase": "done"}, now) is None                              # отсчитывать не от чего
    assert duo.format_wait_age(47) == "47 мин" and duo.format_wait_age(125) == "2 ч 05 мин"


def test_status_says_who_waits_for_whom(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _cfg(tmp_path, repo)

    def status(**over):
        st = duo.load_json(td / "state.json")
        st.update(over)
        duo.save_json(td / "state.json", st)
        duo.cmd_status(cfg, _status_ns())
        return capsys.readouterr().out

    out = status(phase="impl_pending", waiting_since=time.time() - 47 * 60)
    assert "ЖДЁТ ВАС 47 мин" in out and "--continue" in out and "НЕ стартует" in out
    out = status(phase="done", waiting_since=time.time() - 5 * 60)
    assert "ЗАВЕРШЕНА 5 мин назад" in out and "commit-by-plan" in out and "ЖДЁТ ВАС" not in out
    assert "ЗАВЕРШЕНА" not in status(phase="done", waiting_since=time.time() - 3 * 86400)   # давняя — не шумим
    assert "ЖДЁТ ВАС" not in status(phase="impl", waiting_since=time.time() - 3600)
    st = status(phase="needs_human", waiting_since=0, stop_reason="лимит",
                halt={"kind": "limit", "phase": "impl", "resumable": True, "at": "2026-11-20T10:00:00"})
    assert "ЖДЁТ ВАС" in st and "остановка требует решения" in st
    duo.cmd_status(cfg, _status_ns(json=True))
    obj = json.loads(capsys.readouterr().out)
    assert obj["waiting"]["phase"] == "needs_human" and obj["waiting"]["min"] > 0


def test_run_hint_is_a_block_with_the_exact_wait_command(tmp_path: Path, monkeypatch, capsys):
    cfg, _ = _detached(tmp_path, monkeypatch, None)
    duo.run_detached(cfg, "demo", ["run", "--task", "demo"])
    out = capsys.readouterr().out
    assert "СТОРОЖ" in out and "В ФОНЕ" in out and "notify_on_output" in out
    assert "duo wait --task demo --timeout 3600 --quiet" in out
    assert "journalctl --user -u duo-supervise.service" in out                         # где смотреть уведомления
    assert "nohup" in out                                                              # типичная ловушка названа прямо


# --- Н31: @plan в audit.write_zones --------------------------------------------------------------


@pytest.mark.parametrize("zones, plan_zone, active, unresolved", [
    (["pipeline/{task}/"], None, True, False),                      # статика, `@plan` не просили
    (["@plan"], None, False, True),                                 # просили, плана нет: проверять нечем
    (["@plan"], [], True, False),                                   # план заявил «ничего не пишем»
    (["@plan"], ["src/mod/*"], True, False),
    (["pipeline/{task}/", "@plan"], None, True, True),              # статика держится, `@plan` честно пуст
    ([], ["src/mod/*"], False, False),                              # без токена plan_zone игнорируется
])
def test_compile_rules_plan_token(zones, plan_zone, active, unresolved):
    rules = audit.compile_rules({"write_zones": zones}, "demo", plan_zone)
    assert rules["zone_active"] is active and rules["plan_unresolved"] is unresolved
    assert rules["plan_zone"] == (list(plan_zone or []) if "@plan" in zones else [])
    assert "@plan" not in rules["write_zones"]                      # токен не превращается в каталог `@plan`


def _plan_audit(tmp_path: Path, capsys, files: list[str], zones, plan_zone, **over):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _audit_cfg(tmp_path, repo)
    cfg["audit"] = {"write_zones": zones}
    _session(Path(cfg["claude_config_dir"]), "s", [
        _tu(str(i), "Write", {"file_path": str(repo / f), "content": "x"}) for i, f in enumerate(files)])
    rc = audit.cmd_audit(cfg, _audit_ns(**over), td, repo, {"executor_session": "s"}, plan_zone)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if over.get("json", True) else out)


def test_audit_plan_zone_allows_declared_files_dirs_and_globs_only(tmp_path: Path, capsys):
    files = ["src/mod/a.py", "src/mod/sub/b.py", "docs/note.md", "pipeline/demo/rep.md",   # заявлено / служебное
             "src/other.py", "src/module2/x.py", "docs/note.md.bak"]                      # чужое, префиксные ловушки
    rc, res = _plan_audit(tmp_path, capsys, files, ["pipeline/{task}/", "@plan"], ["src/mod/*", "docs/note.md"])
    assert rc == 1
    assert [f["subject"].rsplit("/", 2)[-2:] for f in res["findings"]] == [
        ["src", "other.py"], ["module2", "x.py"], ["docs", "note.md.bak"]]
    assert {f["rule"] for f in res["findings"]} == {"вне write_zones и зоны плана"}
    assert res["plan_zone"] == ["src/mod/*", "docs/note.md"] and res["rules"]["write_zones"] == 3


def test_audit_empty_plan_zone_flags_every_write_outside_static_zones(tmp_path: Path, capsys):
    rc, res = _plan_audit(tmp_path, capsys, ["src/a.py", "pipeline/demo/r.md"], ["pipeline/{task}/", "@plan"], [])
    assert rc == 1 and len(res["findings"]) == 1 and res["findings"][0]["subject"].endswith("src/a.py")
    assert res["findings"][0]["rule"] == "вне write_zones"          # плановой зоны нет — причина без неё


def test_audit_unresolved_plan_token_is_loud_and_keeps_static_zones(tmp_path: Path, capsys):
    rc, res = _plan_audit(tmp_path, capsys, ["runs/x.txt", "src/a.py"], ["runs", "@plan"], None)
    assert rc == 1 and [f["subject"].endswith("src/a.py") for f in res["findings"]] == [True]
    assert res["plan_zone"] is None
    (tmp_path / "t2").mkdir()
    rc, text = _plan_audit(tmp_path / "t2", capsys, ["runs/x.txt"], ["runs", "@plan"], None, json=False)
    assert "`@plan` НЕ раскрыт" in text and "только статические зоны" in text and "запись НЕ проверялась" not in text


def test_audit_only_unresolved_plan_token_says_writes_were_not_checked(tmp_path: Path, capsys):
    rc, text = _plan_audit(tmp_path, capsys, ["src/a.py"], ["@plan"], None, json=False)
    assert rc == 0 and "`@plan` НЕ раскрыт" in text and "запись НЕ проверялась" in text
    assert "нарушения НЕ проверялись" in text                       # «чисто» здесь не значит «проверено»


def test_audit_dispatcher_expands_plan_from_accepted_plan(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cdir = tmp_path / "claude-cfg"
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo), "claude_config_dir": str(cdir),
                                    "audit": {"write_zones": ["@plan"]}}), encoding="utf-8")
    (td / "plan_v1.md").write_text("# План\n\n## files_touch\n\n- `src/mod/*`\n- `docs/note.md`\n\n## Риски\n",
                                   encoding="utf-8")
    st = duo.load_json(td / "state.json")
    st.update(executor_session="wired", plan_version=1)
    duo.save_json(td / "state.json", st)
    _session(cdir, "wired", [_tu("1", "Write", {"file_path": str(repo / "src/mod/a.py"), "content": "x"}),
                             _tu("2", "Write", {"file_path": str(repo / "src/core.py"), "content": "x"})])

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["duo_pipeline.py", "--config", str(cfg_path), *argv])
        with pytest.raises(SystemExit) as ei:
            duo.main()
        return ei.value.code

    assert cli("audit", "--task", "demo", "--json") == 1
    obj = json.loads(capsys.readouterr().out)
    assert obj["plan_zone"] == ["src/mod/*", "docs/note.md"]
    assert [f["subject"].endswith("src/core.py") for f in obj["findings"]] == [True]
    (td / "plan_v1.md").write_text("# План без секции зоны\n", encoding="utf-8")        # секции нет — не раскрыт
    assert cli("audit", "--task", "demo", "--json") == 0
    assert json.loads(capsys.readouterr().out)["plan_zone"] is None


def test_guard_rejects_blank_or_non_string_write_zone_entries(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo, audit={"write_zones": ["@plan", "", 7]})
    bad = duo.guard_config_problems(cfg)
    assert sum("audit.write_zones: пустая/нестроковая запись" in b for b in bad) == 2
    assert not duo.guard_config_problems(_cfg(tmp_path, repo, audit={"write_zones": ["@plan", "src/"]}))


# --- Н32: inconclusive_match ---------------------------------------------------------------------


def _step(code: str, **extra) -> dict:
    return {"name": "check", "argv": _py(code), **extra}


def test_validate_steps_inconclusive_match_normalizes_and_rejects_bad_regex():
    acc = _accept_mod()
    steps, problems = acc.validate_steps({"steps": [_step("pass", inconclusive_match="SSL|locked")]})
    assert not problems and steps[0]["inconclusive_match"] == "SSL|locked"
    steps, problems = acc.validate_steps({"steps": [_step("pass")]})
    assert steps[0]["inconclusive_match"] is None
    for bad in ("(", "", 5, ["x"]):
        _, problems = acc.validate_steps({"steps": [_step("pass", inconclusive_match=bad)]})
        assert problems and "inconclusive_match" in problems[0], bad


def test_inconclusive_match_downgrades_only_failures_and_never_yields_pass(tmp_path: Path):
    acc = _accept_mod()
    pat = r"SSL: CERTIFICATE|screen is locked"
    steps, _ = acc.validate_steps({"steps": [
        _step("import sys; print('ok'); sys.exit(0)", inconclusive_match=pat),                     # rc 0 — PASS
        _step("import sys; sys.stderr.write('SSL: CERTIFICATE_VERIFY_FAILED'); sys.exit(1)", inconclusive_match=pat),
        _step("import sys; print('assert 3 == 4'); sys.exit(1)", inconclusive_match=pat),          # дефект — FAIL
        _step("import sys; print('screen is locked'); sys.exit(1)"),                               # без regex — FAIL
        _step("import sys; print('SSL: CERTIFICATE'); sys.exit(0)", inconclusive_match=pat),       # rc 0 важнее текста
    ]})
    res = acc.run_steps(steps, cwd=tmp_path, env={})
    assert [r["verdict"] for r in res] == ["PASS", "INCONCLUSIVE", "FAIL", "FAIL", "PASS"]
    assert res[1]["reason"] == f"вывод подошёл под inconclusive_match: {pat}" and res[1]["rc"] == 1
    assert res[0]["reason"] is None and res[2]["reason"] is None
    assert acc.aggregate(res) == "FAIL"                                                            # один дефект — FAIL
    assert acc.aggregate([res[0], res[1]]) == "INCONCLUSIVE"                                       # не «зелёный»


def test_inconclusive_rc_wins_over_match_and_both_are_kept(tmp_path: Path):
    acc = _accept_mod()
    steps, _ = acc.validate_steps({"steps": [
        _step("import sys; print('SSL'); sys.exit(2)", inconclusive_rc=2, inconclusive_match="SSL")]})
    res = acc.run_steps(steps, cwd=tmp_path, env={})
    assert res[0]["verdict"] == "INCONCLUSIVE" and "inconclusive_rc" in res[0]["reason"]


def test_accept_command_reports_match_in_state_and_dry_run(tmp_path: Path, capsys):
    steps = [_step("import sys; print('network is unreachable'); sys.exit(1)",
                   inconclusive_match="unreachable", name="net")]
    repo, td, cfg = _accept_setup(tmp_path, steps)
    rc, out = _accept(cfg, capsys, dry_run=True)
    assert rc == 0 and "вывод ~ /unreachable/ = INCONCLUSIVE" in out
    rc, out = _accept(cfg, capsys)
    assert rc == 2 and "ACCEPT-RESULT: verdict=INCONCLUSIVE" in out and "inconclusive_match" in out
    rec = duo.load_json(td / "state.json")["acceptance"]
    assert rec["verdict"] == "INCONCLUSIVE" and rec["steps"][0]["verdict"] == "INCONCLUSIVE"
    assert "network is unreachable" not in json.dumps(rec)                                       # вывод шага — не в state
