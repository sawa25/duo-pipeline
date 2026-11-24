"""chmguard (R5): регресс-фикстуры валидации отчёта председателя.

Инцидент d10 rev002 (2026-10-04): «200 OK с мусором» — однострочный ответ
председателя был принят как валидный REVISE, rev-файл выродился в мета-строку.
Фиксируем контракты:
  _parse_verdict      — вердикт только на ПОСЛЕДНЕЙ непустой строке (fullmatch);
  _looks_like_report  — структурный гард (`# rev —`, `## 0`/`## 1`, >=10 строк);
  _chairman_with_retry — ретрай по дегенеративному выводу + forensic-транскрипты
                        chairman_attempt{N} + сохранность RuntimeError-ретрая.

Модуль оркестратора грузится importlib-ом (tools/dev/duo_pipeline вне sys.path,
module-level без побочных эффектов); живой транспорт подменён фейком,
task_dir/repo = tmp_path (изоляция из tests/conftest.py не обходится).
"""

from __future__ import annotations

import importlib.util
import sys
from collections import deque
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_pipeline_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()

# Инцидент-строка d10 дословно: RESPONSE из transcripts/165908_chairman.md серии
# d10 (архив серии: pipeline/ARCHIVE-INDEX.md#d10), она же всё содержимое
# выродившегося rev002_plan.md (133 байта, 1 строка).
D10_INCIDENT_LINE = (
    "Отчёт ревью выше — полный и финальный, машинная строка `VERDICT: REVISE` в конце."
)


def _mini_report(header: str = "# rev — ревью pipeline/demo.md (фаза: plan)", verdict: str | None = "REVISE") -> str:
    """Валидный мини-отчёт: заголовок, секции ## 0/## 1, ровно 11 непустых строк."""
    lines = [
        header,
        "",
        "Дата: 2026-10-04. Дуэт: a × b. Председатель: test.",
        "",
        "## 0. Вердикт",
        "Доработать: есть замечания.",
        "",
        "## 1. Замечания",
        "Н1. Суть замечания, координаты, категория, что сделать.",
        "Н2. Ещё замечание с координатами.",
        "",
        "## 2. Что проверено и подтвердилось",
        "- факт 1;",
        "- факт 2.",
    ]
    text = "\n".join(lines) + "\n"
    if verdict is not None:
        text += f"\nVERDICT: {verdict}\n"
    return text


class FakeTransport:
    """Скриптованный транспорт: ответы по очереди + журнал (prompt, timeout) вызовов."""

    def __init__(self, responses: list[str], runtime_error_at: set[int] | None = None):
        self._responses = deque(responses)
        self.calls: list[tuple[str, int]] = []
        self._err_at = runtime_error_at or set()

    def call(self, prompt: str, *, mode: str, timeout: int, cwd: Path | None = None, **_kw):
        n = len(self.calls)
        self.calls.append((prompt, timeout))
        if n in self._err_at:
            raise RuntimeError("транспорт: сбой/таймаут (мок)")
        return self._responses.popleft(), "", 0.25


@pytest.fixture
def make_reviewer(tmp_path: Path):
    def _make(responses: list[str], runtime_error_at: set[int] | None = None):
        transport = FakeTransport(responses, runtime_error_at)
        cfg = {"timeouts": {"chairman_sec": 7}}
        reviewer = duo.DuoReviewer(cfg, transport, tmp_path, tmp_path)
        return reviewer, transport

    return _make


# --- R2/R1: чистые функции валидации ----------------------------------------


def test_d10_incident_string_rejected():
    """DoD-1: инцидент-строка d10 отвергается обоими гардами."""
    assert duo.DuoReviewer._parse_verdict(D10_INCIDENT_LINE) is None
    assert duo.DuoReviewer._looks_like_report(D10_INCIDENT_LINE) is False


def test_valid_mini_report():
    """Валидный мини-отчёт: структура True, вердикт REVISE; дефис- и ВЕРХНИЙ-регистр заголовка (Н3/Н2)."""
    report = _mini_report()
    assert duo.DuoReviewer._looks_like_report(report) is True
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE"
    hyphen = _mini_report(header="# rev - ревью pipeline/demo.md (фаза: plan)")
    assert duo.DuoReviewer._looks_like_report(hyphen) is True
    upper = _mini_report(header="# REV — ревью pipeline/demo.md (фаза: plan)")
    assert duo.DuoReviewer._looks_like_report(upper) is True


def test_verdict_only_in_prose():
    """VERDICT упомянут в середине прозы, в конце отсутствует → None (не findall)."""
    report = _mini_report(verdict=None).replace(
        "- факт 2.", "- факт 2; машинная строка VERDICT: ACCEPT в прозе не считается."
    )
    assert duo.DuoReviewer._parse_verdict(report) is None


def test_bold_verdict_last_line():
    """Финальная строка жирным `**VERDICT: ACCEPT**` → ACCEPT."""
    report = _mini_report(verdict=None) + "\n**VERDICT: ACCEPT**\n"
    assert duo.DuoReviewer._parse_verdict(report) == "ACCEPT"


def test_verdict_with_trailing_blanks():
    """Вердикт + хвостовые пустые строки/пробелы → вердикт найден."""
    report = _mini_report() + "\n\n   \n\t\n"
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE"


# --- R3: ретрай по дегенеративному выводу ------------------------------------


def test_retry_recovers_from_degenerate(make_reviewer):
    """DoD-2: 1-я попытка мусор → 2-я валидный отчёт; итог валиден, forensic-след есть (Н2)."""
    reviewer, transport = make_reviewer([D10_INCIDENT_LINE, _mini_report()])
    report, cost, prompt_used = reviewer._chairman_with_retry("БАЗОВЫЙ ПРОМПТ")
    assert duo.DuoReviewer._looks_like_report(report) is True
    assert report == _mini_report()
    assert len(transport.calls) == 2
    # ретрай-промпт: аугментация + цитата первых 200 символов мусора
    assert "Предыдущая попытка вернула неполный/невалидный ответ" in transport.calls[1][0]
    assert D10_INCIDENT_LINE[:200] in transport.calls[1][0]
    assert transport.calls[1][0].startswith("БАЗОВЫЙ ПРОМПТ")
    # Н3: возвращён ФАКТИЧЕСКИЙ промпт (аугментированный), а не базовый
    assert prompt_used == transport.calls[1][0]
    # стоимость суммирована по попыткам
    assert cost == pytest.approx(0.5)
    # forensic-след: полный текст непринятой попытки в transcripts/
    attempts = sorted((reviewer.transcripts).glob("*_chairman_attempt1.md"))
    assert len(attempts) == 1
    assert D10_INCIDENT_LINE in attempts[0].read_text(encoding="utf-8")


def test_retry_exhausted_returns_last(make_reviewer):
    """3× мусор → 3 вызова, все попытки в transcripts/, возвращён последний мусор."""
    junk = ["мусор-1", "мусор-2", "мусор-3"]
    reviewer, transport = make_reviewer(junk)
    report, cost, prompt_used = reviewer._chairman_with_retry("ПРОМПТ")
    assert report == "мусор-3"
    assert len(transport.calls) == 3
    assert cost == pytest.approx(0.75)
    assert prompt_used == transport.calls[2][0] and "мусор-2" in prompt_used
    tags = sorted(p.name.split("_", 1)[1] for p in reviewer.transcripts.glob("*_chairman_attempt*.md"))
    assert tags == ["chairman_attempt1.md", "chairman_attempt2.md", "chairman_attempt3.md"]


def test_fallback_report_selfparseable():
    """Н4: фолбэк-обёртка парсится собственным _parse_verdict; машинная строка последняя."""
    wrapped = duo.DuoReviewer._fallback_report(D10_INCIDENT_LINE)
    assert duo.DuoReviewer._parse_verdict(wrapped) == "REVISE"
    assert wrapped.splitlines()[-1].strip() == "VERDICT: REVISE"
    assert "(дописано оркестратором" in wrapped


def test_runtimeerror_retry_timeout_doubled(make_reviewer):
    """Замок старого поведения: RuntimeError → повторный вызов с удвоенным таймаутом."""
    reviewer, transport = make_reviewer(["ok"], runtime_error_at={0})
    report, cost = reviewer._call_chairman_once("ПРОМПТ", 7)
    assert report == "ok"
    assert cost == pytest.approx(0.25)
    assert [t for _, t in transport.calls] == [7, 14]


# --- chmguard R4: санирайзер преамбулы (инцидент-класс 6/7 ретраев) ------------


def test_sanitize_preamble_strips_transition_phrase():
    """Реальный инцидент nmcase 193717: валидный отчёт + фраза-переход сверху."""
    preamble = ("Все ключевые утверждения проверены по коду и артефактам. "
                "Составляю вердикт председателя.")
    body = ("# rev — ревью pipeline/demo/impl_report_v1.md (фаза: impl)\n\n"
            "## 0. Вердикт\nПринять.\n\n## 1. Замечания\nнет\n\n"
            "VERDICT: ACCEPT")
    out = duo.DuoReviewer._sanitize_preamble(preamble + "\n" + body)
    assert out.startswith("# rev")
    assert out.endswith("VERDICT: ACCEPT")


def test_sanitize_preamble_real_incidents():
    """Исторические 'ложные ретраи' (фразы из transcripts chairman_attempt*)
    санируются: отчёт начинается с # rev, тело не тронуто."""
    incidents = [
        "Все координаты участников сверены по коду. Составляю итоговый отчёт.",
        "Все ключевые якоря проверены по коду и тексту плана — формирую вердикт председателя.",
        "Все якоря проверены по коду. Формирую вердикт председателя.",
        "Все выборочные координаты сходятся (:584, :667, :1271, :1304, :1415 — точно). Проверка председателя завершена. Составляю отчёт.",
        "Все ключевые утверждения дуэта сверены с кодом и файлами независимо. Составляю итоговый отчёт.",
    ]
    body = ("# rev — ревью план (фаза: plan)\n\n## 0. Вердикт\nok\n\n"
            "## 1. Замечания\n- н1\n\nVERDICT: REVISE")
    for pre in incidents:
        out = duo.DuoReviewer._sanitize_preamble(pre + "\n" + body)
        assert out.startswith("# rev"), f"преамбула не срезана: {pre[:40]}..."
        assert "VERDICT: REVISE" in out


def test_sanitize_preamble_keeps_long_garbage():
    """>5 строк до заголовка — не преамбула, а мусор: не трогаем (валидатор забракует)."""
    garbage = "\n".join(f"мусорная строка {i}" for i in range(7))
    body = "# rev — ревью x (фаза: plan)\n\n## 0. Вердикт\nok\n\n## 1. Замечания\n- н1\n\nVERDICT: REVISE"
    out = duo.DuoReviewer._sanitize_preamble(garbage + "\n" + body)
    assert out.startswith("мусорная строка 0")   # не срезано


def test_sanitize_preamble_noop_cases():
    """Заголовка нет вообще / он уже первый — noop."""
    assert duo.DuoReviewer._sanitize_preamble("просто текст без заголовка") == "просто текст без заголовка"
    assert duo.DuoReviewer._sanitize_preamble("") == ""
    first = "# rev — сразу первый\n\n## 0. Вердикт\nok\n\n## 1. Замечания\n- н1\n\nVERDICT: REVISE"
    assert duo.DuoReviewer._sanitize_preamble(first) == first
