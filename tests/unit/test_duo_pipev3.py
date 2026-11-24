"""pipev3: юниты P0 (метр per-call), P1 (A3-lite контекст-пак + read-манифест
+ телеметрия), P1b (граница фильтра артефактов), P2 (B1 worktree-изоляция).

Паттерн test_duo_pipev1/v2.py: оркестратор грузится importlib-ом (модуль без
побочных эффектов), git-песочницы — в tmp_path (изоляция tests/conftest.py
не обходится). Реальные repo/ и pipeline/ не читаются; живой transport
заменяется скриптованным.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"
PROVISION_PATH = REPO_ROOT / "provision_worktree.py"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_pipev3_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()


def _git(repo: Path, *args: str) -> None:
    rc, out = duo.sh(["git", *args], cwd=repo)
    assert rc == 0, f"git {args} failed: {out}"


def _git_out(repo: Path, *args: str) -> str:
    rc, out = duo.sh(["git", *args], cwd=repo)
    assert rc == 0, out
    return out


def _init_repo(tmp_path: Path) -> Path:
    """Песочница: .gitignore с /.worktrees/ в базовом коммите — как в проде
    (иначе worktree-файлы становятся untracked основного дерева); локальная
    git-идентичность — чтобы commit_group работал без глобального config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def _head(repo: Path) -> str:
    return _git_out(repo, "rev-parse", "HEAD").strip()


# --- стаб-транспорт и персоны -------------------------------------------------


def _member_response(n: str) -> str:
    return (f"анализ {n}\n\nSTANCE: revise\nCONFIDENCE: high\n\n"
            '```json\n{"reads": [{"path": "a.py", "touched": true, "why": "проверка диффа"}]}\n```\n')


CHAIR_REPORT = """# rev — ревью тестовое

Дата, состав, модель.

## 0. Вердикт
Доработать: есть замечания.

## 1. Замечания
1. Н1 тестовое (файл:1) — существенное — поправить.

## 2. Что проверено и подтвердилось
- ключевые утверждения проверены по коду.

## 3. Сводка для исполнителя
- поправить Н1.

VERDICT: REVISE
"""


class ScriptedTransport:
    """Ответы/стоимости по порядку вызовов; журнал промптов.

    Дефолтный ответ (когда список responses исчерпан) выбирается по промпту:
    председателю — валидный отчёт, участнику — анализ со STANCE и манифестом
    (иначе председатель уходит в ретраи и съедает лишние cost-слоты).
    """

    def __init__(self, responses: list[str] | None = None, costs: list[float] | None = None):
        self.responses = list(responses or [])
        self.costs = list(costs or [])
        self.calls: list[str] = []

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        self.calls.append(prompt)
        if self.responses:
            resp = self.responses.pop(0)
        elif "Председатель консилиума" in prompt:
            resp = CHAIR_REPORT
        else:
            resp = _member_response("default")
        # pipev4: эмуляция побочного эффекта исполнителя — файл артефакта,
        # упомянутый в промпте, реально создаётся (guard П3 требует наличия)
        if mode == "full":
            m = re.search(r"(pipeline/[\w/]+/(?:plan_v\d+|impl_report_v\d+)\.md)", prompt)
            if m and cwd:
                f = Path(cwd) / m.group(1)
                if not f.exists():
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(f"# stub {m.group(1)}\n\nсоздан FileWritingTransport\n", encoding="utf-8")
        cost = self.costs.pop(0) if self.costs else 0.0
        return resp, None, cost


def _agents_dir(tmp_path: Path, names: tuple[str, str] = ("alpha", "beta")) -> str:
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    for n in names:
        (agents / f"council-{n}.md").write_text(
            f"## Identity\nПерсона {n}.\n\n"
            "## Output Format (Standalone)\n- формат R1.\n\n"
            "## Output Format (Council Round 2)\n- формат R2.\n",
            encoding="utf-8",
        )
    return str(agents)


def _cfg(tmp_path: Path, repo: Path, **over) -> dict:
    cfg = {
        "repo": str(repo),
        "artifacts_dir": "pipeline",
        "claude_config_dir": str(tmp_path / "claude-cfg"),
        "model": "test",
        "council_agents_dir": _agents_dir(tmp_path),
        "duo": {"members_plan": ["alpha", "beta"], "members_impl": ["alpha", "beta"], "rounds": 3},
        "limits": {"max_plan_iterations": 4, "max_impl_iterations": 4, "budget_usd": 100.0},
        "timeouts": {"executor_sec": 5, "reviewer_sec": 5, "chairman_sec": 5, "ping_sec": 5},
        "git": {"auto_branch": True, "branch_prefix": "duo/", "isolation": "shared",
                "worktrees_dir": ".worktrees"},
    }
    cfg.update(over)
    return cfg


def _review(tmp_path: Path, repo: Path, cfg: dict, transport: ScriptedTransport,
            task_dir: Path | None = None, phase: str = "plan",
            artifact_rel: str = "pipeline/demo/plan_v1.md"):
    """Полный дуо-цикл на стабах; task_dir вне репо (гварды не чистат транскрипты).

    pipev4 П3: guard артефакта — файл обязан существовать в песочнице
    (ревью несуществующего артефакта теперь RuntimeError, а не молчаливый
    проход). Создаём минимальный артефакт, если тест его не подготовил.
    """
    td = task_dir or (tmp_path / "td")
    td.mkdir(exist_ok=True)
    for tok in artifact_rel.split():
        tok = tok.strip().strip('`').rstrip('.,;:')
        if tok.endswith(".md") or tok.endswith(".patch"):
            f = repo / tok
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("stub-артефакт (pipev4 guard)\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, transport, td, repo)
    return reviewer.review(
        artifact_rel=artifact_rel, phase=phase, task_text="текст задачи",
        members=["alpha", "beta"], rounds=3,
        rev_file=td / "rev001_plan.md", guard=duo.GitGuard(repo),
        artifacts_rel="pipeline/demo",
    )


def _full_cycle_costs(n_calls: int = 7, base: float = 0.10) -> list[float]:
    return [round(base + i * 0.01, 4) for i in range(n_calls)]


# --- P0: метр per-call ---------------------------------------------------------


def test_meter_per_call_two_sequential_reviews(tmp_path: Path):
    repo = _init_repo(tmp_path)
    costs = _full_cycle_costs(14, 0.10)
    t = ScriptedTransport(costs=costs)
    cfg = _cfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir()
    # pipev4 П3: артефакты обязаны существовать (guard ревью)
    for n in ("plan_v1.md", "plan_v2.md"):
        f = repo / "pipeline" / "demo" / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("stub\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    v1, c1, tel1 = reviewer.review(
        artifact_rel="pipeline/demo/plan_v1.md", phase="plan", task_text="задача",
        members=["alpha", "beta"], rounds=3,
        rev_file=td / "rev001_plan.md", guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo",
    )
    v2, c2, tel2 = reviewer.review(
        artifact_rel="pipeline/demo/plan_v2.md", phase="plan", task_text="задача",
        members=["alpha", "beta"], rounds=3,
        rev_file=td / "rev002_plan.md", guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo",
    )
    assert v1 == "REVISE" and v2 == "REVISE"
    # per-call: каждый review() возвращает ТОЛЬКО стоимость своего цикла
    assert c1 == pytest.approx(sum(costs[:7]), abs=1e-9)
    assert c2 == pytest.approx(sum(costs[7:]), abs=1e-9)
    # кумулятив продолжает расти корректно (для сводок), call_cost — только последний
    assert reviewer.spent == pytest.approx(c1 + c2, abs=1e-9)
    assert reviewer.call_cost == pytest.approx(c2, abs=1e-9)
    assert tel1["cost_usd"] == round(c1, 4) and tel2["cost_usd"] == round(c2, 4)


def test_meter_chairman_retries_counted_once(tmp_path: Path):
    repo = _init_repo(tmp_path)
    # 6 ответов участников + вырожденный председатель + валидный председатель
    responses = [_member_response(f"m{i}") for i in range(6)] + ["мусор", CHAIR_REPORT]
    costs = [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.20, 0.25]
    t = ScriptedTransport(responses, costs)
    cfg = _cfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir()
    # pipev4 П3: артефакт обязан существовать (guard ревью)
    f = repo / "pipeline" / "demo" / "plan_v1.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("stub\n", encoding="utf-8")
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    verdict, cost, telemetry = reviewer.review(
        artifact_rel="pipeline/demo/plan_v1.md", phase="plan", task_text="задача",
        members=["alpha", "beta"], rounds=3,
        rev_file=td / "rev001_plan.md", guard=duo.GitGuard(repo), artifacts_rel="pipeline/demo",
    )
    # ретрай председателя входит ОДИН раз (в total _chairman_with_retry)
    assert verdict == "REVISE"
    assert cost == pytest.approx(sum(costs), abs=1e-9)
    assert reviewer.spent == pytest.approx(cost, abs=1e-9)
    assert telemetry["cost_usd"] == round(cost, 4)
    assert (td / "transcripts").exists()
    names = [f.name for f in (td / "transcripts").glob("*chairman*")]
    assert any("chairman_attempt1" in n for n in names)


# --- P1: build_context_pack ----------------------------------------------------


def test_pack_live_full_texts_and_footer(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("A0", encoding="utf-8")
    (repo / "b.py").write_text("B0", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _head(repo)
    (repo / "a.py").write_text("A1-ПОСЛЕ", encoding="utf-8")           # M
    (repo / "newdir").mkdir()
    (repo / "newdir" / "c.py").write_text("C1", encoding="utf-8")      # ??
    (repo / "pipeline" / "demo").mkdir(parents=True)
    (repo / "pipeline" / "demo" / "artifact.md").write_text("арт", encoding="utf-8")
    pack = duo.build_context_pack(repo, base, "impl", "pipeline/demo")
    # ПОСЛЕ-тексты рабочего дерева, целиком
    assert "### a.py" in pack and "A1-ПОСЛЕ" in pack
    assert "### newdir/c.py" in pack and "C1" in pack
    # нетронутый и артефактный — вне пака
    assert "### b.py" not in pack
    assert "artifact.md" not in pack
    assert "Вошло полным текстом: a.py, newdir/c.py" in pack
    assert "OVERFLOW: no" in pack and duo.pack_overflow(pack) is False


def test_pack_overflow_marker_diff_ladder_and_flag(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "small.py").write_text("S0", encoding="utf-8")
    (repo / "big.py").write_text("B" * 5000, encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _head(repo)
    (repo / "small.py").write_text("S2", encoding="utf-8")
    (repo / "big.py").write_text("B" * 5000 + "tail", encoding="utf-8")
    pack = duo.build_context_pack(repo, base, "impl", "pipeline/demo", max_chars=200)
    assert "### small.py" in pack and "S2" in pack          # малый влез целиком
    # большой — файл ЦЕЛИКОМ не влез: честная пометка, не молчаливое усечение
    assert "> big.py" in pack and "не влез в остаток капа" in pack
    assert "Пометкой «не влез в кап — прочитай в репо»: big.py" in pack
    # дифф-контекст деградирует первым (-U30→-U10→-U0), здесь не влез и -U0 — опущен
    assert "не влезли даже с -U0" in pack
    assert "OVERFLOW: yes" in pack and duo.pack_overflow(pack) is True


def test_pack_overflow_diff_fits_with_degraded_context(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "small.py").write_text("S0", encoding="utf-8")
    (repo / "big.py").write_text("\n".join(f"line{i}" for i in range(200)), encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _head(repo)
    (repo / "small.py").write_text("S2", encoding="utf-8")
    (repo / "big.py").write_text("\n".join(f"line{i}" for i in range(199)) + "\nCHANGED",
                                  encoding="utf-8")
    pack = duo.build_context_pack(repo, base, "impl", "pipeline/demo", max_chars=700)
    assert "### small.py" in pack
    assert "> big.py" in pack                       # полный текст (~1.6К) не влез в остаток
    assert "CHANGED" in pack                        # но дифф файла вошёл (с каким-то -U)
    assert "unified, -U" in pack
    assert "OVERFLOW: yes" in pack and duo.pack_overflow(pack) is True


def test_pack_frozen_patch_before_texts_and_new_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("ДО-ТЕКСТ-A", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = _head(repo)
    (repo / "a.py").write_text("ПОСЛЕ-A", encoding="utf-8")
    (repo / "d.py").write_text("НОВЫЙ", encoding="utf-8")
    _git(repo, "add", "-A")
    patch = _git_out(repo, "diff", "--cached")
    _git(repo, "reset", "-q")
    pack = duo.build_context_pack(repo, base, "impl", "pipeline/demo", diff_text=patch)
    assert "### a.py" in pack and "ДО-ТЕКСТ-A" in pack      # ДО-тексты через git show base:
    assert "+ПОСЛЕ-A" in pack and "+НОВЫЙ" in pack          # диффы — из патча
    assert "> d.py" in pack and "содержимое не доступно паку" in pack
    assert "Нет в источнике текстов (новые): d.py" in pack
    assert "из замороженного патча" in pack
    assert "OVERFLOW: no" in pack and duo.pack_overflow(pack) is False


def test_pack_plan_phase_inline(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "pipeline" / "demo").mkdir(parents=True)
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("# план v1\n\nсуть плана", encoding="utf-8")
    pack = duo.build_context_pack(repo, "", "plan", "pipeline/demo/plan_v1.md")
    assert "Объект ревью инлайн: `pipeline/demo/plan_v1.md`" in pack
    assert "суть плана" in pack
    assert "OVERFLOW: no" in pack


def test_pack_live_deleted_file_gets_before_text_not_novel(tmp_path: Path):
    # Н3 rev002_impl: удалённый итерацией файл — ДО-текст из базы + пометка
    # «удалён», а не ложное «содержимое не доступно паку»/«новые»
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("ДО-УДАЛЁН", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _head(repo)
    (repo / "a.py").unlink()
    (repo / "b.py").write_text("B-ЖИВОЙ", encoding="utf-8")
    pack = duo.build_context_pack(repo, base, "impl", "pipeline/demo")
    assert "### a.py (ДО-текст; файл удалён итерацией" in pack
    assert "ДО-УДАЛЁН" in pack
    assert "Удалены итерацией (ДО-текст): a.py" in pack
    assert "содержимое не доступно паку" not in pack          # не «новый»
    assert "Нет в источнике текстов (новые): —" in pack
    assert "OVERFLOW: no" in pack and duo.pack_overflow(pack) is False


# --- P1: parse_read_manifest и порядок блоков ----------------------------------


def test_parse_read_manifest(tmp_path: Path):
    good = _member_response("x")
    assert duo.parse_read_manifest(good) == {"reads": [{"path": "a.py", "touched": True, "why": "проверка диффа"}]}
    # json-блок без ключа reads — не манифест
    assert duo.parse_read_manifest("```json\n{\"x\": 1}\n```") is None
    assert duo.parse_read_manifest("мусор без блоков") is None
    # битный JSON — None (телеметрия best-effort, ревью не ломается)
    assert duo.parse_read_manifest("```json\n{broken\n```") is None
    # последний блок с reads выигрывает
    two = ('```json\n{"reads": []}\n```\nтекст\n```json\n{"reads": [{"path": "b.py", "touched": false, "why": "w"}]}\n```')
    assert duo.parse_read_manifest(two)["reads"][0]["path"] == "b.py"


def test_round1_legacy_prompt_byte_exact_and_hybrid(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = {"timeouts": {"reviewer_sec": 5}}
    member = {"name": "tester", "identity": "## Identity\nИдентичность.", "fmt_standalone": "- формат R1."}
    td = tmp_path / "td"
    td.mkdir()
    t = ScriptedTransport()
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    reviewer._round1(member, "pipeline/demo/plan_v1.md", "plan", "текст задачи")
    golden = f"""Ты — участник структурированного дуо-консилиума, выступающего надсмотрщиком над работой исполнителя. Работай строго в своей роли.

## Identity
Идентичность.

{duo.OVERSEER_RULES}

Объект ревью: `pipeline/demo/plan_v1.md` (фаза: plan). Прочитай его целиком.
Исходная задача (контекст):
\"\"\"текст задачи\"\"\"

Чек-лист пре-регистрации плана (проверь ДО вердикта): если план содержит измерения/метрики/сравнения «до-после» — закреплены ли ДО прогона: (1) единица счёта и знаменатель каждой метрики; (2) инварианты данных/корпуса; (3) пороги решений (что считается успехом/провалом); (4) правила разбора спорных случаев. Отсутствие пре-регистрации при наличии измерений — существенное замечание (после прогона это станет post-hoc рационализацией). Плана без измерений чек-лист не касается.

Сначала в ОДНОМ предложении переформулируй проблему через свою линзу.
Затем — самостоятельный слепой анализ (ты не знаешь, что скажет оппонент) по своему формату:

## Output Format (Standalone)
- формат R1.

В самом конце обязателен ровно один блок:
STANCE: accept | revise | abstain
CONFIDENCE: high | med | low
(accept = артефакт можно принимать без правок; revise = есть замечания, требующие доработки.)

Лимит: 400 слов."""
    assert t.calls[0] == golden                       # legacy байт-в-байт (pipev4: + чек-лист пре-регистрации на фазе plan)
    assert "Контекст-пак" not in t.calls[0]
    # hybrid: пак + право дочитывать + манифест ПОСЛЕ STANCE-блока
    pack = "## Контекст-пак итерации (A3-lite)\n\n### a.py\n\n````\nA1\n````\n"
    reviewer._round1(member, "pipeline/demo/plan_v1.md", "plan", "текст задачи", "", pack)
    hp = t.calls[1]
    assert pack in hp and "Право дочитывать репозиторий полностью сохраняется" in hp
    assert hp.index("STANCE: accept") < hp.index('"reads"')   # порядок: STANCE → ```json
    assert hp.index('"reads"') < hp.index("Лимит: 400 слов")


def test_round2_partner_manifest_block(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = {"timeouts": {"reviewer_sec": 5}}
    member = {"name": "alpha", "identity": "ID", "fmt_round2": "- формат R2."}
    td = tmp_path / "td"
    td.mkdir()
    t = ScriptedTransport()
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    reviewer._round2(member, "мой R1", "beta", "чужой R1")
    assert "Манифест чтений оппонента" not in t.calls[0]     # legacy без манифеста
    reviewer._round2(member, "мой R1", "beta", "чужой R1", {"reads": [{"path": "a.py", "touched": True, "why": "w"}]})
    assert "Манифест чтений оппонента за R1" in t.calls[1]
    assert '"touched": true' in t.calls[1]
    assert "что важное он НЕ прочитал" in t.calls[1]


def test_chairman_prompt_hybrid_block(tmp_path: Path):
    cfg = {"council_agents_dir": _agents_dir(tmp_path)}
    td = tmp_path / "td"
    td.mkdir()
    reviewer = duo.DuoReviewer(cfg, ScriptedTransport(), td, td)
    m = duo.load_member(cfg, "alpha")
    kw = dict(artifact_rel="pipeline/demo/impl_report_v1.md", phase="impl", task_text="задача",
              A=m, B=m, r1a="a1", r1b="b1", r2a="a2", r2b="b2", r3a="a3", r3b="b3",
              sa="revise", sb="revise")
    legacy = reviewer._build_chairman_prompt(**kw)
    assert "Контекст-пак" not in legacy and "Манифест чтений" not in legacy
    hybrid = reviewer._build_chairman_prompt(
        **kw, hybrid={"pack": "## Контекст-пак итерации (A3-lite)\n...",
                      "manifest_a": {"reads": []}, "manifest_b": None})
    assert "## Контекст-пак итерации (A3-lite)" in hybrid
    assert "Манифест чтений R1 — alpha" in hybrid
    assert "(манифест не распознан)" in hybrid          # manifest_b=None — пометка, не падение
    assert "(источник: R1 | R2/R3)" in hybrid


# --- P1: телеметрия — review() возвращает, Pipeline пишет в state ---------------


def test_review_hybrid_telemetry_and_pipeline_state_write(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "pipeline" / "demo").mkdir(parents=True)
    (repo / "pipeline" / "demo" / "task.md").write_text("# задача demo", encoding="utf-8")
    (repo / "pipeline" / "demo" / "plan_v1.md").write_text("# план v1\n\nсуть", encoding="utf-8")
    duo.save_json(repo / "pipeline" / "demo" / "state.json", {
        "task": "demo", "phase": "plan", "spent_usd": 0.0,
        "artifacts_rel": "pipeline/demo", "verdicts": [],
    })
    cfg = _cfg(tmp_path, repo, review={"context": "hybrid", "pack_max_chars": 60000})
    costs = _full_cycle_costs(7, 0.10)
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport(costs=costs)
    verdict = p.review_once("pipeline/demo/plan_v1.md", phase="plan")
    assert verdict == "REVISE"
    st = duo.load_json(p.state_file)
    # P0: per-call прибавлен ОДИН раз
    assert st["spent_usd"] == round(sum(costs), 4)
    # P1: телеметрия обеих сторон контракта — в state
    tel = st["review_telemetry"]
    assert len(tel) == 1
    assert tel[0]["phase"] == "plan" and tel[0]["context_mode"] == "hybrid"
    assert tel[0]["pack_chars"] > 0 and tel[0]["pack_overflow"] is False
    assert tel[0]["read_manifests"]["alpha"]["reads"][0]["path"] == "a.py"
    assert tel[0]["read_manifests"]["beta"]["reads"][0]["path"] == "a.py"
    assert tel[0]["cost_usd"] == round(sum(costs), 4)


def test_review_legacy_no_pack_no_manifests(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)     # без review-ключа: дефолт legacy
    t = ScriptedTransport(costs=_full_cycle_costs())
    verdict, cost, tel = _review(tmp_path, repo, cfg, t)
    assert verdict == "REVISE"
    assert tel["context_mode"] == "legacy" and tel["pack_chars"] == 0
    assert tel["read_manifests"] == {}
    assert all("Контекст-пак" not in p for p in t.calls)


def test_review_once_passes_impl_base_head_to_live_pack(tmp_path: Path):
    # Н4 rev002_impl: review_once пробрасывает impl_base_head — шапка live-пака
    # несёт базу диффа (иначе git diff "" падал, диффы «опущены», база '?')
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("A0", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    head = _head(repo)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["impl_base_head"] = head
    duo.save_json(td / "state.json", st)
    (repo / "a.py").write_text("A1", encoding="utf-8")
    # pipev4 П3: guard артефакта — impl_report обязан существовать до ревью
    impl_md = repo / "pipeline" / "demo" / "impl_report_v1.md"
    impl_md.parent.mkdir(parents=True, exist_ok=True)
    impl_md.write_text("stub impl report (pipev4 guard)\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo, review={"context": "hybrid", "pack_max_chars": 60000})
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    verdict = p.review_once("pipeline/demo/impl_report_v1.md", phase="impl")
    assert verdict == "REVISE"
    assert f"живое дерево от {head[:10]}" in t.calls[0]      # база в шапке пака R1
    tel = duo.load_json(p.state_file)["review_telemetry"][-1]
    assert tel["phase"] == "impl" and tel["pack_chars"] > 0 and tel["pack_overflow"] is False


# --- P1b: граница фильтра артефактов -------------------------------------------


def test_is_artifact_path():
    assert duo.is_artifact_path("pipeline/demo", "pipeline/demo")
    assert duo.is_artifact_path("pipeline/demo/x.md", "pipeline/demo")
    assert duo.is_artifact_path("pipeline/demo/", "pipeline/demo")      # запись ?? каталога
    assert not duo.is_artifact_path("pipeline/demo2", "pipeline/demo")  # чужая задача
    assert not duo.is_artifact_path("pipeline/demo2/x.md", "pipeline/demo")


def test_untracked_list_excludes_task_dir_entry(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pipeline")
    (repo / "pipeline" / "demo").mkdir()
    (repo / "pipeline" / "demo" / "artifact.md").write_text("арт", encoding="utf-8")
    (repo / "stray.txt").write_text("чужой", encoding="utf-8")
    g = duo.GitGuard(repo)
    out = tmp_path / "untracked.txt"
    g.untracked_list("", out, "pipeline/demo")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines == ["stray.txt"]                 # «?? pipeline/demo/» вырезана целиком


def test_collect_commit_groups_excludes_artifact_dir_entry(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pipeline")
    (repo / "README.md").write_text("modified", encoding="utf-8")
    (repo / "pipeline" / "demo").symlink_to(tmp_path / "td_real", target_is_directory=True)
    td = tmp_path / "td"
    td.mkdir()
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | README.md | fix: правка | п.1 |\n", encoding="utf-8")
    snap = duo.collect_commit_groups(repo, td, 1, "pipeline/demo")
    # симлинк артефактов — запись «?? pipeline/demo» — вне coverage, но показан отдельно
    assert snap["uncovered"] == [] and snap["overlaps"] == []
    assert snap["artifacts"] == ["pipeline/demo"]
    assert sorted(snap["changed"]) == ["README.md", "pipeline/demo"]


def test_cmd_commit_by_plan_scope_excludes_artifact_entry(tmp_path: Path, capsys):
    # Н2 rev002_impl: локальный scope cmd_commit_by_plan — та же граница
    # is_artifact_path; запись «?? pipeline/<task>» (симлинк артефактов) не
    # должна попадать в файлы группы даже под широким глобом pipeline/*
    import types
    repo = _init_repo(tmp_path)
    # артефакты задачи — вне репо; pipeline/demo в репо — СИМЛИНК на них
    # (как в worktree-режиме): git status даёт запись «?? pipeline/demo»
    td = tmp_path / "td_real"
    td.mkdir()
    duo.save_json(td / "state.json", {
        "task": "demo", "phase": "impl", "impl_version": 1, "spent_usd": 0.0,
        "artifacts_rel": "pipeline/demo", "verdicts": [],
    })
    (repo / "pipeline").mkdir(exist_ok=True)
    (repo / "pipeline" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", "pipeline/.keep")
    _git(repo, "commit", "-qm", "pipeline")
    (repo / "pipeline" / "demo").symlink_to(td, target_is_directory=True)
    (repo / "README.md").write_text("modified", encoding="utf-8")
    (td / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | README.md, pipeline/* | fix: правка | п.1 |\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo)
    with pytest.raises(SystemExit) as ei:
        duo.cmd_commit_by_plan(cfg, types.SimpleNamespace(task="demo", version=1, dry_run=True))
    assert ei.value.code in (0, 1)
    out = capsys.readouterr().out
    assert "    README.md" in out                    # файл группы показан
    assert "    pipeline/demo" not in out            # симлинк — НЕ файл группы
    assert "['pipeline/demo']" in out                # ...но показан артефактом


# --- P2: worktree-изоляция ------------------------------------------------------


def _mk_task(repo: Path, task: str = "demo") -> Path:
    td = repo / "pipeline" / task
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.md").write_text(f"# задача {task}", encoding="utf-8")
    duo.save_json(td / "state.json", {
        "task": task, "phase": "plan", "spent_usd": 0.0,
        "artifacts_rel": f"pipeline/{task}", "verdicts": [],
    })
    return td


def test_worktree_create_reuse_pinned_base_and_isolation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    (repo / "pipeline" / ".keep").write_text("", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "pipeline/.keep")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pipeline")
    base = _head(repo)
    cfg = _cfg(tmp_path, repo)
    cfg["git"]["isolation"] = "worktree"
    p = duo.Pipeline(cfg, "demo")
    wt = repo / ".worktrees" / "demo"
    assert p.work_path == wt.resolve() and p.isolation == "worktree"

    st = p.load_state()
    p._ensure_worktree(st)
    assert (wt / ".git").exists()
    assert st["worktree_base"] == base                                  # база спиннена
    assert _git_out(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "duo/demo"
    assert (wt / "pipeline" / "demo").is_symlink()                      # симлинк артефактов
    assert (wt / "pipeline" / "demo" / "task.md").exists()

    # изоляция: правки серии — в worktree; основное дерево их не видит
    # (в main-дереве untracked только артефакты pipeline/demo самой задачи)
    (wt / "README.md").write_text("правка серии", encoding="utf-8")
    assert "README.md" in duo.changed_files_all(wt)
    main_changed = duo.changed_files_all(repo)
    assert "README.md" not in main_changed
    assert all(duo.is_artifact_path(p, "pipeline/demo") for p in main_changed)

    # переиспользование: основное дерево ушло вперёд — база НЕ меняется
    (repo / "main.txt").write_text("правка человека", encoding="utf-8")
    _git(repo, "add", "main.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "human")
    st2 = p.load_state()
    p._ensure_worktree(st2)
    assert st2["worktree_base"] == base
    assert _head(repo) != base


def test_worktree_commit_group_commits_in_worktree(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    cfg["git"]["isolation"] = "worktree"
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    p._ensure_worktree(st)
    wt = p.work_path
    (wt / "README.md").write_text("v2", encoding="utf-8")
    # P1b: симлинк артефактов («?? pipeline/demo») не мешает coverage по worktree
    (p.task_dir / "impl_report_v1.md").write_text(
        "## commit_plan\n\n| группа | файлы (glob) | сообщение | rationale |\n|---|---|---|---|\n"
        "| G1 | README.md | feat: в worktree | п.1 |\n", encoding="utf-8")
    snap = duo.collect_commit_groups(wt, p.task_dir, 1, "pipeline/demo")
    assert snap["uncovered"] == [] and snap["overlaps"] == []
    assert snap["artifacts"] == ["pipeline/demo"]
    ok, out = duo.commit_group(wt, ["README.md"], "feat: в worktree")
    assert ok, out
    assert "feat: в worktree" in _git_out(wt, "log", "-1", "--format=%s")
    # основное дерево не тронуто: в его истории нет этого коммита
    assert "feat: в worktree" not in _git_out(repo, "log", "--format=%s", "-20")


def test_worktree_branch_in_main_tree_loud_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    _git(repo, "checkout", "-b", "duo/demo")          # ветка занята основным деревом
    cfg = _cfg(tmp_path, repo)
    cfg["git"]["isolation"] = "worktree"
    p = duo.Pipeline(cfg, "demo")
    with pytest.raises(SystemExit, match="основном дереве"):
        p._ensure_worktree(p.load_state())
    assert not (repo / ".worktrees" / "demo").exists()  # ничего не создал, не вмешивался


def test_worktree_provisioning_env_error_fail_closed(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "data").mkdir()
    (repo / "data" / "x.bin").write_text("тяжёлый ресурс", encoding="utf-8")
    _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    cfg["git"]["isolation"] = "worktree"
    cfg["git"]["worktree_links"] = ["data"]              # R4: имена — из конфига проекта
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    p._ensure_worktree(st)
    wt = p.work_path
    assert (wt / "data").is_symlink()                    # provisioning сработал
    # «ошибка окружения»: цель занята не-симлинком -> fail-closed, серия не стартует
    (wt / "data").unlink()
    (wt / "data").mkdir()
    with pytest.raises(SystemExit, match="provisioning"):
        p._ensure_worktree(p.load_state())


def test_shared_mode_is_untouched(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _cfg(tmp_path, repo)                            # isolation=shared (дефолт)
    p = duo.Pipeline(cfg, "demo")
    assert p.work_path == p.repo and p.isolation == "shared"
    assert p.guard.repo == p.repo
    p.guard.ensure_branch("demo", cfg, p.task_dir)        # legacy ensure_branch работает
    assert _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "duo/demo"
    assert not (repo / ".worktrees").exists()             # worktree-инфраструктура не создаётся


# --- P2: provision_worktree.py напрямую -----------------------------------------


def test_provision_success_idempotent_fail_closed(tmp_path: Path):
    """R4: имена ресурсов — argv скрипта (оркестратор берёт их из git.worktree_links)."""
    repo = _init_repo(tmp_path)
    (repo / "data").mkdir()
    (repo / "data" / "f.bin").write_text("x", encoding="utf-8")
    _mk_task(repo)
    _git(repo, "worktree", "add", str(repo / ".worktrees" / "demo"), "-b", "duo/demo", _head(repo))
    wt = repo / ".worktrees" / "demo"
    import subprocess
    # без имён — «нечего линковать», rc=0
    r0 = subprocess.run([sys.executable, str(PROVISION_PATH), str(repo), str(wt)],
                        capture_output=True, text=True)
    assert r0.returncode == 0 and "нечего линковать" in r0.stdout
    assert not (wt / "data").exists()
    # с именами — симлинк; повтор идемпотентен
    names = [str(repo), str(wt), "data"]
    r = subprocess.run([sys.executable, str(PROVISION_PATH), *names],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (wt / "data").is_symlink()
    # .env НЕ симлинкуется НИКОГДА — даже переданный явно (секреты; инвариант скрипта)
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")
    r2 = subprocess.run([sys.executable, str(PROVISION_PATH), str(repo), str(wt), "data", ".env"],
                        capture_output=True, text=True)
    assert r2.returncode == 0                            # идемпотентен
    assert not (wt / ".env").exists()
    # fail-closed: цель занята не-симлинком
    (wt / "models").mkdir()
    (repo / "models").mkdir()
    (repo / "models" / "m.bin").write_text("m", encoding="utf-8")
    r3 = subprocess.run([sys.executable, str(PROVISION_PATH), str(repo), str(wt), "models"],
                        capture_output=True, text=True)
    assert r3.returncode != 0 and "не симлинк" in (r3.stderr + r3.stdout)
    # не-worktree — отказ
    fake = tmp_path / "fake_wt"
    fake.mkdir()
    r4 = subprocess.run([sys.executable, str(PROVISION_PATH), str(repo), str(fake), "data"],
                        capture_output=True, text=True)
    assert r4.returncode != 0
    # неверный argv (без имён и без worktree) — usage rc=2
    r5 = subprocess.run([sys.executable, str(PROVISION_PATH), str(repo)],
                        capture_output=True, text=True)
    assert r5.returncode == 2 and "использование" in r5.stderr


# --- pipev4: П3 guard артефакта, П2 prereg-блок, П5 эскалация ------------------


def test_pipev4_artifact_guard_missing_file(tmp_path: Path):
    """Ревью несуществующего артефакта — RuntimeError ДО траты токенов."""
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir()
    t = ScriptedTransport()
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    with pytest.raises(RuntimeError, match="не найден"):
        reviewer.review(
            artifact_rel="pipeline/demo/plan_v1.md", phase="plan",
            task_text="текст задачи", members=["alpha", "beta"], rounds=3,
            rev_file=td / "rev001_plan.md", guard=duo.GitGuard(repo),
            artifacts_rel="pipeline/demo",
        )
    assert t.calls == []          # ни одного вызова модели — guard сработал первым


def test_pipev4_artifact_guard_custom_phase_skipped(tmp_path: Path):
    """phase=custom — свободное описание артефакта, guard не мешает."""
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir()
    t = ScriptedTransport(costs=_full_cycle_costs())
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    verdict, _, _ = reviewer.review(
        artifact_rel="свободное описание объекта ревью", phase="custom",
        task_text="текст задачи", members=["alpha", "beta"], rounds=3,
        rev_file=td / "rev001_custom.md", guard=duo.GitGuard(repo),
        artifacts_rel="pipeline/demo",
    )
    assert verdict == "REVISE" and len(t.calls) > 0


def test_pipev4_prereg_block_plan_yes_impl_no(tmp_path: Path):
    """Чек-лист пре-регистрации — только в plan-ревью R1, не в impl/custom."""
    repo = _init_repo(tmp_path)
    cfg = _cfg(tmp_path, repo)
    td = tmp_path / "td"
    td.mkdir()
    t = ScriptedTransport()
    reviewer = duo.DuoReviewer(cfg, t, td, repo)
    (repo / "pipeline").mkdir(exist_ok=True)
    for name in ("plan_v1.md", "impl_report_v1.md"):
        (repo / "pipeline").mkdir(exist_ok=True)
        f = repo / "pipeline" / name
        if not f.exists():
            f.write_text("x\n", encoding="utf-8")
    # dir demo для artifacts_rel-путей
    d = repo / "pipeline" / "demo"
    d.mkdir(exist_ok=True)
    (d / "plan_v1.md").write_text("x\n", encoding="utf-8")
    (d / "impl_report_v1.md").write_text("x\n", encoding="utf-8")
    reviewer._round1({"name": "a", "identity": "I", "fmt_standalone": "F"}, "pipeline/demo/plan_v1.md", "plan", "task")
    reviewer._round1({"name": "a", "identity": "I", "fmt_standalone": "F"}, "pipeline/demo/impl_report_v1.md", "impl", "task")
    assert "Чек-лист пре-регистрации" in t.calls[0]
    assert "Чек-лист пре-регистрации" not in t.calls[1]


def test_pipev4_escalation_after_n_revisions(tmp_path: Path):
    """П5: после escalation_after REVISE-итераций — needs_human + escalation.md.

    Симулируем run: plan уже написан (plan_v1.md существует), ревью дважды
    возвращает REVISE → на 2-й итерации цикл обязан эскалировать, а не
    продолжать доработки до max_plan_iterations.
    """
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n\nЕсть измерения: метрика X.\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo, limits={"max_plan_iterations": 9, "max_impl_iterations": 4,
                                        "budget_usd": 100.0, "escalation_after": 2})
    p = duo.Pipeline(cfg, "demo")
    t = ScriptedTransport()
    p.t = t
    # run(): executor-вызова не будет (plan_v1 уже есть, phase=plan, план_version
    # проставим в state сами), ревью крутится по кругу REVISE
    st = p.load_state()
    st["plan_version"] = 1
    st["plan_iteration"] = 0
    duo.save_json(p.state_file, st)
    p.run(force_continue=True)
    st2 = p.load_state()
    assert st2["phase"] == "needs_human"
    assert "эскалация" in st2.get("stop_reason", "")
    assert (td / "escalation.md").exists()
    # итерации НЕ дошли до лимита — эскалация раньше
    assert st2["plan_iteration"] == 2 < cfg["limits"]["max_plan_iterations"]


def test_pipev4_escalation_disabled_by_default(tmp_path: Path):
    """escalation_after=None (дефолт) — legacy-поведение: цикл до лимита."""
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    (td / "plan_v1.md").write_text("# план v1\n", encoding="utf-8")
    cfg = _cfg(tmp_path, repo)
    assert "escalation_after" not in cfg["limits"]      # тестовый конфиг не задаёт
    cfg["limits"]["escalation_after"] = None
    cfg["limits"]["max_plan_iterations"] = 2
    p = duo.Pipeline(cfg, "demo")
    p.t = ScriptedTransport()
    st = p.load_state()
    st["plan_version"] = 1
    duo.save_json(p.state_file, st)
    p.run(force_continue=True)
    st2 = p.load_state()
    assert st2["phase"] == "needs_human"
    assert "исчерпан лимит итераций" in st2.get("stop_reason", "")
    assert not (td / "escalation.md").exists()
