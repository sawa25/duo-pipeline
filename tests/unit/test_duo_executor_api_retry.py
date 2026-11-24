"""Устойчивость к транспортным сбоям claude CLI («API Error: The operation timed out.»).

Боевой инцидент (17.10.2026): длинная генерация исполнителя через Z.AI два раза
оборвалась встроенным idle-watchdog стрима claude CLI (пол 300 с); оркестратор принял
текст «API Error: …» за ответ модели → «артефакт ревью не найден» → needs_human.

Что фиксируют тесты:
  * is_api_error_text — узкая сигнатура (только начало ответа);
  * Executor._send — повтор с resume той же сессии и коротким «продолжи»-промптом,
    лимит limits.executor_api_retries, суммирование стоимости, след в transcripts/;
    retries=0 — прежнее поведение (один вызов, без пауз);
  * ClaudeGlm._base_env — claude_env_full только для mode="full", «_comment» не уходит в env;
  * ревьюер: «API Error» — заглушка (штатный один ретрай), цитата в середине — нет;
  * config.json: executor_sec строго больше встроенного idle-таймаута claude (иначе
    оркестратор убьёт процесс раньше его собственного watchdog).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DUO_PIPELINE_PATH = REPO_ROOT / "duo_pipeline.py"
DUO_CONFIG_PATH = REPO_ROOT / "config.json"


def _load_duo_module():
    spec = importlib.util.spec_from_file_location("duo_api_retry_under_test", DUO_PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


duo = _load_duo_module()

API_ERR = "API Error: The operation timed out."
GOOD = "готово: отчёт записан"


class _Transport:
    """Скриптованный транспорт исполнителя: фиксирует prompt/resume_session по вызовам."""

    def __init__(self, responses: list[tuple[str, str | None, float]]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call(self, prompt, *, mode, timeout=0, cwd=None, resume_session=None, tag="", **_kw):
        self.calls.append({"prompt": prompt, "mode": mode, "resume": resume_session})
        return self.responses.pop(0)


def _executor(tmp_path: Path, responses, *, retries=3, session="sid-0"):
    cfg = {"timeouts": {"executor_sec": 5, "api_retry_wait_sec": 30}, "limits": {"executor_api_retries": retries}}
    state = {"executor_session": session, "spent_usd": 1.0, "artifacts_rel": "pipeline/demo"}
    t = _Transport(responses)
    return duo.Executor(cfg, t, tmp_path, tmp_path, state), t, state


def test_is_api_error_text_signature():
    assert duo.is_api_error_text("API Error: The operation timed out.")
    assert duo.is_api_error_text("  \napi error: 529 overloaded")
    assert not duo.is_api_error_text("")
    assert not duo.is_api_error_text(None)
    assert not duo.is_api_error_text("Отчёт готов. В логе была строка API Error: x, она не наша.")


def test_send_retries_with_resume_and_short_prompt(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    ex, t, state = _executor(tmp_path, [(API_ERR, "sid-1", 0.5), (GOOD, "sid-1", 0.25)])
    text = ex._send("ОГРОМНЫЙ ИСХОДНЫЙ ПРОМПТ", "executor_impl_v3")
    assert text == GOOD
    assert len(t.calls) == 2
    assert t.calls[0]["prompt"] == "ОГРОМНЫЙ ИСХОДНЫЙ ПРОМПТ" and t.calls[0]["resume"] == "sid-0"
    # повтор — resume сессии, пришедшей с неудачной попыткой, и короткий промпт (не дубль задания)
    assert t.calls[1]["resume"] == "sid-1" and t.calls[1]["mode"] == "full"
    assert "ТЕХНИЧЕСКОЙ ошибкой" in t.calls[1]["prompt"] and "ОГРОМНЫЙ" not in t.calls[1]["prompt"]
    assert API_ERR in t.calls[1]["prompt"]
    assert sleeps == [30.0]
    assert state["executor_session"] == "sid-1"
    assert state["spent_usd"] == 1.75                      # 1.0 + 0.5 + 0.25
    tr = next((tmp_path / "transcripts").glob("*_executor_impl_v3.md")).read_text(encoding="utf-8")
    assert "## API-RETRY" in tr and API_ERR in tr and GOOD in tr


def test_send_gives_up_after_limit_and_returns_last_error(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    ex, t, _ = _executor(tmp_path, [(API_ERR, "sid-1", 0.1)] * 3, retries=2)
    text = ex._send("p", "executor_impl_v3")
    assert duo.is_api_error_text(text)                     # дальше — штатная остановка «артефакт не найден»
    assert len(t.calls) == 3                               # 1 + 2 повтора, не больше
    assert sleeps == [30.0, 60.0]                          # экспоненциальная пауза


def test_send_default_zero_retries_is_legacy(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    ex, t, _ = _executor(tmp_path, [(API_ERR, "sid-1", 0.1)], retries=0)
    assert duo.is_api_error_text(ex._send("p", "executor_impl_v3"))
    assert len(t.calls) == 1 and sleeps == []


def test_send_without_session_resends_original_prompt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(duo.time, "sleep", lambda s: None)
    ex, t, _ = _executor(tmp_path, [(API_ERR, None, 0.1), (GOOD, "sid-9", 0.1)], session=None)
    assert ex._send("исходное задание", "executor_plan_v1") == GOOD
    # сессии нет (первый вызов упал до init) — «продолжи» бессмыслен, шлём задание заново
    assert t.calls[1]["prompt"] == "исходное задание" and t.calls[1]["resume"] is None


def test_send_normal_answer_single_call(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(duo.time, "sleep", lambda s: sleeps.append(s))
    ex, t, _ = _executor(tmp_path, [(GOOD, "sid-1", 0.1)])
    assert ex._send("p", "t") == GOOD
    assert len(t.calls) == 1 and sleeps == []


def test_claude_env_full_only_for_executor(monkeypatch):
    monkeypatch.delenv("CLAUDE_STREAM_IDLE_TIMEOUT_MS", raising=False)
    cfg = {
        "claude_config_dir": "/tmp/x",
        "model": "opus",
        "claude_env_full": {"CLAUDE_STREAM_IDLE_TIMEOUT_MS": 900000, "_comment": "не env"},
    }
    t = duo.ClaudeGlm(cfg)
    full = t._base_env("full")
    plan = t._base_env("plan")
    assert full["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "900000"
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in plan     # ревьюеры/председатель не затронуты
    assert "_comment" not in full
    assert full["CLAUDE_CONFIG_DIR"] == "/tmp/x"


def test_reviewer_api_error_is_stub():
    assert duo.DuoReviewer._is_stub_response(API_ERR)
    assert duo.DuoReviewer._is_stub_response("")
    assert not duo.DuoReviewer._is_stub_response("Анализ.\n\nSTANCE: accept\nCONFIDENCE: high\n")
    assert not duo.DuoReviewer._is_stub_response("В прогоне была ошибка API Error: 500, разбор ниже.")


def test_config_executor_timeout_exceeds_claude_idle():
    cfg = duo.load_config(DUO_CONFIG_PATH)
    env = cfg["claude_env_full"]
    idle_ms = int(env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"])
    assert cfg["timeouts"]["executor_sec"] * 1000 > idle_ms
    assert idle_ms <= 1_800_000                            # потолок клампа claude CLI (30 мин)
    assert cfg["limits"]["executor_api_retries"] >= 1
    json.dumps(cfg)                                        # сериализуемость (state/telemetry)
