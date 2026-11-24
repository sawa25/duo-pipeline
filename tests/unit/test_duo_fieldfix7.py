"""fieldfix7: выход glm-5.4 — модели по ролям, effort, закрепление модели исполнителя (Н33).

Что случилось: профиль CLI переназначил алиасы (opus 5.3→5.4, sonnet flash→5.3, haiku 5.2→flash), а
пайплайн вызывал ВСЕ роли алиасом `opus`, solo — `sonnet`: все роли молча ушли на 5.4 (на её дефолтном
effort=max), solo лишился дешёвой модели. Теперь: `models` по ролям/персонам, `effort` по алиасу,
`model_expect` (сверка профиля стенда), закрепление модели исполнителя в state серии.
Паттерн test_duo_fieldfix5/6: песочницы в tmp_path, без LLM.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix4 import _SlowProc
from tests.unit.test_duo_fieldfix5 import _settings
from tests.unit.test_duo_pipemodes import PairTransport, _log_text, _pcfg, _state
from tests.unit.test_duo_pipev3 import REPO_ROOT, ScriptedTransport, _init_repo, _mk_task, duo
from tests.unit.test_duo_pipework2 import _set_state

ROLES_CFG = {
    "model": "fallback",
    "models": {"executor": "ex", "reviewer": "rv", "chairman": "ch", "beta": "bt"},
    "effort": {"rv": "high"},
}

# --- разрешение роли → алиас / effort ---------------------------------------------------------


def test_role_model_priority_pinned_persona_role_fallback():
    cfg = dict(ROLES_CFG)
    assert duo.role_model(cfg, "executor") == "ex"
    assert duo.role_model(cfg, "chairman") == "ch"
    assert duo.role_model(cfg, "reviewer", "alpha") == "rv"                 # персоны без записи — роль reviewer
    assert duo.role_model(cfg, "reviewer", "beta") == "bt"                  # персона-исключение
    assert duo.role_model(cfg, "chairman", "beta") == "ch"                  # персона важна только ревьюеру
    assert duo.role_model(cfg, "executor", pinned="pin") == "pin"           # закреплённый алиас главнее всего
    assert duo.role_model({"model": "fb"}, "reviewer", "alpha") == "fb"     # без models — запасной `model`
    assert duo.role_model({"model": "fb", "models": None}, "executor") == "fb"


def test_role_effort_is_keyed_by_alias_and_validated():
    cfg = {"effort": {"opus": "high", "bad": "turbo", "_comment": "x"}}
    assert duo.role_effort(cfg, "opus") == "high"
    assert duo.role_effort(cfg, "sonnet") == ""                             # алиас без записи — флаг не передаётся
    assert duo.role_effort(cfg, "bad") == ""                                # неизвестный уровень не доходит до CLI
    assert duo.role_effort({}, "opus") == "" and duo.role_effort({"effort": None}, "opus") == ""


# --- транспорт: argv ---------------------------------------------------------------------------


def _argv(tmp_path: Path, monkeypatch, cfg_extra: dict, mode: str = "plan", **call_kw) -> list[str]:
    captured: list[list[str]] = []
    events = [{"type": "result", "session_id": "s", "total_cost_usd": 0.0, "result": "ok"}]

    def fake_popen(cmd, **kw):
        captured.append(list(cmd))
        return _SlowProc(events, 0.0)

    monkeypatch.setattr(duo.subprocess, "Popen", fake_popen)
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m", **cfg_extra})
    glm.call("ПРОМПТ", mode=mode, timeout=30, **call_kw)
    return captured[0]


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_argv_model_and_effort_follow_role_and_persona(tmp_path: Path, monkeypatch):
    a = _argv(tmp_path, monkeypatch, ROLES_CFG, role="reviewer", persona="alpha")
    assert _flag(a, "--model") == "rv" and _flag(a, "--effort") == "high"
    b = _argv(tmp_path, monkeypatch, ROLES_CFG, role="reviewer", persona="beta")
    assert _flag(b, "--model") == "bt" and "--effort" not in b              # у «bt» effort не задан → без флага
    c = _argv(tmp_path, monkeypatch, ROLES_CFG, role="chairman")
    assert _flag(c, "--model") == "ch" and "--effort" not in c
    assert a[-1] == "ПРОМПТ"                                                # промпт — по-прежнему последний


def test_argv_explicit_model_wins_and_legacy_call_uses_cfg_model(tmp_path: Path, monkeypatch):
    a = _argv(tmp_path, monkeypatch, ROLES_CFG, role="executor", model="rv", mode="full")
    assert _flag(a, "--model") == "rv" and _flag(a, "--effort") == "high"   # effort следует за АЛИАСОМ
    legacy = _argv(tmp_path, monkeypatch, ROLES_CFG)                        # вызов без роли (старый код/тесты)
    assert _flag(legacy, "--model") == "fallback" and "--effort" not in legacy   # cfg["model"], как раньше


def test_effort_comes_after_model_and_deny_flag_still_followed_by_flag(tmp_path: Path, monkeypatch):
    argv = _argv(tmp_path, monkeypatch, {**ROLES_CFG, "executor_deny": ["Bash(adb:*)"]},
                 mode="full", role="executor", model="rv")
    i = argv.index("--disallowedTools")
    assert argv[i + 2] == "--model"                                         # вариадическая опция по-прежнему закрыта флагом
    assert argv.index("--effort") == argv.index("--model") + 2
    assert argv.index("--effort") < argv.index("--output-format")


# --- ожидаемые модели профиля ------------------------------------------------------------------


def test_model_drift_compares_profile_with_expectation(tmp_path: Path):
    cdir = tmp_path / "prof"
    _settings(cdir, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
                     "ANTHROPIC_AUTH_TOKEN": "sekret-token-123"})
    cfg = {"claude_config_dir": str(cdir),
           "model_expect": {"opus": "glm-5.4", "sonnet": "glm-5.3", "haiku": "glm-5.3-flash"}}
    out = duo.model_drift(cfg, ["opus", "sonnet", "haiku", "opus"])
    assert out == ["алиас opus → glm-5.3, ожидалось glm-5.4"]               # [1m] отсекается; haiku без маппинга — молчим
    assert "sekret" not in " ".join(out)
    assert duo.model_drift({**cfg, "model_expect": None}, ["opus"]) == []
    assert duo.model_drift({"claude_config_dir": str(tmp_path / "нет")}, ["opus"]) == []


def test_models_summary_by_mode(tmp_path: Path):
    cdir = tmp_path / "prof"
    _settings(cdir, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.4[1m]", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]"})
    cfg = {"claude_config_dir": str(cdir), "model": "sonnet",
           "models": {"executor": "sonnet", "reviewer": "opus", "chairman": "sonnet", "socrates2": "sonnet"},
           "effort": {"opus": "high"},
           "duo": {"members_plan": ["socrates2", "feynman2"], "members_impl": ["torvalds2", "meadows2"]},
           "pair": {"reviewer_plan": "feynman2", "reviewer_impl": "torvalds2"}}
    duo_lines = duo.models_summary(cfg, "duo")
    assert duo_lines[0] == "исполнитель: sonnet → glm-5.3[1m]"
    assert "ревьюер feynman2: opus → glm-5.4[1m], effort high" in duo_lines
    assert "ревьюер socrates2: sonnet → glm-5.3[1m]" in duo_lines
    assert duo_lines[-1] == "председатель: sonnet → glm-5.3[1m]"
    pair_lines = duo.models_summary(cfg, "pair")
    assert [ln.split(":")[0] for ln in pair_lines] == ["исполнитель", "ревьюер feynman2", "ревьюер torvalds2"]
    assert duo.models_summary(cfg, "solo", "haiku") == ["исполнитель: haiku"]   # solo: только исполнитель; пин главнее


# --- валидация конфига -------------------------------------------------------------------------


@pytest.mark.parametrize("over,needle", [
    ({"models": ["opus"]}, "models должен быть объектом"),
    ({"models": {"executor": ""}}, "models.executor: пустое"),
    ({"models": {"executor": 5}}, "models.executor: пустое/нестроковое"),
    ({"effort": {"opus": "turbo"}}, "effort.opus: уровень 'turbo' неизвестен"),
    ({"effort": "high"}, "effort должен быть объектом"),
    ({"model_expect": {"opus": None}}, "model_expect.opus"),
])
def test_guard_config_problems_models_effort(over, needle):
    assert any(needle in p for p in duo.guard_config_problems(over)), over


def test_guard_config_models_clean_and_comment_keys_allowed():
    ok = {"models": {"executor": "sonnet", "_comment": "текст"}, "effort": {"opus": "max", "_comment": "x"},
          "model_expect": {"opus": "glm-5.4", "_comment": "y"}}
    assert duo.guard_config_problems(ok) == []


# --- поставляемое назначение ролей (решение Н33 закреплено тестом) -----------------------------


def test_shipped_config_role_assignment(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = duo.load_config(REPO_ROOT / "config.json", repo=repo)
    assert duo.guard_config_problems(cfg) == []
    reviewers = set(cfg["duo"]["members_plan"] + cfg["duo"]["members_impl"])
    assert reviewers == {"socrates2", "feynman2", "torvalds2", "meadows2"}
    rm = lambda persona: duo.role_model(cfg, "reviewer", persona)           # noqa: E731
    # проверяющие код/план — на сильнейшей модели; диалектик и председатель — на средней (другая модель
    # даёт разнообразие и дешевле); исполнитель — на средней (одна сессия, откалибровано на ней)
    assert {rm("feynman2"), rm("torvalds2"), rm("meadows2")} == {"opus"}
    assert rm("socrates2") == "sonnet"
    assert duo.role_model(cfg, "chairman") == "sonnet" and duo.role_model(cfg, "executor") == "sonnet"
    assert cfg["solo"]["model"] == "haiku"                                  # solo — дешёвая (flash), не 5.3
    assert duo.role_effort(cfg, "opus") == "high"                           # 5.4 не на дефолтном max
    assert duo.role_effort(cfg, "sonnet") == "" and duo.role_effort(cfg, "haiku") == ""
    used = {rm(n) for n in reviewers} | {duo.role_model(cfg, r) for r in ("executor", "chairman")} | {"haiku"}
    assert used <= set(cfg["model_expect"]), "каждый используемый алиас должен иметь ожидание в model_expect"
    # 5.4 думает молча дольше 5.3: idle-лимит ревьюера не меньше прежней середины
    assert cfg["timeouts"]["reviewer_sec"] >= 900


def test_defaults_agree_with_shipped_config_on_roles():
    d = duo.DEFAULTS
    cfg = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    for key in ("models", "effort", "model_expect"):
        assert {k: v for k, v in d[key].items()} == {k: v for k, v in cfg[key].items() if not k.startswith("_")}, key
    assert d["model"] == cfg["model"] and d["timeouts"]["reviewer_sec"] == cfg["timeouts"]["reviewer_sec"]


# --- серия: что реально уходит в транспорт и что закрепляется ----------------------------------


class RecTransport(PairTransport):
    """PairTransport + журнал (role, persona, model, mode) каждого вызова."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kw_log: list[dict] = []

    def call(self, prompt, *, mode, **kw):
        self.kw_log.append({"mode": mode, "role": kw.get("role"), "persona": kw.get("persona"),
                            "model": kw.get("model"), "timeout": kw.get("timeout")})
        return super().call(prompt, mode=mode, **kw)


def _rcfg(tmp_path: Path, repo: Path, **over) -> dict:
    cfg = _pcfg(tmp_path, repo, **over)
    cfg.update({"model": "fallback", "models": dict(ROLES_CFG["models"]), "effort": dict(ROLES_CFG["effort"])})
    cfg["timeouts"] = {**cfg["timeouts"], "reviewer_sec": 77, "chairman_sec": 55}
    return cfg


def test_pair_series_routes_roles_pins_executor_and_logs_models(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _rcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    t = RecTransport(verdicts=["ACCEPT"])
    p.t = t
    p.run()
    st = _state(td)
    assert st["executor_model"] == "ex"                                     # закреплена при первом запуске
    ex_calls = [k for k in t.kw_log if k["mode"] == "full"]
    assert ex_calls and all(k["role"] == "executor" and k["model"] == "ex" for k in ex_calls)
    rev = [k for k in t.kw_log if k["mode"] == "plan"]
    assert len(rev) == 1 and rev[0]["role"] == "reviewer" and rev[0]["persona"] == "alpha"
    assert rev[0]["timeout"] == 77                                          # единственный проверяющий — reviewer_sec
    log = _log_text(p)
    assert "МОДЕЛИ: исполнитель: ex" in log and "ревьюер alpha: rv, effort high" in log


def test_duo_series_routes_each_member_and_chairman(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _rcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")                       # режим duo: R1–R3 участников + председатель
    t = RecTransport()
    p.t = t
    p.run()
    plan = [k for k in t.kw_log if k["mode"] == "plan"]
    members = [k for k in plan if k["role"] == "reviewer"]
    chair = [k for k in plan if k["role"] == "chairman"]
    assert {k["persona"] for k in members} == {"alpha", "beta"} and len(members) >= 4   # R1+R2 у обоих
    assert len(chair) == 1 and chair[0]["persona"] in (None, "") and chair[0]["timeout"] == 55
    assert all(k["timeout"] == 77 for k in members)                                     # reviewer_sec
    assert not [k for k in plan if k["role"] not in ("reviewer", "chairman")]           # безымянных вызовов нет
    assert "председатель: ch" in _log_text(p)


def test_pinned_executor_model_survives_config_change_and_rejects_conflicting_flag(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _rcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    st = p.load_state()
    p._pin_executor_model(st)
    assert _state(td)["executor_model"] == "ex"
    cfg["models"]["executor"] = "other"                                     # конфиг поменяли посреди серии
    q = duo.Pipeline(cfg, "demo")
    q._pin_executor_model(_state(td))
    assert _state(td)["executor_model"] == "ex"                             # серия остаётся на своей модели
    r = duo.Pipeline(cfg, "demo", executor_model="ex")                      # тот же алиас флагом — не конфликт
    r._pin_executor_model(_state(td))
    s = duo.Pipeline(cfg, "demo", executor_model="rv")
    with pytest.raises(SystemExit, match="закреплена: ex"):
        s._pin_executor_model(_state(td))


def test_executor_model_flag_pins_new_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_rcfg(tmp_path, repo), "demo", mode="pair", executor_model="rv")
    p._pin_executor_model(p.load_state())
    assert _state(td)["executor_model"] == "rv"


def test_legacy_series_with_session_keeps_old_alias(tmp_path: Path):
    # серия, начатая до закрепления: сессия есть, пина нет → остаётся на алиасе, на котором работала
    # (раньше все роли ходили через `opus`, solo — через solo.model), а не «переезжает» на models.executor
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="impl", executor_session="SESS-OLD")
    p = duo.Pipeline(_rcfg(tmp_path, repo), "demo", mode="pair")
    p._pin_executor_model(p.load_state())
    assert _state(td)["executor_model"] == "opus"
    assert "серия начата до закрепления модели" in _log_text(p)


def test_solo_executor_uses_solo_model_not_models_executor(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _rcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", solo=True)
    p._pin_executor_model(p.load_state())
    assert _state(td)["executor_model"] == "cheap"                          # solo.model, а не models.executor=ex


def test_rework_without_keep_session_releases_executor_model(tmp_path: Path):
    from tests.unit.test_duo_pipework2 import EXT_REVIEW, _cfg2, _rw_ns
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    ext = tmp_path / "e.md"
    ext.write_text(EXT_REVIEW, encoding="utf-8")
    _set_state(td, phase="impl_pending", plan_version=1, executor_session="S", executor_model="ex")
    duo.cmd_rework(_cfg2(tmp_path, repo), _rw_ns(phase="plan", review=ext))
    after = _state(td)
    assert "executor_session" not in after and "executor_model" not in after   # новая сессия — модель заново
    _set_state(td, phase="impl_pending", plan_version=1, executor_session="S2", executor_model="ex")
    duo.cmd_rework(_cfg2(tmp_path, repo), _rw_ns(phase="plan", review=ext, keep_session=True))
    kept = _state(td)
    assert kept["executor_session"] == "S2" and kept["executor_model"] == "ex"  # keep-session — модель остаётся


def test_drift_warning_in_run_log_names_alias_and_expectation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _rcfg(tmp_path, repo)
    cfg["model_expect"] = {"ex": "glm-5.3", "rv": "glm-5.4"}
    _settings(Path(cfg["claude_config_dir"]), {"ANTHROPIC_DEFAULT_EX_MODEL": "glm-5.3-flash",
                                               "ANTHROPIC_DEFAULT_RV_MODEL": "glm-5.4[1m]"})
    p = duo.Pipeline(cfg, "demo", mode="pair")
    p.log_models(p.load_state())
    log = _log_text(p)
    assert "алиас ex → glm-5.3-flash, ожидалось glm-5.3" in log             # исполнитель на «не той» модели — видно
    assert "алиас rv" not in log                                            # совпало (с отсечением [1m])


def test_status_shows_models_and_json_has_models_key(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, mode="pair", executor_model="ex")
    cfg = _rcfg(tmp_path, repo)
    duo.cmd_status(cfg, types.SimpleNamespace(task="demo", all=False, json=False))
    out = capsys.readouterr().out
    assert "модели: исполнитель: ex" in out and "ревьюер alpha: rv, effort high" in out
    duo.cmd_status(cfg, types.SimpleNamespace(task="demo", all=False, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["models"][0] == "исполнитель: ex" and any("ревьюер beta: bt" in ln for ln in data["models"])


class RecScripted(ScriptedTransport):
    """ScriptedTransport (председатель по умолчанию — REVISE) + журнал role/persona/model."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kw_log: list[dict] = []

    def call(self, prompt, *, mode, **kw):
        self.kw_log.append({"mode": mode, "role": kw.get("role"), "persona": kw.get("persona"),
                            "model": kw.get("model"), "prompt": prompt})
        return super().call(prompt, mode=mode, **kw)


def test_escalation_call_goes_to_chairman_model(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n\nЕсть измерения: метрика X.\n", encoding="utf-8")
    cfg = _rcfg(tmp_path, repo)
    cfg["limits"] = {"max_plan_iterations": 9, "max_impl_iterations": 4, "budget_usd": 100.0,
                     "escalation_after": 2}
    p = duo.Pipeline(cfg, "demo")
    t = RecScripted()
    p.t = t
    st = p.load_state()
    st.update(plan_version=1, plan_iteration=0)
    duo.save_json(p.state_file, st)
    p.run(force_continue=True)
    assert p.load_state()["phase"] == "needs_human" and (td / "escalation.md").exists()
    esc = [k for k in t.kw_log if "ключевое разногласие" in k["prompt"] and k["mode"] == "plan"]
    assert esc, "вызов эскалации не найден"
    assert esc[-1]["role"] == "chairman" and not esc[-1]["persona"]


def test_selftest_pings_every_distinct_role_model_and_reports_drift(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    cfg = _rcfg(tmp_path, repo)
    cfg["model_expect"] = {"rv": "glm-5.4"}
    _settings(Path(cfg["claude_config_dir"]), {"ANTHROPIC_DEFAULT_RV_MODEL": "glm-5.3"})
    pinged: list[str] = []

    class FakeT:
        def __init__(self, cfg):
            pass

        def call(self, prompt, *, mode, model="", **kw):
            pinged.append(model)
            return "PONG", "sess1234", 0.0

    monkeypatch.setattr(duo, "ClaudeGlm", FakeT)
    duo.cmd_selftest(cfg, types.SimpleNamespace())
    out = capsys.readouterr().out
    assert sorted(pinged) == ["bt", "ch", "ex", "rv"]                       # каждый алиас ролей — ровно один раз
    assert "SELFTEST OK" in out and "модели по ролям (режим duo)" in out
    assert "ВНИМАНИЕ: профиль моделей отличается от ожидаемого — алиас rv → glm-5.3, ожидалось glm-5.4" in out
