"""guards (29.10.2026): защита от граблей 6–10 серий amdtdlc/m2group/speedlim и улучшения (7)–(10).

Что фиксируют тесты:
  * (7) guard пустого ревью (pair): ACCEPT при нуле tool_use не засчитывается — повтор с напоминанием,
    затем halt kind=review_empty (rev-файла и «сгоревшего» номера нет), `--resume` повторяет ревью;
    REVISE без чтения и транспорты без статистики guard не трогает; hybrid требует признание «не читано»;
    транспорт ClaudeGlm считает tool_use-блоки потока по потокам;
  * (8) commit_plan: колонки по заголовку (прецедент amdtdlc `# | файлы | тип | сообщение`), brace-глобы,
    аннотации «(33)», проза вне бэктиков; `commit-by-plan --yes` — all-or-nothing (+ `--allow-partial`);
  * (9) merge: застейдженный индекс основного дерева — отказ с понятным сообщением (в т.ч. dry-run);
  * (10) правила длинных прогонов на стенде в промптах исполнителя (auto/on/off).
Паттерн test_duo_pipemodes: git-песочницы в tmp_path, скриптованный транспорт, без LLM.
"""

from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_pipemodes import (
    SINGLE_MARK,
    PairTransport,
    _done_series,
    _merge_ns,
    _pcfg,
    _state,
)
from tests.unit.test_duo_pipev3 import (
    ScriptedTransport,
    _git,
    _git_out,
    _init_repo,
    _mk_task,
    duo,
)

# ---------------------------------------------------------------------------
# (8) commit_plan: колонки по заголовку, глобы
# ---------------------------------------------------------------------------

AMDTDLC_TABLE = """# отчёт

## commit_plan

| # | файлы | тип | сообщение |
|---|-------|-----|-----------|
| 1 | `tools/amd/run_s5.sh`, `tools/amd/lab/*.py` | feat | feat(amd): стенд ступени 5 |
| 2 | `pipeline/amdtdlc/{plan_v1,impl_report_v1}.md` (33) untracked серии | docs | docs(amdtdlc): план и отчёт |
| 3 | tools/amd/readme.md ; tools/amd/notes/ | docs | docs(amd): заметки |
"""


def test_parse_commit_plan_header_columns_amdtdlc_precedent():
    """Раньше позиционный разбор брал «тип» за сообщение, а `{a,b}` и «(33)» ломали глобы."""
    groups, warnings = duo.parse_commit_plan(AMDTDLC_TABLE)
    assert warnings == []
    assert [g["name"] for g in groups] == ["1", "2", "3"]
    assert [g["message"] for g in groups] == [
        "feat(amd): стенд ступени 5", "docs(amdtdlc): план и отчёт", "docs(amd): заметки"]
    assert [g["rationale"] for g in groups] == ["feat", "docs", "docs"]            # «тип» → rationale
    assert groups[0]["globs"] == ["tools/amd/run_s5.sh", "tools/amd/lab/*.py"]
    assert groups[1]["globs"] == ["pipeline/amdtdlc/plan_v1.md", "pipeline/amdtdlc/impl_report_v1.md"]
    assert groups[2]["globs"] == ["tools/amd/readme.md", "tools/amd/notes/*"]      # каталог → dir/*


def test_parse_commit_plan_columns_in_any_order_and_without_name_column():
    report = """## commit_plan
| сообщение | rationale | Файлы (glob) |
|---|---|---|
| fix: a | почему | `a.py` |
| fix: b | потому | `b.py`, `c.py` |
"""
    groups, warnings = duo.parse_commit_plan(report)
    assert warnings == []
    assert [(g["name"], g["globs"], g["message"], g["rationale"]) for g in groups] == [
        ("1", ["a.py"], "fix: a", "почему"), ("2", ["b.py", "c.py"], "fix: b", "потому")]


def test_parse_commit_plan_headerless_table_stays_positional():
    """Таблица без заголовка (первая строка — данные) разбирается по-старому: 4 колонки позиционно."""
    report = """## commit_plan
| G1 | a.py | feat: a | п.1 |
| G2 | b.py | feat: b | п.2 |
"""
    groups, warnings = duo.parse_commit_plan(report)
    assert warnings == []
    assert [(g["name"], g["globs"], g["message"]) for g in groups] == [
        ("G1", ["a.py"], "feat: a"), ("G2", ["b.py"], "feat: b")]


def test_parse_commit_plan_row_width_must_match_header():
    report = """## commit_plan
| # | файлы | тип | сообщение |
|---|---|---|---|
| 1 | a.py | feat | feat: a |
| 2 | b.py | feat |
"""
    groups, warnings = duo.parse_commit_plan(report)
    assert [g["name"] for g in groups] == ["1"]
    assert len(warnings) == 1 and "4 колонки" in warnings[0]


@pytest.mark.parametrize("cell,expected", [
    ("`a.py`", ["a.py"]),
    ("a.py, b.py", ["a.py", "b.py"]),
    ("`a.py`; `b.py`", ["a.py", "b.py"]),
    ("a.py, `b.py`", ["a.py", "b.py"]),                              # смесь голого токена и спана
    ("`x/{a,b}.md`", ["x/a.md", "x/b.md"]),
    ("`x/{a,b}/{c,d}.md`", ["x/a/c.md", "x/a/d.md", "x/b/c.md", "x/b/d.md"]),
    ("x/{a,b}.md, y.py", ["x/a.md", "x/b.md", "y.py"]),               # запятая внутри скобок — не разделитель
    ("`x/*.md` (33)", ["x/*.md"]),                                    # аннотация вне спана
    ("x/*.md (33)", ["x/*.md"]),                                      # аннотация без спанов
    ("`x/*.md` untracked серии", ["x/*.md"]),                         # проза вне спанов
    ("`a.py`, untracked, (см. выше)", ["a.py"]),
    ("`dir/`", ["dir/*"]),
    ("./a.py, a.py", ["a.py"]),                                       # дубликаты и ./
    ("`{один}.py`", ["{один}.py"]),                                   # скобка без запятой — не раскрывается
    ("", []),
])
def test_extract_commit_globs(cell: str, expected: list[str]):
    assert duo.extract_commit_globs(cell) == expected


def _commit_cfg_repo(tmp_path: Path, table: str, files: dict[str, str]) -> tuple[dict, Path, int]:
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "impl_report_v1.md").write_text(f"# отчёт\n\n## commit_plan\n{table}", encoding="utf-8")
    for rel, body in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    n0 = len(_git_out(repo, "log", "--oneline").splitlines())
    return _pcfg(tmp_path, repo), repo, n0


def _cbp(cfg: dict, **kw) -> int:
    ns = types.SimpleNamespace(task="demo", version=1, dry_run=False, yes=True, allow_partial=False, **kw)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(cfg, ns)
    code = ei.value.code
    return 0 if code is None else int(code)


def _ncommits(repo: Path) -> int:
    return len(_git_out(repo, "log", "--oneline").splitlines())


GOOD_TABLE = """
| группа | файлы (glob) | сообщение коммита | rationale |
|---|---|---|---|
| A | `a.py` | feat: a | п.1 |
| B | `{b,c}.py` | feat: b и c | п.2 |
"""


def test_commit_by_plan_yes_commits_all_groups_when_plan_is_clean(tmp_path: Path, capsys):
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, GOOD_TABLE, {"a.py": "1", "b.py": "2", "c.py": "3"})
    assert _cbp(cfg) == 0
    assert _ncommits(repo) == n0 + 2
    assert _git_out(repo, "status", "--porcelain", "--untracked-files=all", "--", "*.py").strip() == ""


@pytest.mark.parametrize("scenario", ["uncovered", "overlap", "empty_group", "parser_warning"])
def test_commit_by_plan_yes_refuses_before_first_commit_on_anomaly(tmp_path: Path, capsys, scenario: str):
    table = GOOD_TABLE
    files = {"a.py": "1", "b.py": "2", "c.py": "3"}
    if scenario == "uncovered":
        files["stray.py"] = "x"
    elif scenario == "overlap":
        table = table.replace("`a.py`", "`a.py`, `b.py`")               # b.py — в двух группах
    elif scenario == "empty_group":
        files.pop("a.py")                                              # группа A без изменённых файлов
    elif scenario == "parser_warning":
        table += "| C | d.py | только три колонки |\n"
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, table, files)
    assert _cbp(cfg) == 1
    out = capsys.readouterr().out
    assert "ОТКАЗ --yes" in out and "Ни один коммит не сделан" in out and "--allow-partial" in out
    assert _ncommits(repo) == n0                                       # all-or-nothing: ни одного коммита


def test_commit_by_plan_yes_allow_partial_commits_valid_groups(tmp_path: Path, capsys):
    cfg, repo, n0 = _commit_cfg_repo(tmp_path, GOOD_TABLE, {"a.py": "1", "b.py": "2", "c.py": "3", "stray.py": "x"})
    ns = types.SimpleNamespace(task="demo", version=1, dry_run=False, yes=True, allow_partial=True)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(cfg, ns)
    assert ei.value.code == 1                                          # хвост (uncovered) остаётся виден в коде
    assert _ncommits(repo) == n0 + 2                                   # но валидные группы закоммичены
    assert "stray.py" in _git_out(repo, "status", "--porcelain")


def test_commit_by_plan_amdtdlc_table_end_to_end(tmp_path: Path):
    """Таблица-прецедент целиком: колонки по заголовку, brace-глоб, аннотация — 3 коммита, а не один «docs»."""
    cfg, repo, n0 = _commit_cfg_repo(
        tmp_path,
        "\n| # | файлы | тип | сообщение |\n|---|---|---|---|\n"
        "| 1 | `a.py` | feat | feat: a |\n"
        "| 2 | `docs/{x,y}.md` (33) серии | docs | docs: x и y |\n",
        {"a.py": "1", "docs/x.md": "x", "docs/y.md": "y"})
    assert _cbp(cfg) == 0
    assert _ncommits(repo) == n0 + 2
    assert _git_out(repo, "log", "-1", "--format=%s").strip() == "docs: x и y"
    assert set(_git_out(repo, "show", "--name-only", "--format=", "HEAD").split()) == {"docs/x.md", "docs/y.md"}


# ---------------------------------------------------------------------------
# (9) merge: застейдженный индекс основного дерева
# ---------------------------------------------------------------------------


def test_merge_refuses_with_staged_index_even_in_dry_run(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    a = _done_series(tmp_path, repo, cfg)
    (repo / "foreign.txt").write_text("чужое", encoding="utf-8")
    _git(repo, "add", "foreign.txt")                                   # застейджено, но не закоммичено
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 1
    out = capsys.readouterr().out
    assert "ОТКАЗ" in out and "foreign.txt" in out and "git restore --staged" in out
    assert duo.cmd_merge(cfg, _merge_ns()) == 1
    assert not (repo / "feature.txt").exists() and a.work_path.exists()   # ничего не слито, worktree цел
    _git(repo, "restore", "--staged", "--", "foreign.txt")
    assert duo.cmd_merge(cfg, _merge_ns()) == 0                        # после снятия со стейджа — проходит
    assert (repo / "feature.txt").exists()


# ---------------------------------------------------------------------------
# (7) guard пустого ревью
# ---------------------------------------------------------------------------


class StatTransport(PairTransport):
    """PairTransport + статистика tool_use: на каждый вызов единственного проверяющего берётся число из
    очереди tool_counts (исчерпана — 3 обращения). Вызовы исполнителя счётчик не трогают."""

    def __init__(self, tool_counts=(), **kw):
        super().__init__(**kw)
        self.tool_counts = list(tool_counts)
        self._n = 0

    def reset_call_stats(self):
        self._n = 0

    def take_call_stats(self):
        return {"tool_uses": self._n, "tools": {"Read": self._n} if self._n else {}}

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag=""):
        if SINGLE_MARK in prompt:
            self._n += self.tool_counts.pop(0) if self.tool_counts else 3
        return super().call(prompt, mode=mode, timeout=timeout, cwd=cwd,
                            resume_session=resume_session, tag=tag)


def _single_review_t(tmp_path: Path, repo: Path, t, cfg: dict | None = None):
    cfg = cfg or _pcfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir(exist_ok=True)
    art = repo / "pipeline" / "demo" / "plan_v1.md"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("# план\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    return reviewer, td, dict(
        artifact_rel="pipeline/demo/plan_v1.md", phase="plan", task_text="задача",
        members=["alpha"], rounds=3, rev_file=td / "rev001_plan.md",
        guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo")


def test_empty_review_retried_with_reminder_then_accepted(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = StatTransport(tool_counts=[0, 5])                              # 1-й ACCEPT без чтения, 2-й — 5 обращений
    reviewer, td, kw = _single_review_t(tmp_path, repo, t)
    verdict, _cost, tel = reviewer.review(**kw)
    assert verdict == "ACCEPT"
    assert len(t.reviewer_prompts) == 2
    assert "ACCEPT без единого обращения к инструментам" in t.reviewer_prompts[1]
    assert "0 обращений к инструментам при ACCEPT" in t.reviewer_prompts[1]
    assert "ACCEPT без единого" not in t.reviewer_prompts[0]
    assert (tel["tool_uses"], tel["empty_retries"]) == (5, 1)
    assert list((td / "transcripts").glob("*_reviewer_alpha_empty1.md"))   # след пустой попытки сохранён
    assert (td / "rev001_plan.md").read_text(encoding="utf-8").rstrip().endswith("VERDICT: ACCEPT")


def test_empty_review_twice_raises_and_writes_no_rev_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = StatTransport(tool_counts=[0, 0])
    reviewer, td, kw = _single_review_t(tmp_path, repo, t)
    with pytest.raises(duo.EmptyReviewError, match="0 обращений"):
        reviewer.review(**kw)
    assert len(t.reviewer_prompts) == 2                                # ровно один повтор (empty_guard.retries=1)
    assert not (td / "rev001_plan.md").exists()                        # вердикт не засчитан и не записан


def test_empty_guard_retries_zero_fails_immediately_and_disabled_passes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    cfg["review"] = {"empty_guard": {"enabled": True, "retries": 0}}
    reviewer, _td, kw = _single_review_t(tmp_path, repo, StatTransport(tool_counts=[0]), cfg)
    with pytest.raises(duo.EmptyReviewError):
        reviewer.review(**kw)
    cfg2 = _pcfg(tmp_path, repo)
    cfg2["review"] = {"empty_guard": {"enabled": False}}
    t2 = StatTransport(tool_counts=[0])
    reviewer2, _td2, kw2 = _single_review_t(tmp_path, repo, t2, cfg2)
    verdict, _c, tel = reviewer2.review(**kw2)
    assert verdict == "ACCEPT" and tel["tool_uses"] == 0 and len(t2.reviewer_prompts) == 1


def test_revise_without_tools_is_not_blocked(tmp_path: Path):
    repo = _init_repo(tmp_path)
    t = StatTransport(tool_counts=[0], verdicts=["REVISE"])
    reviewer, _td, kw = _single_review_t(tmp_path, repo, t)
    verdict, _c, tel = reviewer.review(**kw)
    assert verdict == "REVISE" and len(t.reviewer_prompts) == 1 and tel["empty_retries"] == 0


def test_transport_without_stats_keeps_old_behaviour(tmp_path: Path):
    """Заглушки/транспорты без reset/take_call_stats: guard молчит, телеметрия прежняя (без tool_uses)."""
    repo = _init_repo(tmp_path)
    t = PairTransport()
    reviewer, _td, kw = _single_review_t(tmp_path, repo, t)
    verdict, _c, tel = reviewer.review(**kw)
    assert verdict == "ACCEPT" and "tool_uses" not in tel and len(t.reviewer_prompts) == 1


@pytest.mark.parametrize("ctx,verdict,report,stats,expect", [
    ("legacy", "ACCEPT", "всё хорошо", {"tool_uses": 0}, True),
    ("legacy", "ACCEPT", "всё хорошо", {"tool_uses": 1}, False),
    ("legacy", "REVISE", "правки", {"tool_uses": 0}, False),
    ("legacy", None, "нет вердикта", {"tool_uses": 0}, False),
    ("legacy", "ACCEPT", "всё хорошо", None, False),                   # транспорт статистику не ведёт
    ("hybrid", "ACCEPT", "пак прочитан целиком, замечаний нет", {"tool_uses": 0}, False),
    ("hybrid", "ACCEPT", "файл не читан, инструментов нет", {"tool_uses": 0}, True),
    ("hybrid", "ACCEPT", "У меня нет инструментов для проверки", {"tool_uses": 0}, True),
    ("hybrid", "ACCEPT", "файл не читан", {"tool_uses": 2}, False),
])
def test_empty_review_reason_matrix(ctx, verdict, report, stats, expect):
    cfg = {"review": {"context": ctx}}
    assert bool(duo.empty_review_reason(cfg, verdict, report, stats)) is expect


def test_pipeline_halts_review_empty_and_resume_repeats_review(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    t = StatTransport(tool_counts=[0, 0])
    p.t = t
    p.run()
    st = _state(td)
    assert st["phase"] == "needs_human"
    assert (st["halt"]["kind"], st["halt"]["phase"], st["halt"]["resumable"]) == ("review_empty", "plan", True)
    assert "ПУСТОЕ" in st["stop_reason"] and "set-verdict" in st["stop_reason"]
    assert not list(td.glob("rev*_plan.md")) and st.get("rev_counter_plan", 0) == 0   # номер не «сгорел»
    assert not st.get("verdicts")

    q = duo.Pipeline(cfg, "demo")
    t2 = StatTransport(tool_counts=[4])
    q.t = t2
    q.run(resume=True)
    st2 = _state(td)
    assert st2["phase"] == "impl_pending" and st2["verdicts"][-1]["verdict"] == "ACCEPT"
    assert (td / "rev001_plan.md").exists()
    assert st2["review_telemetry"][-1]["tool_uses"] == 4


def test_claude_glm_counts_tool_use_blocks_per_thread(tmp_path: Path, monkeypatch):
    """ClaudeGlm.call считает tool_use-блоки stream-json; счётчик у потока свой, reset обнуляет."""
    import io
    import json as _json

    events = [
        {"type": "assistant", "session_id": "s", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "text", "text": "читаю"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "b.py"}}]}},
        {"type": "result", "session_id": "s", "total_cost_usd": 0.01, "result": "готово"},
    ]

    class FakeProc:
        def __init__(self, *a, **kw):
            self.stdout = io.StringIO("".join(_json.dumps(e) + "\n" for e in events))
            self.stderr = io.StringIO("")

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(duo.subprocess, "Popen", FakeProc)
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"})
    assert glm.take_call_stats() == {"tool_uses": 0, "tools": {}}
    glm.reset_call_stats()
    text, sid, cost = glm.call("промпт", mode="plan", timeout=5)
    assert (text, sid) == ("готово", "s")
    assert glm.take_call_stats() == {"tool_uses": 3, "tools": {"Read": 2, "Grep": 1}}
    other: dict = {}
    th = threading.Thread(target=lambda: other.update(glm.take_call_stats()))
    th.start()
    th.join()
    assert other["tool_uses"] == 0                                     # у другого потока счётчик свой
    glm.reset_call_stats()
    assert glm.take_call_stats()["tool_uses"] == 0


# ---------------------------------------------------------------------------
# (10) правила длинных прогонов на стенде
# ---------------------------------------------------------------------------


def _ex(tmp_path: Path, **cfg_over):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = {"task": "demo", "artifacts_rel": "pipeline/demo"}
    t = ScriptedTransport()
    cfg = _pcfg(tmp_path, repo)
    cfg.update(cfg_over)
    return duo.Executor(cfg, t, td, repo, st), t, repo


@pytest.mark.parametrize("task,mode,expected", [
    ("Прогнать замеры на стенде AMD по ssh", "auto", True),
    ("запусти nohup-драйвер", "auto", True),
    ("поправь парсер commit_plan", "auto", False),
    ("поправь парсер commit_plan", "on", True),
    ("замеры на стенде AMD", "off", False),
])
def test_long_run_rules_switch(task: str, mode: str, expected: bool):
    assert duo.long_run_rules_enabled({"executor_long_run_rules": mode}, task) is expected


def test_executor_prompts_get_long_run_rules_only_for_stand_tasks(tmp_path: Path):
    ex, t, repo = _ex(tmp_path)
    ex.make_plan("поправь парсер", 1)
    assert "ДЛИННЫЕ ПРОГОНЫ НА СТЕНДЕ" not in t.calls[0]               # обычные задания — промпт прежний
    ex.make_plan("замеры на стенде AMD (ssh)", 1)
    assert "ДЛИННЫЕ ПРОГОНЫ НА СТЕНДЕ" in t.calls[1] and "setsid nohup" in t.calls[1]
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("# план\n", encoding="utf-8")
    ex.implement("замеры на стенде AMD (ssh)", "pipeline/demo/plan_v1.md", 1)
    assert "LC_ALL=C" in t.calls[-1] and "impl_report_vN.md" in t.calls[-1]
    assert "СТРОГО эти четыре колонки" in t.calls[-1]                  # формат commit_plan ужесточён
