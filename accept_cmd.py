"""`duo accept` — живая приёмка серии: шаги проекта в РЕАЛЬНОМ дереве, вердикт PASS/FAIL/INCONCLUSIVE.

Зачем (полевой отчёт 20.11.2026, Н27): для проекта, чей результат — поведение на оборудовании, `gates`
бесполезны (чистая копия `git archive`: устройств, сети и ключей в ней нет), и `done` означал лишь
самооценку исполнителя («SOLO_DONE»). Приёмку делали руками вне инструмента, а следа «принято живой
проверкой» нигде не оставалось.

Что делает команда и чего НЕ делает:
  * шаги (`acceptance.steps`, как у `gates.steps` — `argv` без shell) идут в дереве серии (shared — основное,
    worktree — своё) под замками ресурсов серии (`resources`), по явной команде человека;
  * итог (вердикт, шаги, rc, время, HEAD) пишется в `state.json["acceptance"]`; ВЫВОД шагов в state не
    попадает (в нём бывают секреты) — он только в терминале;
  * НЕ меняет `phase` и не «закрывает» серию: `done` остаётся тем, чем был; решает человек
    (`set-verdict`, `merge`); автозапуска приёмки по завершении серии нет — запускать ли код на
    оборудовании, решает человек.

Вердикт шага: rc 0 — PASS; rc == `inconclusive_rc` — INCONCLUSIVE (устройство недоступно — не провал);
rc != 0 и вывод шага (stdout+stderr) подходит под regex `inconclusive_match` — тоже INCONCLUSIVE (Н32: проверяемая
команда проекта не всегда различает «устройство/сеть недоступны» и «дефект» кодом возврата — оба раза rc=1;
правильный способ — разный rc в самой команде и `inconclusive_rc`, а regex — когда команду не поменять;
совпадение никогда не даёт PASS, а его причина пишется в результат); иначе FAIL. Таймаут и «не удалось запустить» — INCONCLUSIVE (проверка не состоялась, дефекта не нашли).
Итог: любой FAIL → FAIL; иначе любой INCONCLUSIVE → INCONCLUSIVE; иначе PASS.
Коды выхода: 0 PASS, 1 FAIL, 2 INCONCLUSIVE либо приёмка не настроена/отказ (как `gates` на пустых шагах).
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable

DEFAULT_TIMEOUT_SEC = 600.0
TAIL_LINES = 15           # сколько последних строк вывода показать у не-PASS шага
RC_BY_VERDICT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}


def validate_steps(cfg_acc) -> tuple[list[dict], list[str]]:
    """Конфиг `acceptance` → (нормализованные шаги, список проблем). Проблемы — текст для оператора:
    приёмка с битым шагом не должна «частично пройти»."""
    problems: list[str] = []
    acc = {} if cfg_acc is None else cfg_acc
    if not isinstance(acc, dict):
        return [], ["acceptance должен быть объектом {steps: […]}"]
    raw = acc.get("steps")
    raw = [] if raw is None else raw
    if not isinstance(raw, list):
        return [], ["acceptance.steps должен быть списком шагов"]
    steps: list[dict] = []
    for i, s in enumerate(raw, 1):
        tag = f"acceptance.steps[{i}]"
        if not isinstance(s, dict):
            problems.append(f"{tag}: шаг должен быть объектом {{name, argv, …}}")
            continue
        argv = s.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
            problems.append(f"{tag}: argv — непустой список непустых строк (команда без shell, как в gates.steps)")
            continue
        inc = s.get("inconclusive_rc")
        if inc is not None and (isinstance(inc, bool) or not isinstance(inc, int) or inc == 0):
            problems.append(f"{tag}: inconclusive_rc — целое, отличное от 0 (rc, означающий «устройство недоступно»)")
            continue
        pat = s.get("inconclusive_match")
        if pat is not None:
            if not isinstance(pat, str) or not pat:
                problems.append(f"{tag}: inconclusive_match — непустая строка-regex")
                continue
            try:
                re.compile(pat)
            except re.error as e:
                problems.append(f"{tag}: inconclusive_match — некорректный regex {pat!r}: {e}")
                continue
        to = s.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
        if isinstance(to, bool) or not isinstance(to, (int, float)) or to <= 0:
            problems.append(f"{tag}: timeout_sec — положительное число секунд")
            continue
        steps.append({"name": str(s.get("name") or f"step{i}"), "argv": list(argv),
                      "inconclusive_rc": inc, "inconclusive_match": pat, "timeout_sec": float(to)})
    return steps, problems


def default_runner(argv: list[str], *, cwd, env: dict, timeout: float) -> tuple[int | None, str, str | None]:
    """(rc, вывод, причина-невозможности). rc=None и причина — шаг не состоялся (таймаут / нет бинарника)."""
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), None
    except subprocess.TimeoutExpired:
        return None, "", f"таймаут {timeout:g} с"
    except FileNotFoundError:
        return None, "", f"не удалось запустить: нет {argv[0]!r}"


def run_steps(steps: list[dict], *, cwd, env: dict, runner: Callable | None = None) -> list[dict]:
    """Выполняет шаги по порядку (НЕ останавливаясь на первом провале: приёмка показывает всю картину).
    Элемент результата: {name, rc, verdict, sec, reason, tail}."""
    run = runner or default_runner
    out: list[dict] = []
    for st in steps:
        t0 = time.time()
        rc, text, why = run(st["argv"], cwd=cwd, env=env, timeout=st["timeout_sec"])
        if why is not None:
            verdict = "INCONCLUSIVE"
        elif rc == 0:
            verdict = "PASS"
        elif st["inconclusive_rc"] is not None and rc == st["inconclusive_rc"]:
            verdict, why = "INCONCLUSIVE", "устройство недоступно (inconclusive_rc шага)"
        elif st.get("inconclusive_match") and re.search(st["inconclusive_match"], text):
            verdict, why = "INCONCLUSIVE", f"вывод подошёл под inconclusive_match: {st['inconclusive_match']}"
        else:
            verdict = "FAIL"
        lines = text.strip().splitlines()
        out.append({"name": st["name"], "rc": rc, "verdict": verdict, "sec": round(time.time() - t0, 1),
                    "reason": why, "tail": "\n".join(lines[-TAIL_LINES:]) if verdict != "PASS" else ""})
    return out


def aggregate(results: list[dict]) -> str:
    verdicts = {r["verdict"] for r in results}
    if "FAIL" in verdicts:
        return "FAIL"
    if "INCONCLUSIVE" in verdicts or not results:
        return "INCONCLUSIVE"
    return "PASS"
