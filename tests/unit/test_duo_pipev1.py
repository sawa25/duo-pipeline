"""pipev1: юниты быстрых побед дуо-пайплайна (П2 GitGuard-clean, П3 анти-повторы, П4 status --all).

Паттерн test_duo_chairman_validation.py: модуль оркестратора грузится importlib-ом
(tools/dev/duo_pipeline вне sys.path, module-level без побочных эффектов);
git-песочницы и state.json-фикстуры — в tmp_path (изоляция из tests/conftest.py
не обходится). Реальные repo/ и pipeline/ в тестах не читаются.

Фикстуры секции «Отклонено» — дословные образцы из серий corpusfix/corp2b/d10
(дрейф формата: «/ понижено», «как необоснованные», «ё», псевдо-«отсутствия»).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_pipev1_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()


def _git(repo: Path, *args: str) -> None:
    rc, out = duo.sh(["git", *args], cwd=repo)
    assert rc == 0, f"git {args} failed: {out}"


# --- П2: GitGuard.clean вне зон + дискриминатор живых задач ------------------


def test_p2_guard_cleans_foreign_keeps_zones_and_live_tasks(tmp_path: Path):
    """Детектор регресса _norm (корневые пути) + дискриминатор живых задач.

    rogue/ (без state.json) и корневой notes.txt удаляются; зона артефактов
    текущей задачи и pipeline/other/ (живая параллельная, есть state.json)
    выживают; испорченный tracked README.md восстанавливается дословным
    pathspec (не «EADME.md»); лог содержит ОТКАТ (с дословным notes.txt)
    и ПРОПУСК для pipeline/other/.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    # pipeline/ в реальном репо tracked (каталоги задач — инфраструктура):
    # иначе porcelain сворачивает свежесозданное в одну запись «?? pipeline/»
    # и индивидуальные пути задач не видны.
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "README.md").write_text("tracked", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    task_dir = tmp_path / "td"
    task_dir.mkdir()

    guard = duo.GitGuard(repo)
    before = guard.snapshot_before_review("pipeline/demo")
    (repo / "pipeline" / "demo").mkdir(parents=True)
    (repo / "pipeline" / "demo" / "keep.md").write_text("keep", encoding="utf-8")
    (repo / "pipeline" / "rogue").mkdir(parents=True)                     # бродячий черновик
    (repo / "pipeline" / "rogue" / "task.md").write_text("draft", encoding="utf-8")
    (repo / "notes.txt").write_text("foreign", encoding="utf-8")          # корневой путь
    (repo / "pipeline" / "other").mkdir(parents=True)                     # живая параллельная
    (repo / "pipeline" / "other" / "state.json").write_text(
        json.dumps({"task": "other", "phase": "impl"}), encoding="utf-8")
    (repo / "pipeline" / "other" / "transcripts").mkdir()
    (repo / "pipeline" / "other" / "transcripts" / "x.md").write_text("tr", encoding="utf-8")
    (repo / "README.md").write_text("corrupted", encoding="utf-8")        # tracked вне зон

    guard.enforce_readonly(before, "pipeline/demo", task_dir)

    assert not (repo / "pipeline" / "rogue").exists(), "бродячий черновик не удалён"
    assert not (repo / "notes.txt").exists(), "корневой foreign-файл не удалён (регресс _norm)"
    assert (repo / "pipeline" / "demo" / "keep.md").exists(), "зона артефактов затронута"
    assert (repo / "pipeline" / "other" / "state.json").exists(), "живая параллельная задача снесена"
    assert (repo / "pipeline" / "other" / "transcripts" / "x.md").exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello", "tracked не восстановлен"

    log_text = (task_dir / "orchestrator.log").read_text(encoding="utf-8")
    # регресс-детектор _norm: путь в перечне дословный («'notes.txt'», не «'otes.txt'»)
    assert "notes.txt" in log_text, "корневой foreign-путь не в перечне отката"
    assert "'otes.txt'" not in log_text, "pathspec искажён (регресс _norm)"
    assert "ОТКАТ" in log_text and "tracked" in log_text
    assert "ПРОПУСК" in log_text and "pipeline/other/" in log_text


def test_p2_guard_clean_no_changes_is_noop(tmp_path: Path):
    """Пустая дельта (new == ∅) — никаких логов и действий."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    task_dir = tmp_path / "td"
    task_dir.mkdir()
    guard = duo.GitGuard(repo)
    guard.enforce_readonly(guard.snapshot_before_review("pipeline/demo"), "pipeline/demo", task_dir)
    assert not (task_dir / "orchestrator.log").exists()


# --- П3: парсер отклонённых замечаний ----------------------------------------

CORP2B_PLAN_REV = """# rev — ревью pipeline/x/plan_v1.md (фаза: plan)
Дата: тест. Дуэт: a × b.

## 1. Замечания
**Н1 (существенное).** Суть замечания.

**Отклонено:**
- Квалификация socrates'ом Н2 как «ложной посылки» — спор о ярлыке, а не о содержании.
- Требование рассмотреть альтернативу «не персистировать вообще». Отклонено как блокер: terrain задан заказчиком.

## 2. Что проверено и подтвердилось
- факт 1;
"""

CORPUSFIX_IMPL_REV = """# rev — ревью pipeline/x/impl_report_v1.md (фаза: impl)
## 1. Замечания
**Н1.** Суть.

**Отклонено / понижено:**
- Формулировка meadows R1 «инцидентный путь достижим через пустоту» — отклонено (убрать «достижим»).
- Прочих придирок, не выдержавших проверки, нет: все существенные утверждения подтвердились.

## 2. Что проверено и подтвердилось
- факт;
"""

D10_PLAN_REV = """# rev — ревью pipeline/x/plan_v2.md (фаза: plan)
## 1. Замечания
**Н1.** Суть.

**Отклонённые замечания: отсутствуют.** Переклассификация одна: притязание socrates на статус «существенное» отклонено в пользу «косметика».

## 2. Что проверено и подтвердилось
- факт;
"""

CORP2B_IMPL_REV = """# rev — ревью pipeline/x/impl_report_v1.md (фаза: impl)
**Отклонено как необоснованные:** ничего — все фактические утверждения участников подтвердились.

## 2. Что проверено и подтвердилось
"""

CORPUSFIX_PLAN_INLINE_REV = """# rev — ревью pipeline/x/plan_v1.md (фаза: plan)
**Отклонено:** отклонённых замечаний нет — все пять находок дуэта подтверждены.

## 2. Что проверено и подтвердилось
- факт;
"""


def _write_rev(task_dir: Path, name: str, text: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / name).write_text(text, encoding="utf-8")


def test_p3_collect_rejected_real_variants(tmp_path: Path):
    """corp2b (пустой заголовок + буллеты) и corpusfix impl («/ понижено») собираются."""
    _write_rev(tmp_path, "rev001_plan.md", CORP2B_PLAN_REV)
    _write_rev(tmp_path, "rev002_impl.md", CORPUSFIX_IMPL_REV)
    out = duo.DuoReviewer.collect_rejected(tmp_path)
    assert "[rev001_plan] Квалификация socrates'ом Н2" in out
    assert "[rev001_plan] Требование рассмотреть альтернативу" in out
    assert "[rev002_impl] Формулировка meadows R1" in out


def test_p3_absence_pseudo_items_filtered(tmp_path: Path):
    """Псевдо-«отсутствия» (d10 «ё»-вариант, corp2b impl «ничего», инлайн corpusfix)
    в блок не попадают; файл без секции вклада не даёт."""
    _write_rev(tmp_path, "rev001_plan.md", D10_PLAN_REV)
    _write_rev(tmp_path, "rev002_impl.md", CORP2B_IMPL_REV)
    _write_rev(tmp_path, "rev003_plan.md", CORPUSFIX_PLAN_INLINE_REV)
    _write_rev(tmp_path, "rev004_plan.md", "# rev — без секции отклонений\n\n## 2. x\n- факт;\n")
    assert duo.DuoReviewer.collect_rejected(tmp_path) == ""


def test_p3_truncation_and_empty_input(tmp_path: Path):
    """Обрезка до 1500 симв. с маркером; отсутствующий каталог — пустая строка."""
    long_bullet = "- " + "очень длинное отклонённое замечание с деталями " * 60
    _write_rev(tmp_path, "rev001_plan.md", f"**Отклонено:**\n{long_bullet}\n\n## 2. x\n")
    out = duo.DuoReviewer.collect_rejected(tmp_path)
    assert out.endswith("…(обрезано)")
    assert len(out) <= 1500 + len("…(обрезано)")
    assert duo.DuoReviewer.collect_rejected(tmp_path / "no_such_dir") == ""


# --- П3: блок анти-повторов в R1-промпте -------------------------------------


class FakeTransport:
    def __init__(self):
        self.calls: list[str] = []

    def call(self, prompt: str, *, mode: str, timeout: int, cwd: Path | None = None, **_kw):
        self.calls.append(prompt)
        return "ответ", "sid-1", 0.0


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


def test_p3_round1_prompt_contains_antirepeat_block(tmp_path: Path):
    """Блок с директивой (и клапаном симметрии) — только при непустом rejected."""
    cfg = {"timeouts": {"reviewer_sec": 5}, "council_agents_dir": _fake_agents_dir(tmp_path)}
    reviewer = duo.DuoReviewer(cfg, FakeTransport(), tmp_path, tmp_path)
    member = duo.load_member(cfg, "tester")

    reviewer._round1(member, "pipeline/x/plan_v1.md", "plan", "текст задачи",
                     rejected="[rev001_plan] требование про альтернативу")
    prompt = reviewer.t.calls[0]
    assert "отклонённые председателем" in prompt
    assert "[rev001_plan] требование про альтернативу" in prompt
    assert "не поднимай эти замечания повторно без НОВЫХ фактов" in prompt
    assert "само отклонение было ошибочным" in prompt  # клапан (Н3 rev001)

    reviewer._round1(member, "pipeline/x/plan_v1.md", "plan", "текст задачи")
    assert "отклонённые председателем" not in reviewer.t.calls[1]


# --- П4: сводный статус -------------------------------------------------------


def test_p4_collect_statuses_order_fields_and_skips(tmp_path: Path, capsys):
    """Сортировка по mtime state.json по убыванию; битый JSON и каталоги без
    state.json пропускаются со строкой в stderr; поля и итоговая строка верны."""

    def mk(task: str, phase: str, spent: float, verdicts: int, mtime: float) -> None:
        d = tmp_path / task
        d.mkdir(parents=True, exist_ok=True)
        st = {
            "task": task, "phase": phase,
            "plan_version": 1, "plan_iteration": 2,
            "impl_version": 0, "impl_iteration": 0,
            "spent_usd": spent,
            "verdicts": [{"rev": f"rev{i:03d}"} for i in range(verdicts)],
        }
        f = d / "state.json"
        f.write_text(json.dumps(st), encoding="utf-8")
        os.utime(f, (mtime, mtime))

    mk("old", "done", 10.0, 3, mtime=1000)
    mk("mid", "impl", 5.5, 2, mtime=2000)
    mk("new", "plan", 1.0, 1, mtime=3000)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "state.json").write_text("{битый json", encoding="utf-8")
    (tmp_path / "nostate").mkdir()

    rows = duo.collect_statuses(tmp_path)
    assert [r["task"] for r in rows] == ["new", "mid", "old"]
    assert rows[0]["phase"] == "plan" and rows[0]["verdicts"] == 1
    assert rows[2]["spent_usd"] == 10.0
    assert "broken" in capsys.readouterr().err

    table = duo.format_status_table(rows)
    assert "new" in table and "impl" in table and "v1/i2" in table
    assert "всего: 3 задач, $16.50" in table
