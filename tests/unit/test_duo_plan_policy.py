"""plan-policy: опциональный триаж «блокер/эррата» в фазе plan + ретрай ответа-заглушки.

Паттерн test_duo_pipev1/v2/v3.py: оркестратор грузится importlib-ом (модуль без
побочных эффектов), живой transport заменён скриптованным, всё — в tmp_path.

Что фиксируют тесты:
  * дефолт (review.plan_policy не задан / "legacy") — промпты R1 и председателя
    не меняются (текст пояснения STANCE прежний, блока политики нет);
  * review.plan_policy="triage" — блок политики только у председателя фазы plan
    (impl/custom не затронуты), пояснение STANCE у R1 согласовано с триажем;
  * ответ-заглушка ревьюера (ExitPlanMode / пусто) — ровно один ретрай с
    уточнением, первая попытка сохраняется, стоимость суммируется.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_plan_policy_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()

GOOD = "анализ по существу\n\nSTANCE: accept\nCONFIDENCE: high\n"
STUB = ("Вердикт раунда 2 сохранён в файл плана `/home/u/.claude-glm/plans/council-x-y.md`. "
        "Инструмент ExitPlanMode в данной сессии недоступен.")


class _Transport:
    """Скриптованный транспорт: ответы по порядку, стоимость 0.1/0.2/... по вызовам."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag=""):
        self.calls.append(prompt)
        resp = self.responses.pop(0) if self.responses else GOOD
        return resp, None, 0.1 * len(self.calls)


def _agents_dir(tmp_path: Path) -> str:
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    (agents / "council-alpha.md").write_text(
        "## Identity\nПерсона alpha.\n\n"
        "## Output Format (Standalone)\n- формат R1.\n\n"
        "## Output Format (Council Round 2)\n- формат R2.\n",
        encoding="utf-8",
    )
    return str(agents)


def _reviewer(tmp_path: Path, responses: list[str] | None = None, **cfg_over):
    cfg = {
        "council_agents_dir": _agents_dir(tmp_path),
        "timeouts": {"reviewer_sec": 5},
        **cfg_over,
    }
    td = tmp_path / "td"
    td.mkdir(exist_ok=True)
    t = _Transport(responses or [])
    return duo.DuoReviewer(cfg, t, td, tmp_path), t, cfg, td


def _chairman_prompt(reviewer, cfg: dict, phase: str) -> str:
    m = duo.load_member(cfg, "alpha")
    return reviewer._build_chairman_prompt(
        artifact_rel="pipeline/demo/plan_v1.md", phase=phase, task_text="задача",
        A=m, B=m, r1a="a1", r1b="b1", r2a="a2", r2b="b2", r3a="a3", r3b="b3",
        sa="revise", sb="revise",
    )


def _r1_prompt(tmp_path: Path, phase: str, **cfg_over) -> str:
    reviewer, t, cfg, _ = _reviewer(tmp_path, **cfg_over)
    member = duo.load_member(cfg, "alpha")
    reviewer._round1(member, "pipeline/demo/plan_v1.md", phase, "задача")
    return t.calls[0]


# --- дефолт: ничего не меняется --------------------------------------------------


def test_default_legacy_prompts_unchanged(tmp_path: Path):
    reviewer, _, cfg, _ = _reviewer(tmp_path)
    for phase in ("plan", "impl", "custom"):
        assert "Политика приёмки плана" not in _chairman_prompt(reviewer, cfg, phase)
    # явное "legacy" эквивалентно отсутствию ключа
    reviewer2, _, cfg2, _ = _reviewer(tmp_path, review={"plan_policy": "legacy"})
    assert "Политика приёмки плана" not in _chairman_prompt(reviewer2, cfg2, "plan")
    r1 = _r1_prompt(tmp_path, "plan")
    assert "(accept = артефакт можно принимать без правок; revise = есть замечания, требующие доработки.)" in r1
    assert "блокеров нет" not in r1


def test_review_cfg_not_a_dict_is_safe(tmp_path: Path):
    reviewer, _, cfg, _ = _reviewer(tmp_path, review=None)
    assert reviewer._plan_triage_on("plan") is False
    assert "Политика приёмки плана" not in _chairman_prompt(reviewer, cfg, "plan")


# --- triage: только председатель фазы plan -----------------------------------------


def test_triage_chairman_block_only_for_plan(tmp_path: Path):
    reviewer, _, cfg, _ = _reviewer(tmp_path, review={"plan_policy": "triage"})
    plan = _chairman_prompt(reviewer, cfg, "plan")
    assert "Политика приёмки плана" in plan
    assert "БЛОКЕР / ЭРРАТА / КОСМЕТИКА" in plan
    assert "## Эрраты к плану" in plan
    assert "commit_plan" not in plan
    # замороженный формат отчёта цел; блок — после VERDICT и до «Правила:»
    assert "## 0" in plan and "## 1" in plan
    assert plan.index("VERDICT: REVISE") < plan.index("Политика приёмки плана") < plan.index("Правила: не выдумывай")
    for phase in ("impl", "custom"):
        other = _chairman_prompt(reviewer, cfg, phase)
        assert "Политика приёмки плана" not in other


def test_triage_round1_stance_hint_only_for_plan(tmp_path: Path):
    plan = _r1_prompt(tmp_path, "plan", review={"plan_policy": "triage"})
    assert "блокеров нет" in plan and "без правок" not in plan
    impl = _r1_prompt(tmp_path, "impl", review={"plan_policy": "triage"})
    assert "(accept = артефакт можно принимать без правок;" in impl
    assert "блокеров нет" not in impl


# --- ретрай ответа-заглушки ----------------------------------------------------------


def test_normal_response_no_retry(tmp_path: Path):
    reviewer, t, cfg, _ = _reviewer(tmp_path, responses=[GOOD])
    member = duo.load_member(cfg, "alpha")
    out = reviewer._round2(member, "мой R1", "beta", "чужой R1")
    assert out == GOOD and len(t.calls) == 1
    assert reviewer.call_cost == 0.1


def test_stub_response_retried_once_and_cost_summed(tmp_path: Path):
    reviewer, t, cfg, td = _reviewer(tmp_path, responses=[STUB, GOOD])
    member = duo.load_member(cfg, "alpha")
    out = reviewer._round2(member, "мой R1", "beta", "чужой R1")
    assert out == GOOD
    assert len(t.calls) == 2
    assert t.calls[1].startswith(t.calls[0])
    assert t.calls[1].endswith(duo.REVIEWER_STUB_RETRY_SUFFIX)
    assert abs(reviewer.call_cost - (0.1 + 0.2)) < 1e-9
    names = sorted(p.name for p in (td / "transcripts").iterdir())
    assert any(n.endswith("_r2_alpha_attempt1.md") for n in names)
    assert any(n.endswith("_r2_alpha.md") for n in names)


def test_empty_response_retried_and_no_cascade(tmp_path: Path):
    reviewer, t, cfg, _ = _reviewer(tmp_path, responses=["  \n", STUB, GOOD])
    member = duo.load_member(cfg, "alpha")
    out = reviewer._round3(member, "мой R2")
    assert len(t.calls) == 2          # один ретрай, без каскада
    assert out == STUB                # второй ответ — заглушка: возвращается как есть
    assert duo.DuoReviewer.parse_stance(out) == "abstain"


def test_stub_signature_is_narrow():
    stub = duo.DuoReviewer._is_stub_response
    assert stub("") and stub("   ") and stub(STUB)
    assert stub("см. план в /home/u/.claude-glm/plans/x-y-z.md")
    assert not stub(GOOD)
    assert not stub("обычный анализ: в плане упомянут каталог plans/ без префикса .claude")


# --- политика v2: эрраты-переопределения, тренд размера, правила исполнителя ---------


def test_triage_block_has_override_errata_rules(tmp_path: Path):
    reviewer, _, cfg, _ = _reviewer(tmp_path, review={"plan_policy": "triage"})
    plan = _chairman_prompt(reviewer, cfg, "plan")
    # приоритет над определениями «блокера» из текста задачи (аддендумы)
    assert "включая аддендумы" in plan
    # тест «эрраты-переопределения» и директивность рекомендации председателя
    assert "эрраты-переопределения" in plan and "Заменяет: §" in plan
    assert "рекомендация председателя — директива" in plan
    # дефект платформы вне фичи и антитредмилл
    assert "Дефект платформы вне фичи" in plan and "К владельцу" in plan
    assert "второй подряд блокер допустим только по п.3(а)" in plan


def test_plan_size_trend_computation(tmp_path: Path):
    d = tmp_path / "pipeline" / "demo"
    d.mkdir(parents=True)
    for v, n in ((1, 100), (2, 80), (3, 90), (4, 99)):
        (d / f"plan_v{v}.md").write_text("x\n" * n, encoding="utf-8")
    trend = duo.plan_size_trend(tmp_path, "pipeline/demo/plan_v4.md")
    assert "v1: 100 → v2: 80 → v3: 90 → v4: 99" in trend
    assert "+10.0 %" in trend and trend.endswith("\n")
    # окно depth=2
    assert "v1:" not in duo.plan_size_trend(tmp_path, "pipeline/demo/plan_v4.md", depth=2)
    # не plan_vN / одна версия / нет файлов — пустая строка
    assert duo.plan_size_trend(tmp_path, "pipeline/demo/impl_report_v4.md") == ""
    assert duo.plan_size_trend(tmp_path, "pipeline/demo/plan_v1.md") == ""
    assert duo.plan_size_trend(tmp_path / "nope", "pipeline/demo/plan_v4.md") == ""
    assert duo.plan_size_trend(tmp_path, "") == ""


def test_triage_chairman_prompt_includes_size_trend_only_for_plan(tmp_path: Path):
    d = tmp_path / "pipeline" / "demo"
    d.mkdir(parents=True)
    (d / "plan_v1.md").write_text("x\n" * 60, encoding="utf-8")
    reviewer, _, cfg, _ = _reviewer(tmp_path, review={"plan_policy": "triage"})
    plan = _chairman_prompt(reviewer, cfg, "plan")      # artifact plan_v1.md: одна версия — тренда нет
    assert "Динамика размера плана" not in plan
    (d / "plan_v2.md").write_text("x\n" * 66, encoding="utf-8")
    m = duo.load_member(cfg, "alpha")
    p2 = reviewer._build_chairman_prompt(
        artifact_rel="pipeline/demo/plan_v2.md", phase="plan", task_text="задача",
        A=m, B=m, r1a="a1", r1b="b1", r2a="a2", r2b="b2", r3a="a3", r3b="b3",
        sa="revise", sb="revise")
    assert "v1: 60 → v2: 66" in p2 and "+10.0 %" in p2
    assert p2.index("Политика приёмки плана") < p2.index("Динамика размера плана") < p2.index("Правила: не выдумывай")
    # legacy: тренда нет даже при наличии файлов
    reviewer_l, _, cfg_l, _ = _reviewer(tmp_path)
    p3 = reviewer_l._build_chairman_prompt(
        artifact_rel="pipeline/demo/plan_v2.md", phase="plan", task_text="задача",
        A=m, B=m, r1a="a1", r1b="b1", r2a="a2", r2b="b2", r3a="a3", r3b="b3",
        sa="revise", sb="revise")
    assert "Динамика размера плана" not in p3


def _executor_prompt(tmp_path: Path, **cfg_over) -> str:
    cfg = {"timeouts": {"executor_sec": 5}, "env_hint": "", **cfg_over}
    t = _Transport(["готово"])
    td = tmp_path / "td_ex"
    td.mkdir(exist_ok=True)
    rev = tmp_path / "rev001_plan.md"
    rev.write_text("# rev\nREVMARK\nVERDICT: REVISE\n", encoding="utf-8")
    ex = duo.Executor(cfg, t, td, tmp_path, {"artifacts_rel": "pipeline/demo"})
    ex.revise_plan("задача", 2, rev)
    return t.calls[0]


def test_revise_plan_default_prompt_has_no_policy_block(tmp_path: Path):
    for over in ({}, {"review": {"plan_policy": "legacy"}}, {"review": None}):
        p = _executor_prompt(tmp_path, **over)
        assert "Правила правки плана" not in p
        assert 'REVMARK\nVERDICT: REVISE\n"""\n\nПерепиши план с учётом правок' in p


def test_revise_plan_triage_adds_executor_rules_before_instruction(tmp_path: Path):
    p = _executor_prompt(tmp_path, review={"plan_policy": "triage"})
    assert duo.PLAN_POLICY_EXECUTOR_BLOCK in p
    assert "Размер плана не растёт без нового требования ТЗ" in p
    assert "`[заменено: Е№]`" in p
    assert p.index("REVMARK") < p.index("Правила правки плана") < p.index("Перепиши план с учётом правок")


# --- impl-policy (review.impl_policy="strict"): только фаза impl, дефолт не меняется ---


def _executor_impl_prompts(tmp_path: Path, **cfg_over) -> tuple[str, str]:
    cfg = {"timeouts": {"executor_sec": 5}, **cfg_over}
    t = _Transport(["готово", "готово"])
    td = tmp_path / "td_impl"
    td.mkdir(exist_ok=True)
    rev = tmp_path / "rev001_impl.md"
    rev.write_text("# rev\nREVMARK\nVERDICT: REVISE\n", encoding="utf-8")
    ex = duo.Executor(cfg, t, td, tmp_path, {"artifacts_rel": "pipeline/demo"})
    ex.implement("задача", "pipeline/demo/plan_v1.md", 1)
    ex.revise_impl("задача", "pipeline/demo/plan_v1.md", 2, rev, "pipeline/demo/diff_impl_v1.patch")
    return t.calls[0], t.calls[1]


def test_impl_policy_default_off_everywhere(tmp_path: Path):
    for over in ({}, {"review": {"impl_policy": "legacy"}}, {"review": None},
                 {"review": {"plan_policy": "triage"}}):      # plan_policy impl НЕ включает
        reviewer, _, cfg, _ = _reviewer(tmp_path, **over)
        assert "Политика приёмки имплементации" not in _chairman_prompt(reviewer, cfg, "impl")
        p_impl, p_rev = _executor_impl_prompts(tmp_path, **over)
        assert "ФАКТИЧЕСКИЙ вывод" not in p_impl and "ФАКТИЧЕСКИЙ вывод" not in p_rev
        assert "класс дефекта" not in p_rev


def test_impl_policy_strict_chairman_only_for_impl(tmp_path: Path):
    reviewer, _, cfg, _ = _reviewer(tmp_path, review={"impl_policy": "strict"})
    impl = _chairman_prompt(reviewer, cfg, "impl")
    assert "Политика приёмки имплементации" in impl
    assert "КЛАСС дефекта" in impl and "ФАКТИЧЕСКИЙ вывод команд" in impl
    # блок — после чек-листа commit_plan и VERDICT, до «Правила:»; формат отчёта цел
    assert impl.index("VERDICT: REVISE") < impl.index("Чек-лист фазы impl") < impl.index(
        "Политика приёмки имплементации") < impl.index("Правила: не выдумывай")
    assert "Политика приёмки плана" not in impl
    for phase in ("plan", "custom"):
        other = _chairman_prompt(reviewer, cfg, phase)
        assert "Политика приёмки имплементации" not in other and "Политика приёмки плана" not in other


def test_impl_policy_strict_executor_blocks(tmp_path: Path):
    p_impl, p_rev = _executor_impl_prompts(tmp_path, review={"impl_policy": "strict"})
    assert duo.IMPL_POLICY_EXECUTOR_REPORT in p_impl
    assert duo.IMPL_POLICY_EXECUTOR_REVISE not in p_impl
    assert p_impl.index("ФАКТИЧЕСКИЙ вывод") < p_impl.index("Внеси правки кода")
    assert duo.IMPL_POLICY_EXECUTOR_REVISE in p_rev and duo.IMPL_POLICY_EXECUTOR_REPORT in p_rev
    assert p_rev.index("REVMARK") < p_rev.index("класс дефекта") < p_rev.index("Доработай имплементацию")


def test_default_config_enables_v2_personas_and_policies_and_merges(tmp_path: Path):
    import json

    cfg = duo.load_config(None)
    assert cfg["duo"]["members_plan"] == ["socrates2", "feynman2"]
    assert cfg["duo"]["members_impl"] == ["torvalds2", "meadows2"]
    base = cfg["review"]
    assert base["plan_policy"] == "triage" and base["impl_policy"] == "strict"
    assert base["context"] == "legacy" and base["pack_max_chars"] == 60000
    # пользовательский конфиг может вернуть legacy; deep_merge не затирает соседние ключи
    user = tmp_path / "cfg.json"
    user.write_text(json.dumps({"review": {"plan_policy": "legacy", "impl_policy": "legacy"},
                                "duo": {"members_plan": ["socrates", "feynman"]}}), encoding="utf-8")
    merged = duo.load_config(user)
    assert merged["review"]["plan_policy"] == "legacy" and merged["review"]["impl_policy"] == "legacy"
    assert merged["review"]["context"] == "legacy" and merged["review"]["pack_max_chars"] == 60000
    assert merged["duo"]["members_plan"] == ["socrates", "feynman"]
    assert merged["duo"]["members_impl"] == ["torvalds2", "meadows2"]
