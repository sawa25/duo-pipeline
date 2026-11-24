"""pipev2: юниты commit_plan — парсер таблицы коммит-групп, покрытие,
промпты исполнителя/председателя, git-хелперы commit-by-plan.

Паттерн test_duo_pipev1.py: модуль оркестратора грузится importlib-ом
(tools/dev/duo_pipeline вне sys.path, module-level без побочных эффектов);
git-песочницы — в tmp_path (изоляция из tests/conftest.py не обходится).
Реальные repo/ и pipeline/ в тестах не читаются.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_pipev2_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()


def _git(repo: Path, *args: str) -> None:
    rc, out = duo.sh(["git", *args], cwd=repo)
    assert rc == 0, f"git {args} failed: {out}"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    # локальная identity репозитория: на CI-раннере глобальной нет, а
    # commit_group (боевой код) коммитит без -c — гермeтичность теста,
    # а не подмена автора в продукте (инцидент CI 21.10)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


# --- парсер таблицы commit_plan -----------------------------------------------


VALID_REPORT = """# impl_report_v1 — отчёт

## 1. Что сделано
- пункт 1;
- пункт 2.

## commit_plan

| группа | файлы (glob) | сообщение коммита (1 строка) | rationale |
|---|---|---|---|
| G1-оркестратор | `tools/dev/duo_pipeline/duo_pipeline.py` | feat(duo): commit_plan в промпте исполнителя | п.2 задания |
| G2-юниты | `tests/unit/test_duo_pipev2.py` | test(duo): парсер commit_plan | п.3 задания |
| G3-мультиглоб | tools/a.py, `tools/b.py` | fix: два файла одной логики | один пункт плана |

## 2. Прочее
- текст после таблицы.
"""


def test_parse_commit_plan_valid():
    groups, warnings = duo.parse_commit_plan(VALID_REPORT)
    assert warnings == []
    assert [g["name"] for g in groups] == ["G1-оркестратор", "G2-юниты", "G3-мультиглоб"]
    assert groups[0]["globs"] == ["tools/dev/duo_pipeline/duo_pipeline.py"]
    assert groups[0]["message"] == "feat(duo): commit_plan в промпте исполнителя"
    assert groups[0]["rationale"] == "п.2 задания"
    assert groups[2]["globs"] == ["tools/a.py", "tools/b.py"]  # split по запятой, бэктики сняты


def test_parse_commit_plan_malformed_rows():
    report = """## commit_plan

| группа | файлы (glob) | сообщение коммита (1 строка) | rationale |
|---|---|---|---|
| ок | good.py | msg | почему |
| кривая | только три колонки |
| пусто-файлы |  | msg | почему |
| пусто-сообщение | x.py |  | почему |
| лишняя | a.py | msg | r | пятая |
"""
    groups, warnings = duo.parse_commit_plan(report)
    assert [g["name"] for g in groups] == ["ок"]  # валидная строка парсируется
    assert len(warnings) == 4, warnings
    assert any("4 колонки" in w for w in warnings)
    assert any("пустая ячейка файлов" in w for w in warnings)
    assert any("пустое сообщение" in w for w in warnings)
    # заголовок и разделитель не дают ни групп, ни предупреждений
    assert all("группа" not in w and "---" not in w for w in warnings)


def test_parse_commit_plan_missing_section():
    groups, warnings = duo.parse_commit_plan("# отчёт без секции\n\nтекст\n")
    assert groups == []
    assert warnings == ["секция commit_plan не найдена"]


def test_uncovered_files_and_overlap():
    groups, _ = duo.parse_commit_plan(VALID_REPORT)
    changed = [
        "tools/dev/duo_pipeline/duo_pipeline.py",  # G1 (точное совпадение)
        "tests/unit/test_duo_pipev2.py",            # G2 (точное совпадение)
        "tools/a.py",                               # G3
        "stray/untouched.txt",                      # вне групп
    ]
    uncovered, overlaps = duo.uncovered_files(groups, changed)
    assert uncovered == ["stray/untouched.txt"]
    assert overlaps == []
    # пересечение: один файл под глобами ДВУХ групп — детектор нарушения
    groups2 = [{"name": "A", "globs": ["tools/**"], "message": "m", "rationale": ""},
               {"name": "B", "globs": ["tools/a.py"], "message": "m", "rationale": ""}]
    uncovered2, overlaps2 = duo.uncovered_files(groups2, ["tools/a.py"])
    assert uncovered2 == []
    assert overlaps2 == ["tools/a.py"]
    # два глоба ОДНОЙ группы на одном файле — НЕ пересечение (не ложный сигнал)
    groups3 = [{"name": "A", "globs": ["tools/**", "tools/a.py"], "message": "m", "rationale": ""}]
    _, overlaps3 = duo.uncovered_files(groups3, ["tools/a.py"])
    assert overlaps3 == []


# --- промпты исполнителя -------------------------------------------------------


class FakeTransport:
    def __init__(self):
        self.calls: list[str] = []

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        self.calls.append(prompt)
        return "ответ", "sid-1", 0.0


def _executor(tmp_path: Path) -> tuple[duo.Executor, FakeTransport]:
    cfg = {"timeouts": {"executor_sec": 5}}
    state = {"executor_session": None, "spent_usd": 0.0, "artifacts_rel": "pipeline/demo"}
    t = FakeTransport()
    ex = duo.Executor(cfg, t, tmp_path, tmp_path, state)
    return ex, t


def test_implement_prompt_requires_commit_plan(tmp_path: Path):
    ex, t = _executor(tmp_path)
    ex.implement("текст задачи", "pipeline/demo/plan_v2.md", 1)
    prompt = t.calls[0]
    assert "## commit_plan" in prompt
    assert "| группа | файлы (glob) | сообщение коммита (1 строка) | rationale |" in prompt
    assert "изменённые/созданные/удалённые файлы" in prompt      # Н4: удалённые тоже
    assert "ровно в ОДНУ группу" in prompt                       # Н1: disjoint-правило
    assert "пересечение групп по файлам запрещено" in prompt
    assert "fnmatch" in prompt                                   # семантика глобов
    ex.make_plan("текст задачи", 1)                              # фаза plan не затронута
    assert "commit_plan" not in t.calls[1]


def test_revise_impl_prompt_updates_commit_plan(tmp_path: Path):
    rev = tmp_path / "rev001_impl.md"
    rev.write_text("# rev\n\n## 3. Сводка\n- правка;\n", encoding="utf-8")
    ex, t = _executor(tmp_path)
    ex.revise_impl("текст", "pipeline/demo/plan_v2.md", 2, rev, "pipeline/demo/diff.patch")
    prompt = t.calls[0]
    assert "commit_plan" in prompt
    assert "состав файлов мог измениться" in prompt


# --- промпт председателя -------------------------------------------------------


def _fake_agents_dir(tmp_path: Path) -> str:
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    (agents / "council-tester.md").write_text(
        "## Identity\nТестовая персона.\n\n"
        "## Output Format (Standalone)\n- пункт формата R1.\n\n"
        "## Output Format (Council Round 2)\n- пункт формата R2.\n",
        encoding="utf-8",
    )
    return str(agents)


def _chairman_prompt(tmp_path: Path, phase: str) -> str:
    cfg = {"council_agents_dir": _fake_agents_dir(tmp_path)}
    reviewer = duo.DuoReviewer(cfg, FakeTransport(), tmp_path, tmp_path)
    m = duo.load_member(cfg, "tester")
    return reviewer._build_chairman_prompt(
        artifact_rel="pipeline/demo/impl_report_v1.md", phase=phase, task_text="задача",
        A=m, B=m, r1a="a1", r1b="b1", r2a="a2", r2b="b2", r3a="a3", r3b="b3",
        sa="revise", sb="revise",
    )


def test_chairman_prompt_impl_checklist_only_for_impl(tmp_path: Path):
    impl = _chairman_prompt(tmp_path, "impl")
    assert "## commit_plan" in impl
    assert "изменённый/созданный/удалённый" in impl            # Н4
    assert "ровно ОДНОЙ группой" in impl                       # Н1: непересекаемость
    plan = _chairman_prompt(tmp_path, "plan")
    assert "commit_plan" not in plan                           # фаза plan не затронута
    custom = _chairman_prompt(tmp_path, "custom")
    assert "commit_plan" not in custom
    # замороженный формат отчёта не изменён (в обоих фазах)
    for prompt in (impl, plan):
        assert "## 0" in prompt and "## 1" in prompt
        assert "VERDICT: ACCEPT" in prompt and "VERDICT: REVISE" in prompt
        assert prompt.index("VERDICT: REVISE") < prompt.index("Правила:")  # чек-лист после VERDICT, до правил


# --- снимок изменённых файлов и collect_commit_groups --------------------------


def test_changed_files_all_expands_untracked_dirs(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("modified", encoding="utf-8")          # M
    (repo / "old.txt").write_text("x", encoding="utf-8")
    # add только old.txt: «add .» унёс бы и модификацию README в этот коммит
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "old.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "old")
    (repo / "old.txt").unlink()                                            # D
    deep = repo / "newdir" / "nested"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text("1", encoding="utf-8")
    (deep / "y.py").write_text("2", encoding="utf-8")
    files = duo.changed_files_all(repo)
    assert "README.md" in files and "old.txt" in files
    # Н3: без -uall каталог схлопнулся бы в одну запись «newdir/»
    assert "newdir/nested/x.py" in files and "newdir/nested/y.py" in files
    assert not any(f.rstrip("/").endswith("newdir") for f in files)
    xy = {path: code for code, path in duo.changed_status_all(repo)}
    assert xy.get("README.md") == " M" and xy.get("old.txt") == " D"
    assert xy.get("newdir/nested/x.py") == "??"


def test_collect_commit_groups_paths(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("modified", encoding="utf-8")
    (repo / "b.txt").write_text("stray", encoding="utf-8")                 # вне групп
    (repo / "pipeline" / "demo").mkdir(parents=True)
    (repo / "pipeline" / "demo" / "artifact.md").write_text("арт", encoding="utf-8")
    task_dir = tmp_path / "td"
    task_dir.mkdir()
    (task_dir / "impl_report_v1.md").write_text(
        VALID_REPORT.replace("tools/dev/duo_pipeline/duo_pipeline.py", "README.md")
                    .replace("tests/unit/test_duo_pipev2.py", "nonexistent_covered.txt"),
        encoding="utf-8",
    )
    snap = duo.collect_commit_groups(repo, task_dir, 1, "pipeline/demo")
    assert len(snap["groups"]) == 3
    assert snap["uncovered"] == ["b.txt"]                     # артефакты не в uncovered
    assert snap["overlaps"] == []
    assert "pipeline/demo/artifact.md" in snap["artifacts"]   # но показаны отдельно
    assert "pipeline/demo/artifact.md" not in snap["uncovered"]
    # Н6: отсутствующий файл отчёта — предупреждение, не исключение
    miss = duo.collect_commit_groups(repo, task_dir, 7, "pipeline/demo")
    assert miss["groups"] == []
    assert any("не найден" in w for w in miss["warnings"])


# --- commit_group: отказ без падения -------------------------------------------


def test_commit_group_failure_is_reported(tmp_path: Path):
    repo = _init_repo(tmp_path)
    # пустой pathspec — не «git add --» (стейдж всего), а управляемый отказ
    ok, out = duo.commit_group(repo, [], "msg")
    assert not ok and "нет путей" in out
    # nothing to commit: файл не менялся — rc≠0, но без исключения (Н2)
    ok, out = duo.commit_group(repo, ["README.md"], "пустой коммит")
    assert not ok
    assert "nothing to commit" in out or "no changes added" in out or "nothing added" in out
    # нормальный путь: изменение закоммичено pathspec-ограниченно
    (repo / "README.md").write_text("v2", encoding="utf-8")
    (repo / "other.txt").write_text("не трогаем", encoding="utf-8")
    ok, out = duo.commit_group(repo, ["README.md"], "feat: группа G1")
    assert ok, out
    rc, log = duo.sh(["git", "log", "-1", "--format=%s"], cwd=repo)
    assert "feat: группа G1" in log
    rc, staged = duo.sh(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo)
    assert "other.txt" in staged       # вне группы — не застейджен и не в коммите
    assert "README.md" not in staged   # закоммичен полностью
