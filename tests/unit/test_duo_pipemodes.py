"""pipemodes (29.10.2026): режим «исполнитель + один проверяющий» (pair) и параллельные серии.

Что фиксируют тесты:
  * режим серии (duo | pair | solo) и изоляция (shared | worktree): приоритет «явный аргумент > пин в
    state > умолчание/существующий worktree/config», пин при первом запуске, отказ при смене изоляции;
  * pair: один проверяющий на стадии (план — alpha, код — beta), 1 вызов модели вместо 7, ретрай
    дегенеративного ответа, фолбэк REVISE, промпты duo при этом не получают pair-текстов;
  * промпты исполнителя: shared остаётся прежним (нет блоков worktree/pair), worktree просит
    `## files_touch`, pair меняет «шапку»;
  * параллельные серии: repo_lock, guard двух shared-серий, зоны записи (parse_files_touch,
    patterns_overlap, zone_conflicts), claim-gate между worktree-сериями (заявленная и фактическая
    зоны, --ignore-claims, shared_ok, отпускание после слияния и claims --drop), sync базы,
    выход за зону;
  * команда merge (dry-run, конфликты, грязный worktree, не-done, успех с очисткой worktree),
    claims, CLI-флаги, колонка режима в статусе.
Паттерн test_duo_pipework2/3: git-песочницы в tmp_path, скриптованный транспорт, без LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

import pytest

from tests.unit.test_duo_pipev3 import (
    CHAIR_REPORT,
    ScriptedTransport,
    _cfg,
    _git,
    _git_out,
    _head,
    _init_repo,
    _mk_task,
    _review,
    duo,
)
from tests.unit.test_duo_pipework2 import AcceptingTransport, _set_state

SINGLE_MARK = "ЕДИНСТВЕННЫЙ проверяющий"
PAIR_RULES_MARK = "Режим одного проверяющего"


def _pcfg(tmp_path: Path, repo: Path, **over) -> dict:
    cfg = _cfg(tmp_path, repo, **over)
    cfg["pair"] = {"reviewer_plan": "alpha", "reviewer_impl": "beta"}
    cfg["solo"] = {"model": "cheap"}
    cfg["auto_proceed_to_impl"] = False
    return cfg


class PairTransport(AcceptingTransport):
    """Исполнитель «пишет» артефакты; единственный проверяющий (по маркеру в промпте) отвечает
    валидным rev-отчётом с вердиктом из очереди verdicts (по умолчанию ACCEPT)."""

    def __init__(self, verdicts=(), **kw):
        super().__init__(**kw)
        self.verdicts = list(verdicts)
        self.reviewer_prompts: list[str] = []

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        if SINGLE_MARK in prompt:
            self.reviewer_prompts.append(prompt)
            if not self.responses:
                v = self.verdicts.pop(0) if self.verdicts else "ACCEPT"
                self.responses = [CHAIR_REPORT.replace("VERDICT: REVISE", f"VERDICT: {v}")]
        return super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                            resume_session=resume_session, tag=tag)


def _state(td: Path) -> dict:
    return duo.load_json(td / "state.json")


def _log_text(p: duo.Pipeline) -> str:
    f = p.task_dir / "orchestrator.log"
    return f.read_text(encoding="utf-8") if f.exists() else ""


# ---------------------------------------------------------------------------
# Разрешение режима и изоляции
# ---------------------------------------------------------------------------


def test_mode_defaults_and_members_by_mode(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    assert (p.mode, p.solo, p.isolation) == ("duo", False, "shared")
    assert p._members("plan") == ["alpha", "beta"] and p._members("impl") == ["alpha", "beta"]
    q = duo.Pipeline(cfg, "demo", mode="pair")
    assert q.mode == "pair" and not q.solo
    assert q._members("plan") == ["alpha"] and q._members("impl") == ["beta"]


def test_mode_pin_from_state_and_explicit_override(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    _set_state(td, mode="pair")
    assert duo.Pipeline(cfg, "demo").mode == "pair"                  # без флагов — режим серии
    assert duo.Pipeline(cfg, "demo", mode="duo").mode == "duo"       # явный аргумент главнее пина
    _set_state(td, mode="solo")
    assert duo.Pipeline(cfg, "demo").mode == "duo"                   # solo молча не подхватывается


def test_solo_mode_cheap_model_and_no_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", solo=True)
    assert p.mode == "solo" and p.solo
    assert cfg["model"] == "cheap" and cfg["git"]["auto_branch"] is False


def test_unknown_mode_and_isolation_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    with pytest.raises(SystemExit, match="неизвестный режим"):
        duo.Pipeline(cfg, "demo", mode="trio")
    with pytest.raises(SystemExit, match="неизвестная изоляция"):
        duo.Pipeline(cfg, "demo", isolation="chroot")


def test_isolation_priority_config_pin_explicit_and_change_refused(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    assert duo.Pipeline(cfg, "demo").isolation == "shared"
    cfg["git"]["isolation"] = "worktree"
    assert duo.Pipeline(cfg, "demo").isolation == "worktree"          # из config
    _set_state(td, isolation="shared")
    assert duo.Pipeline(cfg, "demo").isolation == "shared"            # пин сильнее config
    assert duo.Pipeline(cfg, "demo", isolation="shared").isolation == "shared"
    with pytest.raises(SystemExit, match="сменить"):                  # явная смена пина запрещена
        duo.Pipeline(cfg, "demo", isolation="worktree")


def test_legacy_worktree_series_without_pin_survives_forgotten_config(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg_wt = _pcfg(tmp_path, repo)
    cfg_wt["git"]["isolation"] = "worktree"
    p = duo.Pipeline(cfg_wt, "demo")
    p._ensure_worktree(p.load_state())                                # серия создана «по-старому»
    cfg_plain = _pcfg(tmp_path, repo)                                 # --config забыт: isolation=shared
    q = duo.Pipeline(cfg_plain, "demo")
    assert q.isolation == "worktree" and q.work_path == p.work_path   # не молча в общее дерево


@pytest.mark.parametrize("st,force,resume,auto,expected", [
    ({"phase": "plan"}, False, False, False, True),
    ({"phase": "impl"}, False, False, False, True),
    ({"phase": "done"}, True, True, True, False),
    ({"phase": "impl_pending"}, False, False, False, False),
    ({"phase": "impl_pending"}, True, False, False, True),
    ({"phase": "impl_pending"}, False, False, True, True),
    ({"phase": "needs_human"}, True, True, False, False),
    ({"phase": "needs_human", "halt": {"resumable": True}}, False, True, False, True),
    ({"phase": "needs_human", "halt": {"resumable": False}}, True, True, False, False),
])
def test_will_work_matrix(tmp_path: Path, st, force, resume, auto, expected):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    cfg["auto_proceed_to_impl"] = auto
    assert duo.Pipeline(cfg, "demo")._will_work(st, force, resume) is expected


def test_solo_flag_refused_for_series_past_plan(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    _set_state(td, phase="impl", plan_version=1)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", solo=True)
    with pytest.raises(SystemExit, match="solo"):
        p._pin_mode_isolation(p.load_state())


# ---------------------------------------------------------------------------
# pair: сквозной прогон и промпты
# ---------------------------------------------------------------------------


def test_pair_plan_then_impl_one_reviewer_per_stage(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    t = PairTransport(verdicts=["REVISE", "ACCEPT"])
    p.t = t
    p.run()
    st = _state(td)
    assert st["phase"] == "impl_pending"
    assert (st["mode"], st["isolation"]) == ("pair", "shared")        # пин записан при первом запуске
    assert len(t.reviewer_prompts) == 2                               # по одному вызову на итерацию плана
    assert all("Персона alpha" in c for c in t.reviewer_prompts)      # персона плана
    assert not [c for c in t.calls if "Председатель консилиума" in c]  # ни дуэта, ни председателя
    assert sorted(f.name for f in td.glob("rev*_plan.md")) == ["rev001_plan.md", "rev002_plan.md"]
    assert (td / "rev002_plan.md").read_text(encoding="utf-8").rstrip().endswith("VERDICT: ACCEPT")
    assert [x["mode"] for x in st["review_telemetry"]] == ["pair", "pair"]
    assert st["review_telemetry"][-1]["reviewer"] == "alpha"
    assert "исполнитель ↔ проверяющий" in t.calls[0]                  # «шапка» исполнителя в pair

    # код: без флагов — режим берётся из state; проверяет другая персона
    q = duo.Pipeline(cfg, "demo")
    assert q.mode == "pair"
    t2 = PairTransport(verdicts=["ACCEPT"])
    q.t = t2
    q.run(force_continue=True)
    st2 = _state(td)
    assert st2["phase"] == "done"
    assert len(t2.reviewer_prompts) == 1
    assert "Персона beta" in t2.reviewer_prompts[0] and "Персона alpha" not in t2.reviewer_prompts[0]
    assert not [c for c in t2.calls if "Председатель консилиума" in c]


def _single_review(tmp_path: Path, repo: Path, t: PairTransport, phase: str = "plan"):
    cfg = _pcfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir(exist_ok=True)
    art = repo / "pipeline" / "demo" / "plan_v1.md"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("# план\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    return reviewer.review(
        artifact_rel="pipeline/demo/plan_v1.md", phase=phase, task_text="задача",
        members=["alpha"], rounds=3, rev_file=td / "rev001_plan.md",
        guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo",
    ), td


def test_pair_reviewer_retries_degenerate_answer(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = PairTransport(responses=["ok"])                               # первая попытка — заглушка
    (verdict, _cost, tel), td = _single_review(tmp_path, repo, t)
    assert verdict == "ACCEPT" and tel["mode"] == "pair"
    assert len(t.reviewer_prompts) == 2
    assert "неполный/невалидный" in t.reviewer_prompts[1]            # ретрай с уточнением формата
    assert (td / "rev001_plan.md").read_text(encoding="utf-8").rstrip().endswith("VERDICT: ACCEPT")


def test_pair_reviewer_without_verdict_line_falls_back_to_revise(tmp_path: Path):
    repo = _init_repo(tmp_path)
    no_verdict = CHAIR_REPORT.replace("VERDICT: REVISE", "")
    t = PairTransport(responses=[no_verdict] * duo.CHAIRMAN_MAX_ATTEMPTS)
    (verdict, _cost, _tel), td = _single_review(tmp_path, repo, t)
    assert verdict == "REVISE"                                        # фолбэк: сомнение = доработка
    assert (td / "rev001_plan.md").exists()


def test_pair_prompt_has_rules_and_duo_prompts_do_not(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = PairTransport()
    _single_review(tmp_path, repo, t)
    p = t.reviewer_prompts[0]
    assert PAIR_RULES_MARK in p and "Чек-лист пре-регистрации плана" in p
    # дуэт (alpha+beta) того же артефакта: ни pair-правил, ни маркера единственного проверяющего
    cfg = _pcfg(tmp_path, repo)
    t2 = ScriptedTransport()
    _review(tmp_path, repo, cfg, t2)
    assert t2.calls and not any(PAIR_RULES_MARK in c or SINGLE_MARK in c for c in t2.calls)


def _executor(tmp_path: Path, repo: Path, **state) -> tuple[duo.Executor, ScriptedTransport]:
    td = _mk_task(repo)
    st = {"task": "demo", "artifacts_rel": "pipeline/demo", **state}
    t = ScriptedTransport()
    return duo.Executor(_pcfg(tmp_path, repo), t, td, repo, st), t


def test_executor_prompts_shared_have_no_new_blocks(tmp_path: Path):
    repo = _init_repo(tmp_path)
    ex, t = _executor(tmp_path, repo)                                 # shared + duo (по умолчанию)
    ex.make_plan("задача", 1)
    for needle in ("files_touch", "Изоляция:", "проверяющий"):
        assert needle not in t.calls[0], needle


def test_executor_prompts_worktree_ask_files_touch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    ex, t = _executor(tmp_path, repo, isolation="worktree")
    ex.make_plan("задача", 1)
    assert "## files_touch" in t.calls[0] and "отдельном git-worktree" in t.calls[0]
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    ex.implement("задача", "pipeline/demo/plan_v1.md", 1)
    assert "files_touch" in t.calls[-1]                               # границы зоны — в промпте имплементации


def test_executor_prompt_pair_head(tmp_path: Path):
    repo = _init_repo(tmp_path)
    ex, t = _executor(tmp_path, repo, mode="pair")
    ex.make_plan("задача", 1)
    assert "исполнитель ↔ проверяющий" in t.calls[0]
    assert "files_touch" not in t.calls[0]                            # pair сам по себе зон не требует


# ---------------------------------------------------------------------------
# Зоны записи: чистые функции
# ---------------------------------------------------------------------------

PLAN_TXT = (
    "# план\n\n## Файлы\n- `ignored_before.py`\n\n"
    "## files_touch (зона записи)\n\n"
    "- `api/a.py` — правка\n"
    "- `docs/` (каталог)\n"
    "* tests/unit/test_x.py\n"
    "1. `web/*.tsx`\n"
    "- не путь: пояснение\n"
    "| `tbl/file.py` | правка |\n"
    "|---|---|\n"
    "- `/etc/passwd`\n"
    "- `../escape.py`\n"
    "- `api/a.py`\n"
    "\n## Риски\n- `after.py`\n"
)


def test_parse_files_touch_variants():
    assert duo.parse_files_touch(PLAN_TXT) == [
        "api/a.py", "docs/*", "tests/unit/test_x.py", "web/*.tsx", "tbl/file.py"]
    assert duo.parse_files_touch("# план без секции\n") is None
    assert duo.parse_files_touch("## files_touch\n\n## Дальше\n- `x.py`\n") == []   # заявлено «ничего»
    assert duo.parse_files_touch("## files_touch\n```\nfoo/bar.py\nbaz.py\n```\n") == ["foo/bar.py", "baz.py"]


@pytest.mark.parametrize("a,b,expected", [
    ("api/a.py", "api/a.py", True),
    ("api/*.py", "api/a.py", True),
    ("api/*.py", "web/a.py", False),
    ("docs/*", "docs/x/y.md", True),
    ("api", "api/a.py", True),             # каталог без «/» против файла внутри
    ("api/*.py", "api/*.js", True),        # глоб против глоба — консервативно пересекаются
    ("a.py", "b.py", False),
])
def test_patterns_overlap(a, b, expected):
    assert duo.patterns_overlap(a, b) is expected
    assert duo.patterns_overlap(b, a) is expected                     # симметрия


def test_path_in_zone_and_zone_conflicts_with_shared_ok():
    assert duo.path_in_zone("api/sub/x.py", ["api/*"]) and not duo.path_in_zone("web/x.py", ["api/*"])
    assert duo.zone_conflicts(["README.md", "api/a.py"], ["README.md", "web/b.js"]) == [("README.md", "README.md")]
    assert duo.zone_conflicts(["README.md", "api/a.py"], ["README.md", "web/b.js"], ["README.md"]) == []


def test_repo_lock_exclusive_also_from_linked_worktree(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", isolation="worktree")
    p._ensure_worktree(p.load_state())
    entered = threading.Event()

    def other():
        with duo.repo_lock(p.work_path):                              # тот же lock-файл через worktree
            entered.set()

    with duo.repo_lock(repo):
        th = threading.Thread(target=other, daemon=True)
        th.start()
        assert not entered.wait(0.4)                                  # держим — второй ждёт
    assert entered.wait(5)                                            # отпустили — второй вошёл
    th.join(5)


# ---------------------------------------------------------------------------
# Guard двух shared-серий
# ---------------------------------------------------------------------------


def _live_series(tmp_path: Path, repo: Path, cfg: dict, task: str, **state) -> duo.Pipeline:
    td = _mk_task(repo, task)
    _set_state(td, phase="impl", **state)
    p = duo.Pipeline(cfg, task)
    p.acquire_lock()                                                  # «серия работает»
    return p


def test_shared_parallel_guard_blocks_second_shared_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _live_series(tmp_path, repo, cfg, "a", mode="duo", isolation="shared")
    try:
        _mk_task(repo, "b")
        b = duo.Pipeline(cfg, "b")
        with pytest.raises(SystemExit, match="ОТКАЗ.*a \\(duo"):
            b._shared_parallel_guard(False)
        b._shared_parallel_guard(True)                                # --allow-shared-parallel
        assert "allow-shared-parallel" in _log_text(b)
        # отказ run() не закрепляет isolation=shared: подсказанный `--worktree` остаётся доступным
        with pytest.raises(SystemExit):
            b.run()
        assert "isolation" not in _state(b.task_dir)
        assert duo.Pipeline(cfg, "b", isolation="worktree").isolation == "worktree"
    finally:
        a.release_lock()


def test_shared_parallel_guard_allows_solo_pair_worktree_and_dead_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _live_series(tmp_path, repo, cfg, "a", mode="solo", isolation="shared")
    try:
        _mk_task(repo, "b")
        duo.Pipeline(cfg, "b", solo=True)._shared_parallel_guard(False)   # solo + solo — можно
        with pytest.raises(SystemExit):
            duo.Pipeline(cfg, "b", mode="duo")._shared_parallel_guard(False)   # duo рядом с solo — нет
        duo.Pipeline(cfg, "b", isolation="worktree")._shared_parallel_guard(False)  # своя папка — можно
    finally:
        a.release_lock()
    duo.Pipeline(cfg, "b")._shared_parallel_guard(False)              # серия мертва (lock свободен) — можно


# ---------------------------------------------------------------------------
# claim-gate между worktree-сериями
# ---------------------------------------------------------------------------


def _wt_series(tmp_path: Path, repo: Path, task: str, cfg: dict, plan: str | None = None) -> duo.Pipeline:
    _mk_task(repo, task)
    p = duo.Pipeline(cfg, task, isolation="worktree")
    st = p.load_state()
    p._pin_mode_isolation(st)
    p._ensure_worktree(st)
    if plan is not None:
        (p.task_dir / "plan_v1.md").write_text(plan, encoding="utf-8")
        st["plan_version"] = 1
        p.save_state(st)
    return p


def _gate(p: duo.Pipeline, ignore: bool = False) -> bool:
    return p._claim_gate(p.load_state(), f"pipeline/{p.task}", ignore)


def _zone(*paths: str) -> str:
    return "# план\n\n## files_touch\n\n" + "".join(f"- `{x}`\n" for x in paths)


def test_claim_gate_blocks_overlap_allows_disjoint_and_ignore(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    b = _wt_series(tmp_path, repo, "b", cfg, _zone("api/server.py"))
    c = _wt_series(tmp_path, repo, "c", cfg, _zone("web/app.js"))
    assert _gate(a) is True
    assert _state(a.task_dir)["claim"]["paths"] == ["api/*.py"]
    assert _gate(b) is False                                          # api/server.py ∈ api/*.py
    assert "claim" not in _state(b.task_dir) and _state(b.task_dir)["phase"] == "plan"
    log = _log_text(b)
    assert "БЛОК" in log and "a (phase=" in log and "api/server.py ⇄ api/*.py" in log
    assert _gate(c) is True                                           # непересекающаяся зона — пускаем
    assert _gate(b, ignore=True) is True                              # --ignore-claims: на свой риск
    assert _state(b.task_dir)["claim"]["ignored_conflicts"] == ["a"]
    assert _gate(a) is True                                           # держатель повторно — без пересчёта


def test_claim_gate_uses_actual_changes_of_holder(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("x.py"))
    b = _wt_series(tmp_path, repo, "b", cfg, _zone("core/*"))
    assert _gate(a) is True
    (a.work_path / "core").mkdir()
    (a.work_path / "core" / "engine.py").write_text("v", encoding="utf-8")   # вне заявленной зоны a
    assert _gate(b) is False                                          # держит заявленное ∪ фактически изменённое
    assert "core/engine.py" in _log_text(b)


def test_claim_gate_without_files_touch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, "# план без зоны\n")
    assert _gate(a) is True                                           # держателей нет — пускаем, зона не заявлена
    assert _state(a.task_dir)["claim"]["declared"] is False
    b = _wt_series(tmp_path, repo, "b", cfg, "# тоже без зоны\n")
    assert _gate(b) is False                                          # держатель есть, а проверить нечем
    assert "files_touch" in _log_text(b)
    assert _gate(b, ignore=True) is True


def test_claim_gate_shared_ok_and_shared_isolation_skip(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    cfg["parallel"] = {"shared_ok": ["README.md"], "sync_base_at_impl": True}
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("README.md", "api/a.py"))
    b = _wt_series(tmp_path, repo, "b", cfg, _zone("README.md", "web/b.js"))
    assert _gate(a) is True and _gate(b) is True                      # общий «горячий» файл — не конфликт
    _mk_task(repo, "s")
    s = duo.Pipeline(cfg, "s")                                        # shared-серия зон не занимает
    assert s._claim_gate(s.load_state(), "pipeline/s", False) is True
    assert "claim" not in _state(s.task_dir)


def test_claim_released_after_merge_and_by_drop(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    b = _wt_series(tmp_path, repo, "b", cfg, _zone("api/b.py"))
    assert _gate(a) is True
    (a.work_path / "api").mkdir()
    (a.work_path / "api" / "x.py").write_text("x", encoding="utf-8")
    _git(a.work_path, "add", "api/x.py")
    _git(a.work_path, "commit", "-qm", "feat: a")
    assert _gate(b) is False                                          # коммит в ветке a, но не в HEAD — держит
    duo.cmd_claims(cfg, argparse.Namespace(task=None, drop=False))
    out = capsys.readouterr().out
    assert "a (phase=" in out and "api/*.py" in out and "api/x.py" in out
    _git(repo, "merge", "--no-ff", "-qm", "merge a", "duo/a")         # человек слил руками
    assert _gate(b) is True                                           # зона a отпущена слиянием

    c = _wt_series(tmp_path, repo, "c", cfg, _zone("web/*"))
    d = _wt_series(tmp_path, repo, "d", cfg, _zone("web/d.js"))
    assert _gate(c) is True and _gate(d) is False
    duo.cmd_claims(cfg, argparse.Namespace(task="c", drop=True))      # брошенная серия — отпустить зону
    assert _state(c.task_dir)["claim"]["released"] is True
    assert _gate(d) is True
    duo.cmd_claims(cfg, argparse.Namespace(task="c", drop=True))
    assert "и так не занята" in capsys.readouterr().out


def test_claims_drop_refused_for_live_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    assert _gate(a) is True
    a.acquire_lock()
    try:
        with pytest.raises(SystemExit, match="запущена"):
            duo.cmd_claims(cfg, argparse.Namespace(task="a", drop=True))
    finally:
        a.release_lock()
    with pytest.raises(SystemExit, match="--task"):
        duo.cmd_claims(cfg, argparse.Namespace(task=None, drop=True))


def test_zone_outside_and_plan_zone_ignores_artifacts(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py", "pipeline/a/notes.md"))
    zone, src = a._zone_from_plan(a.load_state())
    assert zone == ["api/*.py"] and src == "plan_v1.md"               # свои артефакты из зоны убраны
    assert _gate(a) is True
    (a.work_path / "api").mkdir()
    (a.work_path / "api" / "ok.py").write_text("1", encoding="utf-8")
    (a.work_path / "README.md").write_text("правка вне зоны", encoding="utf-8")
    assert a._zone_outside(a.load_state(), "pipeline/a") == ["README.md"]


class WritingTransport(PairTransport):
    """+ исполнитель в ФАЗЕ ИМПЛЕМЕНТАЦИИ создаёт файлы impl_writes (относительно своего cwd)."""

    def __init__(self, *a, impl_writes=(), **kw):
        super().__init__(*a, **kw)
        self.impl_writes = list(impl_writes)

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        out = super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                           resume_session=resume_session, tag=tag)
        if mode == "full" and cwd and "ФАЗА: ИМПЛЕМЕНТАЦИЯ" in prompt:
            for rel in self.impl_writes:
                f = Path(cwd) / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("код", encoding="utf-8")
        return out


def test_two_worktree_series_run_wait_for_holder_then_merge_and_sync(tmp_path: Path):
    """Сквозной прогон через настоящий run(): две pair-серии в своих worktree с пересекающимися
    зонами. a занимает зону и доходит до done; b после приёмки плана ЖДЁТ в impl_pending (исполнитель
    не вызывался); после merge a — b стартует, база подтянута к HEAD (видит файлы a)."""
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    tds = {}
    for name, zone in (("a", _zone("api/*.py")), ("b", _zone("api/b.py"))):
        td = _mk_task(repo, name)
        (td / "plan_v1.md").write_text(zone, encoding="utf-8")        # план уже есть: сразу ревью плана
        _set_state(td, plan_version=1, plan_iteration=0)
        p = duo.Pipeline(cfg, name, mode="pair", isolation="worktree")
        p.t = WritingTransport()
        p.run()
        assert _state(td)["phase"] == "impl_pending"
        assert (_state(td)["mode"], _state(td)["isolation"]) == ("pair", "worktree")
        assert "ЗОНА: plan_v1.md заявляет" in _log_text(p)            # зона объявлена при приёмке плана
        tds[name] = td

    # a: зона свободна → имплементация идёт; README.md — вне заявленной зоны
    pa = duo.Pipeline(cfg, "a")
    ta = WritingTransport(impl_writes=["api/a1.py", "README.md"])
    pa.t = ta
    pa.run(force_continue=True)
    sa = _state(tds["a"])
    assert sa["phase"] == "done" and sa["claim"]["paths"] == ["api/*.py"]
    assert pa.work_path != repo and (pa.work_path / "api" / "a1.py").exists()
    assert not (repo / "api").exists()                                # основное дерево серия не трогала
    assert any("вне зоны записи" in c and "README.md" in c for c in ta.reviewer_prompts[-1:])
    assert "ЗОНА: изменены файлы вне заявленной" in _log_text(pa)

    # b: зона пересекается с зоной a (a ещё не слита) → ждёт, исполнитель не вызывался
    pb = duo.Pipeline(cfg, "b")
    tb = WritingTransport(impl_writes=["api/b.py"])
    pb.t = tb
    pb.run(force_continue=True)
    sb = _state(tds["b"])
    assert sb["phase"] == "impl_pending" and "claim" not in sb
    assert "БЛОК" in _log_text(pb) and "a (phase=done)" in _log_text(pb)
    assert not any("ФАЗА: ИМПЛЕМЕНТАЦИЯ" in c for c in tb.calls)

    # человек коммитит и вливает a → зона отпущена
    _git(pa.work_path, "add", "api/a1.py", "README.md")
    _git(pa.work_path, "commit", "-qm", "feat: a")
    assert duo.cmd_merge(cfg, _merge_ns(task="a")) == 0

    # b теперь стартует; база worktree подтянута к HEAD основного дерева (в нём уже слитый a)
    pb2 = duo.Pipeline(cfg, "b")
    tb2 = WritingTransport(impl_writes=["api/b.py"])
    pb2.t = tb2
    pb2.run(force_continue=True)
    sb2 = _state(tds["b"])
    assert sb2["phase"] == "done" and sb2["claim"]["paths"] == ["api/b.py"]
    assert sb2["worktree_base"] == _head(repo)
    assert (pb2.work_path / "api" / "a1.py").exists()                 # результат a виден в базе b
    assert any("ФАЗА: ИМПЛЕМЕНТАЦИЯ" in c for c in tb2.calls)


# ---------------------------------------------------------------------------
# sync базы
# ---------------------------------------------------------------------------


def _human_commit(repo: Path, name: str = "main.txt") -> str:
    (repo / name).write_text("правка человека", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"human {name}")
    return _head(repo)


def test_sync_base_fast_forwards_clean_worktree(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    base = _state(a.task_dir)["worktree_base"]
    new_head = _human_commit(repo)
    st = a.load_state()
    a._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == new_head != base
    assert (a.work_path / "main.txt").exists() and _head(a.work_path) == new_head
    assert "БАЗА: worktree подтянут" in _log_text(a)


@pytest.mark.parametrize("scenario", ["dirty", "own_commit", "impl_started", "disabled"])
def test_sync_base_skips_when_unsafe(tmp_path: Path, scenario: str):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    if scenario == "disabled":
        cfg["parallel"] = {"shared_ok": [], "sync_base_at_impl": False}
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    st = a.load_state()
    base = st["worktree_base"]
    if scenario == "dirty":
        (a.work_path / "README.md").write_text("правка серии", encoding="utf-8")
    elif scenario == "own_commit":
        (a.work_path / "own.txt").write_text("x", encoding="utf-8")
        _git(a.work_path, "add", "own.txt")
        _git(a.work_path, "commit", "-qm", "own")
    elif scenario == "impl_started":
        st["impl_base_head"] = base
    _human_commit(repo)
    a._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == base                                # база не сдвинута
    assert not (a.work_path / "main.txt").exists()


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def _done_series(tmp_path: Path, repo: Path, cfg: dict, task: str = "a", commit: bool = True,
                 fname: str = "feature.txt") -> duo.Pipeline:
    p = _wt_series(tmp_path, repo, task, cfg, _zone(fname))
    assert _gate(p) is True
    st = p.load_state()
    st["phase"] = "done"
    p.save_state(st)
    if commit:
        (p.work_path / fname).write_text("фича", encoding="utf-8")
        _git(p.work_path, "add", fname)
        _git(p.work_path, "commit", "-qm", f"feat: {task}")
    return p


def _merge_ns(task="a", dry_run=False, yes=True, keep=False, force=False) -> argparse.Namespace:
    return argparse.Namespace(task=task, dry_run=dry_run, yes=yes, keep_worktree=keep, force=force)


def test_merge_success_releases_zone_and_removes_worktree(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)
    _human_commit(repo)                                               # история разошлась, конфликтов нет
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 0
    assert "конфликтов нет" in capsys.readouterr().out
    assert not (repo / "feature.txt").exists()                        # dry-run дерево не трогал
    assert duo.cmd_merge(cfg, _merge_ns()) == 0
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "фича"
    assert len(_git_out(repo, "rev-list", "--parents", "-1", "HEAD").split()) == 3   # merge-коммит (--no-ff)
    assert not a.work_path.exists()                                   # worktree убран
    assert _git_out(repo, "rev-parse", "--verify", "duo/a").strip()   # ветка сохранена
    st = _state(a.task_dir)
    assert st["claim"]["released"] is True and st["merged"]["new_commits"] is True
    assert "MERGE:" in _log_text(a)
    assert duo.collect_claim_holders(cfg, repo) == []                 # зона отпущена


def test_merge_keep_worktree_and_nothing_to_merge(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg, commit=False)               # коммитов в ветке нет
    assert duo.cmd_merge(cfg, _merge_ns(keep=True)) == 0
    assert "нечего сливать" in capsys.readouterr().out
    assert a.work_path.exists() and _state(a.task_dir)["claim"]["released"] is True
    b = _done_series(tmp_path, repo, cfg, task="b")
    assert duo.cmd_merge(cfg, _merge_ns(task="b", keep=True)) == 0
    assert b.work_path.exists() and (repo / "feature.txt").exists()
    # main не уходил вперёд (fast-forward возможен), но слияние всё равно --no-ff: серия видна в истории
    assert len(_git_out(repo, "rev-list", "--parents", "-1", "HEAD").split()) == 3


def test_merge_refusals_phase_dirty_conflicts_and_cancel(tmp_path: Path, capsys, monkeypatch):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)
    # 1. не done
    st = a.load_state()
    st["phase"] = "impl"
    a.save_state(st)
    assert duo.cmd_merge(cfg, _merge_ns()) == 1
    assert "phase=impl" in capsys.readouterr().out
    st["phase"] = "done"
    a.save_state(st)
    # 2. грязный worktree
    (a.work_path / "dirty.txt").write_text("x", encoding="utf-8")
    assert duo.cmd_merge(cfg, _merge_ns()) == 1
    assert "незакоммиченные" in capsys.readouterr().out
    (a.work_path / "dirty.txt").unlink()
    # 3. конфликт: обе стороны меняют README.md по-разному
    (a.work_path / "README.md").write_text("версия серии", encoding="utf-8")
    _git(a.work_path, "add", "README.md")
    _git(a.work_path, "commit", "-qm", "series readme")
    (repo / "README.md").write_text("версия человека", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "human readme")
    assert duo.cmd_merge(cfg, _merge_ns()) == 1
    out = capsys.readouterr().out
    assert "КОНФЛИКТЫ" in out and "README.md" in out and "git -C" in out
    assert not (repo / "feature.txt").exists()                        # дерево человека не тронуто
    assert _state(a.task_dir)["claim"].get("released") is None        # зона не отпущена
    # 4. отмена на подтверждении: ничего не слито, worktree цел
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    b = _done_series(tmp_path, repo, cfg, task="b", fname="b.txt")
    assert duo.cmd_merge(cfg, _merge_ns(task="b", yes=False)) == 1
    assert "отменено" in capsys.readouterr().out
    assert b.work_path.exists() and not (repo / "b.txt").exists()
    assert "duo/b" not in _git_out(repo, "branch", "--merged", "HEAD")


def test_merge_rejects_shared_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    _mk_task(repo, "s")
    with pytest.raises(SystemExit, match="сливать нечего"):
        duo.cmd_merge(cfg, _merge_ns(task="s"))


# ---------------------------------------------------------------------------
# CLI и статус
# ---------------------------------------------------------------------------


def test_resolve_mode_flags():
    ns = argparse.Namespace
    assert duo.resolve_mode_flags(ns(mode=None, solo=False, isolation=None)) == {}
    assert duo.resolve_mode_flags(ns(mode=None, solo=True, isolation=None)) == {"mode": "solo"}
    assert duo.resolve_mode_flags(ns(mode="solo", solo=True, isolation="worktree")) == {
        "mode": "solo", "isolation": "worktree"}
    assert duo.resolve_mode_flags(ns(task="x")) == {}                 # у части подкоманд флагов нет
    with pytest.raises(SystemExit, match="несовместим"):
        duo.resolve_mode_flags(ns(mode="pair", solo=True, isolation=None))


def _run_cli(monkeypatch, cfg_path: Path, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["duo_pipeline.py", "--config", str(cfg_path), *argv])
    duo.main()


def test_cli_init_pins_mode_and_isolation_and_run_validates_flags(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo)}), encoding="utf-8")
    _run_cli(monkeypatch, cfg_path, "init", "--task", "t1", "--mode", "pair", "--worktree")
    st = _state(repo / "pipeline" / "t1")
    assert st["mode"] == "pair" and st["isolation"] == "worktree"
    _run_cli(monkeypatch, cfg_path, "init", "--task", "t2")           # без флагов — пинов нет
    st2 = _state(repo / "pipeline" / "t2")
    assert "mode" not in st2 and "isolation" not in st2
    _run_cli(monkeypatch, cfg_path, "init", "--task", "t2", "--mode", "pair")   # state уже есть — не перезаписывается
    assert "mode" not in _state(repo / "pipeline" / "t2")
    with pytest.raises(SystemExit, match="несовместим"):              # противоречие ловится в родителе, до detached
        _run_cli(monkeypatch, cfg_path, "run", "--task", "t2", "--solo", "--mode", "pair")
    with pytest.raises(SystemExit):                                   # init не знает solo
        _run_cli(monkeypatch, cfg_path, "init", "--task", "t3", "--mode", "solo")


def test_status_table_shows_mode_and_isolation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t1 = _mk_task(repo, "t1")
    _set_state(t1, mode="pair", isolation="worktree")
    _mk_task(repo, "t2")                                              # старая серия без пинов
    t3 = _mk_task(repo, "t3")
    _set_state(t3, mode="solo", isolation="shared")
    rows = {r["task"]: r for r in duo.collect_statuses(repo / "pipeline")}
    assert (rows["t1"]["mode"], rows["t1"]["isolation"]) == ("pair", "worktree")
    assert (rows["t2"]["mode"], rows["t2"]["isolation"]) == ("—", "—")
    table = duo.format_status_table(list(rows.values()))
    assert "режим" in table.splitlines()[0]
    assert "pair/wt" in table and "solo/sh" in table and "—/—" in table


def test_cmd_status_prints_mode_isolation_and_claim(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _wt_series(tmp_path, repo, "a", cfg, _zone("api/*.py"))
    assert _gate(a) is True
    duo.cmd_status(cfg, argparse.Namespace(task="a", all=False))
    out = capsys.readouterr().out
    assert "режим: duo, изоляция: worktree" in out and "ветка duo/a" in out
    assert "зона записи: держит, заявлено путей: 1" in out


def test_selftest_loads_pair_reviewers_from_config(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    cfg["pair"] = {"reviewer_plan": "alpha", "reviewer_impl": "beta"}
    seen: list[str] = []
    real = duo.load_member
    monkeypatch.setattr(duo, "load_member", lambda c, n: (seen.append(n), real(c, n))[1])

    class Boom(Exception):
        pass

    class FakeT:
        def __init__(self, cfg):
            pass

        def call(self, *a, **k):
            raise Boom

    monkeypatch.setattr(duo, "ClaudeGlm", FakeT)
    cfg["duo"] = {"members_plan": ["alpha"], "members_impl": ["alpha"], "rounds": 3}
    with pytest.raises(Boom):
        duo.cmd_selftest(cfg, argparse.Namespace())
    assert set(seen) == {"alpha", "beta"}                             # beta — только из секции pair


def test_other_live_series_lists_only_locked_foreign_series(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _live_series(tmp_path, repo, cfg, "a", mode="pair")
    _mk_task(repo, "idle")                                            # lock свободен — не живая
    _mk_task(repo, "me")
    try:
        live = duo.Pipeline(cfg, "me").other_live_series()
        assert [t for t, _ in live] == ["a"] and live[0][1]["mode"] == "pair"
    finally:
        a.release_lock()
    assert duo.Pipeline(cfg, "me").other_live_series() == []
