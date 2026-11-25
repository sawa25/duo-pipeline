"""fieldfix8: «проверяющий вернул неполный отчёт» — классы сбоя доставки ответа (Н38/Н42, полевая волна 1).

Что случилось: в полевом отчёте волны 1 9 «неполных отчётов» и ретраи подряд. Разбор журналов сессий и
синтетика на живой glm-5.4 показали: формат отчёта модель выдаёт верно (effort high и low — заголовок,
§0, §1, VERDICT последней строкой), когда ответ дошёл до конца. Ломалась ДОСТАВКА:
  * output_limit — рассуждения съели общий лимит вывода claude CLI (32000 токенов): «API Error: Claude's
    response exceeded the 32000 output token maximum» либо отчёт оборван на полуслове (CLI продолжает ход
    новым сообщением, а result.result хранит только хвост);
  * transient — «API Error: 529 … temporarily overloaded»: повтор без паузы и с припиской «выведи ПОЛНЫЙ
    отчёт» бесполезен;
  * ошибка API после исчерпания ретраев превращалась в подставной REVISE (исполнитель переделывал
    работу по несуществующему замечанию); стоимость оборванной цепочки не попадала в spent_usd.
Паттерн test_duo_chairman_validation / test_duo_pipemodes: фейковые транспорты, без LLM.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from tests.unit.test_duo_chairman_validation import _mini_report
from tests.unit.test_duo_fieldfix3 import _ns
from tests.unit.test_duo_fieldfix4 import _SlowProc
from tests.unit.test_duo_pipemodes import SINGLE_MARK, PairTransport, _pcfg, _single_review, _state
from tests.unit.test_duo_pipev3 import ScriptedTransport, _cfg, _init_repo, _mk_task, _review, duo

API_529 = ("API Error: 529 [1305][The service may be temporarily overloaded, please try again later]"
           "[20261125114053f29968770a1f41e9]. This is a server-side issue, usually temporary — try again in a "
           "moment. If it persists, check your inference gateway (api.z.ai).")
API_LIMIT = ("API Error: Claude's response exceeded the 32000 output token maximum. To configure this behavior, "
             "set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.")
API_400 = "API Error: 400 {\"error\":{\"message\":\"invalid request\"}}"


# --- классификатор ------------------------------------------------------------------------------


@pytest.mark.parametrize("text,meta,expected", [
    (API_529, None, "transient"),
    (API_529, {"api_error_status": 529}, "transient"),
    ("API Error: The operation timed out.", None, "transient"),
    (API_LIMIT, None, "output_limit"),
    ("текст оборван", {"stop_reason": "max_tokens"}, "output_limit"),
    ("", {"stop_reason": "max_tokens"}, "output_limit"),
    (API_400, None, "api_error"),
    ("API Error: 403 forbidden", {"api_error_status": 403}, "api_error"),
    ("API Error: что-то странное", {"api_error_status": 503}, "transient"),   # статус из события result
    ("", None, "stub"),
    ("   \n", {"stop_reason": "end_turn"}, "stub"),
    ("Готово: план в ~/.claude-glm/plans/x.md, вызвал ExitPlanMode", None, "stub"),
    ("Отчёт ревью выше — полный и финальный.", None, "format"),
    # цитата ошибки ВНУТРИ нормального текста — не сбой API (узкая сигнатура: только начало ответа)
    ("В прогоне была ошибка API Error: 529, разбор ниже.", None, "format"),
])
def test_classify_failure(text, meta, expected):
    assert duo.classify_failure(text, meta) == expected


def test_lower_effort_steps():
    assert [duo.lower_effort(x) for x in ("max", "xhigh", "high", "medium", "low")] == \
        ["high", "high", "medium", "low", "low"]
    assert duo.lower_effort("") == "medium"                 # у алиаса не задан — дефолт CLI тяжелее medium
    assert duo.lower_effort("turbo") == "medium"


# --- склейка разорванного ответа ------------------------------------------------------------------


def test_stitch_plain_concat_when_seam_is_mid_word():
    full = _mini_report()
    cut = full.index("замечания") + 4                       # шов внутри слова
    assert duo.stitch_messages([full[:cut], full[cut:]]) == full


def test_stitch_dedupes_restarted_continuation():
    """Продолжение «перезапустилось» с пункта Н1 (поле: S1-plan — с пункта 7): дубль срезается."""
    full = _mini_report()
    first = full[:full.index("Н2.") + 8]
    second = full[full.index("Н1."):]
    assert duo.stitch_messages([first, second]) == full


def test_stitch_degenerate_inputs():
    assert duo.stitch_messages([]) == "" and duo.stitch_messages(["", ""]) == ""
    assert duo.stitch_messages(["один"]) == "один"
    # короткая первая строка продолжения (<12 знаков) не ищется в уже выведенном — прямая склейка
    assert duo.stitch_messages(["abc ok", "ok"]) == "abc okok"


# --- транспорт: meta, effort/env повтора -------------------------------------------------------------


def _stream(tmp_path: Path, monkeypatch, events: list[dict], cfg_extra: dict | None = None,
            **call_kw):
    monkeypatch.setattr(duo.subprocess, "Popen", lambda *a, **k: _SlowProc(events, 0.0))
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m", **(cfg_extra or {})})
    out = glm.call("ПРОМПТ", mode="plan", timeout=30, **call_kw)
    return glm, out


def _asst(mid: str, *blocks: dict) -> dict:
    return {"type": "assistant", "message": {"id": mid, "content": list(blocks)}}


def _res(text: str, **kw) -> dict:
    return {"type": "result", "session_id": "s", "total_cost_usd": 0.5, "result": text, "stop_reason": "end_turn",
            "is_error": False, "subtype": "success", "terminal_reason": "completed", "num_turns": 1, **kw}


def test_transport_meta_collects_split_messages_and_thinking(tmp_path: Path, monkeypatch):
    events = [
        _asst("m1", {"type": "thinking", "thinking": "x" * 40}),
        _asst("m1", {"type": "text", "text": "# rev — начало\n"}),
        _asst("m2", {"type": "text", "text": "## 0 продолжение\n"}),
        _res("## 0 продолжение\n"),
    ]
    glm, (text, _sid, cost) = _stream(tmp_path, monkeypatch, events)
    meta = glm.take_call_meta()
    assert text == "## 0 продолжение\n" and cost == 0.5            # возврат call() не изменился: хвост
    assert meta["tail_msgs"] == ["# rev — начало\n", "## 0 продолжение\n"]
    assert meta["messages"] == 2 and meta["thinking_chars"] == 40
    assert meta["stop_reason"] == "end_turn" and meta["is_error"] is False and meta["subtype"] == "success"


def test_transport_meta_tail_starts_after_last_tool_use(tmp_path: Path, monkeypatch):
    events = [
        _asst("m1", {"type": "text", "text": "смотрю файл"},
              {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}},
        _asst("m2", {"type": "text", "text": "итог"}),
        _res("итог"),
    ]
    glm, _ = _stream(tmp_path, monkeypatch, events)
    assert glm.take_call_meta()["tail_msgs"] == ["итог"]           # реплика до инструмента — не часть ответа


def test_transport_meta_captures_api_error_fields(tmp_path: Path, monkeypatch):
    events = [_res(API_529, is_error=True, api_error_status=529, subtype="success", stop_reason="stop_sequence")]
    glm, (text, _s, _c) = _stream(tmp_path, monkeypatch, events)
    m = glm.take_call_meta()
    assert m["is_error"] is True and m["api_error_status"] == 529
    assert duo.classify_failure(text, m) == "transient"


def test_transport_meta_without_call_is_empty(tmp_path: Path):
    assert duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m"}).take_call_meta() == {}


def _capture_popen(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    def fake_popen(cmd, **kw):
        seen.append((list(cmd), kw["env"]))
        return _SlowProc([_res("ok")], 0.0)

    monkeypatch.setattr(duo.subprocess, "Popen", fake_popen)
    return seen


def test_claude_env_plan_only_for_plan_and_call_extra_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
    seen = _capture_popen(monkeypatch)
    glm = duo.ClaudeGlm({"claude_config_dir": str(tmp_path), "model": "m", "effort": {"m": "high"},
                         "claude_env_plan": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": 64000, "_comment": "не env"},
                         "claude_env_full": {"CLAUDE_STREAM_IDLE_TIMEOUT_MS": "900000"}})
    glm.call("p", mode="plan", timeout=30)
    glm.call("p", mode="full", timeout=30)
    glm.call("p", mode="plan", timeout=30, effort="low", env_extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "99000"})
    glm.call("p", mode="plan", timeout=30, effort="turbo")        # мусорный override не доходит до CLI
    (c_plan, e_plan), (c_full, e_full), (c_over, e_over), (c_bad, _e_bad) = seen
    assert e_plan["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000" and "_comment" not in e_plan
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in e_plan            # env исполнителя ревьюерам не уходит
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in e_full            # и наоборот
    assert e_full["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "900000"
    assert c_plan[c_plan.index("--effort") + 1] == "high"           # effort алиаса из конфига
    assert c_over[c_over.index("--effort") + 1] == "low" and e_over["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "99000"
    assert c_bad[c_bad.index("--effort") + 1] == "high"


def test_config_ships_output_limit_for_reviewers():
    assert duo.DEFAULTS["claude_env_plan"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert duo.DEFAULTS["limits"]["overload_retries"] >= 1
    cfg = duo.load_config(Path(duo.__file__).resolve().parent / "config.json")
    assert int(cfg["claude_env_plan"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]) >= 64000
    assert cfg["limits"]["overload_retries"] >= 1
    assert duo.guard_config_problems(cfg) == [] and duo.guard_config_problems(duo.DEFAULTS) == []
    json.dumps(cfg)


@pytest.mark.parametrize("over,needle", [
    ({"claude_env_plan": ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]}, "claude_env_plan должен быть объектом"),
    ({"claude_env_plan": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "много"}}, "целое число токенов"),
    ({"claude_env_plan": {"X": ""}}, "claude_env_plan.X"),
    ({"claude_env_plan": {"X": True}}, "claude_env_plan.X"),
    ({"limits": {"overload_retries": -1}}, "overload_retries"),
    ({"limits": {"overload_retries": "4"}}, "overload_retries"),
])
def test_guard_config_problems_for_plan_env_and_overload(over, needle):
    assert any(needle in p for p in duo.guard_config_problems(over)), over


# --- _chairman_with_retry: лекарство по классу -----------------------------------------------------


class MetaTransport:
    """Скриптованный транспорт с `take_call_meta`: [(text, cost, meta) | Exception] по очереди + журнал."""

    def __init__(self, script):
        self.script = deque(script)
        self.calls: list[dict] = []
        self._meta: dict = {}

    def call(self, prompt, *, mode, timeout, cwd=None, **kw):
        self.calls.append({"prompt": prompt, "timeout": timeout, **kw})
        item = self.script.popleft()
        if isinstance(item, Exception):                     # сбой транспорта (idle-kill и т.п.)
            raise item
        text, cost, meta = item
        self._meta = meta
        return text, "", cost

    def take_call_meta(self):
        return dict(self._meta)


def _rv(tmp_path: Path, script, monkeypatch, **cfg_over):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = {"timeouts": {"chairman_sec": 7, "reviewer_sec": 9, "api_retry_wait_sec": 10},
           "limits": {"overload_retries": 2},
           "models": {"reviewer": "opus", "chairman": "sonnet"}, "effort": {"opus": "high"},
           "claude_env_plan": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}}
    cfg.update(cfg_over)
    t = MetaTransport(script)
    return duo.DuoReviewer(cfg, t, tmp_path, tmp_path), t, sleeps


def test_overload_pauses_and_retries_without_format_hint(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [(API_529, 0.0, {}), (_mini_report(), 0.3, {})], monkeypatch)
    report, cost, prompt_used = rv._chairman_with_retry("БАЗА", persona="gamma")
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE" and cost == pytest.approx(0.3)
    assert sleeps == [10.0]                                         # пауза ПЕРЕД повтором
    assert t.calls[1]["prompt"] == "БАЗА" == prompt_used            # без «выведи ПОЛНЫЙ отчёт»
    assert "effort" not in t.calls[1] and "env_extra" not in t.calls[1]
    assert rv._ev["overload_retries"] == 1 and rv._ev["incomplete_retries"] == 0
    tr = next(rv.transcripts.glob("*_chairman_attempt1.md")).read_text(encoding="utf-8")
    assert "класс=transient" in tr


def test_overload_does_not_consume_format_attempts(tmp_path: Path, monkeypatch):
    """Два 529 подряд, затем два брака формата и валидный отчёт: брак имеет свои CHAIRMAN_MAX_ATTEMPTS."""
    script = [(API_529, 0.0, {}), (API_529, 0.0, {}), ("мусор-1", 0.1, {}), ("мусор-2", 0.1, {}),
              (_mini_report(), 0.1, {})]
    rv, t, sleeps = _rv(tmp_path, script, monkeypatch)
    report, _cost, _p = rv._chairman_with_retry("БАЗА")
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE" and len(t.calls) == 5
    assert sleeps == [10.0, 20.0]                                   # экспоненциальные паузы
    assert "ПОЛНЫЙ отчёт" not in t.calls[1]["prompt"] and "ПОЛНЫЙ отчёт" in t.calls[3]["prompt"]


def test_overload_exhausted_raises_instead_of_fake_revise(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [(API_529, 0.25, {})] * 3, monkeypatch)       # limits.overload_retries = 2
    with pytest.raises(duo.ReviewIncompleteError) as ei:
        rv._chairman_with_retry("БАЗА", persona="gamma")
    assert ei.value.kind == "transport" and "529" in str(ei.value)
    assert len(t.calls) == 3 and sleeps == [10.0, 20.0]
    assert rv.call_cost == pytest.approx(0.75)                      # цена цепочки уже учтена (Н42)


def test_output_limit_retries_once_with_lower_effort_and_hint(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [(API_LIMIT, 4.0, {}), (_mini_report(), 0.4, {})], monkeypatch)
    report, cost, prompt_used = rv._chairman_with_retry("БАЗА", persona="gamma")
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE" and cost == pytest.approx(4.4)
    assert t.calls[1]["effort"] == "medium"                         # opus: high → medium
    assert "env_extra" not in t.calls[1]                            # 64000 уже в claude_env_plan
    assert t.calls[1]["prompt"] == "БАЗА" + duo.LIMIT_RETRY_HINT == prompt_used
    assert "ПОЛНЫЙ отчёт" not in prompt_used and rv._ev["limit_retries"] == 1


def test_output_limit_raises_limit_env_when_plan_env_is_low(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [(API_LIMIT, 1.0, {}), (_mini_report(), 0.4, {})], monkeypatch,
                    claude_env_plan={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"},
                    effort={"opus": "high", "sonnet": "medium"})
    rv._chairman_with_retry("БАЗА")                                  # председатель: алиас sonnet (medium), не opus
    assert t.calls[1]["effort"] == "low"
    assert t.calls[1]["env_extra"] == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}


def test_output_limit_by_stop_reason_with_partial_text(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [("# rev — оборван на полусло", 3.0, {"stop_reason": "max_tokens"}),
                               (_mini_report(), 0.4, {})], monkeypatch)
    rv._chairman_with_retry("БАЗА", persona="gamma")
    assert t.calls[1]["effort"] == "medium" and rv._ev["limit_retries"] == 1


def test_output_limit_twice_halts_for_human_not_fake_revise(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [(API_LIMIT, 4.0, {}), (API_LIMIT, 1.5, {})], monkeypatch)
    with pytest.raises(duo.ReviewIncompleteError) as ei:
        rv._chairman_with_retry("БАЗА", persona="gamma")
    assert ei.value.kind == "review_incomplete" and "effort" in str(ei.value)
    assert len(t.calls) == 2 and rv.call_cost == pytest.approx(5.5)


def test_split_report_is_stitched_without_second_payment(tmp_path: Path, monkeypatch):
    full = _mini_report()
    cut = full.index("Н2.") + 8
    first, second = full[:cut], full[full.index("Н1."):]            # продолжение «перезапустилось»
    meta = {"tail_msgs": [first, second], "messages": 2, "stop_reason": "end_turn"}
    rv, t, _s = _rv(tmp_path, [(second, 4.1, meta)], monkeypatch)
    report, cost, _p = rv._chairman_with_retry("БАЗА", persona="gamma")
    assert report.strip() == full.strip() and len(t.calls) == 1 and cost == pytest.approx(4.1)
    assert rv._ev["stitched"] == 1
    assert "СКЛЕЙКА" in (tmp_path / "orchestrator.log").read_text(encoding="utf-8")


def test_split_report_that_stays_invalid_is_not_accepted(tmp_path: Path, monkeypatch):
    """Склейка принимается только если ВАЛИДНА; иначе — обычный путь класса format."""
    meta = {"tail_msgs": ["# rev — начало", " и обрыв"], "messages": 2}
    rv, t, _s = _rv(tmp_path, [(" и обрыв", 0.2, meta), (_mini_report(), 0.2, {})], monkeypatch)
    report, _c, _p = rv._chairman_with_retry("БАЗА")
    assert len(t.calls) == 2 and "ПОЛНЫЙ отчёт" in t.calls[1]["prompt"] and rv._ev["stitched"] == 0
    assert duo.DuoReviewer._parse_verdict(report) == "REVISE"


def test_stub_gets_stub_hint_and_other_api_error_gets_none(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [("", 0.1, {}), (_mini_report(), 0.1, {})], monkeypatch)
    rv._chairman_with_retry("БАЗА")
    assert t.calls[1]["prompt"] == "БАЗА" + duo.REVIEWER_STUB_RETRY_SUFFIX and rv._ev["incomplete_retries"] == 1
    rv2, t2, _s2 = _rv(tmp_path / "b", [(API_400, 0.0, {}), (_mini_report(), 0.1, {})], monkeypatch)
    rv2._chairman_with_retry("БАЗА")
    assert t2.calls[1]["prompt"] == "БАЗА"                          # ошибка API — не формат: без приписки


def test_persistent_api_error_raises_and_junk_still_falls_back(tmp_path: Path, monkeypatch):
    rv, t, _s = _rv(tmp_path, [(API_400, 0.2, {})] * duo.CHAIRMAN_MAX_ATTEMPTS, monkeypatch)
    with pytest.raises(duo.ReviewIncompleteError) as ei:
        rv._chairman_with_retry("БАЗА")
    assert ei.value.kind == "transport" and len(t.calls) == duo.CHAIRMAN_MAX_ATTEMPTS
    assert rv.call_cost == pytest.approx(0.6)                       # цена цепочки в учёте даже без вердикта
    # прежний контракт для настоящего брака формата: исчерпание → последний ответ уходит в фолбэк review()
    rv2, t2, _s2 = _rv(tmp_path / "b", [("мусор-1", 0.1, {}), ("мусор-2", 0.1, {}), ("мусор-3", 0.1, {})],
                       monkeypatch)
    report, cost, _p = rv2._chairman_with_retry("БАЗА")
    assert report == "мусор-3" and cost == pytest.approx(0.3)


def test_transport_failure_mid_chain_keeps_paid_attempts_in_cost(tmp_path: Path, monkeypatch):
    script = [("мусор", 1.0, {}), RuntimeError("idle-kill"), RuntimeError("idle-kill")]
    rv, t, _s = _rv(tmp_path, script, monkeypatch)
    with pytest.raises(RuntimeError, match="idle-kill"):
        rv._chairman_with_retry("БАЗА")
    assert len(t.calls) == 3                                        # попытка + (сбой, повтор с удвоенным таймаутом)
    assert rv.call_cost == pytest.approx(1.0)                       # оплаченная первая попытка не потеряна (Н42)


def test_attempt_log_line_shows_class_and_cost(tmp_path: Path, monkeypatch):
    meta = {"stop_reason": "end_turn", "thinking_chars": 118000, "thinking_tokens_peak": 29500, "messages": 2}
    rv, _t, _s = _rv(tmp_path, [("мусор", 4.12, meta), (_mini_report(), 0.1, {})], monkeypatch)
    rv._chairman_with_retry("БАЗА")
    log_text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "класс format, $4.12, рассуждений 118000 знаков (пик ≈29500 ток.), сообщений 2" in log_text
    assert "вернул неполный отчёт (класс format, попытка 1/3)" in log_text


def test_final_transcript_header_marks_cost_as_cumulative(tmp_path: Path, monkeypatch):
    rv, _t, _s = _rv(tmp_path, [("мусор", 2.0, {}), (_mini_report(), 1.0, {})], monkeypatch)
    rv._chairman_with_retry("БАЗА")
    assert "КУМУЛЯТИВ всех 2 вызовов" in rv._final_note()
    rv._save_tr("chairman", "p", "r", 3.0, note=rv._final_note())
    head = next(rv.transcripts.glob("*_chairman.md")).read_text(encoding="utf-8").splitlines()[:3]
    assert head[1] == "cost=$3.0000" and "не складывать" in head[2]
    rv3, _t3, _s3 = _rv(tmp_path / "c", [(_mini_report(), 1.0, {})], monkeypatch)
    rv3._chairman_with_retry("БАЗА")
    assert rv3._final_note() == ""                                  # без отброшенных попыток шапка прежняя


def test_final_transcripts_of_duo_chairman_and_pair_reviewer_carry_cumulative_note(tmp_path: Path):
    class JunkOnceChair(ScriptedTransport):
        junk_done = False

        def call(self, prompt, **kw):
            if "Председатель консилиума" in prompt and not self.junk_done:
                self.junk_done = True
                self.calls.append(prompt)
                return "мусор", None, 0.0
            return super().call(prompt, **kw)

    repo = _init_repo(tmp_path)
    _review(tmp_path, repo, _cfg(tmp_path, repo), JunkOnceChair())
    head = next((tmp_path / "td" / "transcripts").glob("*_chairman.md")).read_text(encoding="utf-8")
    assert "КУМУЛЯТИВ всех 2 вызовов" in head.split("<!--")[0]
    # pair: единственный проверяющий, первая попытка — брак формата
    pair_dir = tmp_path / "p"
    pair_dir.mkdir()
    (_v, _c, _tel), td = _single_review(pair_dir, _init_repo(pair_dir), PairTransport(responses=["ok"]))
    head2 = next((td / "transcripts").glob("*_reviewer_alpha.md")).read_text(encoding="utf-8")
    assert "КУМУЛЯТИВ всех 2 вызовов" in head2.split("<!--")[0]


# --- _reviewer_call (R1–R3 дуэта) -------------------------------------------------------------------


def test_reviewer_call_overload_pause_no_exitplanmode_suffix(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [(API_529, 0.0, {}), ("анализ\nSTANCE: accept", 0.2, {})], monkeypatch)
    text, cost, prompt_used = rv._reviewer_call("P", "r1_alpha", persona="alpha")
    assert text.endswith("accept") and cost == pytest.approx(0.2)
    assert sleeps == [10.0] and t.calls[1]["prompt"] == "P" == prompt_used
    assert rv._ev["stub_retries"] == 1 and rv._ev["overload_retries"] == 1      # счётчик заглушек прежний


def test_reviewer_call_real_stub_keeps_suffix_and_limit_lowers_effort(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [("вызвал ExitPlanMode", 0.1, {}), ("ok", 0.1, {})], monkeypatch)
    rv._reviewer_call("P", "r1_alpha", persona="alpha")
    assert t.calls[1]["prompt"] == "P" + duo.REVIEWER_STUB_RETRY_SUFFIX and sleeps == []
    rv2, t2, _s = _rv(tmp_path / "b", [(API_LIMIT, 3.0, {}), ("ok", 0.1, {})], monkeypatch)
    rv2._reviewer_call("P", "r1_alpha", persona="alpha")
    assert t2.calls[1]["effort"] == "medium" and t2.calls[1]["prompt"] == "P" + duo.LIMIT_RETRY_HINT
    assert rv2._ev["limit_retries"] == 1
    rv3, t3, _s3 = _rv(tmp_path / "c", [("нормальный анализ", 0.1, {})], monkeypatch)
    assert rv3._reviewer_call("P", "r1_alpha", persona="alpha")[0] == "нормальный анализ" and len(t3.calls) == 1


def test_reviewer_call_other_api_error_has_no_hint_and_partial_text_at_limit_is_retried(tmp_path: Path, monkeypatch):
    rv, t, sleeps = _rv(tmp_path, [(API_400, 0.0, {}), ("ok", 0.1, {})], monkeypatch)
    rv._reviewer_call("P", "r1_alpha", persona="alpha")
    assert t.calls[1]["prompt"] == "P" and sleeps == []             # не формат и не перегрузка — без приписки/паузы
    # частичный ответ + stop_reason=max_tokens — не «нормальный ответ»: ретрай с effort ниже
    rv2, t2, _s = _rv(tmp_path / "b", [("частичный анализ без STANCE", 2.0, {"stop_reason": "max_tokens"}),
                                       ("ok", 0.1, {})], monkeypatch)
    text, cost, _p = rv2._reviewer_call("P", "r1_alpha", persona="alpha")
    assert text == "ok" and len(t2.calls) == 2 and t2.calls[1]["effort"] == "medium"
    assert cost == pytest.approx(2.1)


# --- review(): стоимость оборванного ревью + Pipeline.halt --------------------------------------------


def test_review_attaches_cost_to_any_exception(tmp_path: Path, monkeypatch):
    rv, _t, _s = _rv(tmp_path, [], monkeypatch)

    def boom(**_kw):
        rv.call_cost = 1.75
        raise RuntimeError("сбой")

    monkeypatch.setattr(rv, "_review_impl", boom)
    with pytest.raises(RuntimeError) as ei:
        rv.review(artifact_rel="x")
    assert ei.value.review_cost == 1.75


class _FailingReviewer(PairTransport):
    """Единственный проверяющий всегда получает один и тот же сбойный ответ (text, cost)."""

    def __init__(self, text: str, cost: float, **kw):
        super().__init__(**kw)
        self._bad = (text, cost)
        self.reviewer_calls = 0

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **kw):
        if SINGLE_MARK in prompt:
            self.reviewer_calls += 1
            self.calls.append(prompt)
            return self._bad[0], None, self._bad[1]
        return super().call(prompt, mode=mode, timeout=timeout, cwd=cwd, resume_session=resume_session,
                            tag=tag, **kw)


def _pair_run(tmp_path: Path, monkeypatch, text: str, cost: float, **limits):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    cfg = _pcfg(tmp_path, repo)
    cfg["limits"].update(limits)
    p = duo.Pipeline(cfg, "demo", mode="pair")
    t = _FailingReviewer(text, cost)
    p.t = t
    p.run()
    return _state(td), td, t


def test_pipeline_overload_after_retries_halts_transport_without_fake_review(tmp_path: Path, monkeypatch):
    st, td, t = _pair_run(tmp_path, monkeypatch, API_529, 0.0, overload_retries=1)
    assert st["halt"]["kind"] == "transport" and st["halt"]["resumable"] is True
    assert t.reviewer_calls == 2                                    # вызов + один повтор после паузы
    assert not list(td.glob("rev*_plan.md"))                        # подставного REVISE-отчёта нет
    assert st.get("verdicts", []) == [] and "не получило пригодного ответа" in st["stop_reason"]


def test_pipeline_output_limit_halts_review_incomplete_and_charges_spent(tmp_path: Path, monkeypatch):
    st, td, t = _pair_run(tmp_path, monkeypatch, API_LIMIT, 1.0)
    assert st["halt"]["kind"] == "review_incomplete" and t.reviewer_calls == 2
    assert st["spent_usd"] == pytest.approx(2.0)                    # цена обеих попыток в бюджете серии (Н42)
    assert not list(td.glob("rev*_plan.md"))
    assert "effort" in st["stop_reason"]


def test_empty_review_halt_charges_spent(tmp_path: Path):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    p = duo.Pipeline(_pcfg(tmp_path, repo), "demo", mode="pair")
    st = p.load_state()
    err = duo.EmptyReviewError("ACCEPT без единого чтения файла")
    err.review_cost = 1.25                                          # проставляет DuoReviewer.review
    p._halt_empty_review(st, "plan", err)
    assert st["spent_usd"] == pytest.approx(1.25) and _state(td)["halt"]["kind"] == "review_empty"


def test_status_reports_new_degradation_counters(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st["review_telemetry"] = [{"incomplete_retries": 1, "overload_retries": 2, "limit_retries": 1, "stitched": 1},
                              {"stub_retries": 0}]
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), _ns(json=True))
    obj = json.loads(capsys.readouterr().out)
    assert obj["review_transport"]["overload_retries"] == 2 and obj["review_transport"]["stitched"] == 1
    duo.cmd_status(_cfg(tmp_path, repo), _ns())
    out = capsys.readouterr().out
    assert "пауз на перегрузку 2" in out and "склеек 1" in out
