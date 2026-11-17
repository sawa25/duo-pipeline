#!/usr/bin/env python3
"""
duo_pipeline.py — оркестратор пайплайна «исполнитель ↔ дуо-консилиум».

Исполнитель      : агентская сессия claude-glm (glm-5.3 через Z.AI), полные права,
                   персистентность через --resume.
Надсмотрщик      : дуо-консилиум (2 council-персоны, полярная пара) по механике
                   скилла ~/.claude/skills/council: R1 вслепую -> R2 перекрёстный
                   допрос (анти-конформизм) -> R3 финальные позиции (STANCE) ->
                   председатель синтезирует revNNN.md с машинным VERDICT.
Все вызовы GLM 5.3 через ключ Z.AI (профиль ~/.claude-glm). Подписка Cursor
не расходуется.

Режимы серии (флаг --mode / --solo; записывается в state.json при первом запуске):
  duo   (по умолчанию) исполнитель + председатель + дуэт (план: socrates2+feynman2, код: torvalds2+meadows2)
  pair  исполнитель + ОДИН проверяющий (config pair.reviewer_plan/impl): те же стадии план -> код,
        проверяющий сам выносит VERDICT, на каждой стадии — пока не примет
  solo  (--solo) один исполнитель на дешёвой модели, только код, без плана и ревью
Изоляция серии (--worktree | --isolation shared|worktree; пинуется в state.json):
  shared    общее дерево (по умолчанию); две живые shared-серии не допускаются (guard по .lock)
  worktree  своя папка .worktrees/<task> и ветка duo/<task>: параллельные серии; зона записи плана
            (## files_touch) + claim-gate при старте имплементации + merge с проверкой конфликтов

Подкоманды:
  init        --task NAME [--task-file F] [--mode duo|pair] [--worktree]   создать задачу (task.md, state.json)
  run         --task NAME [--fg] [--continue|--resume] [--mode M] [--worktree] [--solo]
              [--allow-shared-parallel] [--ignore-claims]
                                              цикл: план -> ревью -> правки -> ... -> имплементация
                                              (без --fg — detached: ре-экзек в новой сессии,
                                              переживает закрытие терминала; --fg — старое поведение)
  merge       --task NAME [--dry-run] [--yes] [--keep-worktree] [--force]   влить принятую worktree-серию
                                              (ветка duo/NAME) в текущую ветку; конфликты видны до касания дерева
  claims      [--task NAME [--drop]]           зоны записи работающих worktree-серий; --drop отпускает зону брошенной
  review      --task NAME --artifact PATH      разовое дуо-ревью артефакта (пилот/ручной режим)
  status      --task NAME [--json] | --all     состояние задачи / сводная таблица всех задач (+ процесс: lock/liveness);
                                              --json — ровно один JSON-объект без прозы (для скриптов оператора)
  wait        --task NAME [--timeout S] [--quiet]   блокирующее ожидание остановки/приёмки; последняя строка
                                              `WAIT-RESULT: done|impl_pending|halted|dead|idle|timeout`, код выхода
                                              0/0/10/11/12/13 — замена самодельным bash-сторожам помощника
  supervise   [--resume] [--dry-run] [--loop]      тик надзора по всем сериям (systemd-timer, см. systemd/install.sh):
                                              dead/halted -> алерт и авто-возобновление transport/crash,
                                              done/impl_pending -> уведомление
  errata      --task NAME [--apply]                перенос эррат председателя принятого ревью в plan_vN.md
                                              (автоматически при приёмке плана; команда — статус/ручной повтор)
  set-verdict --task NAME --verdict ACCEPT|REVISE [--note ...]   ручной вердикт (напр., после своего ревью в редакторе)
  hold|release --task NAME                    DEPRECATED: флаг hold_impl (сторож больше не
                                              автопродолжает impl_pending); поведение сохранено
  rework      --task NAME --phase plan|impl --review FILE [--keep-session] [--run] [--force]
                                              доработка артефакта по внешнему ревью: регистрирует ревью,
                                              ставит маркер, run первым шагом зовёт исполнителя (revise)
  commit-by-plan --task NAME [--version N] [--dry-run] [--yes [--allow-partial]]
                                              прогон коммит-групп из ## commit_plan отчёта имплементации:
                                              diff-stat по группе -> y/n -> add+commit. ПОМОЩНИК человека,
                                              не автопилот: без явного y на каждую группу — ноль коммитов.
                                              Колонки таблицы — по заголовку (файлы/сообщение), brace-глобы
                                              раскрываются; `--yes` — all-or-nothing: любая аномалия плана →
                                              отказ до первого коммита (`--allow-partial` — только валидные)
  gates       --task NAME [--ref REF] [--name NAME] [--only ШАГ,...]
              [--extra PATH ...] [--dry-run] [--keep]   гейты поставки в ЧИСТОЙ копии
                                              (git archive REF): шаги и их argv — из
                                              gates.steps конфига (плейсхолдеры {py}/{pybin}/{cpus};
                                              примеры — docs/PROJECT-SETUP.md); логи и итоговые
                                              строки — в pipeline/<task>/logs/gates_<UTC>/*/summary.txt,
                                              код возврата ≠ 0 при любом красном шаге
  archive     --task NAME [--dry-run] [--yes] [--min-age-days 14] [--archive-root DIR]
              [--force-young] [--print-digest-template] | --list
                                              уборка завершённой серии pipeline/NAME (Правило 5
                                              pipeline/expert/pipeline_cleanup_plan_20261101.md §11):
                                              допуск (слита, карантин ≥14 дн, нет worktree/.lock,
                                              дайджест в ARCHIVE-INDEX.md без <заполнить>, нет ссылок
                                              вне pipeline/) → снимок tar.zst + MANIFEST.tsv в
                                              ~/pipeline_archive/<дата>/series/ → верификация (zstd -t,
                                              sha256, сверка со свежим обходом; расхождение = откат
                                              без удаления) → git rm + удаление остатка → дозапись
                                              pipeline/archive/ARCHIVED.txt. НИЧЕГО не коммитит
                                              (логика: archive_cmd.py рядом с оркестратором)
  projects    [--add PATH] [--remove PATH] [--prune]   реестр проектов для мультипроектного
                                              надзора (supervise без --repo)
  setup       [--yes] [--no-register]        подключить проект: git init (по подтверждению),
                                              каркас .duo/config.json, записи .gitignore,
                                              регистрация в реестре надзора; повтор — no-op
  selftest                                     проверка транспорта (1 дешёвый вызов)

Артефакты (в <repo>/<artifacts_dir>/<task>/):
  task.md, plan_v{N}.md, rev{NNN}_plan.md, impl_report_v{N}.md (в конце — секция
  ## commit_plan: таблица коммит-групп), rev{NNN}_impl.md,
  diff_impl_v{N}.patch, untracked_impl_v{N}.txt, state.json, orchestrator.log,
  transcripts/*.md
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import io
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"

DEFAULTS = {
    # R1 (duoportable): ключа repo больше НЕТ — корень проекта разрешается на каждом
    # запуске: --repo > DUO_REPO > legacy-ключ `repo` слоя --config > автодетект от cwd
    # (resolve_repo ниже). Проектные переопределения — <repo>/.duo/config.json (R2).
    "artifacts_dir": "pipeline",
    "claude_config_dir": "~/.claude-glm",           # ~ и $VAR раскрываются в load_config
    "council_agents_dir": "~/.claude/agents",
    "model": "opus",                    # алиас -> ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.3
    "duo": {
        # персоны «2» (триаж блокер/эррата для plan; impl-триаж и динамика для impl) — по умолчанию;
        # оригиналы socrates/feynman/torvalds/meadows (для /council) не тронуты — `members_*` в config.json
        "members_plan": ["socrates2", "feynman2"],
        "members_impl": ["torvalds2", "meadows2"],
        "rounds": 3,
    },
    # режим pair (`run --mode pair`): исполнитель + ОДИН проверяющий вместо дуэта+председателя.
    # План и код проверяют разные персоны: feynman2 — исполним ли план буквально, сходятся ли
    # числа (триаж блокер/эррата); torvalds2 — делает ли код буквально то, что обещали план,
    # эрраты и отчёт (честность отчёта и тестов, тихие fallback'и).
    "pair": {
        "reviewer_plan": "feynman2",
        "reviewer_impl": "torvalds2",
    },
    # параллельные серии (git.isolation=worktree): защита от пересечения зон записи
    "parallel": {
        "shared_ok": [],                # globs «горячих» файлов, пересечение по которым не блокирует
        "sync_base_at_impl": True,      # перед первой имплементацией подтянуть базу worktree к HEAD основного дерева (только fast-forward, только при чистом worktree)
    },
    "limits": {
        "max_plan_iterations": 4,
        "max_impl_iterations": 4,
        "budget_usd": 25.0,
        # pipev4 П1: эскалация вместо зацикливания — после N итераций без
        # ACCEPT председатель обязан сформулировать ЕДИНСТВЕННОЕ ключевое
        # разногласие и остановить цикл для человеческого set-verdict
        # (вместо тихого продолжения до лимита). До этого порога — обычный
        # цикл доработок (legacy-поведение при escalation_after=None).
        "escalation_after": None,
        # боевой инцидент (17.10.2026): число повторов вызова исполнителя при транспортном сбое
        # claude CLI («API Error: …») с resume той же сессии; 0 = прежнее поведение
        # (ошибка сразу уходит в «артефакт не найден» → needs_human)
        "executor_api_retries": 0,
        # pipework3 (21.10.2026, боевой инцидент: ответ исполнителя v2 оборвался на полуслове,
        # impl_report_v2.md не записан → СТОП «артефакт не найден»): число «дожимающих»
        # ходов (resume той же сессии), если после шага исполнителя обязательный артефакт
        # (plan_vN.md / impl_report_vN.md) не появился. 0 = прежнее поведение.
        "executor_continue_retries": 0,
        # pipework3: повторы вызова ревьюера/председателя при транспортном сбое
        # (idle-kill, claude без result) с паузами api_retry_wait_sec×2^n; 0 = как раньше
        # (RuntimeError валил процесс молча посреди ревью).
        "reviewer_retries": 0,
        # pipework3: «мягкий» бюджет — ревью уже сделанной исполнителем работы доводится до
        # конца, даже если бюджет исчерпан, пока потрачено < budget_usd + budget_grace_usd.
        # Жёсткая проверка бюджета — только на границе итерации (перед работой исполнителя).
        "budget_grace_usd": 0.0,
    },
    "timeouts": {
        "executor_sec": 600,
        "reviewer_sec": 420,
        "chairman_sec": 600,
        "ping_sec": 60,
        "api_retry_wait_sec": 30,       # база экспоненциальной паузы перед повтором (×2, потолок 240)
    },
    # env ТОЛЬКО для вызовов исполнителя (mode="full"), напр. CLAUDE_STREAM_IDLE_TIMEOUT_MS
    "claude_env_full": {},
    "git": {
        "auto_branch": True,
        "branch_prefix": "duo/",
        "isolation": "shared",           # pipev3 P2 (B1): shared | worktree
        # Н5 rev002_impl: каталог worktree-серий; корневой /.worktrees/ в .gitignore
        # захардкожен под ЭТОТ дефолт — при смене обновить паттерн и там
        "worktrees_dir": ".worktrees",
        # R4 (duoportable): имена тяжёлых untracked-ресурсов, симлинкуемых provisioning'ом
        # в worktree серии (provision_worktree.py); пусто — симлинков нет
        "worktree_links": [],
    },
    # R3 (duoportable): гейты полностью из конфига. steps — список {name, argv, pp, timeout,
    # summary}; плейсхолдеры argv: {py} (интерпретатор), {pybin} (его каталог), {cpus}.
    # python: None → sys.executable (интерпретатор оркестратора = выбор лаунчера bin/duo на
    # КАЖДОЙ машине; в проектном конфиге ключ обычно не задаётся — он непереносим между стендами);
    # cpus: None → $DUO_GATES_CPUS (машинная настройка), иначе все доступные процессу ядра; stubs: None/"" → заглушек нет, иначе каталог
    # пакетов-заглушек (относительный путь — от корня проекта). Шагов нет → `gates` отвечает
    # rc=2 с подсказкой.
    "gates": {"cpus": None, "python": None, "stubs": None, "steps": []},
    # R5 (duoportable): пути, которые ревьюер НЕ имеет права откатывать (правки хозяина/
    # оркестратора легитимны): относительно корня repo, fnmatch-глобы допустимы
    "guard": {"never_revert": []},
    "review": {
        "context": "legacy",             # pipev3 P1 (A3-lite): legacy | hybrid
        "pack_max_chars": 60000,
        "plan_policy": "triage",         # triage | legacy: фаза plan по блокерам, эрраты не блокируют (PLAN_POLICY_*)
        "impl_policy": "strict",         # strict | legacy: факт-вывод тестов в отчёте, фикс класса дефекта (IMPL_POLICY_*)
        # guard пустого ревью (pair, 29.10.2026; боевой инцидент: ACCEPT без единого чтения файла).
        # ACCEPT при нуле обращений к инструментам не засчитывается: один повтор с жёстким
        # напоминанием, затем halt kind=review_empty. enabled=false — прежнее поведение.
        "empty_guard": {"enabled": True, "retries": 1},
    },
    "auto_proceed_to_impl": False,      # после ACCEPT плана остановиться и ждать `run --continue`
    "executor_env_hint": "",
    # R7 (duoportable): проектная подсказка в блок изоляции worktree-серий (исполнитель
    # видит её в каждом промпте); пусто — блок без подсказки
    "executor_worktree_note": "",
    # правила длинных прогонов на стенде для исполнителя (EXECUTOR_LONG_RUN_RULES):
    # auto — добавлять, если task.md упоминает ssh/стенд/AMD/nohup/setsid; on | off — принудительно
    "executor_long_run_rules": "auto",
    # свои слова-маркеры для режима auto (имена хостов/стендов проекта): задание, где они
    # встречаются, получает правила длинных прогонов; встроенный набор — LONG_RUN_TASK_RE
    "executor_long_run_markers": [],
    # pipework3: штатный надзор (`wait`, `supervise`, liveness в `status`) вместо внешних bash-сторожей
    "supervisor": {
        "stale_sec": 300,               # state не обновлялся дольше И .lock свободен → оркестратор мёртв
        "dead_max_age_sec": 86400,      # «мёртв» только в пределах суток; старше — idle (давний труп)
        "auto_resume": False,           # supervise сам перезапускает умершие/остановленные (см. kinds)
        "auto_resume_kinds": ["transport", "crash"],   # какие halt можно возобновлять без человека
        "max_resumes": 3,               # потолок авто-возобновлений на задачу за resume_window_sec
        "resume_window_sec": 21600,
    },
    "notify": {"desktop": False},       # notify-send при остановке (в config.json — true)
}


@contextlib.contextmanager
def state_write_lock(task_dir: Path):
    """Короткая эксклюзивная блокировка записи state.json (pipework2 P11).

    flock на дескрипторе КАТАЛОГА задачи (inode каталога стабилен, в отличие от
    state.json, который атомарно подменяется os.replace) — без лишних файлов.
    Берётся на мгновения read-modify-write: save_state оркестратора и hold/release.
    Не связана с .lock (тот держится всю жизнь процесса).
    """
    fd = os.open(str(task_dir), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class ArtifactMissingError(RuntimeError):
    """Артефакт ревью (plan_vN.md / impl_report_vN.md / diff) физически отсутствует —
    ревью не диспетчеризируется (pipev4 П3). pipework2 P12: Pipeline.run ловит это и
    переводит серию в needs_human с причиной, а не умирает с exit 2 при phase=plan
    (сторож видел «оркестратор мёртв», причины в state не было)."""


class EmptyReviewError(RuntimeError):
    """Проверяющий вернул ACCEPT, не обратившись ни к одному инструменту (и повтор тоже):
    вердикт «вакуумный», засчитывать его нельзя. Pipeline.run ловит и ставит halt
    kind=review_empty (возобновляемый: `--resume` повторит ревью)."""


class ClaudeTransportError(RuntimeError):
    """Транспортный сбой claude CLI: idle-kill (нет событий stream-json) либо завершение без
    result-события. pipework3: несёт session_id (приходит в любом событии потока) и
    накопленную стоимость, чтобы повтор продолжил ТУ ЖЕ сессию, а не начинал заново."""

    def __init__(self, msg: str, *, session_id: str | None = None, cost: float = 0.0):
        super().__init__(msg)
        self.session_id = session_id
        self.cost = cost


class BudgetExhausted(RuntimeError):
    """Бюджет серии исчерпан (граница итерации). pipework3: Pipeline.run переводит это в
    чистый halt kind=budget, а не в молчаливую смерть процесса с exit 2."""


class ErrataError(Exception):
    """Перенос эррат председателя из ревью в план не удался (пустой/частичный блок, не
    прошла проверка после записи). НЕ RuntimeError: это не сбой модели, а повод для человека."""


def _read_json_layer(path: Path | None) -> dict | None:
    """Один конфиг-слой: None — файла нет; пустой dict — пустой JSON-объект.
    Битый JSON / не-объект / нечитаемый — SystemExit с путём (не трейсбек)."""
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"конфиг {path}: некорректный JSON — {e}") from None
    except OSError as e:
        raise SystemExit(f"конфиг {path}: не читается — {e}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"конфиг {path}: ожидался JSON-объект, получено {type(data).__name__}")
    return data


def _expand_cfg_paths(cfg: dict) -> None:
    """R2: раскрытие ~ и $VAR в путевых ключах конфига (на месте)."""
    for key in ("claude_config_dir", "council_agents_dir"):
        val = cfg.get(key)
        if isinstance(val, str):
            cfg[key] = os.path.expandvars(os.path.expanduser(val))
    gates = cfg.get("gates")
    if isinstance(gates, dict):
        for key in ("python", "stubs"):
            val = gates.get(key)
            if isinstance(val, str):
                gates[key] = os.path.expandvars(os.path.expanduser(val))


def load_config(path: Path | None, repo: Path | str | None = None) -> dict:
    """Трёхслойная сборка cfg (R2): DEFAULTS ← <инструмент>/config.json ← <repo>/.duo/config.json.
    Слои deep_merge'ятся (списки замещаются целиком), пути раскрываются (~/$VAR).
    `_project_config_path` — путь проектного слоя, если файл существует (иначе None)."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    project_path = Path(repo).resolve() / ".duo" / "config.json" if repo is not None else None
    for layer in (path, project_path):
        user = _read_json_layer(Path(layer) if layer is not None else None)
        if user:
            deep_merge(cfg, user)
    cfg["_project_config_path"] = str(project_path) if project_path and project_path.exists() else None
    _expand_cfg_paths(cfg)
    return cfg


def deep_merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


def detect_repo_root(cwd: Path | None = None) -> Path | None:
    """R1: корень git-проекта от каталога. git-common-dir — ОСНОВНОЕ дерево даже из
    linked worktree (.worktrees/<task>/.git — файл-указатель, а common dir — основной
    .git). None — не в git / git недоступен."""
    try:
        rc, out = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if rc != 0:
        return None
    p = Path(out.strip())
    return p.parent.resolve() if p.name == ".git" else None


def resolve_repo(*, flag: str | None, legacy: str | None, cmd: str,
                 cwd: Path | None = None) -> Path | None:
    """R1: корень проекта. Приоритет: --repo > DUO_REPO > legacy-ключ `repo` слоя --config >
    автодетект от cwd. supervise БЕЗ явного указания — режим реестра (R8): автодетект не
    применяется (иначе таймер надзирал бы за репозиторием, в каталоге которого живёт).
    projects/setup/selftest работают и без корня (None); остальным отсутствие корня —
    SystemExit с подсказкой (не трейсбек)."""
    repo_arg = flag or os.environ.get("DUO_REPO") or None
    if repo_arg:
        p = Path(repo_arg).expanduser()
        if not p.is_dir():
            raise SystemExit(f"--repo/DUO_REPO: каталог не найден: {p}")
        return p.resolve()
    if legacy:
        p = Path(legacy).expanduser()
        if not p.is_dir():
            raise SystemExit(f"ключ repo в конфиге указывает на несуществующий каталог: {p}")
        return p.resolve()
    if cmd == "supervise":
        return None
    root = detect_repo_root(cwd)
    if root is None and cmd not in ("projects", "setup", "selftest"):
        where = cwd or Path.cwd()
        raise SystemExit(
            f"каталог {where} вне git-репозитория — корень проекта не определён. "
            "Запусти из каталога проекта, передай --repo PATH (или экспортируй DUO_REPO), "
            "либо выполни `duo setup` в каталоге проекта")
    return root


def cfg_for_repo(config_path: Path, repo: Path) -> dict:
    """Полный cfg для конкретного проекта (мультипроектный надзор, R8): слои от --config
    и <repo>/.duo/config.json + служебные ключи _config_path/repo."""
    cfg = load_config(config_path, repo=repo)
    cfg["_config_path"] = str(Path(config_path).resolve())
    cfg["repo"] = str(Path(repo).resolve())
    return cfg


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------

def log(task_dir: Path, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(task_dir / "orchestrator.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_json(path: Path, data: dict) -> None:
    """Атомарная запись (pipework2 P4): временный файл в той же директории +
    os.replace. Прежний `write_text` при падении/kill посреди записи оставлял
    усечённый state.json (инцидент потери state целиком, 12.10.2026)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sh(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def artifacts_tracked_in_ref(repo: Path, ref: str, arts_rel: str) -> list[str]:
    """Файлы каталога артефактов серии, закоммиченные в ref (git ls-tree). Для worktree-серии
    это конфликт: в её worktree `<artifacts>/<task>` обязан быть симлинком на основное дерево,
    а закоммиченный каталог в базе ветки — реальный. Ошибка git → пусто (не блокируем)."""
    rc, out = sh(["git", "ls-tree", "-r", "--name-only", ref, "--", arts_rel], cwd=repo)
    return [x for x in out.splitlines() if x.strip()] if rc == 0 else []


def worktree_artifacts_conflict_msg(task: str, artifacts_dir: str, tracked: list[str]) -> str:
    """Единое сообщение для init/run: артефакты worktree-серии закоммичены в базу."""
    rel = f"{artifacts_dir}/{task}"
    what = (f"{len(tracked)} файл(ов), напр. {tracked[0]}" if tracked else "реальный каталог в worktree")
    return (f"worktree-серия {task}: каталог артефактов {rel}/ закоммичен в базу ветки ({what}). В worktree он "
            f"обязан быть симлинком на основное дерево, поэтому серия не стартует. Варианты: другое имя "
            f"серии; либо снять каталог с учёта (решение человека: git rm -r --cached {rel} && git commit); "
            f"либо вести серию без --worktree. Правило: task.md и прочие артефакты worktree-серии "
            f"коммитить только ПОСЛЕ merge серии.")


def is_artifact_path(path: str, artifacts_rel: str) -> bool:
    """pipev3 P1b: единая граница фильтра артефактов задачи.

    path == artifacts_rel ИЛИ path начинается с artifacts_rel + "/". Раньше
    untracked_list резал по bare startswith (вырезал и «pipeline/демо-чужой»),
    а collect_commit_groups — по префиксу со слешем (пропускал запись
    «?? pipeline/<task>» — сам каталог артефактов/симлинк worktree попадал
    в coverage). Теперь обе точки одну и ту же семантику.
    """
    a = artifacts_rel.rstrip("/")
    return path == a or path.startswith(a + "/")


def _remove_path(p: Path) -> None:
    """Убрать путь из рабочего дерева, файл он или каталог (грабля 1: fast-forward
    подтяжки базы может принести реальные pipeline/<task>/… на место симлинка артефактов)."""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Транспорт: claude CLI через профиль claude-glm (Z.AI, glm-5.3)
# ----------------------------------------------------------------------------

class ClaudeGlm:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.config_dir = cfg["claude_config_dir"]
        self.model = cfg["model"]
        self._tls = threading.local()           # статистика вызова: свой счётчик у каждого потока

    # --- статистика обращений к инструментам (guard пустого ревью, 29.10.2026) -------------
    # Боевой инцидент: проверяющий вернул ACCEPT, не прочитав ни одного файла («нет инструментов»),
    # и вердикт был принят «по букве протокола». Транспорт теперь считает tool_use-блоки потока;
    # DuoReviewer сбрасывает счётчик перед вызовом и забирает после (потокобезопасно).

    def reset_call_stats(self) -> None:
        self._tls.stats = {"tool_uses": 0, "tools": {}}

    def take_call_stats(self) -> dict:
        s = getattr(self._tls, "stats", None) or {"tool_uses": 0, "tools": {}}
        return {"tool_uses": int(s["tool_uses"]), "tools": dict(s["tools"])}

    def _count_tool_use(self, name: str) -> None:
        s = getattr(self._tls, "stats", None)
        if s is None:
            self.reset_call_stats()
            s = self._tls.stats
        s["tool_uses"] += 1
        s["tools"][name] = s["tools"].get(name, 0) + 1

    def _base_env(self, mode: str = "plan") -> dict:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = self.config_dir
        env["NO_COLOR"] = "1"
        # claude_env_full — только для вызовов исполнителя (mode="full"): длинные
        # генерации (отчёты/тесты) упираются во встроенный idle-watchdog стрима
        # claude CLI (пол 300 с без байт; повышается лишь CLAUDE_STREAM_IDLE_TIMEOUT_MS)
        # → «API Error: The operation timed out.» (боевой инцидент 17.10.2026, дважды).
        # Ревьюеры (mode="plan") и председатель не затронуты.
        if mode == "full":
            for k, v in (self.cfg.get("claude_env_full") or {}).items():
                if not str(k).startswith("_"):      # «_comment» и т.п. — не переменные окружения
                    env[str(k)] = str(v)
        return env

    def call(
        self,
        prompt: str,
        *,
        mode: str = "plan",                 # plan (read-only) | full (bypass)
        resume_session: str | None = None,
        timeout: int = 420,                 # сейчас: минуты ТИШИНЫ, не общее время
        cwd: Path | None = None,
        tag: str = "",
    ) -> tuple[str, str | None, float]:
        """Потоковый вызов с watchdog по неактивности.

        Процесс жив, пока из stream-json приходят события (tool_use, текст,
        прогресс) — независимо от суммарной длительности. Убивает только
        N минут тишины подряд (idle-таймаут). timeout теперь = секунды тишины.
        Возвращает (text, session_id, cost_usd).
        """
        cmd = ["claude", "-p", "--model", self.model,
               "--output-format", "stream-json", "--verbose"]
        if mode == "plan":
            cmd += ["--permission-mode", "plan"]
        else:
            cmd += ["--dangerously-skip-permissions"]
        if resume_session:
            cmd += ["--resume", resume_session]
        cmd.append(prompt)
        idle_limit = timeout
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=self._base_env(mode),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out_q: queue.Queue[str | None] = queue.Queue()

        def reader():
            try:
                for line in proc.stdout:
                    out_q.put(line)
            finally:
                out_q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        text_parts: list[str] = []
        session_id = None
        cost = 0.0
        got_result = False
        last_activity = time.time()
        idle_killed = False
        while True:
            try:
                line = out_q.get(timeout=15)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                if time.time() - last_activity > idle_limit:
                    idle_killed = True
                    proc.kill()
                    break
                continue
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            last_activity = time.time()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            # pipework3: session_id приходит в каждом событии потока — запоминаем сразу,
            # иначе после idle-kill (result не пришёл) resume оборванной сессии невозможен
            if isinstance(ev, dict) and ev.get("session_id"):
                session_id = ev.get("session_id")
            et = ev.get("type")
            if et == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        self._count_tool_use(str(name))
                        inp = block.get("input", {})
                        brief = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
                        brief = str(brief)[:80]
                        print(f"    · {name}({brief})", flush=True)
            elif et == "result":
                # финальный текст берём из result-события; дубли не копим
                session_id = ev.get("session_id")
                cost = float(ev.get("total_cost_usd") or 0.0)
                got_result = True
                if ev.get("result"):
                    text_parts = [ev["result"]]
        if idle_killed and not got_result:
            raise ClaudeTransportError(
                f"таймаут неактивности {idle_limit}s (нет событий stream-json; процесс убит)",
                session_id=session_id, cost=cost,
            )
        if not got_result:
            rc = proc.poll()
            err = (proc.stderr.read() or "")[:300] if proc.poll() is not None else ""
            raise ClaudeTransportError(f"claude завершился без result-события (rc={rc}): {err}",
                                       session_id=session_id, cost=cost)
        return "".join(text_parts), session_id, cost


# ----------------------------------------------------------------------------
# Council-персоны: извлечение секций из ~/.claude/agents/council-<name>.md
# ----------------------------------------------------------------------------

SECTION_HEADERS = [
    "Identity", "Grounding Protocol", "Analytical Method",
    "What You See That Others Miss", "What You Tend to Miss",
    "When Deliberating in Council", "Output Format (Council Round 2)",
    "Output Format (Standalone)",
]


def _split_md_sections(md: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in md.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            if current:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


def load_member(cfg: dict, name: str) -> dict:
    path = Path(cfg["council_agents_dir"]) / f"council-{name}.md"
    md = path.read_text(encoding="utf-8")
    sections = _split_md_sections(md)
    def sect(h): return sections.get(h, "")
    identity = "\n\n".join(
        f"## {h}\n{sect(h)}" for h in
        ["Identity", "Grounding Protocol", "What You See That Others Miss", "What You Tend to Miss"]
        if sect(h)
    )
    return {
        "name": name,
        "identity": identity,
        "fmt_standalone": sect("Output Format (Standalone)"),
        "fmt_round2": sect("Output Format (Council Round 2)"),
    }


# ----------------------------------------------------------------------------
# Git-хелперы и защита от правок надсмотрщика
# ----------------------------------------------------------------------------

class GitGuard:
    def __init__(self, repo: Path, never_revert: tuple[str, ...] | list[str] = ()):
        # R5 (duoportable): пути, защищённые от отката, приходят из конфига
        # (guard.never_revert; напр. каталог кода, лежащий в дереве самого проекта).
        # Юнит-тесты могут звать без списка — дефолт пуст.
        self.repo = repo
        self.never_revert = tuple(never_revert)

    def status(self) -> str:
        # NUL-separated: надёжный парсинг путей без срезов по индексам
        rc, out = sh(["git", "status", "--porcelain", "-z"], cwd=self.repo)
        if rc != 0:
            return ""
        entries = [e for e in out.split("\0") if e]
        lines = []
        for e in entries:
            if len(e) >= 4:
                lines.append(e[:2] + " " + e[3:])
        return "\n".join(lines)

    def snapshot_before_review(self, artifacts_rel: str) -> str:
        return self.status()

    def enforce_readonly(self, before: str, artifacts_rel: str, task_dir: Path) -> None:
        after = self.status()
        b = set(l for l in before.splitlines() if l.strip())
        a = set(l for l in after.splitlines() if l.strip())
        new = a - b
        if not new:
            return
        # Зоны, где revert не делается никогда: guard.never_revert конфига (R5) и
        # артефакты задач. pipev5 P-parallel: весь корень артефактов (pipeline/) —
        # не территория ревьюера: tracked-файлы ЖИВЫХ параллельных серий (state.json,
        # solo_report) легитимно мутируют во время нашего ревью; откат по
        # снапшоту калечил их (b2bgguf 07.10: три отката state/solo_report).
        # Правку кода ревьюером под pipeline/ фиксируем логом, не revert'ом.
        arts_root_rel = artifacts_rel.rsplit("/", 1)[0]
        never_revert = (*self.never_revert, artifacts_rel, f"{arts_root_rel}/")
        # Пути идут в git ДОСЛОВНО из porcelain текущего status() (NUL-разбор):
        # хак _norm прошлых версий («фолбэк для кривых парсингов») удалён —
        # он обрезал первый символ корневых путей (notes.txt→otes.txt,
        # README.md→EADME.md) и тихо глушил обе защитные ветки (pipev1 П2).
        dirty_tracked = [l[3:] for l in new if l.startswith((" M", "M ", "MM", " D", "D "))]
        untracked = [l[3:] for l in new if l.startswith("??")]
        revertable = [p for p in dirty_tracked if not p.startswith(never_revert)]
        # Untracked-мусор чистим ВСЕГДА, в т.ч. под pipeline/: never_revert
        # (pipev5 P-parallel) защищает только tracked-мутации живых серий;
        # бродячие черновики pipeline/rogue без state.json — мусор ревьюера
        # (pipev1 П2), их задание никогда не отменялось (регресс 9c2912ef).
        foreign_untracked = untracked
        if revertable:
            rc, out = sh(["git", "checkout", "--", *revertable], cwd=self.repo)
            if rc != 0:
                log(task_dir, f"ВНИМАНИЕ: revert не удался ({out.strip()[:120]}) — проверь git status вручную")
            else:
                log(task_dir, f"ОТКАТ: надсмотрщик тронул tracked-файлы {revertable} — восстановлено")
        else:
            foreign_arts = [p for p in dirty_tracked if p.startswith(f"{arts_root_rel}/")
                           and not p.startswith(artifacts_rel)]
            if foreign_arts:
                log(task_dir, f"ИНФО: tracked-файлы параллельных серий изменились за время ревью "
                              f"(не трогаем, это их легитимные мутации): {foreign_arts}")
        if foreign_untracked:
            # pipev1 П2: созданное надсмотрщиком вне зон — удалять (git clean),
            # кроме живых параллельных задач: путь pipeline/<X>/… при существующем
            # state.json (cmd_init пишет его немедленно; бродячие черновики
            # надсмотрщиков state.json не имеют; .lock персонален — два
            # оркестратора на разных задачах сосуществуют законно).
            arts_root_rel = artifacts_rel.rsplit("/", 1)[0]
            own_task = artifacts_rel.rsplit("/", 1)[-1]
            live, to_clean = [], []
            for p in foreign_untracked:
                rest = p[len(arts_root_rel) + 1:] if p.startswith(arts_root_rel + "/") else ""
                first = rest.split("/", 1)[0] if rest else ""
                # Зона артефактов самой серии (pipeline/<own>) и живые параллельные
                # задачи (state.json) — не мусор; остальное под pipeline/ — бродячие
                # черновики ревьюера (pipev1 П2 + регресс 9c2912ef).
                if first and (first == own_task or (self.repo / arts_root_rel / first / "state.json").exists()):
                    live.append(p)
                else:
                    to_clean.append(p)
            if live:
                log(task_dir, f"ПРОПУСК: похоже на живые параллельные задачи (есть state.json), "
                              f"clean не применён: {live}")
            if to_clean:
                rc, out = sh(["git", "clean", "-fd", "--", *to_clean], cwd=self.repo)
                if rc != 0:
                    log(task_dir, f"ВНИМАНИЕ: git clean не удался ({out.strip()[:120]}) — проверь git status вручную")
                else:
                    log(task_dir, f"ОТКАТ: удалены файлы, созданные надсмотрщиком вне зон записи: {to_clean}")

    def head(self) -> str:
        rc, out = sh(["git", "rev-parse", "HEAD"], cwd=self.repo)
        return out.strip() if rc == 0 else ""

    def ensure_branch(self, task: str, cfg: dict, task_dir: Path) -> None:
        if not cfg["git"].get("auto_branch"):
            return
        branch = f"{cfg['git']['branch_prefix']}{task}"
        rc, out = sh(["git", "rev-parse", "--verify", branch], cwd=self.repo)
        if rc == 0:
            rc2, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo)
            if cur.strip() != branch:
                sh(["git", "checkout", branch], cwd=self.repo)
                log(task_dir, f"переключился на ветку {branch}")
        else:
            sh(["git", "checkout", "-b", branch], cwd=self.repo)
            log(task_dir, f"создана ветка {branch}")

    def diff_patch(self, base: str, out_file: Path) -> None:
        rc, out = sh(["git", "diff", base], cwd=self.repo, timeout=180)
        out_file.write_text(out, encoding="utf-8")

    def untracked_list(self, base_status_ignore: str, out_file: Path, artifacts_rel: str) -> None:
        st = self.status()
        # pipev3 P1b: is_artifact_path — единая граница с collect_commit_groups
        lines = [l[3:] for l in st.splitlines()
                 if l.startswith("??") and not is_artifact_path(l[3:], artifacts_rel)]
        out_file.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------
# Дуо-консилиум (надсмотрщик)
# ----------------------------------------------------------------------------

ANTI_CONFORMITY = """**Анти-конформистская директива.** Если твоя позиция R1 верна — защищай её.
Не обновляй позицию лишь потому, что оппонент не согласен или формируется консенсус.
Обновляй позицию только если предъявлена конкретная ошибка в твоём аргументе —
и назови её явно. Не можешь назвать ошибку — не обновляй позицию."""

OVERSEER_RULES = """Правила надсмотрщика (строго):
- Ты НЕ правишь код, план и любые файлы. Только читаешь репозиторий (Read/Grep/Glob) и рассуждаешь.
- Не создавай и не изменяй файлы вне своего объекта ревью; черновики следующих задач — не твоя работа; идеи оформляй текстом в отчёте.
- Каждое замечание обязано иметь координаты (файл:строки или файл) и проверку по фактическому коду/тексту, не по догадке.
- Не выдумывай факты: не можешь проверить — пометь «не проверено».
- Различай: блокер (нельзя принимать), существенное (нужно поправить), косметика (можно отметить)."""

# plan-policy (опционально, review.plan_policy="triage"; по умолчанию "legacy" —
# все промпты байт-в-байт прежние): фаза plan судится по БЛОКЕРАМ, а не по числу
# замечаний. Мотив — серия ingestclient: 9 из 10 ревью = REVISE по замечаниям,
# чинимым «одним-двумя предложениями» (председатель сам писал «правка — один абзац,
# архитектура не меняется»), при этом механизма ACCEPT-с-эрратами в фазе plan нет.
PLAN_POLICY_TRIAGE = "triage"
STANCE_HINT_LEGACY = ("(accept = артефакт можно принимать без правок; "
                      "revise = есть замечания, требующие доработки.)")
STANCE_HINT_TRIAGE = ("(accept = блокеров нет — эрраты перечисли отдельным списком, они вердикт не меняют; "
                      "revise = есть хотя бы один БЛОКЕР со сценарием отказа.)")

PLAN_POLICY_CHAIRMAN_BLOCK = """
Политика приёмки плана (фаза plan; уточняет формат выше и при расхождении приоритетнее него и любых определений «блокера» в тексте задачи, включая аддендумы):
1. Категорий замечаний три: БЛОКЕР / ЭРРАТА / КОСМЕТИКА (слово «существенное» не используй). `VERDICT: REVISE` — только если есть хотя бы один БЛОКЕР; эрраты и косметика на вердикт не влияют.
2. Тест «эрраты-переопределения»: эрраты плана главнее его тела (при расхождении исполнитель следует эрратам). Поэтому дефект — ЭРРАТА, если его можно закрыть самодостаточной эрратой-РЕШЕНИЕМ до ~15 строк: что именно делать и что она заменяет («Заменяет: §…, DoD-тест …»). Это верно и при серьёзном дефекте, и при затронутых ≥2 срезах, и при выборе из нескольких вариантов, и когда тело плана придётся переписать (его перепишут при имплементации по эрратам).
3. БЛОКЕР — только если эрратой дефект закрыть нельзя: (а) решение принадлежит владельцу либо меняет требование/решение ТЗ (необратимо для данных или стоимости); (б) нужен новый механизм/подсистема, которую не описать в ~15 строк; (в) исполнитель не может начать срез, не получив ответа. К блокеру обязательны сценарий отказа (≤3 шага) и предписанный ремонт. Тяжесть последствий дефекта блокером его не делает.
4. Вариантов несколько — выбери САМ (по ТЗ и принципу «минимальная дельта текста плана») и запиши как `Е№ (РЕШЕНИЕ)`: что / что заменяет / тест. Формулировка «рекомендую (а), выбор за исполнителем» запрещена: рекомендация председателя — директива.
5. Дефект платформы вне фичи: если он воспроизводится и без этой фичи (в существующем пути кода), это не блокер плана и не работа плана. Запиши его в «К владельцу» и, если нужно, дай эрратой минимальную защиту фичи (проверка/ограничение v1); план не обязан чинить платформу.
6. Антитредмилл: сверь замечания с последними `rev*_plan.md` каталога артефакта. Если предыдущее ревью уже дало БЛОКЕР в той же подсистеме, второй подряд блокер допустим только по п.3(а); иначе оформи эррату-РЕШЕНИЕ. Если дефект внесён правкой предыдущей итерации, сначала оцени откат этой правки как вариант.
7. Размер плана: если за последнюю итерацию он вырос более чем на 5 % без нового требования ТЗ, потребуй в сводке вносить решения эрратами, а не вставками в тело, и назови, что вытеснено.
8. «Сводка для исполнителя»: при REVISE — блокеры отдельно от эррат. При ACCEPT вместо «правок не требуется» выведи готовый блок `## Эрраты к плану` (заголовок СТРОГО такой, на своей строке; затем нумерованный список: `Е№ (серьёзность)`: координаты / что дописать или РЕШЕНИЕ / что заменяет; без заголовков уровня `##` и выше внутри блока) — оркестратор дословно допишет его в конец плана перед имплементацией; без этого блока эрраты потеряются.
9. Если ответ участника — заглушка (нет анализа и/или STANCE; упоминание ExitPlanMode или файла в `/plans/`), напиши в отчёте «ответ участника (раунд N) утрачен» и не опирайся на его пересказ.
"""

# Правила исполнителя при правке плана (только review.plan_policy="triage").
# Мотив — ingestclient v10→v12: исполнитель отвечал на каждый блокер вставками в
# тело плана (987→919→964→1058 строк) и дублировал их в «Эрратах» «якорем».
PLAN_POLICY_EXECUTOR_BLOCK = """Правила правки плана (политика triage):
1. Размер плана не растёт без нового требования ТЗ. Замена механизма — это УДАЛЕНИЕ прежнего текста, а не дополнение к нему. В ответе укажи число строк плана до/после.
2. Решения председателя `Е№ (РЕШЕНИЕ)` принимай как есть: выбор уже сделан, не пересматривай и не расширяй его.
3. Решения, эрраты и «К владельцу» вноси в раздел `## Эрраты к плану` — он последний в файле, заголовок один; прежние эрраты сохраняй. В теле плана заменяемое место отмечай одной строкой `[заменено: Е№]` вместо переписывания абзацев; правь тело, только если без этого решение недостижимо.

"""

# impl-policy (опционально, review.impl_policy="strict"; по умолчанию выкл — все промпты
# байт-в-байт прежние). Мотив — impl-ревью серий a3run/a3eval: (1) ревьюеры и председатель
# тесты не запускают («фактический прогон не выполнялся — песочница»), «N passed» сверялся
# подсчётом тест-функций; (2) a3run rev002: фикс замечания rev001 закрыл только названное
# место, аналоги (m2/rb) остались — лишний круг impl.
IMPL_POLICY_STRICT = "strict"
IMPL_POLICY_CHAIRMAN_BLOCK = """
Политика приёмки имплементации (фаза impl; дополняет формат и чек-листы выше, не отменяет их):
1. Тесты и ruff ревью не запускает. В `impl_report` обязателен ФАКТИЧЕСКИЙ вывод команд (хвост вывода с итоговой строкой), а не пересказ; пересказ без вывода — существенное замечание. В отчёте прямо напиши, что прогоны ревью не перегонялись.
2. Для каждого замечания ПРЕДЫДУЩЕГО ревью проверь по коду, что исправлен КЛАСС дефекта на всём периметре задачи (аналоги найди grep'ом сам), а не только названная строка. Частичный фикс — повторный блокер: перечисли непокрытые места.
3. Расхождение имплементации с планом, эрратой или ТЗ без записи в «отклонениях» отчёта — блокер; задокументированное отклонение, не нарушающее ТЗ и DoD, — не блокер.
"""
IMPL_POLICY_EXECUTOR_REPORT = (
    "В `impl_report` приведи ФАКТИЧЕСКИЙ вывод (хвост с итоговой строкой) каждой запущенной команды тестов "
    "и ruff, не пересказ: ревью тесты не запускает и сверяет отчёт только статически.\n"
)
IMPL_POLICY_EXECUTOR_REVISE = (
    "Для КАЖДОГО замечания определи класс дефекта и найди все его экземпляры в периметре задачи (grep); "
    "исправь все и перечисли в ответе «замечание → найденные и исправленные места». Исправление только "
    "названной строки при существующих аналогах — повтор замечания.\n"
)

# pipev4 П2: чек-лист пре-регистрации для план-ревью (R1 дуэта и единственный проверяющий pair)
PREREG_CHECKLIST_PLAN = """Чек-лист пре-регистрации плана (проверь ДО вердикта): если план содержит измерения/метрики/сравнения «до-после» — закреплены ли ДО прогона: (1) единица счёта и знаменатель каждой метрики; (2) инварианты данных/корпуса; (3) пороги решений (что считается успехом/провалом); (4) правила разбора спорных случаев. Отсутствие пре-регистрации при наличии измерений — существенное замечание (после прогона это станет post-hoc рационализацией). Плана без измерений чек-лист не касается.

"""

# Параллельные серии (git.isolation=worktree): исполнитель обязан заявить зону записи плана —
# по ней оркестратор не пускает в имплементацию серию, чья зона пересекается с зоной уже
# работающей (claim-gate). Для shared-серий этих блоков в промптах нет (байт-в-байт прежние).
FILES_TOUCH_PLAN_BLOCK = """

В САМОМ КОНЦЕ плана — обязательная секция `## files_touch`: список путей (от корня репо; допустимы glob и каталоги с `/` на конце), которые имплементация ИЗМЕНИТ, СОЗДАСТ или УДАЛИТ. Только запись: файлы, которые лишь читаются, не перечисляй; каталог артефактов серии `pipeline/<task>/` не перечисляй. Формат — маркированный список, один путь на строку, путь в бэктиках. Заголовок пиши ровно `## files_touch` — без номера и дополнений (`## 12. files_touch` и подобные варианты разбор зоны не увидит, защита пересечений не сработает). По этой зоне оркестратор не допускает пересечения параллельных серий: слишком широкая зона блокирует их, слишком узкая — выход имплементации за неё будет замечанием ревью."""
FILES_TOUCH_REVISE_PLAN_BLOCK = """
Секцию `## files_touch` (зона записи) сохрани в конце плана; обнови, если зона изменилась. Заголовок — без номера."""
FILES_TOUCH_IMPL_BLOCK = (
    "Зона записи — секция `## files_touch` плана: за её пределы не выходи; необходимость выйти — "
    "отклонение с обоснованием в отчёте.\n"
)

# Режим pair: единственный проверяющий совмещает роли критика и председателя. Персоны «2» написаны
# под дуэт (STANCE, раунды R2/R3, «в этом дуо accept означает…») — блок переопределяет эти места
# и закрывает главный риск одного рецензента: его слепую зону больше никто не прикроет.
PAIR_REVIEWER_RULES = """Режим одного проверяющего (приоритетнее формулировок твоей роли про дуэт):
- Формулировки роли про «дуо», оппонента, раунды R2/R3 и «смену позиции» к тебе не применяются; STANCE читай как итоговый вердикт: `accept` = `VERDICT: ACCEPT`, `revise` = `VERDICT: REVISE`. Правила триажа и калибровки из твоей роли действуют полностью.
- Слепую зону из раздела «What You Tend to Miss» никто не прикроет: перед вердиктом один раз проверь её на этом артефакте (одна-две строки в разделе 2 отчёта).
- Ты не подгоняешь вердикт под ожидания исполнителя: ACCEPT — только если обязательных правок нет; в «Сводке для исполнителя» только то, что войдёт в следующую итерацию."""

# Сигнатуры «ответа-заглушки» ревьюера (инцидент ingestclient rev011: в режиме plan
# модель вызвала ExitPlanMode и ушла в plan-файл — в стенограмме остался пересказ из
# двух строк, R2 участника не дошёл ни до R3, ни до председателя).
REVIEWER_STUB_RE = re.compile(r"ExitPlanMode|[/\\]\.claude[\w.-]*[/\\]plans[/\\]")
REVIEWER_STUB_RETRY_SUFFIX = (
    "\n\nПредыдущая попытка вернула заглушку (пустой ответ либо ссылка на ExitPlanMode / файл плана). "
    "Выведи полный ответ ПРЯМО в тексте ответа: не вызывай инструменты планирования, "
    "не записывай ответ в файлы; заверши блоком STANCE по формату выше."
)

# Транспортный сбой claude CLI: текст результата целиком = «API Error: <причина>»
# (например «The operation timed out.» — idle-watchdog стрима, боевой инцидент 17.10.2026).
# Узкая сигнатура — только начало ответа: нормальный ответ, где фраза встречается
# внутри текста (цитата лога), не срабатывает.
API_ERROR_RE = re.compile(r"^\s*API Error\b", re.IGNORECASE)

EXECUTOR_API_RETRY_PROMPT = (
    "Предыдущий ход оборвался ТЕХНИЧЕСКОЙ ошибкой связи с API («{err}»): это сбой "
    "канала, не твоя ошибка и не часть задания. Контекст сессии и все правки в "
    "рабочем дереве сохранены. Продолжи с места обрыва: коротко сверь, что уже "
    "сделано (git status / git diff --stat, существующие артефакты серии), и доделай "
    "ВСЁ, что требовалось последним заданием этой сессии, включая обязательные "
    "артефакты и итоговый ответ, — не повторяя уже выполненную работу. Большие файлы "
    "(отчёты, объёмные тесты) пиши ЧАСТЯМИ (несколько Write/Edit по ~10–12 КБ), а не "
    "одним огромным вызовом: именно длинная однократная генерация обычно и обрывается."
)


EXECUTOR_ARTIFACT_CONTINUE_PROMPT = (
    "Предыдущий ход завершился, но ОБЯЗАТЕЛЬНЫЙ артефакт `{path}` НЕ записан (ответ оборван "
    "посреди работы либо отчёт не сохранён на диск). Контекст сессии и все правки в рабочем "
    "дереве сохранены. Сверь состояние (git status / git diff --stat, существующие файлы серии), "
    "доделай оставшуюся работу последнего задания и ОБЯЗАТЕЛЬНО запиши `{path}` целиком "
    "({what}). Большие файлы пиши ЧАСТЯМИ (несколько Write/Edit по ~10–12 КБ), а не одним "
    "огромным вызовом. Не повторяй уже выполненную работу."
)

EXECUTOR_RESUMED_PREFACE = (
    "ВНИМАНИЕ: предыдущая попытка этого шага была ПРЕРВАНА (процесс оркестратора или вызов "
    "исполнителя оборвался). Часть работы могла быть уже сделана: сначала сверь состояние "
    "(git status / git diff --stat, существующие файлы серии) и доделывай недостающее, "
    "не переписывая готовое. Задание шага — ниже, без изменений.\n\n"
)


def is_api_error_text(text: str | None) -> bool:
    """Ответ — транспортная ошибка claude CLI («API Error: …»), а не результат модели."""
    return bool(text) and bool(API_ERROR_RE.match(text))


# --- guard пустого ревью (29.10.2026, боевой инцидент: ревью impl) ---------------------------
# Проверяющий вернул «VERDICT: ACCEPT», признав в тексте «нет инструментов / файл не читан», и
# оркестратор принял вердикт «по букве протокола». Критерий пустоты — ФАКТ из транспорта (число
# tool_use-блоков в потоке), а не слова ревьюера; слова — только вспомогательный признак там, где
# контекст ревью уже включён в промпт (review.context=hybrid: читать файлы можно не всегда).
EMPTY_REVIEW_UNVERIFIED_RE = re.compile(
    r"не\s+(?:был[аио]?\s+)?(?:читан|прочитан|проверен|проверял|выполнял|запускал|смотрел)\w*"
    r"|нет\s+(?:доступа|инструмент)\w*|без\s+инструмент\w*|инструмент\w*\s+(?:недоступн|не\s+доступн)\w*"
    r"|no\s+tools|did\s+not\s+read|not\s+read",
    re.IGNORECASE,
)
EMPTY_REVIEW_RETRY_SUFFIX = (
    "\n\nПредыдущая попытка отклонена оркестратором: ACCEPT без единого обращения к инструментам "
    "({reason}). Инструменты у тебя ЕСТЬ: Read, Glob, Grep (и Bash в пределах режима) — "
    "артефакты и дифф лежат на диске по путям из задания выше. Прежде чем выносить вердикт, "
    "ПРОЧИТАЙ файлы и сверь не менее трёх конкретных утверждений отчёта с кодом/выводом; "
    "каждую сверку отрази в отчёте (файл:строка → что увидел). ACCEPT без чтения недопустим: "
    "если прочитать не удалось — вердикт REVISE с указанием, что именно не удалось проверить."
)


def empty_review_reason(cfg: dict, verdict: str | None, report: str, stats: dict | None) -> str | None:
    """Причина, по которой ревью «пустое» (вердикт ACCEPT без проверки), либо None.

    stats — {"tool_uses": N, …} из транспорта; None = транспорт статистику не ведёт (тестовые
    заглушки) → guard молчит. Срабатывает ТОЛЬКО на ACCEPT: REVISE без чтения вреда не
    причиняет (исполнитель получит замечания и перепроверит), а вакуумный ACCEPT — пропускает
    непроверенную работу. review.context=legacy: проверяющий обязан читать файлы сам → ноль
    tool_use достаточен; hybrid: контекст уже в промпте → нужен ещё признак «не читано» в тексте."""
    rv = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
    g = rv.get("empty_guard") if isinstance(rv.get("empty_guard"), dict) else {}
    if not g.get("enabled", True) or verdict != "ACCEPT" or not isinstance(stats, dict):
        return None
    if int(stats.get("tool_uses", 0) or 0) > 0:
        return None
    ctx = rv.get("context", "legacy")
    confessed = EMPTY_REVIEW_UNVERIFIED_RE.search(report or "")
    if ctx == "hybrid" and not confessed:
        return None
    return (f"0 обращений к инструментам при ACCEPT (review.context={ctx})"
            + (f"; в отчёте: «{confessed.group(0)}»" if confessed else ""))


# --- правила длинных прогонов на стенде для исполнителя (29.10.2026, грабли 6–10) ----------------
# Источник — реальные потери серий (боевая эксплуатация): драйвер, убитый вместе с сессией ssh;
# скрипт, перезаписанный под работающим драйвером; запятая вместо точки в замерах (локаль);
# flock-дескриптор, унаследованный фоновым ребёнком; серия без impl_report при живых прогонах.
EXECUTOR_LONG_RUN_RULES = """
ДЛИННЫЕ ПРОГОНЫ НА СТЕНДЕ (обязательные правила — это уже стоило нам серий):
- Всё, что идёт дольше ~10 минут (замеры, прогоны корпуса, пулы), запускай ОТВЯЗАННО от своей сессии: `setsid nohup <cmd> > <лог> 2>&1 < /dev/null &`, запиши PID и путь лога в отчёт. Прямой запуск в foreground-вызове инструмента или `ssh host cmd &` умирает вместе с сессией/ssh.
- Имена скриптов-драйверов УНИКАЛЬНЫ на запуск (метка времени/номер ступени в имени). Никогда не перезаписывай файл скрипта, который может исполняться: bash читает скрипт по мере выполнения — правка «на лету» ломает работающий драйвер. Новая версия = новый файл.
- Числовой вывод и разбор чисел — под `LC_ALL=C` (иначе на ru_RU запятая вместо точки ломает awk/sort/python float). Время/метки в логах — ISO, UTC либо явно с зоной.
- `flock`: дескриптор блокировки НЕ должен наследоваться фоновыми детьми — запускай ребёнка как `setsid nohup … 9>&-`, либо используй `flock -o`; иначе блокировка живёт, пока жив любой потомок, и следующий запуск «занят».
- Состояние прогона — в файле (`*.status`/JSON с этапом и счётчиками), а не только в памяти сессии: после обрыва ты (или оркестратор по `--resume`) должен продолжить с места, а не гадать по `ps`.
- Остановка своего процесса — только по сохранённому PID/точному слушателю порта; `pkill -f` с шаблоном, совпадающим с твоей же командой, запрещён.
- ОБЯЗАТЕЛЬНЫЙ артефакт `impl_report_vN.md` пиши РАНО и обновляй по ходу (первый вариант — сразу после запуска прогонов: что запущено, PID, ожидаемое окончание, критерии), а не «когда всё досчитается»: серия без отчёта при живых прогонах — остановка `artifact_missing`.
"""

LONG_RUN_TASK_RE = re.compile(r"\bssh\b|стенд|\bAMD\b|\bamd\b|nohup|setsid", re.IGNORECASE)


def long_run_rules_enabled(cfg: dict, task_text: str) -> bool:
    """executor_long_run_rules: on | off | auto (auto — если задание упоминает ssh/стенд/AMD/nohup…
    либо слово из executor_long_run_markers проекта)."""
    mode = str(cfg.get("executor_long_run_rules", "auto")).lower()
    if mode in ("on", "true", "1"):
        return True
    if mode in ("off", "false", "0"):
        return False
    if LONG_RUN_TASK_RE.search(task_text or ""):
        return True
    extras = [str(m) for m in (cfg.get("executor_long_run_markers") or []) if str(m)]
    return bool(extras) and bool(re.search("|".join(re.escape(m) for m in extras), task_text or "",
                                           re.IGNORECASE))


def plan_triage_enabled(cfg: dict) -> bool:
    """review.plan_policy == "triage". Дефолт ("legacy" / ключа нет / review не dict) — False."""
    rv = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
    return rv.get("plan_policy") == PLAN_POLICY_TRIAGE


def impl_strict_enabled(cfg: dict) -> bool:
    """review.impl_policy == "strict". Дефолт (ключа нет / "legacy" / review не dict) — False."""
    rv = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
    return rv.get("impl_policy") == IMPL_POLICY_STRICT


_PLAN_VER_RE = re.compile(r"^(?P<base>.*/)?plan_v(?P<n>\d+)\.md$")


def plan_size_trend(repo: Path, artifact_rel: str, depth: int = 4) -> str:
    """Динамика размера плана (строк) по последним `depth` версиям рядом с artifact_rel.

    Пусто, если artifact_rel — не `plan_vN.md` или версий найдено меньше двух
    (тогда промпт председателя не получает лишней строки)."""
    m = _PLAN_VER_RE.match((artifact_rel or "").strip())
    if not m:
        return ""
    base, n = m.group("base") or "", int(m.group("n"))
    pts: list[tuple[int, int]] = []
    for k in range(max(1, n - depth + 1), n + 1):
        f = Path(repo) / f"{base}plan_v{k}.md"
        try:
            pts.append((k, len(f.read_text(encoding="utf-8").splitlines())))
        except OSError:
            continue
    if len(pts) < 2 or pts[-2][1] <= 0:
        return ""
    last, prev = pts[-1][1], pts[-2][1]
    series = " → ".join(f"v{k}: {ln}" for k, ln in pts)
    return (f"Динамика размера плана (строк): {series}; последняя итерация "
            f"{(last - prev) / prev * 100:+.1f} %.\n")


# chmguard R3: макс. число попыток председателя (1 основная + 2 ретрая по
# дегенеративному выводу). В config.json сознательно не вынесено (граница задачи).
CHAIRMAN_MAX_ATTEMPTS = 3

# pipev1 П1 (Д-11): сколько родитель ждёт detached-ребёнка, чтобы отличить
# «живёт и работает» от «умер сразу» (нет задачи / .lock занят / битый конфиг).
# 2 секунды пренебрежимы на фоне цикла в часы. В config.json сознательно не
# вынесено (граница задачи, прецедент CHAIRMAN_MAX_ATTEMPTS).
DETACHED_CONFIRM_SEC = 2.0

# ----------------------------------------------------------------------------
# pipev3 P1 (A3-lite): контекст-пак итерации + read-манифест (флаг review.context)
# ----------------------------------------------------------------------------

DEFAULT_PACK_MAX_CHARS = 60000

# pipework2 P6: текст задания в промптах НЕ режется «молча» ([:4000] у ревьюеров и
# [:3000] у исполнителя скрывали хвост task.md — у a3int 16 922 симв., ревьюеры
# видели 4000). Теперь — полный текст; предохранитель — только от патологии,
# с явной пометкой, что текст обрезан и где читать полностью.
TASK_TEXT_SAFEGUARD = 60000
ERRATA_PROMPT_CAP = 30000


def task_for_prompt(task_text: str, cap: int = TASK_TEXT_SAFEGUARD, path_hint: str = "") -> str:
    """Полный текст задания для промпта; до cap символов — байт-в-байт как есть."""
    if len(task_text) <= cap:
        return task_text
    where = f" (`{path_hint}`)" if path_hint else ""
    return (task_text[:cap]
            + f"\n\n[... текст задания обрезан оркестратором: показано {cap} из {len(task_text)} симв. "
              f"— прочитай полный файл task.md{where} ...]")


# pipework2 P7: конвенция «раздел `## Эрраты…` в конце плана» (эксперт дописывает его
# после приёмки плана). Оркестратор подмешивает раздел в промпты impl-фазы:
# исполнитель обязан его реализовать, ревьюеры/председатель — проверить по коду.
ERRATA_HEADING = re.compile(r"^#{1,6}[ \t]+эрраты\b.*$", re.I | re.M)


def extract_errata(plan_text: str) -> str:
    """Раздел эррат плана: от заголовка `## Эрраты…` (регистр любой) до конца файла.

    Пустая строка — раздела нет (тогда промпты не меняются ни на байт).
    """
    m = ERRATA_HEADING.search(plan_text or "")
    if not m:
        return ""
    text = plan_text[m.start():].strip()
    if len(text) > ERRATA_PROMPT_CAP:
        text = text[:ERRATA_PROMPT_CAP] + "\n[... эрраты обрезаны оркестратором — читай план целиком ...]"
    return text


def read_plan_errata(base: Path, plan_rel: str) -> str:
    """Эрраты плана по относительному пути (пусто при отсутствии файла/раздела)."""
    if not plan_rel:
        return ""
    try:
        return extract_errata((Path(base) / plan_rel).read_text(encoding="utf-8"))
    except OSError:
        return ""


# pipework3: автоперенос блока «Эрраты к плану» из принятого ревью плана в сам план.
# Раньше перенос делал помощник вручную и регулярно забывал (10 из 17 серий: блок остался
# только в revNNN_plan.md, исполнитель имплементировал план БЕЗ эррат). Надёжность — не в
# «умном» разборе: механика детерминирована (заголовок на своей строке, текст берётся
# дословно ДО машинной строки VERDICT, вне ограждённых блоков кода), любая неоднозначность —
# ErrataError (остановка), а не молчаливая порча плана. Проверка после записи; идемпотентно.
ERRATA_REV_HEADING = re.compile(
    r"^(#{1,6})[ \t]+(?:\d+[.)][ \t]+)?эрраты(?:[ \t]+к[ \t]+плану)?[ \t]*:?[ \t]*$", re.I)
_ERRATA_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_VERDICT_LINE = re.compile(r"\**\s*VERDICT:\s*\**\s*(ACCEPT|REVISE)\s*\**", re.I)
_FALLBACK_NOTE = re.compile(r"^\*?\(дописано оркестратором:.*\)\*?$")


def _errata_norm(text: str) -> str:
    return re.sub(r"[\s*_`]+", " ", text).strip().lower()


def extract_chairman_errata(rev_text: str) -> str | None:
    """Тело блока `## Эрраты к плану` из ревью председателя (без заголовка), дословно.

    None — блока нет. Строка-заголовок ищется вне ограждённых блоков кода, берётся ПОСЛЕДНЯЯ;
    конец — следующий заголовок того же или более высокого уровня либо конец текста; машинная
    строка VERDICT и пометка оркестратора с хвоста отрезаются.
    """
    lines = (rev_text or "").splitlines()
    in_fence = False
    start = level = None
    for i, ln in enumerate(lines):
        st = ln.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ERRATA_REV_HEADING.match(ln.rstrip())
        if m:
            start, level = i, len(m.group(1))
    if start is None:
        return None
    end = len(lines)
    in_fence = False
    for i in range(start + 1, len(lines)):
        st = lines[i].lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})[ \t]+\S", lines[i])
        if m and len(m.group(1)) <= level:
            end = i
            break
    body = lines[start + 1:end]
    while body and (not body[-1].strip() or _VERDICT_LINE.fullmatch(body[-1].strip())
                    or _FALLBACK_NOTE.match(body[-1].strip())):
        body.pop()
    return "\n".join(body).strip("\n").strip()


def _errata_signatures(body: str) -> list[str]:
    """Отпечатки пунктов блока (нормализованный фрагмент середины текста пункта — без «Е№»)."""
    items = [m.group(1) for ln in body.splitlines() if (m := _ERRATA_ITEM.match(ln))]
    if not items:
        items = [body]
    sigs = []
    for it in items:
        n = _errata_norm(it)
        sigs.append(n[20:90] if len(n) > 90 else n[min(len(n) // 4, 20):])
    return [x for x in sigs if x]


def apply_accept_errata(rev_path: Path, plan_path: Path) -> tuple[str, dict]:
    """Дописать эрраты председателя (rev_path) в конец плана (plan_path).

    Возвращает (status, info): "applied" — дописано (info: items, sha); "present" — уже есть
    (маркер оркестратора либо все пункты найдены в разделе эррат плана — перенесено вручную);
    "none" — в ревью блока нет. ErrataError: пустой блок, частичное присутствие, ЭРРАТА-метки
    при отсутствии блока, не прошла проверка после записи (план восстанавливается).
    """
    rev_text = Path(rev_path).read_text(encoding="utf-8")
    body = extract_chairman_errata(rev_text)
    if body is None:
        if re.search(r"\(\s*ЭРРАТ[АЫ]\b", rev_text):
            raise ErrataError(f"в {Path(rev_path).name} замечания помечены «ЭРРАТА», но блока "
                              f"`## Эрраты к плану` нет — перенос невозможен без догадок")
        return "none", {}
    if not body.strip():
        raise ErrataError(f"блок `## Эрраты к плану` в {Path(rev_path).name} пуст")
    rev_name = Path(rev_path).name
    plan_text = Path(plan_path).read_text(encoding="utf-8")
    if f"<!-- duo:errata {rev_name} " in plan_text:
        return "present", {}
    sigs = _errata_signatures(body)
    m = ERRATA_HEADING.search(plan_text)
    section = _errata_norm(plan_text[m.start():]) if m else ""
    covered = [sg in section for sg in sigs]
    if covered and all(covered):
        return "present", {}
    if any(covered):
        raise ErrataError(f"эрраты из {rev_name} перенесены в {Path(plan_path).name} частично "
                          f"({sum(covered)} из {len(sigs)} пунктов) — доделай вручную, чтобы не задвоить")
    import hashlib
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    block = f"<!-- duo:errata {rev_name} sha256:{sha} -->\n{body}\n<!-- /duo:errata {rev_name} -->\n"
    if m:
        add = f"\n\n### Эрраты приёмки {rev_name} (председатель дуэта)\n\n{block}"
    else:
        add = (f"\n\n## Эрраты к плану\n\nИсточник: {rev_name} (председатель дуэта; "
               f"дописано оркестратором).\n\n{block}")
    new_text = plan_text.rstrip("\n") + add
    tmp = Path(str(plan_path) + ".errata.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, plan_path)
    check = _errata_norm(extract_errata(Path(plan_path).read_text(encoding="utf-8")))
    if not all(sg in check for sg in sigs):
        Path(plan_path).write_text(plan_text, encoding="utf-8")        # откат
        raise ErrataError(f"проверка после записи не прошла (эрраты {rev_name} не найдены в плане) — план восстановлен")
    return "applied", {"items": len(sigs), "sha": sha}


def errata_block_reviewer(plan_rel: str, errata: str) -> str:
    """Блок R1-ревьюера impl-фазы (пусто, если эррат нет)."""
    if not errata:
        return ""
    return f"""Принятый план: `{plan_rel}`. Его раздел «Эрраты» — ОБЯЗАТЕЛЬНЫЕ поправки, при расхождении приоритетнее основного текста плана:
\"\"\"{errata}\"\"\"
Чек-лист эррат (проверь ДО вердикта): для КАЖДОЙ эрраты найди её реализацию в диффе/коде (файл:строка или тест). Нереализованная эррата — либо реализованная вопреки её тексту — БЛОКЕР (не «существенное»), даже если отчёт исполнителя заявляет «реализовано»: отчёт — самоаттестация, сверяй по коду. Правило эксперта: в `impl_report` обязательна таблица «эрраты → реализация» (файл:строка или тест для каждой эрраты); её отсутствие — существенное замечание.

"""


def errata_block_chairman(plan_rel: str, errata: str) -> str:
    """Блок председателя impl-фазы (пусто, если эррат нет)."""
    if not errata:
        return ""
    return f"""
Эрраты принятого плана (`{plan_rel}`, раздел «Эрраты») — обязательные поправки, приоритетнее основного текста плана:
\"\"\"{errata}\"\"\"
Чек-лист эррат (обязателен): каждая эррата должна быть реализована — проверь по диффу и коду сам, не по отчёту исполнителя. Хотя бы одна нереализованная эррата — блокер и VERDICT: REVISE, независимо от STANCE участников. Отсутствие в `impl_report` таблицы «эрраты → реализация» — замечание (существенное), без исправления — REVISE.
"""


def errata_block_executor(plan_rel: str, errata: str, revise: bool = False) -> str:
    """Блок исполнителя (implement/revise_impl): пусто, если эррат нет."""
    if not errata:
        return ""
    head = ("Проверь, что КАЖДАЯ эррата ниже реализована, и обнови таблицу «эрраты → реализация» в отчёте."
            if revise else
            f"Перед началом заново прочитай план `{plan_rel}` ЦЕЛИКОМ, включая раздел «Эрраты» в конце — даже если ты сам "
            "писал план в этой сессии: эрраты внесены после, в твоей памяти о плане их нет.")
    return f"""ЭРРАТЫ ПЛАНА — обязательны к реализации, при расхождении приоритетнее основного текста плана. {head}
\"\"\"{errata}\"\"\"
В отчёте обязательна таблица «эрраты → реализация»: для КАЖДОЙ эрраты — файл:строка или тест, где она реализована; нереализованную пометь явно с причиной (молчаливый пропуск — блокер ревью).
"""

PACK_OVERFLOW_RE = re.compile(r"^OVERFLOW:\s*(yes|no)\s*$", re.M)

# Инструкция read-манифеста для R1-промпта гибридного режима (плоская строка:
# ```json-пример содержит литеральные фигурные скобы). Порядок финальных блоков
# ответа: STANCE-блок, затем этот ```json-манифест (последний блок ответа).
READ_MANIFEST_INSTR = """Сразу ПОСЛЕ STANCE-блока заверши ответ read-манифестом чтений (один ```json-блок, самый последний блок ответа):
```json
{"reads": [{"path": "<прочитанный файл>", "touched": true, "why": "<зачем читал>"}]}
```
touched = файл затронут итерацией (есть в контекст-паке/диффе). Перечисли ВСЕ файлы, которые читал (в т.ч. вне пака — право дочитывать репо сохраняется); ничего не читал — `"reads": []`."""


def pack_overflow(pack: str) -> bool:
    """Факт overflow из финальной строки пака (телеметрия режима капа)."""
    m = PACK_OVERFLOW_RE.search(pack)
    return bool(m and m.group(1) == "yes")


def parse_read_manifest(text: str) -> dict | None:
    """Последний ```json-блок ответа с ключом "reads" (read-манифест A3-lite).

    Толерантен к мусору вокруг; невалидный JSON / нет ключа reads / нет блока —
    None: телеметрия best-effort, деградация не ломает ревью.
    """
    for raw in reversed(re.findall(r"```json\s*\n(.*?)```", text, re.S)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "reads" in data:
            return data
    return None


def _split_patch_by_file(diff_text: str) -> dict[str, str]:
    """Разрез замороженного патча на чанки по файлам (ключ — b-путь)."""
    chunks: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        m = re.match(r"^diff --git a/\S+ b/(.+?)\s*$", line)
        if m:
            if cur is not None:
                chunks[cur] = "".join(buf)
            cur, buf = m.group(1), [line]
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        chunks[cur] = "".join(buf)
    return chunks


def build_context_pack(git_dir: Path, base_head: str, phase: str, artifacts: str,
                       *, full_text_ref: str | None = None, diff_text: str | None = None,
                       max_chars: int = DEFAULT_PACK_MAX_CHARS) -> str:
    """Контекст-пак итерации (A3-lite; механика флага review.context=hybrid).

    phase="plan": `artifacts` — путь объекта ревью (файл плана) от корня репо;
    пак = текст плана инлайн (task.md уже в промпте R1 отдельным блоком).
    phase="impl": `artifacts` — artifacts_rel задачи (пути под ним исключаются):
      - живой прогон (diff_text=None): набор файлов — `git status -uall` в
        git_dir (единый источник с diff_impl), полные тексты — ПОСЛЕ-тексты
        рабочего дерева, диффы — `git diff base_head`;
      - альтернативный источник (diff_text=<патч>): набор файлов и диффы — из
        патча, полные тексты — ДО-тексты `git show (full_text_ref|base_head):<path>`;
        новые файлы — пометка «содержимое не доступно паку».
    Детерминированный overflow: файлы в сортировке; полные тексты — файл
    ЦЕЛИКОМ (не влез в остаток капа — пометка «>path: N симв. — прочитай в
    репо», дифф файла всё равно в пуле); остаток капа — unified diff невошедших
    с деградацией контекста -U30→-U10→-U0, затем «дифф опущен»; финал —
    «вошло/опущено» + строка OVERFLOW: yes|no (см. pack_overflow).
    """
    git_dir = Path(git_dir)
    max_chars = max(0, int(max_chars))

    if phase == "plan":
        p = git_dir / artifacts
        if p.is_file():
            body = p.read_text(encoding="utf-8", errors="replace")
            over = len(body) > max_chars
            if over:
                body = (f"(план {len(body)} симв. превышает кап {max_chars} — "
                        f"прочитай файл `{artifacts}` в репо)")
        else:
            body, over = f"(файл `{artifacts}` не найден — прочитай его в репо)", False
        return ("## Контекст-пак итерации (A3-lite, фаза plan)\n\n"
                f"### Объект ревью инлайн: `{artifacts}`\n\n"
                f"````\n{body}\n````\n\nOVERFLOW: {'yes' if over else 'no'}\n")

    # --- фаза impl ---
    arts = artifacts.rstrip("/")

    def own(path: str) -> bool:
        return is_artifact_path(path, arts)

    if diff_text is None:
        paths = [p for _, p in changed_status_all(git_dir) if not own(p)]
        full: dict[str, str | None] = {}
        deleted: set[str] = set()
        for p in paths:
            f = git_dir / p
            if f.is_file():
                full[p] = f.read_text(encoding="utf-8", errors="replace")
            else:
                # Н3 rev002_impl: файла нет в рабочем дереве — он удалён итерацией;
                # ДО-текст берём из базы (как в замороженном режиме), а не
                # помечаем «новым» (ложная статистика футера)
                rc, out = sh(["git", "show", f"{base_head}:{p}"], cwd=git_dir, timeout=60)
                if rc == 0:
                    full[p] = out
                    deleted.add(p)
                else:   # нет нигде (мусорная запись status) — честная пометка
                    full[p] = None
    else:
        paths = []
        for m in re.finditer(r"^diff --git a/\S+ b/(.+?)\s*$", diff_text, re.M):
            p = m.group(1)
            if p not in paths and not own(p):
                paths.append(p)
        full = {}
        deleted = set()   # в замороженном режиме новый файл патча — просто novel
        for p in paths:
            rc, out = sh(["git", "show", f"{full_text_ref or base_head}:{p}"], cwd=git_dir, timeout=60)
            full[p] = out if rc == 0 else None   # None = нет в базе (новый файл)

    paths = sorted(paths)
    budget = max_chars
    parts: list[str] = []
    included: list[str] = []
    marked_cap: list[str] = []   # не влезли в кап — пометка «прочитай в репо»
    novel: list[str] = []        # нет в источнике текстов (новые файлы патча)
    for p in paths:
        text = full.get(p)
        if text is None:
            parts.append(f"> {p}: содержимое не доступно паку — прочитай в репо")
            novel.append(p)
            continue
        if p in deleted:
            block = f"### {p} (ДО-текст; файл удалён итерацией, {len(text)} симв.)\n\n````\n{text}\n````\n"
        else:
            block = f"### {p} (полный текст, {len(text)} симв.)\n\n````\n{text}\n````\n"
        if len(block) <= budget:
            parts.append(block)
            budget -= len(block)
            included.append(p)
        else:
            parts.append(f"> {p}: файл {len(text)} симв. не влез в остаток капа "
                         f"({budget} симв.) — прочитай в репо")
            marked_cap.append(p)

    overflow = bool(marked_cap)
    pool = [p for p in paths if p not in included]
    diff_note = ""
    if pool:
        if diff_text is None:
            chosen, ctx_u = None, 30
            for u in (30, 10, 0):
                rc, out = sh(["git", "diff", f"-U{u}", base_head, "--", *pool],
                             cwd=git_dir, timeout=180)
                if rc == 0 and len(out) <= budget:
                    chosen, ctx_u = out, u
                    break
            if chosen is None:
                diff_note = ("### Диффы невошедших полным текстом: не влезли даже с -U0 — "
                             "опущены (читай diff_impl-патч итерации)\n")
                overflow = True
            else:
                diff_note = (f"### Диффы невошедших полным текстом (unified, -U{ctx_u})\n\n"
                             f"```diff\n{chosen}\n```\n")
        else:
            # замороженный режим: полные тексты — ДО-тексты, изменение файла
            # видно ТОЛЬКО из диффа, поэтому дифф-секция идёт по всем файлам
            # патча, не только по невошедшим полным текстом (live-режиму пула
            # хватает: его полные тексты уже ПОСЛЕ-тексты).
            chunks = _split_patch_by_file(diff_text)
            sel: list[str] = []
            used = 0
            for p in paths:
                c = chunks.get(p, "")
                if not c:
                    continue
                if used + len(c) <= budget:
                    sel.append(c)
                    used += len(c)
                else:
                    sel.append(f"> {p}: дифф не влез в остаток капа — см. замороженный патч\n")
                    overflow = True
            diff_note = ("### Диффы (из замороженного патча)\n\n"
                         + "".join(sel))

    def _lst(xs: list[str]) -> str:
        return ", ".join(xs) if xs else "—"

    src = "замороженный патч" if diff_text is not None else "живое дерево"
    head = (f"## Контекст-пак итерации (A3-lite, фаза impl; источник: {src}"
            f" от {base_head[:10] or '?'}; кап {max_chars} симв.)\n")
    footer = (
        f"\nВошло полным текстом: {_lst(included)}\n"
        f"Пометкой «не влез в кап — прочитай в репо»: {_lst(marked_cap)}\n"
        f"Нет в источнике текстов (новые): {_lst(novel)}\n"
    )
    if deleted:   # строка только при наличии удалённых — не шуметь в каждом паке
        footer += f"Удалены итерацией (ДО-текст): {_lst(sorted(deleted))}\n"
    footer += (
        f"Диффом: {_lst(pool)}\n"
        f"OVERFLOW: {'yes' if overflow else 'no'}\n"
    )
    return head + "\n" + "\n".join(parts) + "\n" + diff_note + footer


class DuoReviewer:
    def __init__(self, cfg: dict, transport: ClaudeGlm, task_dir: Path, repo: Path):
        self.cfg = cfg
        self.t = transport
        self.task_dir = task_dir
        self.repo = repo
        self.transcripts = task_dir / "transcripts"
        self.transcripts.mkdir(exist_ok=True)
        self.spent = 0.0
        self.call_cost = 0.0   # pipev3 P0: per-call аккумулятор текущего review()
        # события транспорта ревью текущего review() — в телеметрию и в `status --task`
        # (раньше ретрай заглушки был виден только в orchestrator.log). Ревьюеры идут в потоках.
        self._ev = {"stub_retries": 0, "transport_retries": 0}
        self._ev_lock = threading.Lock()

    def _bump(self, key: str) -> None:
        # устойчив к объекту без __init__ (тесты собирают DuoReviewer через __new__)
        lock = self.__dict__.setdefault("_ev_lock", threading.Lock())
        with lock:
            ev = self.__dict__.setdefault("_ev", {"stub_retries": 0, "transport_retries": 0})
            ev[key] = ev.get(key, 0) + 1

    def _acc(self, cost: float) -> None:
        """pipev3 P0: копим и в кумулятив self.spent, и в per-call self.call_cost
        (сбрасывается в начале каждого review()). Возврат из review() именно
        call_cost чинит двойной учёт spent_usd: раньше review() возвращал
        кумулятив одного экземпляра, живущего весь Pipeline.run(), а run()
        приплюсовывал его к state на каждой итерации — ~2x завышение."""
        self.spent += cost
        self.call_cost += cost

    def _plan_triage_on(self, phase: str) -> bool:
        """review.plan_policy == "triage" И фаза plan. Дефолт ("legacy"/ключа нет) —
        все промпты байт-в-байт прежние."""
        return phase == "plan" and plan_triage_enabled(self.cfg)

    @staticmethod
    def _is_stub_response(text: str) -> bool:
        """Ответ ревьюера — пустой либо заглушка ExitPlanMode/plan-файла (узкая сигнатура:
        ложные срабатывания на нормальных ответах исключены, число вызовов в
        штатном режиме не меняется)."""
        return ((not text or not text.strip()) or bool(REVIEWER_STUB_RE.search(text))
                or is_api_error_text(text))

    def _reviewer_call(self, prompt: str, tag: str) -> tuple[str, float, str]:
        """Вызов ревьюера с одним ретраем на ответ-заглушку.

        Возвращает (text, суммарная_стоимость, фактический_промпт); провальная
        первая попытка сохраняется в transcripts/ тегом {tag}_attempt1 (forensic-след,
        прецедент chairman_attempt{N}). Если и ретрай — заглушка, возвращается он
        (parse_stance даст abstain) — без каскада ретраев."""
        timeout = self.cfg["timeouts"]["reviewer_sec"]
        text, _, cost = self._transport_retry(
            lambda: self.t.call(prompt, mode="plan", timeout=timeout, cwd=self.repo), tag)
        if not self._is_stub_response(text):
            return text, cost, prompt
        log(self.task_dir, f"ВНИМАНИЕ: ответ {tag} — заглушка (ExitPlanMode/пусто/API Error) — ретрай с уточнением")
        self._bump("stub_retries")
        self._save_tr(f"{tag}_attempt1", prompt, text, cost)
        retry_prompt = prompt + REVIEWER_STUB_RETRY_SUFFIX
        text2, _, cost2 = self._transport_retry(
            lambda: self.t.call(retry_prompt, mode="plan", timeout=timeout, cwd=self.repo), tag)
        return text2, cost + cost2, retry_prompt

    def _transport_retry(self, fn, what: str):
        """pipework3: повтор вызова ревьюера/председателя при транспортном сбое claude CLI
        (RuntimeError из ClaudeGlm.call: idle-kill, завершение без result). Раньше такой сбой
        валил процесс молча посреди ревью — R1–R3 сгорали. Число повторов —
        limits.reviewer_retries (0 = прежнее поведение), паузы api_retry_wait_sec×2^n (потолок 240 с)."""
        retries = int(self.cfg.get("limits", {}).get("reviewer_retries", 0) or 0)
        wait = float(self.cfg.get("timeouts", {}).get("api_retry_wait_sec", 30))
        attempt = 0
        while True:
            try:
                return fn()
            except RuntimeError as e:
                if attempt >= retries:
                    raise
                attempt += 1
                self._bump("transport_retries")
                delay = min(wait * (2 ** (attempt - 1)), 240.0)
                log(self.task_dir, f"РЕВЬЮ: {what}: {str(e)[:140]} — повтор {attempt}/{retries} через {delay:.0f}с")
                time.sleep(delay)

    def _save_tr(self, tag: str, prompt: str, resp: str, cost: float) -> None:
        f = self.transcripts / f"{datetime.now().strftime('%H%M%S')}_{tag}.md"
        f.write_text(
            f"# {tag}\ncost=${cost:.4f}\n\n"
            f"<!-- Структура: ## PROMPT — что получила персона. Внутри PROMPT блок "
            f"«Исходная задача (контекст)» в кавычках \"\"\" — ЦИТАТА задания исполнителя "
            f"(task.md), адресовано НЕ этой персоне. ## RESPONSE — ответ персоны. -->\n\n"
            f"## PROMPT\n\n{prompt}\n\n## RESPONSE\n\n{resp}\n",
            encoding="utf-8",
        )

    def _call_chairman_once(self, prompt: str, timeout: int) -> tuple[str, float]:
        """Один вызов председателя; при сбое/таймауте — ретрай с удвоенным таймаутом."""
        try:
            report, _, cost = self.t.call(prompt, mode="plan", timeout=timeout, cwd=self.repo)
            return report, cost
        except RuntimeError:
            log(self.task_dir, "председатель: сбой/таймаут — ретрай с удвоенным таймаутом")
            report, _, cost = self._transport_retry(
                lambda: self.t.call(prompt, mode="plan", timeout=timeout * 2, cwd=self.repo), "chairman")
            return report, cost

    def _chairman_with_retry(self, chairman_prompt: str, *, tag: str = "chairman",
                             who: str = "председатель") -> tuple[str, float, str]:
        """Председатель: до CHAIRMAN_MAX_ATTEMPTS попыток (chmguard R3).

        tag/who (режим pair): тот же контракт «валидный rev-отчёт с VERDICT» нужен и единственному
        проверяющему — он заменяет председателя, поэтому цикл ретраев общий. Дефолты дают прежние
        имена транскриптов (chairman_attempt{N}) и прежние тексты лога.

        Помимо транспортного ретрая (RuntimeError внутри _call_chairman_once,
        semantics прежнего кода сохранена), ретраится структурно дегенеративный
        вывод — ответ, не прошедший _looks_like_report или _parse_verdict:
        в промпт дописывается аугментация с цитатой неудачного ответа.
        Каждая непринятая попытка целиком сохраняется в transcripts/ тегом
        chairman_attempt{N} (forensic-след класса d10, Н2 rev001).
        Возвращает (report, total_cost, фактический_промпт) — при успехе
        ретрая это аугментированный промпт, review() сохраняет его в тег
        chairman без рассинхрона с chairman_attempt{N} (Н3 rev003).
        """
        total_cost = 0.0
        prompt = chairman_prompt
        report = ""
        for n in range(1, CHAIRMAN_MAX_ATTEMPTS + 1):
            report, cost = self._call_chairman_once(prompt, self.cfg["timeouts"]["chairman_sec"])
            total_cost += cost
            # chmguard R4: сначала санирайзер преамбулы — большинство
            # «дегенеративных» ответов были валидными отчётами с лишней
            # вводной строкой (ложный ретрай стоил $0.5-0.9)
            report = self._sanitize_preamble(report)
            if self._looks_like_report(report) and self._parse_verdict(report) is not None:
                return report, total_cost, prompt
            self._save_tr(f"{tag}_attempt{n}", prompt, report, cost)
            if n < CHAIRMAN_MAX_ATTEMPTS:
                log(self.task_dir, f"ВНИМАНИЕ: {who} вернул неполный отчёт "
                                   f"(попытка {n}/{CHAIRMAN_MAX_ATTEMPTS}) — ретрай с уточнением формата")
                snippet = report.strip()[:200]
                prompt = chairman_prompt + (
                    f"\n\nПредыдущая попытка вернула неполный/невалидный ответ (цитата: «{snippet}»)."
                    "\nВыведи ПОЛНЫЙ отчёт по формату, начиная с `# rev —`,"
                    " до машинной строки VERDICT в конце."
                )
            else:
                log(self.task_dir, f"ВНИМАНИЕ: {who} вернул неполный отчёт "
                                   f"(попытка {n}/{CHAIRMAN_MAX_ATTEMPTS}) — попытки исчерпаны, отдаю фолбэку review()")
        return report, total_cost, prompt

    def _round1(self, member: dict, artifact_rel: str, phase: str, task_text: str,
                rejected: str = "", pack: str | None = None, errata_block: str = "") -> str:
        # pipev1 П3: блок анти-повторов — замечания, уже отклонённые председателем
        # в предыдущих раундах этой задачи; с клапаном симметрии к ANTI_CONFORMITY
        # (повтор правомерен и при ошибке самого отклонения, не только при новых
        # фактах — иначе ошибочное отклонение тиражировалось бы на всю серию).
        rejected_block = ""
        if rejected:
            rejected_block = f"""Замечания, отклонённые председателем в предыдущих раундах этой задачи:
\"\"\"{rejected}\"\"\"
Директива: не поднимай эти замечания повторно без НОВЫХ фактов (цитата кода/строк), меняющих оценку; новые факты приводи явно. Или покажи, что само отклонение было ошибочным — конкретная ошибка в его аргументе.

"""
        # pipev4 П2: чек-лист пре-регистрации для план-ревью — те самые классы
        # вопросов, которые в srocr «изобретались» заново 3 раунда подряд
        # (знаменатели, спорные корзины, пороги). Один явный вопрос в R1
        # вместо трёх раундов додумывания.
        prereg_block = ""
        if phase == "plan":
            prereg_block = PREREG_CHECKLIST_PLAN
        # plan-policy: при review.plan_policy="triage" и phase=plan пояснение STANCE
        # согласовано с триажем блокер/эррата; иначе — прежний текст байт-в-байт.
        stance_hint = STANCE_HINT_TRIAGE if self._plan_triage_on(phase) else STANCE_HINT_LEGACY
        if pack is None:
            # legacy (review.context=legacy, дефолт): промпт байт-в-байт прежний
            prompt = f"""Ты — участник структурированного дуо-консилиума, выступающего надсмотрщиком над работой исполнителя. Работай строго в своей роли.

{member['identity']}

{OVERSEER_RULES}

Объект ревью: `{artifact_rel}` (фаза: {phase}). Прочитай его целиком.
Исходная задача (контекст):
\"\"\"{task_for_prompt(task_text, path_hint=str(self.task_dir / "task.md"))}\"\"\"

{rejected_block}{prereg_block}{errata_block}Сначала в ОДНОМ предложении переформулируй проблему через свою линзу.
Затем — самостоятельный слепой анализ (ты не знаешь, что скажет оппонент) по своему формату:

## Output Format (Standalone)
{member['fmt_standalone']}

В самом конце обязателен ровно один блок:
STANCE: accept | revise | abstain
CONFIDENCE: high | med | low
{stance_hint}

Лимит: 400 слов."""
        else:
            # pipev3 P1 (A3-lite, review.context=hybrid): контекст-пак итерации
            # вместо «прочитай всё целиком»; право дочитывать репо сохраняется
            # (пак экономит чтение, не запрещает его); read-манифест — телеметрия.
            prompt = f"""Ты — участник структурированного дуо-консилиума, выступающего надсмотрщиком над работой исполнителя. Работай строго в своей роли.

{member['identity']}

{OVERSEER_RULES}

Объект ревью: `{artifact_rel}` (фаза: {phase}). Ниже — контекст-пак итерации (полные тексты затронутых файлов, unified diff остального, пометки «прочитай в репо» для не влезевшего в кап). Право дочитывать репозиторий полностью сохраняется.
Исходная задача (контекст):
\"\"\"{task_for_prompt(task_text, path_hint=str(self.task_dir / "task.md"))}\"\"\"

{pack}

{rejected_block}{prereg_block}{errata_block}Сначала в ОДНОМ предложении переформулируй проблему через свою линзу.
Затем — самостоятельный слепой анализ (ты не знаешь, что скажет оппонент) по своему формату:

## Output Format (Standalone)
{member['fmt_standalone']}

В самом конце обязателен ровно один блок:
STANCE: accept | revise | abstain
CONFIDENCE: high | med | low
{stance_hint}

{READ_MANIFEST_INSTR}

Лимит: 400 слов."""
        text, cost, prompt = self._reviewer_call(prompt, f"r1_{member['name']}")
        self._acc(cost)
        self._save_tr(f"r1_{member['name']}", prompt, text, cost)
        return text

    def _round2(self, member: dict, own_r1: str, other_name: str, other_r1: str,
                other_manifest: dict | None = None) -> str:
        # pipev3 P1: при hybrid — манифест чтений оппонента (телеметрия R1)
        # в промпте допроса; пустой блок = legacy-промпт байт-в-байт прежний.
        manifest_block = ""
        if other_manifest is not None:
            manifest_block = f"""Манифест чтений оппонента за R1 (что и зачем он читал):
```json
{json.dumps(other_manifest, ensure_ascii=False)}
```
Предмет допроса: чтения вне затронутых итерацией файлов — обоснованы ли; что важное он НЕ прочитал.

"""
        prompt = f"""Ты — council-{member['name']}, раунд 2 дуо-консилиума (перекрёстный допрос).

{member['identity']}

Твой оппонент ({other_name}) в раунде 1 аргументировал:

\"\"\"{other_r1}\"\"\"
{manifest_block}
Твой собственный раунд 1 (для памяти):
\"\"\"{own_r1}\"\"\"

{ANTI_CONFORMITY}

Ответь по формату:
## Output Format (Council Round 2)
{member['fmt_round2']}

1. В чём оппонент неправ — взаимодействуй с его конкретными утверждениями (указывай, каким).
2. В чём оппонент прав — признай заслуживающее признания.
3. Переформулируй свою позицию с учётом обмена.
В конце:
STANCE: accept | revise | abstain
Лимит: 300 слов."""
        text, cost, prompt = self._reviewer_call(prompt, f"r2_{member['name']}")
        self._acc(cost)
        self._save_tr(f"r2_{member['name']}", prompt, text, cost)
        return text

    def _round3(self, member: dict, r2_own: str) -> str:
        prompt = f"""Финальный раунд. Вот твой раунд 2 (для памяти):
\"\"\"{r2_own}\"\"\"

Финальное заявление: максимум 50 слов, без новых аргументов — кристаллизация позиции.
В конце обязателен блок:
STANCE: accept | revise | abstain | CONFIDENCE: high|med|low"""
        text, cost, prompt = self._reviewer_call(prompt, f"r3_{member['name']}")
        self._acc(cost)
        self._save_tr(f"r3_{member['name']}", prompt, text, cost)
        return text

    @staticmethod
    def parse_stance(text: str) -> str:
        m = re.findall(r"STANCE:\s*\**\s*(accept|revise|abstain)", text, re.I)
        return m[-1].lower() if m else "abstain"

    # --- pipev1 П3: анти-повторы замечаний ----------------------------------

    _REJECTED_MARKER = re.compile(r"^[\s\-*]*\**\s*отклонен")
    _REJECTED_ABSENCE_RE = re.compile(r"^отклонен\w*\s+замечани\w*\s+(нет|отсутствуют)")
    _ABSENCE_FIRST_WORDS = {"нет", "ничего", "отсутствуют", "отсутствует"}

    @classmethod
    def _is_absence(cls, elem: str) -> bool:
        """Псевдо-элемент «отклонений нет»: «ничего», «отсутствуют. …»,
        «отклонённых замечаний нет — …» (нормализация ё→е для детекции)."""
        norm = elem.lower().replace("ё", "е")
        first = re.split(r"[\s.,;:!—-]+", norm, maxsplit=1)[0]
        return first in cls._ABSENCE_FIRST_WORDS or bool(cls._REJECTED_ABSENCE_RE.match(norm))

    @classmethod
    def collect_rejected(cls, task_dir: Path, limit: int = 1500) -> str:
        """pipev1 П3: отклонённые председателем замечания из прошлых rev-файлов.

        Источник — секции «Отклонено…» (формат дрейфует: «Отклонено как
        необоснованные:», «Отклонено / понижено:», «Отклонённые замечания:», е/ё —
        контракт промпта :646 закрепляет лишь «отдельную строку с пометкой
        „отклонено“»). Детектор толерантен к основе «отклонен» после ведущих
        **/пробелов/дефиса (нормализация ё→е — только для детекции, элементы
        извлекаются из исходного текста). Секция = от заголовка до следующего
        «## »; элементы — её буллеты с тегом файла + инлайн-текст после первого
        «:» заголовка. Псевдо-элементы «отсутствий» отбрасываются. Итог — в
        R1-блок анти-повторов; rev-файл текущего раунда пишется позже review().
        """
        items: list[str] = []
        for rev_file in sorted(task_dir.glob("rev*.md")):
            in_section = False
            for line in rev_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("## "):
                    in_section = False
                    continue
                norm = line.lower().replace("ё", "е")
                if cls._REJECTED_MARKER.match(norm):
                    in_section = True
                    inline = line.split(":", 1)[1] if ":" in line else ""
                    inline = inline.replace("*", "").strip()
                    if inline and not cls._is_absence(inline):
                        items.append(f"[{rev_file.stem}] {inline}")
                    continue
                if not in_section:
                    continue
                elem = line.strip().lstrip("-*").strip()
                if elem and not cls._is_absence(elem):
                    items.append(f"[{rev_file.stem}] {elem}")
        joined = "\n".join(items)
        if len(joined) > limit:
            joined = joined[:limit] + "…(обрезано)"
        return joined

    @staticmethod
    def _sanitize_preamble(report: str) -> str:
        """chmguard R4: срез преамбулы перед валидным `# rev —`-отчётом.

        Инцидент-класс (6/7 ретраев за историю): модель после чтения кода
        пишет переходную фразу («Все ключевые утверждения проверены…
        Составляю вердикт.») и ТОЛЬКО ПОТОМ заголовок — при полностью
        валидном теле отчёта. Жёсткий `^# rev` в _looks_like_report
        браковал такие ответы => ложный ретрай ($0.5-0.9, 2-6 мин).
        Санирайзер отрезает ≤5 строк преамбулы, если остаток — валидный
        заголовок; содержимое отчёта не трогает. Идемпотентен.
        """
        m = re.search(r"^#\s*rev\s*[—–-]", report, re.I | re.M)
        if not m or m.start() == 0:
            return report
        preamble_lines = report[:m.start()].strip().splitlines()
        if len(preamble_lines) > 5:      # это не преамбула — мусор/чужой текст
            return report
        return report[m.start():]

    @staticmethod
    def _looks_like_report(report: str) -> bool:
        """chmguard R1: структурный гард отчёта председателя (анти-дегенерат).

        Заголовок `# rev —` толерантен к дрейфу тире (em/en/ASCII-дефис —
        модели регулярно нормализуют em-dash из промпт-контракта, Н3 rev001);
        требуются секции `## 0` и `## 1` и >= 10 непустых строк.
        Валидатор формы, не истины: структурно корректный пустой отчёт пройдёт.
        """
        text = report.strip()
        return bool(
            re.match(r"^#\s*rev\s*[—–-]", text, re.I)
            and "## 0" in text
            and "## 1" in text
            and len([ln for ln in text.splitlines() if ln.strip()]) >= 10
        )

    @staticmethod
    def _parse_verdict(report: str) -> str | None:
        """chmguard R2: вердикт = ПОСЛЕДНЯЯ непустая строка, строго fullmatch.

        Упоминания VERDICT в теле/цитатах/бактиках игнорируются (инцидент d10:
        токен внутри прозаической строки воровал вердикт через findall[-1]).
        Префикс `\\**\\s*` в паттерне — толерантность к жирному `**VERDICT: ...**`
        в финальной строке (фикстура R5; отступление от буквы задания — задание
        не учитывало звёзды слева от токена).
        """
        lines = [ln.strip() for ln in report.splitlines() if ln.strip()]
        if not lines:
            return None
        m = re.fullmatch(r"\**\s*VERDICT:\s*\**\s*(ACCEPT|REVISE)\s*\**", lines[-1], re.I)
        return m.group(1).upper() if m else None

    @staticmethod
    def _fallback_report(report: str) -> str:
        """chmguard: фолбэк-обёртка вырожденного отчёта (последний рубеж).

        Пометка — отдельной строкой, машинная VERDICT: REVISE — ПОСЛЕДНЕЙ
        строкой: обёртка обязана парситься собственным _parse_verdict,
        иначе сенсор и актуатор расходятся в одном модуле (Н4 rev003).
        """
        return report.rstrip() + (
            "\n\n*(дописано оркестратором: председатель не выдал машинный вердикт)*\nVERDICT: REVISE"
        )

    def _build_chairman_prompt(self, *, artifact_rel: str, phase: str, task_text: str,
                               A: dict, B: dict, r1a: str, r1b: str,
                               r2a: str, r2b: str, r3a: str, r3b: str,
                               sa: str, sb: str,
                               hybrid: dict | None = None,
                               errata_block: str = "") -> str:
        """Промпт председателя (pipev2: вынесен из review() ради тестируемости).

        Для phase=impl добавляется чек-лист commit_plan (pipev2 п.4 задания) —
        ДО абзаца «Правила», ВНЕ замороженного формата отчёта `# rev —`/`## 0..3`
        (прецедент pipev1 П2). Для plan/custom промпт байт-в-байт прежний.
        pipev3 P1: при hybrid (review.context=hybrid) — контекст-пак итерации +
        оба read-манифеста + директива метить источники находок; при
        hybrid=None байты не меняются.
        """
        checks = self._checks_blocks(phase, artifact_rel)
        hybrid_block = ""
        if hybrid:
            def _mf(m: dict | None) -> str:
                return json.dumps(m, ensure_ascii=False) if m is not None else "(манифест не распознан)"
            hybrid_block = f"""
Контекст-пак итерации и read-манифесты участников (гибридный режим контекста):

{hybrid.get('pack') or ""}

Манифест чтений R1 — {A['name']}:
```json
{_mf(hybrid.get('manifest_a'))}
```

Манифест чтений R1 — {B['name']}:
```json
{_mf(hybrid.get('manifest_b'))}
```
Каждую находку в отчёте помечай источником: `(источник: R1 | R2/R3)` — кто из участников её предъявил.
"""
        return f"""Ты — Председатель консилиума. Ты не участвовал в дискуссии — ты аудируешь её и выносишь вердикт.

Исходная задача:
\"\"\"{task_for_prompt(task_text, path_hint=str(self.task_dir / "task.md"))}\"\"\"
{hybrid_block}{errata_block}
Объект ревью: `{artifact_rel}` (фаза: {phase}). Прочитай его сам в репозитории и проверь ключевые утверждения участников по коду.

Транскрипт дуэта (имена видны):

## Раунд 1 — {A['name']}
{r1a}

## Раунд 1 — {B['name']}
{r1b}

## Раунд 2 — {A['name']}
{r2a}

## Раунд 2 — {B['name']}
{r2b}

## Раунд 3 — финальные позиции
{A['name']}: {r3a}
STANCE: {sa}

{B['name']}: {r3b}
STANCE: {sb}

{self._report_format_block(artifact_rel, phase, single=False)}
{checks}
{self._report_rules(single=False)}"""

    # --- общие куски отчёта rev-формата: председатель (дуэт) и единственный проверяющий (pair) ---

    def _checks_blocks(self, phase: str, artifact_rel: str) -> str:
        """Чек-листы фазы и политики приёмки (commit_plan, plan triage, impl strict): ВНЕ
        замороженного формата отчёта, до абзаца «Правила». Общие для председателя дуэта и
        единственного проверяющего режима pair — вердикт и требования к отчёту одни и те же."""
        commit_plan_check = ""
        if phase == "impl":
            commit_plan_check = """
Чек-лист фазы impl (обязателен): в конце impl_report должна быть секция `## commit_plan` — таблица коммит-групп. Проверь по диффу и untracked-списку: (1) секция существует; (2) полнота — каждый изменённый/созданный/удалённый файл покрыт группой; (3) непересекаемость — каждый файл покрыт ровно ОДНОЙ группой (пересечение групп по файлам запрещено). Отсутствие секции, дыры в покрытии или пересечения — замечание (существенное).
"""
        # plan-policy: только phase=plan и только при review.plan_policy="triage";
        # ВНЕ замороженного формата отчёта (как commit_plan_check), до абзаца «Правила».
        impl_policy_check = IMPL_POLICY_CHAIRMAN_BLOCK if (phase == "impl" and impl_strict_enabled(self.cfg)) else ""
        plan_policy_check = ""
        if self._plan_triage_on(phase):
            plan_policy_check = PLAN_POLICY_CHAIRMAN_BLOCK + plan_size_trend(self.repo, artifact_rel)
        return f"{commit_plan_check}{plan_policy_check}{impl_policy_check}"

    @staticmethod
    def _report_format_block(artifact_rel: str, phase: str, *, single: bool) -> str:
        """Формат итогового rev-отчёта (от «Твоя задача…» до машинной строки VERDICT).
        single=False — председатель дуэта (текст байт-в-байт прежний, держит золотой тест);
        single=True — единственный проверяющий (pair): те же разделы и та же машинная строка,
        формулировки без дуэта (нет STANCE участников и транскрипта)."""
        who_line = "Дата, ревьюер (персона), модель." if single else "Дата, состав дуэта, модель."
        verdict_hint = (
            "Принимать или доработать — с одной фразой обоснования; решает наличие блокеров "
            "(политика приёмки фазы — ниже), а не число замечаний."
            if single else
            "Принимать или доработать — с одной фразой обоснования. Если STANCE участников "
            "разошлись — взвесь по существу аргументов, а не по голосам."
        )
        rejected_hint = (
            "Замечания, которые ты проверил по коду и отверг как необоснованные, — перечисли "
            "отдельной строкой с пометкой «отклонено» и почему."
            if single else
            "Замечания участника, которые ты отверг как необоснованные, — перечисли отдельной "
            "строкой с пометкой «отклонено» и почему."
        )
        checked_hint = (
            "Кратко: охват (что прочитал, какие якоря сверил, что «не проверено») и какие утверждения "
            "артефакта ты проверил по коду и они верны."
            if single else
            "Кратко: какие утверждения артефакта ты/участники проверили по коду и они верны."
        )
        return f"""Твоя задача — составить итоговый отчёт ревью на РУССКОМ в формате привычных rev-файлов команды:

# rev — ревью {artifact_rel} (фаза: {phase})

{who_line}

## 0. Вердикт
{verdict_hint}

## 1. Замечания
Нумерованный список Н1, Н2, ... Каждое: суть, координаты (файл:строки), категория (блокер/существенное/косметика), что конкретно сделать. {rejected_hint}

## 2. Что проверено и подтвердилось
{checked_hint}

## 3. Сводка для исполнителя
Маркированный список правок «что сделать» — только то, что войдёт в следующую итерацию исполнителя. Если вердикт «Принимать» — пиши «правок не требуется».

В САМОМ КОНЦЕ отчёта — ровно одна машинная строка (исполнительский протокол, её будут парсить):
VERDICT: ACCEPT
или
VERDICT: REVISE"""

    @staticmethod
    def _report_rules(*, single: bool) -> str:
        """Завершающие правила отчёта (после чек-листов и политик)."""
        if single:
            return (
                "Правила: не выдумывай замечания ради количества и не принимай ради скорости; не поднимай "
                "придирки, не выдерживающие проверки по коду; читай код сам, отчёт исполнителя — самоаттестация. "
                "Не создавай и не изменяй файлы — только читай; черновики следующих задач — не твоя работа.\n"
                "Выведи ТОЛЬКО текст отчёта, начиная сразу с заголовка `# rev — ...` (без преамбулы про то, "
                "что ты прочитал, и без служебных комментариев)."
            )
        return (
            "Правила: не выдумывай замечания ради количества; отклоняй придирки, не выдерживающие проверки; "
            "читай код сам, доверяй, но проверяй участников. Не создавай и не изменяй файлы — только читай; "
            "черновики следующих задач — не твоя работа.\n"
            "Выведи ТОЛЬКО текст отчёта, начиная сразу с заголовка `# rev — ...` (без преамбулы про то, "
            "что ты прочитал, и без служебных комментариев)."
        )

    def _build_single_prompt(self, *, member: dict, artifact_rel: str, phase: str, task_text: str,
                             rejected: str = "", pack: str | None = None,
                             errata_block: str = "") -> str:
        """Промпт единственного проверяющего (режим pair): персона + метод анализа + итоговый отчёт
        в формате rev-файла с машинной строкой VERDICT. Заменяет собой весь дуэт и председателя:
        отчёт идёт исполнителю напрямую. Чек-листы и политики приёмки — те же, что у председателя.
        """
        rejected_block = ""
        if rejected:
            rejected_block = f"""Замечания, отклонённые тобой в предыдущих раундах этой задачи:
\"\"\"{rejected}\"\"\"
Директива: не поднимай эти замечания повторно без НОВЫХ фактов (цитата кода/строк), меняющих оценку; новые факты приводи явно. Или покажи, что само отклонение было ошибочным — конкретная ошибка в его аргументе.

"""
        prereg_block = ""
        if phase == "plan":
            prereg_block = PREREG_CHECKLIST_PLAN
        pack_block = ""
        if pack is not None:
            pack_block = f"""Ниже — контекст-пак итерации (полные тексты затронутых файлов, unified diff остального, пометки «прочитай в репо» для не влезевшего в кап). Право дочитывать репозиторий полностью сохраняется.

{pack}

"""
        return f"""Ты — ЕДИНСТВЕННЫЙ проверяющий в режиме «исполнитель + проверяющий» автоматизированного пайплайна: оппонента и председателя нет, итоговый отчёт и вердикт выносишь ты сам. Работай строго в своей роли.

{member['identity']}

{OVERSEER_RULES}

{PAIR_REVIEWER_RULES}

Объект ревью: `{artifact_rel}` (фаза: {phase}). Прочитай его целиком и проверяй утверждения по фактическому коду.
Исходная задача (контекст):
\"\"\"{task_for_prompt(task_text, path_hint=str(self.task_dir / "task.md"))}\"\"\"

{pack_block}{rejected_block}{prereg_block}{errata_block}Метод анализа — по твоему формату ниже (он описывает, КАК думать; итоговый отчёт оформи по формату rev-файла дальше, перенеся в него суть: охват и прогон — в раздел 2, блокеры/существенное/косметика — в раздел 1):

## Output Format (Standalone)
{member['fmt_standalone']}

{self._report_format_block(artifact_rel, phase, single=True)}
{self._checks_blocks(phase, artifact_rel)}
{self._report_rules(single=True)}"""

    def review(
        self,
        *,
        artifact_rel: str,
        phase: str,               # plan | impl | custom
        task_text: str,
        members: list[str],
        rounds: int,
        rev_file: Path,
        guard: GitGuard,
        artifacts_rel: str,
        base_head: str = "",      # pipev3 P1: база диффа для контекст-пака (impl)
        plan_rel: str = "",       # pipework2 P7: принятый план (impl) — источник эррат
    ) -> tuple[str, float, dict]:
        """Полный дуо-цикл. Возвращает (verdict, cost_usd, review_telemetry).

        pipev3 P0: cost_usd — стоимость ИМЕННО этого вызова (per-call), не
        кумулятив экземпляра; Pipeline.run() приплюсовывает её к spent_usd
        один раз (раньше возвращался кумулятив — двойной учёт). pipev3 P1:
        при review.context=hybrid и фазе plan/impl собирается контекст-пак
        итерации: R1 получают его + требование read-манифеста, R2 — манифест
        оппонента, председатель — пак и оба манифеста; телеметрия (режим
        контекста, размер/overflow пака, манифесты, per-call стоимость)
        возвращается третьим элементом (в state пишет Pipeline, DuoReviewer
        stateless). Пишет rev-файл.
        """
        self.call_cost = 0.0   # P0: per-call аккумулятор этого review()
        with self._ev_lock:
            self._ev = {"stub_retries": 0, "transport_retries": 0}
        if len(members) == 1:
            # режим pair: единственный проверяющий вместо дуэта и председателя
            return self._review_single(
                member_name=members[0], artifact_rel=artifact_rel, phase=phase, task_text=task_text,
                rev_file=rev_file, guard=guard, artifacts_rel=artifacts_rel,
                base_head=base_head, plan_rel=plan_rel,
            )
        A, B = load_member(self.cfg, members[0]), load_member(self.cfg, members[1])

        self._check_artifact_exists(artifact_rel, phase)

        log(self.task_dir, f"дуо R1 (вслепую, параллельно): {A['name']} vs {B['name']} — {artifact_rel}")

        # pipev3 P1: контекст-пак (один на оба плеча — строится до потоков)
        ctx_cfg = self.cfg.get("review") if isinstance(self.cfg.get("review"), dict) else {}
        pack, overflow = self._build_pack(phase, artifact_rel, artifacts_rel, base_head)

        rejected = self.collect_rejected(self.task_dir)   # pipev1 П3: анти-повторы
        # pipework2 P7: эрраты принятого плана — только impl и только если раздел есть
        # (без эррат промпты байт-в-байт прежние)
        errata = read_plan_errata(self.repo, plan_rel) if phase == "impl" else ""
        errata_r1 = errata_block_reviewer(plan_rel, errata)
        before = guard.snapshot_before_review(artifacts_rel)
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(self._round1, A, artifact_rel, phase, task_text, rejected, pack, errata_r1)
            fb = ex.submit(self._round1, B, artifact_rel, phase, task_text, rejected, pack, errata_r1)
            r1a, r1b = fa.result(), fb.result()
        guard.enforce_readonly(before, artifacts_rel, self.task_dir)
        ma = parse_read_manifest(r1a) if pack is not None else None
        mb = parse_read_manifest(r1b) if pack is not None else None

        log(self.task_dir, "дуо R2 (перекрёстный допрос, параллельно)")
        before = guard.snapshot_before_review(artifacts_rel)
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(self._round2, A, r1a, B["name"], r1b, mb)
            fb = ex.submit(self._round2, B, r1b, A["name"], r1a, ma)
            r2a, r2b = fa.result(), fb.result()
        guard.enforce_readonly(before, artifacts_rel, self.task_dir)

        r3a = r3b = ""
        if rounds >= 3:
            log(self.task_dir, "дуо R3 (финальные позиции)")
            before = guard.snapshot_before_review(artifacts_rel)
            with ThreadPoolExecutor(max_workers=2) as ex:
                fa = ex.submit(self._round3, A, r2a)
                fb = ex.submit(self._round3, B, r2b)
                r3a, r3b = fa.result(), fb.result()
            guard.enforce_readonly(before, artifacts_rel, self.task_dir)

        sa, sb = self.parse_stance(r3a or r2a), self.parse_stance(r3b or r2b)
        log(self.task_dir, f"STANCE: {A['name']}={sa}, {B['name']}={sb}; синтез председателя")

        # --- Председатель: отдельная сессия, не участник дуэта ---
        chairman_prompt = self._build_chairman_prompt(
            artifact_rel=artifact_rel, phase=phase, task_text=task_text,
            A=A, B=B, r1a=r1a, r1b=r1b, r2a=r2a, r2b=r2b, r3a=r3a, r3b=r3b, sa=sa, sb=sb,
            hybrid={"pack": pack, "manifest_a": ma, "manifest_b": mb} if pack is not None else None,
            errata_block=errata_block_chairman(plan_rel, errata),
        )
        before = guard.snapshot_before_review(artifacts_rel)
        report, cost, prompt_used = self._chairman_with_retry(chairman_prompt)
        self._acc(cost)
        guard.enforce_readonly(before, artifacts_rel, self.task_dir)
        self._save_tr("chairman", prompt_used, report, cost)

        report, verdict = self._finish_report(report)

        telemetry = {
            "phase": phase,
            "context_mode": ctx_cfg.get("context", "legacy"),
            "pack_chars": len(pack) if pack else 0,
            "pack_overflow": overflow,
            "read_manifests": {A["name"]: ma, B["name"]: mb} if pack is not None else {},
            "cost_usd": round(self.call_cost, 4),   # P0: per-call, не кумулятив
            **self._ev,                             # stub_retries / transport_retries
        }
        rev_file.write_text(report, encoding="utf-8")
        return verdict, self.call_cost, telemetry

    # --- общие шаги review() дуэта и review() единственного проверяющего ---

    def _check_artifact_exists(self, artifact_rel: str, phase: str) -> None:
        """pipev4 П3: guard артефакта — ревью не диспетчеризируется, если файла
        физически нет (урок srocr v6: API-таймаут executor'а «сжёг» план,
        дуо-ревью ушло по несуществующему пути). phase=custom допускает
        свободное описание артефакта; impl проверяет diff-файл (артефакт —
        составной: отчёт + дифф + untracked-список)."""
        if phase in ("plan", "impl"):
            cand = [p.strip().strip('`') for p in artifact_rel.split() if p.endswith(".md") or p.endswith(".patch")]
            missing = [p for p in cand if not (self.repo / p).exists()]
            if missing:
                raise ArtifactMissingError(
                    f"ревью не диспетчеризировано: артефакт не найден: {', '.join(missing)}. "
                    f"Вероятно, executor не завершил работу (таймаут?). Перезапусти итерацию."
                )

    def _build_pack(self, phase: str, artifact_rel: str, artifacts_rel: str,
                    base_head: str) -> tuple[str | None, bool]:
        """pipev3 P1: контекст-пак итерации (review.context=hybrid, фазы plan/impl) → (pack, overflow)."""
        ctx_cfg = self.cfg.get("review") if isinstance(self.cfg.get("review"), dict) else {}
        pack, overflow = None, False
        if ctx_cfg.get("context") == "hybrid" and phase in ("plan", "impl"):
            try:
                pack = build_context_pack(
                    self.repo, base_head, phase,
                    artifact_rel if phase == "plan" else artifacts_rel,
                    max_chars=int(ctx_cfg.get("pack_max_chars", DEFAULT_PACK_MAX_CHARS)),
                )
            except Exception as e:  # пак не должен убивать ревью: право читать репо остаётся
                log(self.task_dir, f"ВНИМАНИЕ: контекст-пак не собрался ({e}); ревью без пака")
                pack = None
            else:
                overflow = pack_overflow(pack)
        return pack, overflow

    def _reset_tool_stats(self) -> None:
        """Обнулить счётчик tool_use транспорта (заглушки без поддержки — молча пропускаются)."""
        fn = getattr(self.t, "reset_call_stats", None)
        if callable(fn):
            fn()

    def _take_tool_stats(self) -> dict | None:
        """Статистика обращений к инструментам с последнего reset; None — транспорт её не ведёт."""
        fn = getattr(self.t, "take_call_stats", None)
        if not callable(fn):
            return None
        stats = fn()
        return stats if isinstance(stats, dict) else None

    def _finish_report(self, report: str, *, gen: str = "председателя",
                       nom: str = "председатель") -> tuple[str, str]:
        """chmguard: вердикт строго по последней непустой строке (R2); R4-подсчёт
        коротких ответов — ДО дописывания фолбэка, иначе фолбэк-строки гасят
        предупреждение на ребре «9 строк без вердикта» (Н4 rev001).
        Возвращает (report, verdict); нет VERDICT-строки — подставляется REVISE."""
        verdict = self._parse_verdict(report)
        if len([ln for ln in report.splitlines() if ln.strip()]) < 10:
            log(self.task_dir, f"ВНИМАНИЕ: ответ {gen} < 10 непустых строк — вероятен дегенеративный отчёт")
        if verdict is None:
            report = self._fallback_report(report)
            log(self.task_dir, f"ВНИМАНИЕ: {nom} не выдал VERDICT-строку — подставлен REVISE")
            verdict = "REVISE"
        return report, verdict

    def _review_single(self, *, member_name: str, artifact_rel: str, phase: str, task_text: str,
                       rev_file: Path, guard: GitGuard, artifacts_rel: str,
                       base_head: str = "", plan_rel: str = "") -> tuple[str, float, dict]:
        """Режим pair: ОДИН проверяющий читает артефакт и сам выносит вердикт — без R1–R3 и
        председателя (1 вызов модели вместо 7). Итоговый отчёт — тот же rev-формат с машинной
        строкой VERDICT, поэтому всё остальное (эрраты плана, антиповторы, rework, set-verdict,
        commit_plan) работает без изменений. Контекст-пак в hybrid-режиме даётся как есть, но
        read-манифест не запрашивается: вердикт обязан быть ПОСЛЕДНЕЙ строкой ответа."""
        member = load_member(self.cfg, member_name)
        self._check_artifact_exists(artifact_rel, phase)
        log(self.task_dir, f"pair: единственный проверяющий {member['name']} — {artifact_rel}")
        ctx_cfg = self.cfg.get("review") if isinstance(self.cfg.get("review"), dict) else {}
        pack, overflow = self._build_pack(phase, artifact_rel, artifacts_rel, base_head)
        rejected = self.collect_rejected(self.task_dir)
        errata = read_plan_errata(self.repo, plan_rel) if phase == "impl" else ""
        # эрраты принятого плана — чек-лист «каждая эррата реализована, иначе блокер»; берём блок
        # ревьюера (без отсылок к STANCE участников и председателю — их в pair нет)
        prompt = self._build_single_prompt(
            member=member, artifact_rel=artifact_rel, phase=phase, task_text=task_text,
            rejected=rejected, pack=pack, errata_block=errata_block_reviewer(plan_rel, errata),
        )
        before = guard.snapshot_before_review(artifacts_rel)
        tag = f"reviewer_{member['name']}"
        self._reset_tool_stats()
        report, cost, prompt_used = self._chairman_with_retry(prompt, tag=tag, who="проверяющий")
        self._acc(cost)
        guard.enforce_readonly(before, artifacts_rel, self.task_dir)
        stats = self._take_tool_stats()
        # guard пустого ревью: ACCEPT при нуле tool_use — вакуумный вердикт (боевой инцидент).
        # Один повтор с жёстким напоминанием; не помогло — EmptyReviewError → halt review_empty.
        guard_cfg = ctx_cfg.get("empty_guard") if isinstance(ctx_cfg.get("empty_guard"), dict) else {}
        max_retry = int(guard_cfg.get("retries", 1) or 0)
        empty_retries = 0
        reason = empty_review_reason(self.cfg, self._parse_verdict(report), report, stats)
        while reason and empty_retries < max_retry:
            empty_retries += 1
            log(self.task_dir, f"ВНИМАНИЕ: пустое ревью — {reason}; повтор {empty_retries}/{max_retry} "
                               f"с напоминанием об инструментах")
            self._save_tr(f"{tag}_empty{empty_retries}", prompt_used, report, 0.0)
            self._reset_tool_stats()
            report, cost2, prompt_used = self._chairman_with_retry(
                prompt + EMPTY_REVIEW_RETRY_SUFFIX.format(reason=reason), tag=tag, who="проверяющий")
            self._acc(cost2)
            cost += cost2
            guard.enforce_readonly(before, artifacts_rel, self.task_dir)
            stats = self._take_tool_stats()
            reason = empty_review_reason(self.cfg, self._parse_verdict(report), report, stats)
        self._save_tr(tag, prompt_used, report, cost)
        if reason:
            raise EmptyReviewError(reason)
        report, verdict = self._finish_report(report, gen="проверяющего", nom="проверяющий")
        telemetry = {
            "phase": phase,
            "mode": "pair",
            "reviewer": member["name"],
            "context_mode": ctx_cfg.get("context", "legacy"),
            "pack_chars": len(pack) if pack else 0,
            "pack_overflow": overflow,
            "read_manifests": {},
            "cost_usd": round(self.call_cost, 4),
            **self._ev,                             # stub_retries / transport_retries
        }
        if stats is not None:
            telemetry["tool_uses"] = int(stats.get("tool_uses", 0))
            telemetry["empty_retries"] = empty_retries
        rev_file.write_text(report, encoding="utf-8")
        return verdict, self.call_cost, telemetry


# ----------------------------------------------------------------------------
# Исполнитель
# ----------------------------------------------------------------------------

class Executor:
    def __init__(self, cfg: dict, transport: ClaudeGlm, task_dir: Path, repo: Path, state: dict):
        self.cfg = cfg
        self.t = transport
        self.task_dir = task_dir
        self.repo = repo
        self.state = state

    def _call_once(self, prompt: str, sid: str | None) -> tuple[str, str | None, float]:
        """Один вызов исполнителя; resume сессии не удался → одна попытка в новой сессии."""
        timeout = self.cfg["timeouts"]["executor_sec"]
        try:
            return self.t.call(prompt, mode="full", resume_session=sid, timeout=timeout, cwd=self.repo)
        except RuntimeError as e:
            if sid and "resume" in str(e).lower():
                log(self.task_dir, f"resume сессии не удался ({e}); пробую новую сессию")
                return self.t.call(prompt, mode="full", timeout=timeout, cwd=self.repo)
            raise

    def _finalize(self, prompt: str, tag: str, text: str, cost: float, new_sid: str | None,
                  sid: str | None, failed: list[str]) -> None:
        """Учёт стоимости/сессии и транскрипт шага (и при успехе, и при окончательном сбое:
        pipework3 — после провала сессия остаётся известной, стоимость не теряется)."""
        self.state["executor_session"] = new_sid or sid
        self.state["spent_usd"] = round(self.state.get("spent_usd", 0.0) + cost, 4)
        (self.task_dir / "transcripts").mkdir(exist_ok=True)
        f = self.task_dir / "transcripts" / f"{datetime.now().strftime('%H%M%S')}_{tag}.md"
        retry_note = ""
        if failed:
            retry_note = "\n\n## API-RETRY (неудачные попытки до итогового ответа)\n\n" + "\n".join(
                f"{i}. {m}" for i, m in enumerate(failed, 1))
        f.write_text(f"# {tag}\ncost=${cost:.4f}\n\n## PROMPT\n\n{prompt}\n\n## RESPONSE\n\n{text}{retry_note}\n",
                     encoding="utf-8")

    def _send(self, prompt: str, tag: str) -> str:
        sid = self.state.get("executor_session")
        # Транспортный сбой claude CLI — не ответ модели: (а) текст результата «API Error: …»,
        # (б) pipework3: исключение ClaudeTransportError (idle-kill / завершение без result) —
        # повтор с resume той же сессии (контекст и правки дерева сохраняются), а не остановка
        # серии. Лимит — limits.executor_api_retries (0 = как раньше).
        retries = int(self.cfg.get("limits", {}).get("executor_api_retries", 0) or 0)
        wait = float(self.cfg.get("timeouts", {}).get("api_retry_wait_sec", 30))
        failed: list[str] = []
        attempt = 0
        cost = 0.0
        new_sid: str | None = None
        text = ""
        cur_prompt, cur_sid = prompt, sid
        while True:
            err: ClaudeTransportError | None = None
            try:
                text, sid_out, cost_i = self._call_once(cur_prompt, cur_sid)
            except ClaudeTransportError as e:
                err, text, sid_out, cost_i = e, "", e.session_id, e.cost
            cost += cost_i
            new_sid = sid_out or new_sid
            if err is None and not is_api_error_text(text):
                break
            failure = str(err) if err is not None else text.strip()
            if attempt >= retries:
                if err is not None:
                    self._finalize(prompt, tag, f"[сбой исполнителя: {failure}]", cost, new_sid, sid, failed)
                    raise err
                break                        # «API Error» исчерпал повторы — вернуть как есть (прежнее поведение)
            attempt += 1
            failed.append(failure)
            delay = min(wait * (2 ** (attempt - 1)), 240.0)
            log(self.task_dir, f"ИСПОЛНИТЕЛЬ: {failure[:160]} — повтор {attempt}/{retries} "
                               f"через {delay:.0f}с (resume сессии {(new_sid or sid or '—')[:8]})")
            time.sleep(delay)
            sid2 = new_sid or sid
            cur_prompt = EXECUTOR_API_RETRY_PROMPT.format(err=failure[:200]) if sid2 else prompt
            cur_sid = sid2
        self._finalize(prompt, tag, text, cost, new_sid, sid, failed)
        return text

    def _ensure_artifact(self, rel: str, what: str, tag: str) -> None:
        """pipework3: постусловие шага исполнителя — обязательный артефакт (plan_vN.md /
        impl_report_vN.md) записан на диск и не пуст.

        Боевой инцидент (21.10.2026): ответ исполнителя v2 оборвался на полуслове
        («Исправление логики и»), result-событие всё же пришло → оркестратор принял это за
        успех, отправился в ревью и остановился «артефакт не найден»; человек гнал rework с
        нуля (свежая сессия, ещё $8). Теперь: нет файла → до limits.executor_continue_retries
        «дожимающих» ходов resume той же сессии. Если файла всё ещё нет — управление
        возвращается run(): штатная остановка ArtifactMissingError (halt kind=artifact_missing).
        """
        retries = int(self.cfg.get("limits", {}).get("executor_continue_retries", 0) or 0)
        target = Path(self.repo) / rel
        for n in range(1, retries + 1):
            if target.is_file() and target.stat().st_size > 0:
                return
            log(self.task_dir, f"ИСПОЛНИТЕЛЬ: артефакт {rel} не записан после шага {tag} — "
                               f"дожимающий ход {n}/{retries} (resume сессии)")
            prompt = EXECUTOR_ARTIFACT_CONTINUE_PROMPT.format(path=rel, what=what)
            self._send(prompt, f"{tag}_continue{n}")

    def _common(self, solo: bool = False) -> str:
        arts = self.state["artifacts_rel"]
        env_hint = self.cfg.get("executor_env_hint", "")
        # pipev3 P2: в worktree-режиме подсказки окружения указывают в дерево серии
        main_repo = str(Path(self.cfg["repo"]).resolve()) if self.cfg.get("repo") else ""
        if env_hint and main_repo and str(self.repo) != main_repo:
            env_hint = env_hint.replace(main_repo, str(self.repo))
        if solo:
            return f"""Ты — исполнитель в SOLO-режиме автоматизированного пайплайна (без ревью).
Рабочая директория артефактов пайплайна: `{arts}/` (отчёты пиши только туда).
{('Окружение: ' + env_hint) if env_hint else ''}
Не меняй файлы пайплайна вне своей задачи; не трогай `.env` и секреты.
Отвечай по-русски, кратко (≤ 20 строк): что сделал, что идёт фоном, что дальше.
""" + self._wt_block()
        if self.state.get("mode") == "pair":
            head = ("Ты — исполнитель в автоматизированном пайплайне «исполнитель ↔ проверяющий».\n"
                    "Твоя работа проходит ревью одного независимого проверяющего; его отчёты тебе будут передаваться.\n")
        else:
            head = ("Ты — исполнитель в автоматизированном пайплайне «исполнитель ↔ дуо-надсмотрщик».\n"
                    "Твоя работа проходит ревью двумя независимыми критиками и председателем; их отчёты тебе будут передаваться.\n")
        return head + f"""Рабочая директория артефактов пайплайна: `{arts}/` (планы и отчёты пиши только туда).
{('Окружение: ' + env_hint) if env_hint else ''}
Не меняй файлы пайплайна вне своей задачи; не трогай `.env` и секреты.
Отвечай по-русски, кратко (≤ 20 строк): что сделал, какие файлы создал/изменил.
""" + self._wt_block()

    def _is_worktree(self) -> bool:
        return self.state.get("isolation") == "worktree"

    def _wt_block(self) -> str:
        """Изоляция worktree (параллельные серии): пусто в shared — промпты байт-в-байт прежние.
        R7: проектная подсказка (сервисы, которые нельзя перезапускать) — ключ
        executor_worktree_note (пусто → блок без проектной приписки)."""
        if not self._is_worktree():
            return ""
        branch = f"{(self.cfg.get('git') or {}).get('branch_prefix', 'duo/')}{self.state.get('task', '')}"
        note = str(self.cfg.get("executor_worktree_note") or "").strip()
        arts_task = (self.state.get("artifacts_rel")
                     or f"{self.cfg.get('artifacts_dir', 'pipeline')}/{self.state.get('task', '')}")
        return (f"Изоляция: ты работаешь в отдельном git-worktree `{self.repo}` (ветка `{branch}`); параллельно "
                "могут идти другие серии в своих worktree. Правь файлы только в этом дереве: основное дерево и "
                f"чужие worktree не трогай{', ' + note if note else '.'}\n"
                # Н13 (полевой прогон 17.11.2026): исполнитель положил скрипт симуляции в каталог
                # артефактов, запустил из worktree, а `Path(__file__).resolve()` провёл через симлинк
                # в ОСНОВНОЕ дерево (старый код) — потерян ход модели
                f"Каталог `{arts_task}` в этом дереве — симлинк на артефакты серии в основном дереве. "
                "Скрипты, которые ты кладёшь туда и запускаешь, не должны определять корень проекта через "
                "`Path(__file__).resolve()` или `os.path.realpath`: путь уведёт в основное дерево со старым "
                "кодом. Бери корень от текущего каталога (запускай из корня worktree) или "
                "`Path(__file__).absolute()` без resolve.\n")

    def _long_run_block(self, task_text: str) -> str:
        """Правила длинных прогонов на стенде (EXECUTOR_LONG_RUN_RULES): только если задание о стенде/ssh."""
        return EXECUTOR_LONG_RUN_RULES if long_run_rules_enabled(self.cfg, task_text) else ""

    def _zone_plan_block(self) -> str:
        return FILES_TOUCH_PLAN_BLOCK if self._is_worktree() else ""

    def _zone_revise_plan_block(self) -> str:
        return FILES_TOUCH_REVISE_PLAN_BLOCK if self._is_worktree() else ""

    def _zone_impl_block(self) -> str:
        return FILES_TOUCH_IMPL_BLOCK if self._is_worktree() else ""

    def make_plan(self, task_text: str, version: int, resumed: bool = False) -> str:
        prompt = (EXECUTOR_RESUMED_PREFACE if resumed else "") + self._common() + f"""
ЗАДАЧА:
\"\"\"{task_text}\"\"\"

ФАЗА: ПЛАНИРОВАНИЕ (код не править!).
Составь детальный план имплементации и запиши его в `{self.state['artifacts_rel']}/plan_v{version}.md`.
Обязательные разделы: цель; инвентаризация затрагиваемых файлов (проверь их существование); пошаговые изменения; риски/границы; критерии готовности (DoD); какие тесты затронуты/добавлены; что сознательно НЕ делаем; пре-регистрация измерений (если есть метрики/сравнения: единица счёта и знаменатель каждой метрики, инварианты данных, пороги решений, правила разбора спорных случаев — всё запинь ДО прогона; после прогона это будет post-hoc рационализацией).
Пиши по-русски, конкретно, с координатами файлов. Не начинай имплементацию.{self._zone_plan_block()}{self._long_run_block(task_text)}"""
        reply = self._send(prompt, f"executor_plan_v{version}")
        self._ensure_artifact(f"{self.state['artifacts_rel']}/plan_v{version}.md",
                              "план целиком, все обязательные разделы", f"executor_plan_v{version}")
        return reply

    def revise_plan(self, task_text: str, version: int, rev_file: Path, resumed: bool = False) -> str:
        rev = rev_file.read_text(encoding="utf-8")
        policy_block = PLAN_POLICY_EXECUTOR_BLOCK if plan_triage_enabled(self.cfg) else ""
        prompt = (EXECUTOR_RESUMED_PREFACE if resumed else "") + self._common() + f"""
Отчёт ревью надсмотрщика по твоему плану (учти КАЖДОЕ замечание из «Сводки для исполнителя»; отвергнуть замечание можно только с явной аргументацией — тогда добавь раздел «Отклонённые замечания»):

\"\"\"{rev}\"\"\"

{policy_block}Перепиши план с учётом правок в `{self.state['artifacts_rel']}/plan_v{version}.md` (новая версия целиком, сохрани структуру разделов). Код по-прежнему не править. В ответе перечисли, какие пункты ревью учёл и как.{self._zone_revise_plan_block()}"""
        reply = self._send(prompt, f"executor_plan_v{version}")
        self._ensure_artifact(f"{self.state['artifacts_rel']}/plan_v{version}.md",
                              "новая версия плана целиком", f"executor_plan_v{version}")
        return reply

    def implement(self, task_text: str, plan_rel: str, version: int, resumed: bool = False) -> str:
        errata_block = errata_block_executor(plan_rel, read_plan_errata(self.repo, plan_rel))
        impl_block = IMPL_POLICY_EXECUTOR_REPORT if impl_strict_enabled(self.cfg) else ""
        task_view = task_for_prompt(task_text, path_hint=f"{self.state['artifacts_rel']}/task.md")
        prompt = (EXECUTOR_RESUMED_PREFACE if resumed else "") + self._common() + f"""
ЗАДАЧА (для контекста):
\"\"\"{task_view}\"\"\"

ФАЗА: ИМПЛЕМЕНТАЦИЯ. Принятый план: `{plan_rel}`. Следуй ему буквально; отступления — только с обоснованием в отчёте.
{errata_block}{impl_block}{self._zone_impl_block()}{self._long_run_block(task_text)}Внеси правки кода, запусти относящиеся к задаче тесты (фиксируй команды и результаты).
Запиши отчёт в `{self.state['artifacts_rel']}/impl_report_v{version}.md` с разделами: что сделано (по пунктам плана); изменённые/созданные файлы; команды тестов и их результат; отклонения от плана.
В самом КОНЦЕ отчёта — обязательная секция `## commit_plan` — таблица разметки логических коммитов (для человека, подтверждающего коммиты после ACCEPT):
| группа | файлы (glob) | сообщение коммита (1 строка) | rationale |
Правила commit_plan: заголовок секции — ровно `## commit_plan` (без номера и дополнений — иначе парсер `commit-by-plan` её не найдёт); заголовок таблицы — СТРОГО эти четыре колонки в этом порядке; группы соответствуют логике плана (пункт задания → коммит); в колонке «файлы» — только пути/globs от корня репо (fnmatch, напр. `src/*.py`, `tests/unit/test_*.py`), каждый в бэктиках, через запятую; пояснения («untracked», «(33)» и т.п.) — в rationale, не в колонку файлов; группы покрывают ВСЕ изменённые/созданные/удалённые файлы задачи; единица группировки — ФАЙЛ: каждый файл входит ровно в ОДНУ группу (несколько пунктов плана в одном файле → одна группа, пункты перечисли в rationale); пересечение групп по файлам запрещено — иначе первый add унесёт общий файл в чужой коммит.
Не делай git commit — версионированием управляет оркестратор."""
        reply = self._send(prompt, f"executor_impl_v{version}")
        self._ensure_artifact(f"{self.state['artifacts_rel']}/impl_report_v{version}.md",
                              "отчёт со всеми разделами и финальной секцией `## commit_plan`",
                              f"executor_impl_v{version}")
        return reply

    def revise_impl(self, task_text: str, plan_rel: str, version: int, rev_file: Path, diff_rel: str,
                    resumed: bool = False) -> str:
        rev = rev_file.read_text(encoding="utf-8")
        errata_block = errata_block_executor(plan_rel, read_plan_errata(self.repo, plan_rel), revise=True)
        impl_block = (IMPL_POLICY_EXECUTOR_REVISE + IMPL_POLICY_EXECUTOR_REPORT) if impl_strict_enabled(self.cfg) else ""
        prompt = (EXECUTOR_RESUMED_PREFACE if resumed else "") + self._common() + f"""
Отчёт ревью надсмотрщика по твоей имплементации (учти КАЖДОЕ замечание из «Сводки для исполнителя»):

\"\"\"{rev}\"\"\"

Контекст: план `{plan_rel}`, текущий дифф `{diff_rel}`.
{errata_block}{impl_block}{self._zone_impl_block()}{self._long_run_block(task_text)}Доработай имплементацию по замечаниям, перезапусти затронутые тесты, обнови отчёт: `{self.state['artifacts_rel']}/impl_report_v{version}.md` (новая версия). Обнови и финальную секцию `## commit_plan` (состав файлов мог измениться). В ответе — по каждому замечанию: как учтено или почему отклонено."""
        reply = self._send(prompt, f"executor_impl_v{version}")
        self._ensure_artifact(f"{self.state['artifacts_rel']}/impl_report_v{version}.md",
                              "обновлённый отчёт целиком, включая финальную секцию `## commit_plan`",
                              f"executor_impl_v{version}")
        return reply

    def solo_turn(self, task_text: str, turn: int) -> str:
        """solo-режим (pipev5): один вызов исполнителя без ревью. Протокол:
        executor пишет артефакты, краткое резюме — в ответ (не файл);
        завершение — маркер SOLO_DONE последней строкой. Оркестратор
        продолжает вызовы (resume той же сессии) до SOLO_DONE/лимита."""
        v = self.state.get("solo_report_version", 1)
        # НЕ обрезаем task_text: у solo-серий задания длинные, критичные
        # дополнения живут в конце файла (аддендум A b2bgguf: обрезка
        # [:3000] скрыла аддендум — исполнитель увидел только выполненный
        # исходный таск и дважды ответил ложным SOLO_DONE).
        prompt = self._common(solo=True) + f"""
ЗАДАЧА (полный текст task.md, включая аддендумы в конце файла):
\"\"\"{task_text}\"\"\"

ФАЗА: SOLO-ИСПОЛНЕНИЕ (без план-фазы и без ревью; доверяем таску).
Вызов {turn}. Выполняй следующий фрагмент задачи (или продолжай с места,
где остановился в прошлый вызов — сессия продолжается). Инструменты
окружения уже проверены серией-прецедентом; ничего в прод-коде не менять.
Прогресс фиксируй в `{self.state['artifacts_rel']}/solo_report.md`
(дописывай разделы «вызов N»: что сделано, команды, результаты, метки
места остановки). НЕ пытайся выполнить ВСЁ за один вызов: длинные
фоновые операции (скачивание) запускай и переходи к следующему шагу,
контролируй их в следующих вызовах.
Когда ВСЁ по DoD таска выполнено — проверь DoD по пунктам, допиши
в solo_report.md строку «## SOLO_DONE» и заверши ответ строкой:
SOLO_DONE
Иначе заверши ответ кратким статусом (что идёт фоном, что дальше)."""
        return self._send(prompt, f"solo_turn_{turn}")


# ----------------------------------------------------------------------------
# Параллельные серии: зоны записи (files_touch), держатели зон, блокировка репо
# ----------------------------------------------------------------------------
#
# Параллельность держится на worktree-изоляции (git.isolation=worktree): у каждой серии своё
# дерево и своя ветка duo/<task>, GitGuard смотрит только в своё дерево, поэтому серии друг друга
# не откатывают. Остаётся защитить СЛИЯНИЕ: две серии, меняющие одни файлы, дадут конфликт.
# Защита — зона записи: план заявляет секцию `## files_touch`; серия, начиная имплементацию,
# «занимает» зону (state.claim), а следующая серия с пересекающейся зоной не стартует, пока
# держатель не слит (merge) или не отпущен (claims --drop). Зона держателя = заявленная ∪
# фактически изменённые файлы его worktree (правда о диске, а не только обещание плана).

VALID_MODES = ("duo", "pair", "solo")
VALID_ISOLATIONS = ("shared", "worktree")

# грабля 11 (30.10.2026, боевой случай): план писал `## 12. files_touch` — секция не находилась,
# зона занимала 0 путей и claim-gate никого не защищал. Терпим необязательный номер заголовка
# (`## 12. files_touch`, `## 5) files_touch`, `## 12.files_touch`); files_touch обязан быть
# первым словом после номера — `## 12. files_touch_notes`/`## 12. Зона записи files_touch`
# секцией не считаются (`\b` не срабатывает перед `_`, входящим в `\w`).
FILES_TOUCH_HEADING = re.compile(r"^#{1,6}[ \t]+(?:\d+[.)][ \t]*)?files_touch\b.*$", re.I | re.M)
_ANY_HEADING = re.compile(r"^#{1,6}[ \t]+\S", re.M)
_ZONE_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
_ZONE_BACKTICK = re.compile(r"`([^`]+)`")
_GLOB_CHARS = "*?["


def _norm_zone_token(tok: str) -> str | None:
    """Токен зоны → нормализованный путь/glob от корня репо; None — не похоже на путь.
    Каталог `dir/` → `dir/*` (fnmatch: `*` пересекает `/`); абсолютные пути и `..` отклоняются."""
    t = tok.strip().strip("`'\"").rstrip(",;:.").replace("\\", "/")
    while t.startswith("./"):
        t = t[2:]
    if not t or t.startswith(("/", "~")) or any(c.isspace() for c in t):
        return None
    if ".." in t.split("/"):
        return None
    if "/" not in t and "." not in t and not any(c in t for c in _GLOB_CHARS):
        return None                       # «файл», «путь» из шапки таблицы и прочие слова
    if t.endswith("/"):
        t += "*"
    return t


def parse_files_touch(plan_text: str) -> list[str] | None:
    """Секция `## files_touch` плана → список нормализованных путей/globs; None — секции нет.

    Допустимы маркированные/нумерованные пункты (путь — в бэктиках или первым словом), строки
    таблицы (первая ячейка), голые строки внутри ```-блока. Секция заканчивается следующим
    заголовком. Пустая секция → [] (заявлено «ничего не пишем»)."""
    m = FILES_TOUCH_HEADING.search(plan_text)
    if not m:
        return None
    rest = plan_text[m.end():]
    nxt = _ANY_HEADING.search(rest)
    body = rest[:nxt.start()] if nxt else rest
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("```") or re.fullmatch(r"[|:\-\s]+", s):
            continue
        b = _ZONE_BULLET.match(s)
        if b:
            s = b.group(1).strip()
        elif s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            s = cells[0] if cells else ""
        tick = _ZONE_BACKTICK.search(s)
        tok = tick.group(1) if tick else (s.split()[0] if s.split() else "")
        norm = _norm_zone_token(tok)
        if norm and norm not in out:
            out.append(norm)
    return out


def path_in_zone(path: str, zone: list[str]) -> bool:
    """Покрыт ли путь зоной (точное совпадение или fnmatch-glob от корня репо)."""
    p = path.replace("\\", "/")
    return any(p == z or fnmatch.fnmatch(p, z) for z in zone)


def _glob_prefix(p: str) -> str:
    idx = [p.index(c) for c in _GLOB_CHARS if c in p]
    return p[:min(idx)] if idx else p


def patterns_overlap(p: str, q: str) -> bool:
    """Могут ли два пути/glob'а описывать один файл. Консервативно: глоб против глоба —
    по общему литеральному префиксу (`api/*.py` и `api/*.js` считаются пересекающимися,
    ложное срабатывание лечится `--ignore-claims`/`parallel.shared_ok`)."""
    if p == q:
        return True
    if fnmatch.fnmatch(p, q) or fnmatch.fnmatch(q, p):
        return True
    if q.startswith(p.rstrip("/") + "/") or p.startswith(q.rstrip("/") + "/"):
        return True                       # каталог без `/` на конце против файла внутри
    gp, gq = _glob_prefix(p), _glob_prefix(q)
    if gp != p and gq != q:
        return gp.startswith(gq) or gq.startswith(gp)
    return False


def _shared_ok(x: str, ok: list[str]) -> bool:
    return any(x == o or fnmatch.fnmatch(x, o) for o in ok)


def zone_conflicts(mine: list[str], theirs: list[str], ok: list[str] | None = None) -> list[tuple[str, str]]:
    """Пары (моя запись, их запись), которые могут описывать один файл; ok — globs «горячих»
    файлов (parallel.shared_ok), по которым пересечение не считается конфликтом."""
    ok = ok or []
    mine_f = [m for m in mine if not _shared_ok(m, ok)]
    theirs_f = [t for t in theirs if not _shared_ok(t, ok)]
    return [(m, t) for m in mine_f for t in theirs_f if patterns_overlap(m, t)]


@contextlib.contextmanager
def repo_lock(repo: Path, name: str = "duo-parallel.lock"):
    """Эксклюзивная блокировка уровня репозитория: создание worktree и проверка/занятие зон
    параллельных серий — атомарно (раньше `git worktree add` двух серий разносили `sleep 60`).
    Файл — в общем каталоге git (`.git/`), рабочее дерево и GitGuard его не видят."""
    rc, out = sh(["git", "rev-parse", "--git-common-dir"], cwd=Path(repo))
    gdir = Path(out.strip()) if rc == 0 and out.strip() else Path(repo) / ".git"
    if not gdir.is_absolute():
        gdir = Path(repo) / gdir
    gdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(gdir / name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                      # закрытие дескриптора снимает flock


def worktree_path(cfg: dict, repo: Path, task: str) -> Path:
    return Path(repo) / (cfg.get("git") or {}).get("worktrees_dir", ".worktrees") / task


def series_isolation(cfg: dict, repo: Path, task: str, st: dict) -> str:
    """Изоляция ЧУЖОЙ серии: пин в state; для старых серий без пина — по наличию их worktree."""
    iso = st.get("isolation")
    if iso in VALID_ISOLATIONS:
        return iso
    return "worktree" if (worktree_path(cfg, repo, task) / ".git").exists() else "shared"


def claim_released(cfg: dict, repo: Path, task: str, st: dict) -> bool:
    """Отпущена ли зона серии: нет claim / claims --drop / ветки нет (слита и удалена) /
    ветка с коммитами сверх базы целиком в HEAD основного дерева (слита руками или merge-командой).
    Ветка без коммитов (tip == база) НЕ считается слитой — серия в имплементации держит зону."""
    claim = st.get("claim")
    if not isinstance(claim, dict) or claim.get("released"):
        return True
    branch = f"{(cfg.get('git') or {}).get('branch_prefix', 'duo/')}{task}"
    rc, tip = sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=Path(repo))
    if rc != 0:
        return True
    tip = tip.strip()
    base = st.get("worktree_base")
    if base and tip != base:
        rc2, _ = sh(["git", "merge-base", "--is-ancestor", tip, "HEAD"], cwd=Path(repo))
        if rc2 == 0:
            return True
    return False


def done_zone_note(cfg: dict, repo: Path, task: str, st: dict, now: datetime | None = None) -> str:
    """Пометка для `status --all`: worktree-серия в phase=done, чья ветка не слита и зона
    записи не отпущена (claim держится до merge — тот же источник, что у `claims`).
    Пустая строка — пометки нет; иначе «не слита, зона держится N ч» (полные часы; `?` —
    если время занятия зоны не записано/не читается)."""
    if st.get("phase") != "done" or not isinstance(st.get("claim"), dict):
        return ""
    if series_isolation(cfg, repo, task, st) != "worktree" or claim_released(cfg, repo, task, st):
        return ""
    held = "не слита, зона держится"
    at = (st["claim"].get("at") or "").strip()
    try:
        started = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return f"{held} ?"
    now = now or datetime.now()
    return f"{held} {max(0, int((now - started).total_seconds() // 3600))} ч"


def series_changed_paths(cfg: dict, repo: Path, task: str, st: dict) -> list[str]:
    """Фактически изменённые файлы worktree серии: статус дерева ∪ коммиты сверх базы
    (без артефактов серии и provisioning-симлинков)."""
    wt = worktree_path(cfg, repo, task)
    if not (wt / ".git").exists():
        return []
    arts_rel = st.get("artifacts_rel") or f"{cfg['artifacts_dir']}/{task}"
    links = worktree_links_of(cfg)
    paths = {p for p in changed_files_all(wt)
             if not is_provisioned_symlink(wt, p, links) and not is_artifact_path(p, arts_rel)}
    base = st.get("worktree_base")
    if base:
        rc, out = sh(["git", "diff", "--name-only", base, "HEAD"], cwd=wt)
        if rc == 0:
            paths |= {ln for ln in out.splitlines() if ln.strip() and not is_artifact_path(ln, arts_rel)}
    return sorted(paths)


def collect_claim_holders(cfg: dict, repo: Path, exclude_task: str | None = None) -> list[dict]:
    """Серии, держащие зоны: worktree-серии с неотпущенным claim (см. claim_released).
    Зона держателя = заявленная (claim.paths) ∪ фактически изменённая (series_changed_paths)."""
    holders: list[dict] = []
    arts_root = Path(repo) / cfg["artifacts_dir"]
    for st_file in sorted(arts_root.glob("*/state.json")):
        task = st_file.parent.name
        if task == exclude_task:
            continue
        try:
            st = load_json(st_file)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(st, dict) or not isinstance(st.get("claim"), dict):
            continue
        if series_isolation(cfg, repo, task, st) != "worktree" or claim_released(cfg, repo, task, st):
            continue
        holders.append({
            "task": task, "phase": st.get("phase", "?"),
            "declared": list(st["claim"].get("paths") or []),
            "declared_known": bool(st["claim"].get("declared")),
            "actual": series_changed_paths(cfg, repo, task, st),
        })
    return holders


def find_claim_conflicts(mine: list[str], holders: list[dict], ok: list[str] | None = None) -> list[dict]:
    """Пересечения моей зоны с зонами держателей: [{task, phase, pairs:[(моя, их)]}]."""
    found: list[dict] = []
    for h in holders:
        theirs = list(dict.fromkeys(h["declared"] + h["actual"]))
        pairs = zone_conflicts(mine, theirs, ok)
        if pairs:
            found.append({"task": h["task"], "phase": h["phase"], "pairs": pairs})
    return found


# ----------------------------------------------------------------------------
# Оркестратор цикла
# ----------------------------------------------------------------------------

class Pipeline:
    def __init__(self, cfg: dict, task: str, solo: bool = False, mode: str | None = None,
                 isolation: str | None = None):
        self.cfg = cfg
        self.task = task
        self.repo = Path(cfg["repo"]).resolve()
        self.arts_root = self.repo / cfg["artifacts_dir"]
        self.task_dir = self.arts_root / task
        self.state_file = self.task_dir / "state.json"
        if not self.task_dir.exists():
            sys.exit(f"Задача не найдена: {self.task_dir}. Сначала `init --task {task}`.")
        st0 = self._peek_state()
        # Режим серии (duo | pair | solo): явный аргумент > пин в state (только duo/pair) > duo.
        # solo из state НЕ подхватывается молча: он меняет модель и запрещает ветку — его просят
        # флагом каждый раз, как и раньше. Пин нужен, чтобы `--continue`/`--resume`/сторож
        # продолжали серию в том режиме, в котором она начата, без повторения флагов.
        req = "solo" if solo else mode
        if req is None:
            req = st0.get("mode") if st0.get("mode") in ("duo", "pair") else "duo"
        if req not in VALID_MODES:
            sys.exit(f"неизвестный режим {req!r}: допустимы {', '.join(VALID_MODES)}")
        self.mode = req
        self.solo = req == "solo"
        if self.solo:
            # solo: исполнитель на дешёвой модели (sonnet -> glm-5.3-flash),
            # без план-фазы и дуо-ревью; ветка не создаётся (артефактные серии)
            scfg = cfg.get("solo", {})
            cfg["model"] = scfg.get("model", "sonnet")
            cfg["git"]["auto_branch"] = False
        self.t = ClaudeGlm(cfg)
        # pipev3 P2 (B1): git.isolation=worktree — серия работает в собственном
        # worktree <repo>/<worktrees_dir>/<task> (ветка duo/<task>), артефакты —
        # в основном дереве (симлинк из worktree). shared — прежнее поведение:
        # work_path == repo, все git-операции в основном дереве.
        # Изоляция ПИНУЕТСЯ в state при первом запуске: раньше её определял только config, и
        # забытый `--config` при `--continue`/commit-by-plan молча переводил серию в shared
        # (git checkout в дереве человека, коммиты не в то дерево). Приоритет: явный аргумент >
        # пин в state > существующий worktree серии > git.isolation из config.
        pinned = st0.get("isolation")
        if isolation is not None:
            if isolation not in VALID_ISOLATIONS:
                sys.exit(f"неизвестная изоляция {isolation!r}: допустимы {', '.join(VALID_ISOLATIONS)}")
            if pinned in VALID_ISOLATIONS and pinned != isolation:
                sys.exit(f"серия '{task}' уже запущена с изоляцией {pinned}: сменить на {isolation} нельзя "
                         f"(начни новую серию либо оставь {pinned})")
            self.isolation = isolation
        elif pinned in VALID_ISOLATIONS:
            self.isolation = pinned
        elif (worktree_path(cfg, self.repo, task) / ".git").exists():
            self.isolation = "worktree"
        else:
            self.isolation = cfg.get("git", {}).get("isolation", "shared")
        if self.isolation == "worktree":
            self.work_path = worktree_path(cfg, self.repo, task).resolve()
        else:
            self.work_path = self.repo
        # R5: guard.never_revert — пути, которые ревьюер не откатывает (правки хозяина)
        never_revert = (cfg.get("guard") or {}).get("never_revert") or []
        self.guard = GitGuard(self.work_path, never_revert)   # статус/дифф/гварды — по дереву серии
        self.main_guard = GitGuard(self.repo, never_revert)   # worktree-администрация — из основного дерева
        self._lock_fd = None

    def _peek_state(self) -> dict:
        """Состояние без выхода при отсутствии/порче (для разрешения режима/изоляции в __init__)."""
        try:
            st = load_json(self.state_file)
        except (OSError, json.JSONDecodeError):
            return {}
        return st if isinstance(st, dict) else {}

    def _members(self, phase: str) -> list[str]:
        """Ревьюеры фазы: pair — ОДИН проверяющий (pair.reviewer_plan/impl), иначе дуэт из config."""
        if self.mode == "pair":
            key = "reviewer_plan" if phase == "plan" else "reviewer_impl"
            name = (self.cfg.get("pair") or {}).get(key) or DEFAULTS["pair"][key]
            return [name]
        return self.cfg["duo"]["members_plan" if phase == "plan" else "members_impl"]

    def acquire_lock(self) -> None:
        """Блокировка задачи: один процесс — одна задача. Только для мутирующих команд."""
        self._lock_fd = open(self.task_dir / ".lock", "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"Задача '{self.task}' уже выполняется другим процессом (pipeline/{self.task}/.lock). "
                     f"Проверь: ps aux | rg duo_pipeline")

    def release_lock(self) -> None:
        """Снять .lock до выхода процесса (pipework2: короткие мутирующие команды
        set-verdict/rework/hold обязаны отпустить блокировку до `run --run` —
        иначе detached-ребёнок получит «уже выполняется»)."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                self._lock_fd.close()
                self._lock_fd = None

    # ---------- state ----------

    def load_state(self) -> dict:
        try:
            return load_json(self.state_file)
        except FileNotFoundError:
            sys.exit(f"state.json не найден для задачи '{self.task}'. "
                     f"Для новой задачи сначала: duo init --task {self.task} "
                     f"(или duo init --task {self.task} --task-file <файл>)")

    def save_state(self, st: dict) -> None:
        st["state_updated_at"] = time.time()  # pipev4: heartbeat — свежость state для сторожей
        # pipework2 P11: hold_impl принадлежит человеку (CLI hold/release); оркестратор его
        # не пишет, а при записи подхватывает с диска — иначе живой run затирал бы флаг,
        # поставленный между его load_state и save_state.
        with state_write_lock(self.task_dir):
            try:
                disk = load_json(self.state_file)
            except (OSError, json.JSONDecodeError):
                disk = None
            if disk is not None:
                if disk.get("hold_impl"):
                    st["hold_impl"] = disk["hold_impl"]
                else:
                    st.pop("hold_impl", None)
            save_json(self.state_file, st)

    def budget_left(self, st: dict) -> float:
        # budget_extra_usd — добавка конкретной серии (`run --resume --add-budget N`) без правки config.json
        return (self.cfg["limits"]["budget_usd"] + float(st.get("budget_extra_usd", 0.0) or 0.0)
                - st.get("spent_usd", 0.0))

    def iter_limit(self, st: dict, phase: str) -> int:
        """Лимит итераций фазы: config + st["iter_extra"][phase] (`run --resume --add-iterations N`)."""
        key = "max_plan_iterations" if phase == "plan" else "max_impl_iterations"
        return int(self.cfg["limits"][key]) + int((st.get("iter_extra") or {}).get(phase, 0) or 0)

    def check_budget(self, st: dict, soft: bool = False) -> None:
        """Жёсткая проверка — на ГРАНИЦЕ итерации, перед работой исполнителя. soft=True — перед
        ревью уже сделанной работы: оно доводится до конца в пределах limits.budget_grace_usd
        сверх бюджета (pipework3: стоп между работой исполнителя и её ревью оставлял
        непроверенную версию)."""
        grace = float(self.cfg["limits"].get("budget_grace_usd", 0.0) or 0.0) if soft else 0.0
        if self.budget_left(st) + grace <= 0:
            total = self.cfg["limits"]["budget_usd"] + float(st.get("budget_extra_usd", 0.0) or 0.0)
            raise BudgetExhausted(f"Бюджет ${total:g} исчерпан (потрачено ${st.get('spent_usd', 0):.2f})")

    def _escalation_question(self, st: dict, phase: str, rev_file: Path) -> bool:
        """pipev4 П5: эскалация вместо зацикливания (урок srocr: 10 ревизий).

        После escalation_after итераций без ACCEPT — отдельный вызов
        председателя: сформулировать ЕДИНСТВЕННОЕ ключевое разногласие.
        Ответ пишется в escalation.md, задача уходит в needs_human —
        решение за человеком (set-verdict или ручная правка артефакта,
        затем run --continue). escalation_after=None (дефолт) — выключено.

        pipework2 P15: возвращает True ТОЛЬКО если эскалация реально выполнена
        (серия ушла в needs_human). Раньше при уже существующем escalation.md
        функция молча возвращала управление, а вызывающий `_escalate_if_due`
        отдавал True — цикл останавливался с phase=plan без причины (a3run,
        «эскалация гасила микро-итерацию v4»).
        """
        if self.cfg["limits"].get("escalation_after") is None:
            return False
        esc_file = self.task_dir / "escalation.md"
        if esc_file.exists():
            # эскалация этой серии уже была и не архивирована ручным вмешательством —
            # не спамим председателя, цикл доработок продолжается
            log(self.task_dir, f"эскалация фазы {phase} пропущена: {esc_file.name} уже есть "
                               f"(архивируется при set-verdict/rework) — цикл продолжается")
            return False
        last_rev = rev_file.read_text(encoding="utf-8") if rev_file.exists() else ""
        prompt = f"""Ты — Председатель консилиума. Серия ревью фазы «{phase}» задачи зашла в тупик: итераций без ACCEPT больше {self.cfg['limits']['escalation_after']}, вердикт так и не получен.

Текст задачи (контекст):
\"\"\"{task_for_prompt(self.task_text(st), path_hint=str(self.task_dir / "task.md"))}\"\"\"

Последнее ревью (полностью):
\"\"\"{last_rev[:6000]}\"\"\"

Каталог артефактов серии: `{self.task_dir}` — планы/отчёты/ревью всех итераций; прочитай нужное сам.

Сформулируй для человека ЕДИНСТВЕННОЕ ключевое разногласие, из-за которого цикл не сходится: в чём именно расходятся стороны (исполнитель vs ревьюеры), по каким фактам, и какие 2-3 исхода решения возможны (с последствиями каждого). Не более 200 слов. Пиши по-русски, конкретно, с координатами (файл:строки)."""
        text, _, cost = self.t.call(prompt, mode="plan",
                                    timeout=self.cfg["timeouts"]["chairman_sec"], cwd=self.repo)
        st["spent_usd"] = round(st.get("spent_usd", 0.0) + cost, 4)
        esc_file.write_text(
            f"# Эскалация фазы {phase} (итераций без ACCEPT > {self.cfg['limits']['escalation_after']})\n\n"
            f"Дата: {time.strftime('%Y-%m-%d %H:%M')}. Вызов председателя: сформулируй ключевое разногласие.\n\n{text}\n\n"
            f"---\nРешение за человеком: `set-verdict --task {self.task} --verdict ACCEPT|REVISE --note ...` "
            f"и перезапуск `run --task {self.task} --continue`, либо правка артефакта руками.",
            encoding="utf-8")
        st["phase"] = "needs_human"
        st["escalated_phase"] = phase   # pipework2 P9: явное поле вместо разбора stop_reason подстрокой
        st["stop_reason"] = f"эскалация: {phase} не сходится {st.get(f'{phase}_iteration', '?')} итераций — см. escalation.md"
        self.save_state(st)
        log(self.task_dir, f"ЭСКАЛАЦИЯ: {phase} не сходится — председатель сформулировал разногласие, "
                           f"решение за человеком ({esc_file.name})")
        return True

    def archive_escalation(self) -> str | None:
        """pipework2: ручное вмешательство (set-verdict/rework) архивирует escalation.md
        в escalation_<ts>.md — иначе следующая эскалация серии была бы невозможна."""
        esc_file = self.task_dir / "escalation.md"
        if not esc_file.exists():
            return None
        target = self.task_dir / f"escalation_{time.strftime('%Y%m%d_%H%M%S')}.md"
        n = 1
        while target.exists():
            target = self.task_dir / f"escalation_{time.strftime('%Y%m%d_%H%M%S')}_{n}.md"
            n += 1
        esc_file.rename(target)
        return target.name

    @staticmethod
    def eff_iteration(st: dict, phase: str) -> int:
        """Итерации фазы с момента последнего ручного вмешательства (iter_base):
        лимит и эскалация считают их, а не сырой счётчик (pipework2)."""
        base = (st.get("iter_base") or {}).get(phase, 0)
        return max(0, st.get(f"{phase}_iteration", 0) - base)

    def _escalate_if_due(self, st: dict, phase: str, rev_file: Path) -> bool:
        """Обёртка для циклов: истина = остановиться (ушли в needs_human).

        Смотрит только вердикты СВОЕЙ фазы (ACCEPT план-фазы не гасит
        эскалацию impl-фазы).
        """
        esc = self.cfg["limits"].get("escalation_after")
        if esc is None:
            return False
        if self.eff_iteration(st, phase) < esc:
            return False
        # ACCEPT, выданные ДО последнего ручного вмешательства (iter_base.verdicts),
        # эскалацию не гасят: rework возвращает фазу, прежний ACCEPT уже не действует
        vb = (st.get("iter_base") or {}).get("verdicts", 0)
        if any(v.get("phase") == phase and v.get("verdict") == "ACCEPT"
               for v in st.get("verdicts", [])[vb:]):
            return False
        return self._escalation_question(st, phase, rev_file)   # P15: True только при реальной эскалации

    def _halt_missing_artifact(self, st: dict, phase: str, err: Exception) -> None:
        """pipework2 P12: артефакта ревью нет (исполнитель не записал файл / ручная правка
        версии в state) — серия уходит в needs_human с причиной; ревью не тратилось.
        Номер ревью, взятый перед вызовом, возвращаем (rev-файла не будет)."""
        self.halt(                                  # halt() сам возвращает «сгоревший» номер ревью
            st, "artifact_missing",
            f"артефакт ревью фазы {phase} не найден — ревью не запускалось ({err}). "
            f"Восстанови файл либо `set-verdict --verdict REVISE` (исполнитель перепишет по последнему ревью); "
            f"либо `run --task {self.task} --resume` (исполнитель повторит шаг в той же сессии)",
            phase=phase,
        )

    def _halt_empty_review(self, st: dict, phase: str, err: Exception) -> None:
        """Guard пустого ревью (29.10.2026): проверяющий вернул ACCEPT, не обратившись к инструментам,
        и повтор не помог — вердикт не засчитан, rev-файл не записан (номер возвращает halt()).
        Транскрипты попыток — transcripts/*_reviewer_*_empty*.md и итоговый."""
        self.halt(
            st, "review_empty",
            f"ревью фазы {phase} ПУСТОЕ: {err}; вердикт ACCEPT не засчитан (повтор с напоминанием не помог). "
            f"Проверь артефакт сам и `set-verdict --task {self.task} --verdict ACCEPT|REVISE`, "
            f"либо `run --task {self.task} --resume` (ревью повторится)",
            phase=phase,
        )

    def task_text(self, st: dict) -> str:
        f = self.task_dir / "task.md"
        return f.read_text(encoding="utf-8") if f.exists() else st.get("task", "")

    def next_rev_number(self, st: dict, phase: str) -> int:
        n = st.get(f"rev_counter_{phase}", 0) + 1
        st[f"rev_counter_{phase}"] = n
        return n

    # ---------- pipev3 P2 (B1): worktree-изоляция ----------

    def _ensure_worktree(self, st: dict) -> None:
        """Worktree на задачу (git.isolation=worktree).

        База = HEAD основного дерева на момент создания, пиннится в state
        (worktree_base) и при переиспользовании НЕ меняется. Ветка задачи,
        уже checkout в основном дереве, — громкий отказ ДО старта: в дерево
        человека оркестратор не вмешивается. Provisioning (симлинки тяжёлых
        untracked-ресурсов из основного дерева) — fail-closed: не поднялось —
        серия не стартует. Артефакты задачи — симлинк
        <worktree>/pipeline/<task> -> основное дерево (P1b вычитает запись
        из untracked/coverage).
        """
        branch = f"{self.cfg['git']['branch_prefix']}{self.task}"
        wt = self.work_path
        created = False
        branch_created = False
        if (wt / ".git").exists():
            # переиспользование: базу не меняем (пиннена в state)
            rc, h = sh(["git", "rev-parse", "HEAD"], cwd=wt)
            if rc == 0:
                st.setdefault("worktree_base", h.strip())
            self.save_state(st)
            log(self.task_dir, f"worktree переиспользуется: {wt}")
        else:
            rc, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo)
            if rc == 0 and cur.strip() == branch:
                sys.exit(f"ветка {branch} уже checkout в основном дереве — оркестратор "
                         f"в дерево человека не вмешивается; переключись сам или разреши вручную")
            # висячие записи git о worktree, чей каталог удалили руками (rm -rf), иначе
            # `worktree add` может отказать «уже зарегистрирован»; чужие живые не трогает
            sh(["git", "worktree", "prune"], cwd=self.repo)
            head = self.main_guard.head()
            if not head:
                sys.exit("в основном дереве нет коммитов (git rev-parse HEAD пуст): worktree-серии нужен "
                         "хотя бы один коммит — сделай базовый коммит и повтори")
            rc_v, _ = sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.repo)
            branch_existed = rc_v == 0
            if branch_existed:
                # ветка с наработками прошлой попытки: пин базы сохраняется (ветка выросла от неё)
                base = st.setdefault("worktree_base", head)
            else:
                # ни worktree, ни ветки нет — пин мог остаться только от прошлой ПРОВАЛЕННОЙ
                # попытки (Н3, 2026-11): тянуть устаревший пин = собрать серию на старой базе
                old = st.get("worktree_base")
                base = head
                st["worktree_base"] = base
                if old and old != base:
                    log(self.task_dir, f"БАЗА: пин worktree_base {str(old)[:10]} устарел (worktree и ветки "
                                       f"{branch} нет) — пересоздаю от HEAD {base[:10]}")
            # Н2: артефакты серии в коммитах базы — отказ ДО создания worktree/ветки (раньше
            # узнавали на provisioning, оставляя ветку-сироту и пин)
            tracked = artifacts_tracked_in_ref(self.repo, branch if branch_existed else base,
                                               f"{self.cfg['artifacts_dir']}/{self.task}")
            if tracked:
                self.save_state(st)
                sys.exit(worktree_artifacts_conflict_msg(self.task, self.cfg["artifacts_dir"], tracked))
            self.save_state(st)
            if branch_existed:
                rc, out = sh(["git", "worktree", "add", str(wt), branch], cwd=self.repo, timeout=300)
            else:
                rc, out = sh(["git", "worktree", "add", str(wt), "-b", branch, base],
                             cwd=self.repo, timeout=300)
                branch_created = rc == 0
                if rc != 0:
                    # гонка: ветку успели создать между проверкой и add — цепляем её
                    rc_v, _ = sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.repo)
                    if rc_v == 0:
                        rc, out = sh(["git", "worktree", "add", str(wt), branch], cwd=self.repo, timeout=300)
            if rc != 0:
                sys.exit(f"git worktree add не удался: {out.strip()[:300]}")
            created = True
            log(self.task_dir, f"создан worktree {wt} (ветка {branch}, база {base[:10]})")
        try:
            self._provision_worktree(wt)
        except SystemExit:
            if created:
                self._rollback_worktree(st, wt, branch, branch_created)
            raise

    def _provision_worktree(self, wt: Path) -> None:
        """Provisioning ресурсов + симлинк артефактов в worktree (идемпотентно, каждый запуск).
        Не удалось — SystemExit (fail-closed); откат только что созданного worktree — на вызывающем."""
        # имена ресурсов — из git.worktree_links конфига (R4)
        rc, out = sh([sys.executable, str(SCRIPT_DIR / "provision_worktree.py"),
                      str(self.repo), str(wt), *worktree_links_of(self.cfg)], timeout=300)
        if rc != 0:
            sys.exit(f"provisioning worktree НЕ удался (fail-closed, серия не стартует): "
                     f"{out.strip()[:300]}")
        # симлинк артефактов: <worktree>/pipeline/<task> -> основное дерево
        arts_link = wt / self.cfg["artifacts_dir"] / self.task
        if arts_link.exists() and not arts_link.is_symlink():
            sys.exit(worktree_artifacts_conflict_msg(self.task, self.cfg["artifacts_dir"], []))
        if not arts_link.is_symlink():
            arts_link.parent.mkdir(parents=True, exist_ok=True)
            arts_link.symlink_to(self.task_dir.resolve(), target_is_directory=True)

    def _rollback_worktree(self, st: dict, wt: Path, branch: str, branch_created: bool) -> None:
        """Откат ТОЛЬКО что созданного (в этом вызове) worktree после сбоя provisioning: иначе остаётся
        каталог + ветка-сирота + пин базы, и повтор `run` стартует на устаревшей базе (Н3).
        Ветка удаляется лишь если создана этим же вызовом (наработки прошлых попыток не трогаем)."""
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=self.repo, timeout=120)
        sh(["git", "worktree", "prune"], cwd=self.repo)
        if branch_created:
            sh(["git", "branch", "-D", branch], cwd=self.repo)
            st.pop("worktree_base", None)
            self.save_state(st)
        left = " (каталог не удалился — убери вручную)" if wt.exists() else ""
        log(self.task_dir, f"ОТКАТ создания worktree {wt}: провал provisioning"
                           f"{', ветка ' + branch + ' удалена, пин базы снят' if branch_created else ''}{left}")

    # ---------- режим/изоляция серии и параллельные серии ----------

    def _will_work(self, st: dict, force_continue: bool, resume: bool) -> bool:
        """Сделает ли этот запуск работу (а не просто напечатает подсказку и выйдет): только такие
        запуски пинуют режим/изоляцию и проходят guard параллельных серий."""
        ph = st.get("phase", "plan")
        if ph in ("plan", "impl", "solo"):
            return True
        if ph == "impl_pending":
            return bool(force_continue or self.cfg.get("auto_proceed_to_impl"))
        if ph == "needs_human":
            h = st.get("halt")
            return bool((force_continue or resume) and isinstance(h, dict) and h.get("resumable"))
        return False

    def _pin_mode_isolation(self, st: dict) -> None:
        """Записать режим и изоляцию серии в state: следующие запуски (--continue, --resume, сторож,
        commit-by-plan, merge) берут их оттуда, а не из флагов/конфига, который легко забыть."""
        if self.solo and st.get("phase") in ("impl_pending", "impl"):
            sys.exit(f"--solo неприменим к серии в phase={st['phase']}: solo не имеет план-фазы и ревью. "
                     f"Продолжи без --solo (режим duo/pair) либо начни новую серию")
        changed = False
        old = st.get("mode")
        if old != self.mode:
            if old:
                log(self.task_dir, f"РЕЖИМ: {old} → {self.mode}")
            st["mode"] = self.mode
            changed = True
        if st.get("isolation") != self.isolation:
            st["isolation"] = self.isolation
            changed = True
        if changed:
            self.save_state(st)
            log(self.task_dir, f"серия: режим {self.mode}, изоляция {self.isolation}")

    def other_live_series(self) -> list[tuple[str, dict]]:
        """Другие серии с живым оркестратором (занятый .lock): [(task, state)]."""
        out: list[tuple[str, dict]] = []
        for st_file in sorted(self.arts_root.glob("*/state.json")):
            d = st_file.parent
            if d.name == self.task or not lock_held(d):
                continue
            try:
                ost = load_json(st_file)
            except (OSError, json.JSONDecodeError):
                ost = {}
            out.append((d.name, ost if isinstance(ost, dict) else {}))
        return out

    def _shared_parallel_guard(self, allow: bool) -> None:
        """Две shared-серии в одном дереве убивают друг друга: GitGuard одной откатывает tracked-
        правки и вычищает новые файлы другой, `git checkout -b duo/<task>` переключает ветку под
        работающей серией. Отказ при живой другой shared-серии (исключение — пара solo+solo: у solo
        нет ревьюеров и гвардов). Выход: `--worktree` (своя папка и ветка) или дождаться."""
        if self.isolation != "shared":
            return
        clash = []
        for task, ost in self.other_live_series():
            if series_isolation(self.cfg, self.repo, task, ost) != "shared":
                continue
            o_mode = ost.get("mode") or "duo"
            if self.mode == "solo" and o_mode == "solo":
                continue
            clash.append(f"{task} ({o_mode}, phase={ost.get('phase', '?')})")
        if not clash:
            return
        msg = (f"в общем дереве уже работает серия: {', '.join(clash)}. Две серии в одном дереве убивают друг "
               f"друга (GitGuard откатывает/удаляет чужие файлы, смена ветки). Запусти эту серию в своей папке: "
               f"`run --task {self.task} --worktree` — или дождись завершения той; "
               f"`--allow-shared-parallel` — на свой риск (напр. та серия — solo без правок кода)")
        if allow:
            log(self.task_dir, f"ВНИМАНИЕ (--allow-shared-parallel): {msg}")
            return
        sys.exit(f"ОТКАЗ: {msg}")

    def _zone_from_plan(self, st: dict) -> tuple[list[str] | None, str]:
        """Зона записи из принятого плана (секция `## files_touch`): (paths|None, имя плана)."""
        name = f"plan_v{st.get('plan_version', 1) or 1}.md"
        f = self.task_dir / name
        if not f.is_file():
            return None, name
        zone = parse_files_touch(f.read_text(encoding="utf-8"))
        if zone is not None:
            arts_rel = f"{self.cfg['artifacts_dir']}/{self.task}"
            zone = [z for z in zone if not is_artifact_path(z, arts_rel)]
        return zone, name

    def _zone_notice(self, st: dict) -> None:
        """План принят: сообщить, какую зону записи он заявляет (и что её нет — защита не сработает)."""
        if self.isolation != "worktree":
            return
        zone, src = self._zone_from_plan(st)
        if zone is None:
            log(self.task_dir, f"ЗОНА: в {src} нет секции `## files_touch` — зона записи не заявлена; "
                               f"защита от пересечений с параллельными сериями возможна только по факту изменённых файлов")
        else:
            shown = ", ".join(zone[:8]) + (" …" if len(zone) > 8 else "")
            log(self.task_dir, f"ЗОНА: {src} заявляет {len(zone)} путей записи: {shown}")

    def _claim_gate(self, st: dict, arts_rel: str, ignore_claims: bool) -> bool:
        """Занять зону записи перед ПЕРВОЙ имплементацией (worktree-серии). False — пересечение
        с серией-держателем: серия остаётся в impl_pending, ничего не изменено (состояние лога и
        exit-код сообщают, кого ждать). Проверка и занятие атомарны (repo_lock): две серии не
        займут пересекающиеся зоны одновременно."""
        if self.isolation != "worktree":
            return True
        claim = st.get("claim")
        if isinstance(claim, dict) and not claim.get("released"):
            return True                                   # уже держим (возобновление/доработка)
        zone, src = self._zone_from_plan(st)
        ok = list((self.cfg.get("parallel") or {}).get("shared_ok") or [])
        with repo_lock(self.repo):
            holders = collect_claim_holders(self.cfg, self.repo, exclude_task=self.task)
            conflicts: list[dict] = []
            if zone is None:
                log(self.task_dir, f"ЗОНА: в {src} нет секции `## files_touch` — зона записи не заявлена")
                if holders and not ignore_claims:
                    names = ", ".join(h["task"] for h in holders)
                    log(self.task_dir,
                        f"БЛОК: имплементация {self.task} не стартует — идут серии с занятыми зонами ({names}), "
                        f"а зона этой серии не заявлена: пересечения проверить нечем. Допиши в {src} секцию "
                        f"`## files_touch` (список путей записи) и повтори `run --task {self.task} --continue`, "
                        f"либо `--ignore-claims` (на свой риск)")
                    return False
            else:
                conflicts = find_claim_conflicts(zone, holders, ok)
                if conflicts and not ignore_claims:
                    log(self.task_dir, f"БЛОК: имплементация {self.task} не стартует — зона записи пересекается "
                                       f"с работающей серией:")
                    for c in conflicts:
                        pairs = "; ".join(f"{m} ⇄ {t}" for m, t in c["pairs"][:5])
                        more = f" (+ещё {len(c['pairs']) - 5})" if len(c["pairs"]) > 5 else ""
                        log(self.task_dir, f"  • {c['task']} (phase={c['phase']}): {pairs}{more}")
                    holders_cmd = " ".join(f"`merge --task {c['task']}`" for c in conflicts)
                    log(self.task_dir,
                        f"Дождись слияния держателя ({holders_cmd}) и повтори `run --task {self.task} --continue`; "
                        f"брошенную серию отпусти `claims --task <имя> --drop`; "
                        f"`--ignore-claims` — на свой риск (конфликт слияния)")
                    return False
            now = datetime.now().isoformat(timespec="seconds")
            st["claim"] = {"paths": list(zone or []), "declared": zone is not None, "source": src, "at": now}
            if conflicts:
                st["claim"]["ignored_conflicts"] = [c["task"] for c in conflicts]
                log(self.task_dir, f"ВНИМАНИЕ (--ignore-claims): пересечение с {', '.join(c['task'] for c in conflicts)} "
                                   f"проигнорировано — ожидай конфликт при слиянии")
            self.save_state(st)
        n = len(zone or [])
        log(self.task_dir, f"ЗОНА: серия занимает {n} путей записи" + ("" if zone is not None else " (зона не заявлена)"))
        return True

    def _sync_base(self, st: dict, arts_rel: str) -> None:
        """Перед ПЕРВОЙ имплементацией подтянуть базу worktree к HEAD основного дерева: за время
        план-фазы (часы) человек и параллельные серии успели слить свои изменения, а имплементация
        на старой базе обернулась бы конфликтом при слиянии. Только fast-forward и только если в
        worktree ещё нет ничего своего; любое сомнение — пропуск с записью в лог, не ошибка."""
        if self.isolation != "worktree" or st.get("impl_base_head"):
            return
        if not (self.cfg.get("parallel") or {}).get("sync_base_at_impl", True):
            return
        base = st.get("worktree_base")
        main = self.main_guard.head()
        rc, head = sh(["git", "rev-parse", "HEAD"], cwd=self.work_path)
        head = head.strip()
        if not base or not main or rc != 0 or head == main:
            return
        if head != base:
            log(self.task_dir, "БАЗА: не подтягиваю — в ветке серии уже есть коммиты сверх базы")
            return
        dirty = [p for p in changed_files_all(self.work_path)
                 if not is_provisioned_symlink(self.work_path, p, worktree_links_of(self.cfg))
                 and not is_artifact_path(p, arts_rel)]
        if dirty:
            log(self.task_dir, f"БАЗА: не подтягиваю — в worktree есть изменения: {dirty[:5]}")
            return
        rc, _ = sh(["git", "merge-base", "--is-ancestor", head, main], cwd=self.repo)
        if rc != 0:
            log(self.task_dir, "БАЗА: не подтягиваю — HEAD основного дерева не потомок базы серии (другая линия истории)")
            return
        # грабля 1 (30.10.2026): непрослеживаемый симлинк pipeline/<task> в worktree блокирует
        # `git merge --ff-only`, если основное дерево закоммитило файлы pipeline/<task>/… —
        # «untracked working tree files would be overwritten», серия молча шла на старой базе.
        # Снимаем симлинк на время слияния и возвращаем в finally при любом исходе.
        arts_link = self.work_path / self.cfg["artifacts_dir"] / self.task
        link_was = arts_link.is_symlink()
        if link_was:
            arts_link.unlink()
        try:
            rc, out = sh(["git", "merge", "--ff-only", main], cwd=self.work_path, timeout=300)
            if rc != 0:
                log(self.task_dir, f"БАЗА: fast-forward не удался ({out.strip()[:160]}) — имплементация на прежней базе")
                return
        finally:
            if link_was:
                # ff мог принести реальные pipeline/<task>/… из базы: артефакты серии живут
                # в симлинке (общий каталог с основным деревом) — пришедшее убираем; статус
                # worktree показывает ` D pipeline/<task>/…`, это фильтруется is_artifact_path
                if arts_link.exists() and not arts_link.is_symlink():
                    _remove_path(arts_link)
                if not arts_link.is_symlink():
                    arts_link.parent.mkdir(parents=True, exist_ok=True)
                    arts_link.symlink_to(self.task_dir.resolve(), target_is_directory=True)
        rc, cnt = sh(["git", "rev-list", "--count", f"{head}..{main}"], cwd=self.repo)
        st["worktree_base"] = main
        self.save_state(st)
        log(self.task_dir, f"БАЗА: worktree подтянут к HEAD основного дерева: +{cnt.strip() or '?'} коммит(ов) "
                           f"({head[:10]} → {main[:10]}); план составлен на прежней базе")

    def _zone_outside(self, st: dict, arts_rel: str) -> list[str]:
        """Изменённые в worktree файлы вне заявленной зоны записи (пусто: зона не заявлена/shared)."""
        claim = st.get("claim")
        if self.isolation != "worktree" or not isinstance(claim, dict) or not claim.get("declared"):
            return []
        zone = list(claim.get("paths") or [])
        changed = [p for p in changed_files_all(self.work_path)
                   if not is_provisioned_symlink(self.work_path, p, worktree_links_of(self.cfg))
                   and not is_artifact_path(p, arts_rel)]
        return [p for p in changed if not path_in_zone(p, zone)]

    # ---------- основной цикл ----------

    # ---------- pipework3: чистые остановки, шаги исполнителя, возобновление ----------

    def halt(self, st: dict, kind: str, reason: str, *, phase: str | None = None,
             resumable: bool = True) -> None:
        """Единая точка остановки серии (pipework3).

        Любая остановка — запись в state (phase=needs_human, stop_reason, halt{kind,phase,
        resumable}) + строка в orchestrator.log + (опционально) десктоп-уведомление. Раньше
        RuntimeError из транспорта/бюджета уходил в `main` и процесс умирал МОЛЧА с phase=impl:
        ни причины в state, ни строки в логе — сторож видел только «мёртв».

        Прерванный шаг доработки (inflight revise_*) превращается в маркер rework (источник
        auto-resume) — `run --resume` первым делом доделает именно его, в той же сессии.
        """
        cur = phase or st.get("phase")
        if cur not in ("plan", "impl", "solo"):
            cur = (st.get("halt") or {}).get("phase") or cur
        if cur in ("plan", "impl"):
            # ревью оборвалось до записи rev-файла (сбой посреди R1–R3/синтеза): номер не должен
            # «сгорать» — иначе в каталоге дыра (rev001 нет, следующий rev002)
            n = st.get(f"rev_counter_{cur}", 0)
            if n > 0 and not (self.task_dir / f"rev{n:03d}_{cur}.md").exists():
                st[f"rev_counter_{cur}"] = n - 1
        inf = st.get("inflight")
        if isinstance(inf, dict) and inf.get("step") in ("revise_plan", "revise_impl") \
                and inf.get("rev") and not st.get("rework"):
            st["rework"] = {"phase": inf.get("phase"), "rev": inf["rev"], "source": "auto-resume",
                            "note": f"шаг {inf['step']} прерван: {kind}",
                            "at": datetime.now().isoformat(timespec="seconds")}
        if cur not in ("plan", "impl"):
            resumable = False                # solo и прочее: возобновления с места нет — решает человек
        desktop = bool((self.cfg.get("notify") or {}).get("desktop"))
        st["phase"] = "needs_human"
        st["stop_reason"] = reason
        st["halt"] = {"kind": kind, "phase": cur, "resumable": bool(resumable), "notified": desktop,
                      "at": datetime.now().isoformat(timespec="seconds")}
        self.save_state(st)
        log(self.task_dir, f"СТОП: {reason}")
        self._notify(f"{self.task}: остановка ({kind})", reason[:200])

    def _notify(self, title: str, body: str) -> None:
        """Best-effort десктоп-уведомление (notify.desktop в config.json; в тестах выключено)."""
        if not (self.cfg.get("notify") or {}).get("desktop"):
            return
        try:
            subprocess.run(["notify-send", "-u", "critical", "-t", "0", title, body],
                           timeout=5, capture_output=True)
        except Exception:
            pass

    @staticmethod
    def _rework_label(st: dict) -> str:
        return ("возобновление доработки" if (st.get("rework") or {}).get("source") == "auto-resume"
                else "ручная доработка")

    def _exec_step(self, st: dict, step: str, phase: str, version: int, rev: str | None, fn):
        """Шаг исполнителя с маркером inflight: прерванный шаг (halt или смерть процесса)
        возобновляется следующим запуском в той же сессии с преамбулой «сверь состояние»."""
        prev = st.get("inflight")
        resumed = bool(isinstance(prev, dict) and prev.get("step") == step and prev.get("version") == version)
        st["inflight"] = {"step": step, "phase": phase, "version": version, "rev": rev,
                          "at": datetime.now().isoformat(timespec="seconds")}
        self.save_state(st)
        result = fn(resumed)
        st.pop("inflight", None)      # сохранится вместе с новой версией артефакта (одна запись)
        return result

    def _recover_inflight(self, st: dict) -> None:
        """Процесс умер посреди revise-шага без halt (kill -9, перезагрузка, OOM): восстановить
        маркер rework из inflight — иначе рестарт отревьюил бы прежнюю версию артефакта."""
        inf = st.get("inflight")
        if not isinstance(inf, dict):
            return
        if inf.get("step") in ("revise_plan", "revise_impl") and inf.get("rev") and not st.get("rework"):
            st["rework"] = {"phase": inf.get("phase"), "rev": inf["rev"], "source": "auto-resume",
                            "note": f"шаг {inf['step']} прерван (процесс умер)",
                            "at": datetime.now().isoformat(timespec="seconds")}
            log(self.task_dir, f"ВОССТАНОВЛЕНИЕ: прерванный шаг {inf['step']} v{inf.get('version')} — "
                               f"доработка по {inf['rev']} будет доведена первым шагом")

    def _try_resume_halt(self, st: dict) -> bool:
        """`run --resume` (и --continue) из needs_human с машинной записью halt: возобновить с места остановки.
        True — серия выведена из needs_human (st изменён, не сохранён); False — нужен человек."""
        h = st.get("halt")
        if not isinstance(h, dict) or not h.get("resumable") or h.get("phase") not in ("plan", "impl"):
            return False
        kind, phase = h.get("kind"), h["phase"]
        if kind == "budget" and self.budget_left(st) <= 0:
            log(self.task_dir, "СТОП остаётся: бюджет исчерпан. Подними limits.budget_usd в config.json "
                               f"или `run --task {self.task} --resume --add-budget USD`")
            return False
        if kind == "limit":
            if self.eff_iteration(st, phase) >= self.iter_limit(st, phase):
                log(self.task_dir, f"СТОП остаётся: лимит итераций {phase} ({self.iter_limit(st, phase)}) исчерпан. "
                                   f"Подними limits.max_{phase}_iterations в config.json или "
                                   f"`run --task {self.task} --resume --add-iterations N`")
                return False
            rev = last_review_name(self.task_dir, phase)
            if not rev:
                return False
            if not st.get("rework"):
                st["rework"] = {"phase": phase, "rev": rev, "source": "auto-resume",
                                "note": "лимит итераций был исчерпан — доработка по последнему ревью",
                                "at": datetime.now().isoformat(timespec="seconds")}
            st["phase"] = phase
        elif kind == "artifact_missing":
            # шаг исполнителя «завершился» без артефакта: откат счётчика версии → шаг повторяется
            # в той же сессии (плана нет → make_plan v1, иначе revise по последнему ревью)
            vkey = "plan_version" if phase == "plan" else "impl_version"
            st[vkey] = max(0, st.get(vkey, 0) - 1)
            if st[vkey] >= 1:
                rev = last_review_name(self.task_dir, phase)
                if not rev:
                    st[vkey] += 1
                    return False
                st["rework"] = {"phase": phase, "rev": rev, "source": "auto-resume",
                                "note": "артефакт шага не записан — шаг повторяется",
                                "at": datetime.now().isoformat(timespec="seconds")}
            st["phase"] = phase
        else:
            st["phase"] = phase
        st.pop("halt", None)
        st["stop_reason"] = f"возобновлено (--resume/--continue) после остановки {kind}"
        log(self.task_dir, f"ВОЗОБНОВЛЕНИЕ: {kind} → фаза {phase}")
        return True

    def _errata_gate(self, st: dict, arts_rel: str) -> None:
        """Эрраты председателя принятого плана обязаны лежать в plan_vN.md ДО имплементации
        (pipework3). Идемпотентно: блок уже дописан (маркер) либо перенесён вручную — ничего не
        меняется. Не удалось (пустой блок, частичный перенос) — ErrataError → halt.

        Только при review.plan_policy=triage: именно эта политика обязывает председателя выдавать
        блок `## Эрраты к плану` (итоговый ACCEPT план не правится никем — ветка `break` цикла)."""
        if not plan_triage_enabled(self.cfg):
            return
        last = next((v for v in reversed(st.get("verdicts", [])) if v.get("phase") == "plan"), None)
        if not last or last.get("verdict") != "ACCEPT" or not str(last.get("rev", "")).startswith("rev"):
            return                                   # ручной ACCEPT (set-verdict): блока председателя нет
        rev_path = self.task_dir / last["rev"]
        plan_path = self.task_dir / f"plan_v{st.get('plan_version', 1)}.md"
        if not rev_path.is_file() or not plan_path.is_file():
            return
        status, info = apply_accept_errata(rev_path, plan_path)
        if status == "applied":
            st["errata_applied"] = {"rev": last["rev"], "plan": plan_path.name, **info,
                                    "at": datetime.now().isoformat(timespec="seconds")}
            log(self.task_dir, f"ЭРРАТЫ: блок {last['rev']} дописан в {arts_rel}/{plan_path.name} "
                               f"({info['items']} пунктов, sha256:{info['sha']})")
        elif status == "present":
            log(self.task_dir, f"ЭРРАТЫ: блок {last['rev']} уже в {plan_path.name} — без изменений")
        else:
            log(self.task_dir, f"ЭРРАТЫ: в {last['rev']} блока «Эрраты к плану» нет — эррат у плана нет")

    def _boundary_stop(self, st: dict, phase: str, rev_file: Path, arts_rel: str) -> bool:
        """Граница итерации (pipework3): ревью + синтез председателя уже доведены до конца, дальше
        — работа исполнителя. Именно здесь, и только здесь, проверяются лимит итераций и бюджет;
        остановка оставляет маркер rework (доработка по rev_file не начата), и `run --resume`
        продолжает ровно с неё, а не гоняет повторное ревью. True — серия остановлена."""
        marker = {"phase": phase, "rev": rev_file.name, "source": "auto-resume",
                  "note": "граница итерации: ревью завершено, доработка не начата",
                  "at": datetime.now().isoformat(timespec="seconds")}
        lim = self.iter_limit(st, phase)
        if self.eff_iteration(st, phase) >= lim:
            st["rework"] = marker
            what = "плана" if phase == "plan" else "имплементации"
            key = "max_plan_iterations" if phase == "plan" else "max_impl_iterations"
            self.halt(st, "limit",
                      f"исчерпан лимит итераций {what} ({lim}): ревью {rev_file.name} (REVISE) завершено, "
                      f"доработка по нему НЕ начата. Подними limits.{key} (или `run --task {self.task} "
                      f"--resume --add-iterations N`) — продолжение начнётся с доработки по {rev_file.name}. "
                      f"Артефакты: `{arts_rel}/`", phase=phase)
            return True
        try:
            self.check_budget(st)
        except BudgetExhausted as e:
            st["rework"] = marker
            self.halt(st, "budget", f"{e} — ревью {rev_file.name} завершено, доработка по нему НЕ начата. "
                      f"Подними limits.budget_usd либо `run --task {self.task} --resume --add-budget USD`",
                      phase=phase)
            return True
        return False

    def run(self, phases: str = "auto", force_continue: bool = False, add_budget: float = 0.0,
            add_iterations: int = 0, resume: bool = False, allow_shared_parallel: bool = False,
            ignore_claims: bool = False) -> None:
        """Цикл серии. pipework3: любая необработанная ошибка превращается в чистую остановку
        (halt: state + лог), а не в молчаливую смерть процесса с phase=plan|impl.

        allow_shared_parallel — разрешить shared-серию при живой другой shared-серии (см.
        _shared_parallel_guard); ignore_claims — стартовать имплементацию вопреки пересечению зон
        записи с серией-держателем (см. _claim_gate)."""
        self.acquire_lock()
        self._live_state: dict | None = None
        try:
            self._run_impl(phases, force_continue, add_budget, add_iterations, resume,
                           allow_shared_parallel, ignore_claims)
        except (KeyboardInterrupt, SystemExit):
            raise
        except ErrataError as e:
            st = self._live_state or self.load_state()
            self.halt(st, "errata", f"перенос эррат председателя в план не удался: {e}. Допиши блок "
                      f"`## Эрраты к плану` в plan_v{st.get('plan_version', 1)}.md руками (или поправь ревью и "
                      f"`errata --task {self.task} --apply`), затем `set-verdict --task {self.task} --verdict ACCEPT`",
                      phase="plan", resumable=False)
        except BudgetExhausted as e:
            st = self._live_state or self.load_state()
            self.halt(st, "budget", f"{e}. Подними limits.budget_usd либо "
                      f"`run --task {self.task} --resume --add-budget USD`")
        except RuntimeError as e:
            st = self._live_state or self.load_state()
            self.halt(st, "transport", f"сбой вызова модели после повторов: {e}. "
                      f"Продолжить: `run --task {self.task} --resume`")
        except Exception as e:
            traceback.print_exc()
            st = self._live_state or self.load_state()
            self.halt(st, "crash", f"внутренняя ошибка оркестратора: {type(e).__name__}: {str(e)[:200]} "
                      f"(трейс — orchestrator_bg.log). Продолжить: `run --task {self.task} --resume`")
        finally:
            self.release_lock()          # процесс обычно завершается следом; явный release — для вложенных/тестовых вызовов

    def _run_impl(self, phases: str, force_continue: bool, add_budget: float, add_iterations: int = 0,
                  resume: bool = False, allow_shared_parallel: bool = False,
                  ignore_claims: bool = False) -> None:
        st = self.load_state()
        self._live_state = st
        # Н1: задание — нетронутый шаблон/пусто: отказ ДО любых побочных эффектов (worktree, бюджет,
        # пины). Только для старта планирования/solo — продолжение impl идёт по принятому плану.
        tfile = self.task_dir / "task.md"
        if (st.get("phase", "plan") in ("plan", "solo") and tfile.exists()
                and task_md_is_placeholder(tfile.read_text(encoding="utf-8"))):
            sys.exit(f"task.md серии {self.task} пуст или остался шаблоном «{TASK_TEMPLATE_MARK}» — "
                     f"исполнитель спланировал бы пустоту. Напиши задание в {tfile} и повтори run. "
                     f"Если задание было и пропало — восстанови из git/бэкапа (не из шаблона).")
        if add_budget:
            st["budget_extra_usd"] = round(float(st.get("budget_extra_usd", 0.0) or 0.0) + float(add_budget), 4)
            log(self.task_dir, f"БЮДЖЕТ: добавлено ${add_budget:g} (итого extra ${st['budget_extra_usd']:g})")
            self.save_state(st)
        if add_iterations:
            ph = (st.get("halt") or {}).get("phase") if st.get("phase") == "needs_human" else st.get("phase")
            if ph in ("plan", "impl"):
                ie = st.setdefault("iter_extra", {})
                ie[ph] = int(ie.get(ph, 0) or 0) + int(add_iterations)
                log(self.task_dir, f"ЛИМИТ ИТЕРАЦИЙ: +{add_iterations} для фазы {ph} (итого {self.iter_limit(st, ph)})")
                self.save_state(st)
            else:
                log(self.task_dir, f"--add-iterations {add_iterations} проигнорирован: фаза остановки не plan|impl")
        self._recover_inflight(st)
        if self._will_work(st, force_continue, resume):
            # guard ДО пина: отказ не должен закрепить isolation=shared — иначе предложенный
            # в сообщении выход `--worktree` упёрся бы в «изоляцию серии сменить нельзя»
            self._shared_parallel_guard(allow_shared_parallel)
            self._pin_mode_isolation(st)
        if self.isolation == "worktree":
            with repo_lock(self.repo):          # создание worktree двух серий — строго по очереди
                self._ensure_worktree(st)       # pipev3 P2: ветка/база/provisioning worktree
        else:
            self.guard.ensure_branch(self.task, self.cfg, self.task_dir)
        st.setdefault("phase", "plan")
        # pipev3 P2: исполнитель и ревьюеры работают с cwd = дереву серии
        # (shared — основное дерево, worktree — .worktrees/<task>)
        ex = Executor(self.cfg, self.t, self.task_dir, self.work_path, st)
        reviewer = DuoReviewer(self.cfg, self.t, self.task_dir, self.work_path)
        arts_rel = f"{self.cfg['artifacts_dir']}/{self.task}"

        # ---- фаза SOLO (упрощённый режим: исполнитель без ревью) ----
        # pipev5 solo: до фазы PLAN — solo-задача вообще не проходит
        # планирование (артефактные серии, модель glm-5.3-flash)
        if st["phase"] == "plan" and self.solo:
            st["phase"] = "solo"
            st["solo_turn"] = st.get("solo_turn", 0)
            st["solo_base_head"] = self.guard.head()
            # hygiene: маркер прошлой серии в solo_report — ловушка ложного
            # DONE (b2bgguf аддендум A: resume-сессия увидела старый маркер
            # и завершилась за 15 сек). Вход в solo стирает его.
            sr = self.task_dir / "solo_report.md"
            if sr.exists():
                txt = sr.read_text(encoding="utf-8")
                for marker in ("## SOLO_DONE", "SOLO_DONE"):
                    if marker in txt:
                        txt = txt.replace("## SOLO_DONE\n", "").replace(
                            "## SOLO_DONE", "").replace("SOLO_DONE\n", "").replace("SOLO_DONE", "")
                        log(self.task_dir, "SOLO: стёрт маркер прошлой серии из solo_report.md")
                        break
                sr.write_text(txt, encoding="utf-8")
            self.save_state(st)
            log(self.task_dir, f"SOLO: режим без план-фазы и дуо-ревью "
                f"(модель: {self.cfg['model']}); исполнитель выполняет task.md")
        if st["phase"] == "solo":
            ex_s = Executor(self.cfg, self.t, self.task_dir, self.work_path, st)
            max_turns = int(self.cfg.get("solo", {}).get("max_turns", 24))
            while st["phase"] == "solo":
                turn = st.get("solo_turn", 0) + 1
                if turn > max_turns:
                    st["phase"] = "needs_human"
                    st["stop_reason"] = f"solo: исчерпан лимит вызовов ({max_turns})"
                    self.save_state(st)
                    log(self.task_dir, f"СТОП: {st['stop_reason']}. Вмешайся: продолжи hands-on или подними лимит.")
                    return
                self.check_budget(st)
                log(self.task_dir, f"SOLO вызов {turn}/{max_turns}")
                reply = ex_s.solo_turn(self.task_text(st), turn)
                st["solo_turn"] = turn
                self.save_state(st)
                # маркер = СТРОГО последняя строка ответа, отдельной строкой;
                # подстрочное вхождение в тексте планов ("...затем SOLO_DONE")
                # НЕ считается (ложный выход b2bgguf аддендум A, 22:16)
                lines = [l.strip() for l in reply.strip().splitlines() if l.strip()]
                if lines and lines[-1] == "SOLO_DONE":
                    st["phase"] = "done"
                    st["stop_reason"] = "solo: исполнитель заявил SOLO_DONE"
                    self.save_state(st)
                    log(self.task_dir, f"SOLO: SOLO_DONE — задача завершена "
                        f"({st.get('spent_usd', 0.0):.2f} USD, {turn} вызов(ов)).")
                    return

        # ---- needs_human: оркестратор молча не продолжает (pipework2) ----
        # pipework3: `run --resume|--continue` возобновляет остановку с машинной записью halt
        # (транспорт, лимит итераций, бюджет, артефакт не записан) сам; остальное — человек.
        if st["phase"] == "needs_human":
            if (force_continue or resume) and self._try_resume_halt(st):
                self.save_state(st)
            else:
                hint = (f" Либо `run --task {self.task} --resume` (возобновить с места остановки)."
                        if (st.get("halt") or {}).get("resumable") else "")
                log(self.task_dir, f"СТОП: серия в needs_human ({st.get('stop_reason') or 'причина не записана'}). "
                    f"Выведи: `set-verdict --task {self.task} --verdict ACCEPT|REVISE` "
                    f"или `rework --task {self.task} --phase plan|impl --review FILE`.{hint}")
                return

        # маркер доработки должен соответствовать фазе; иначе он мусорный
        rw = st.get("rework")
        if rw and rw.get("phase") != st["phase"]:
            log(self.task_dir, f"ВНИМАНИЕ: маркер rework фазы {rw.get('phase')} при phase={st['phase']} — сброшен")
            st.pop("rework", None)
            self.save_state(st)

        # ---- фаза PLAN ----
        if st["phase"] == "plan":
            v = st.get("plan_version", 0) + 1
            if v == 1:
                log(self.task_dir, f"PLAN: исполнитель делает plan_v{v}")
                self.check_budget(st)
                self._exec_step(st, "make_plan", "plan", v, None,
                                lambda resumed: ex.make_plan(self.task_text(st), v, resumed=resumed))
                st["plan_version"] = v
                self.save_state(st)
            # pipework2: ручная доработка — ПЕРВЫМ шагом исполнитель переписывает план
            # по зарегистрированному ревью, и только потом ревью уже новой версии
            if st.get("rework", {}).get("phase") == "plan":
                rw_rev = self.task_dir / st["rework"]["rev"]
                nv = st.get("plan_version", 0) + 1
                self.check_budget(st)
                log(self.task_dir, f"PLAN: {self._rework_label(st)} — исполнитель переписывает план до v{nv} по {rw_rev.name}")
                self._exec_step(st, "revise_plan", "plan", nv, rw_rev.name,
                                lambda resumed: ex.revise_plan(self.task_text(st), nv, rw_rev, resumed=resumed))
                st["plan_version"] = nv
                st.pop("rework", None)
                self.save_state(st)
            while st["phase"] == "plan":
                cur_v = st["plan_version"]
                it = st.get("plan_iteration", 0) + 1
                # pipework3: лимит итераций проверяется на ГРАНИЦЕ (после ревью, перед доработкой),
                # а не здесь: уже сделанная версия плана ВСЕГДА доходит до ревью и синтеза
                # председателя. Раньше стоп стоял тут — после доработки, перед её ревью (6 из 6
                # остановок по лимиту имели ровно эту форму: непроверенная версия в воздухе).
                self.check_budget(st, soft=True)
                n = self.next_rev_number(st, "plan")
                rev_file = self.task_dir / f"rev{n:03d}_plan.md"
                log(self.task_dir, f"PLAN ревью #{it} (rev{n:03d}) плана v{cur_v}")
                try:
                    verdict, cost, telemetry = reviewer.review(
                        artifact_rel=f"{arts_rel}/plan_v{cur_v}.md", phase="plan",
                        task_text=self.task_text(st),
                        members=self._members("plan"),
                        rounds=self.cfg["duo"]["rounds"],
                        rev_file=rev_file, guard=self.guard, artifacts_rel=arts_rel,
                    )
                except ArtifactMissingError as e:
                    self._halt_missing_artifact(st, "plan", e)
                    return
                except EmptyReviewError as e:
                    self._halt_empty_review(st, "plan", e)
                    return
                st["plan_iteration"] = it
                st["spent_usd"] = round(st.get("spent_usd", 0.0) + cost, 4)
                st.setdefault("review_telemetry", []).append(telemetry)   # pipev3 P1
                st.setdefault("verdicts", []).append({"rev": rev_file.name, "phase": "plan", "verdict": verdict})
                if verdict == "ACCEPT":
                    st["phase"] = "impl_pending"
                    self.save_state(st)
                    log(self.task_dir, f"План ПРИНЯТ (rev{n:03d}).")
                    self._errata_gate(st, arts_rel)       # pipework3: эрраты председателя → в plan_vN.md
                    self._zone_notice(st)                 # worktree: какую зону записи заявил план
                    self.save_state(st)
                    break
                self.save_state(st)
                # pipev4 П5: эскалация после N итераций без ACCEPT (если включена)
                if self._escalate_if_due(st, "plan", rev_file):
                    return
                # граница итерации: ревью + синтез председателя доведены до конца — теперь можно стоять
                if self._boundary_stop(st, "plan", rev_file, arts_rel):
                    return
                nv = cur_v + 1
                log(self.task_dir, f"PLAN: исполнитель дорабатывает план до v{nv} по {rev_file.name}")
                self._exec_step(st, "revise_plan", "plan", nv, rev_file.name,
                                lambda resumed: ex.revise_plan(self.task_text(st), nv, rev_file, resumed=resumed))
                st["plan_version"] = nv
                self.save_state(st)

        # ---- пауза перед имплементацией ----
        if st["phase"] == "impl_pending":
            if not (self.cfg["auto_proceed_to_impl"] or force_continue):
                log(self.task_dir, "План принят. Остановка перед имплементацией. "
                    f"Продолжить: `run --task {self.task} --continue` (или auto_proceed_to_impl=true).")
                return
            self._errata_gate(st, arts_rel)               # pipework3: страховка (ACCEPT мог быть ручным/до патча)
            if not self._claim_gate(st, arts_rel, ignore_claims):
                self.save_state(st)                       # зона пересекается с работающей серией: ждём в impl_pending
                return
            self._sync_base(st, arts_rel)                 # worktree: база → HEAD основного дерева (только ff, чистый worktree)
            st["phase"] = "impl"
            self.save_state(st)

        # ---- фаза IMPL ----
        if st["phase"] == "impl":
            st.setdefault("impl_base_head", self.guard.head())
            ex2 = Executor(self.cfg, self.t, self.task_dir, self.work_path, st)
            v = st.get("impl_version", 0) + 1
            if v == 1:
                log(self.task_dir, f"IMPL: исполнитель имплементирует план (отчёт v{v})")
                self.check_budget(st)
                plan_rel = f"{arts_rel}/plan_v{st['plan_version']}.md"
                self._exec_step(st, "implement", "impl", v, None,
                                lambda resumed: ex2.implement(self.task_text(st), plan_rel, v, resumed=resumed))
                st["impl_version"] = v
                self.save_state(st)
            # pipework2: ручная доработка impl — исполнитель правит по зарегистрированному
            # ревью ДО нового ревью (иначе ревьюировалась бы прежняя версия)
            if st.get("rework", {}).get("phase") == "impl":
                rw_rev = self.task_dir / st["rework"]["rev"]
                cur = st.get("impl_version", 1)
                nv = cur + 1
                self.check_budget(st)
                self.guard.diff_patch(st["impl_base_head"], self.task_dir / f"diff_impl_v{cur}.patch")
                log(self.task_dir, f"IMPL: {self._rework_label(st)} — исполнитель правит до v{nv} по {rw_rev.name}")
                self._exec_step(
                    st, "revise_impl", "impl", nv, rw_rev.name,
                    lambda resumed: ex2.revise_impl(
                        self.task_text(st),
                        f"{arts_rel}/plan_v{st['plan_version']}.md",
                        nv, rw_rev,
                        f"{arts_rel}/diff_impl_v{cur}.patch",
                        resumed=resumed,
                    ))
                st["impl_version"] = nv
                st.pop("rework", None)
                self.save_state(st)
            while st["phase"] == "impl":
                cur_v = st.get("impl_version", 1)
                it = st.get("impl_iteration", 0) + 1
                # pipework3: лимит — на границе после ревью (см. PLAN); версия, уже сделанная
                # исполнителем, всегда проходит ревью + синтез председателя
                diff_file = self.task_dir / f"diff_impl_v{cur_v}.patch"
                self.guard.diff_patch(st["impl_base_head"], diff_file)
                self.guard.untracked_list("", self.task_dir / f"untracked_impl_v{cur_v}.txt", arts_rel)
                self.check_budget(st, soft=True)
                n = self.next_rev_number(st, "impl")
                rev_file = self.task_dir / f"rev{n:03d}_impl.md"
                log(self.task_dir, f"IMPL ревью #{it} (rev{n:03d}): impl_report_v{cur_v} + diff")
                # pipev4 П4: убрано дубль-имя задачи в путях диффа/untracked
                # (было pipeline/<task>/<task>/diff_impl_v{N}.patch — путь не
                # существовал, ревьюеры искали файл на ощупь; srocr rev004)
                artifact_desc = (
                    f"{arts_rel}/impl_report_v{cur_v}.md и дифф {arts_rel}/diff_impl_v{cur_v}.patch "
                    f"(дифф всех изменений относительно {st['impl_base_head'][:10]}; "
                    f"новые untracked-файлы: {arts_rel}/untracked_impl_v{cur_v}.txt). "
                    f"Сверяй отчёт с фактическим диффом и кодом."
                )
                outside = self._zone_outside(st, arts_rel)
                if outside:
                    shown = ", ".join(outside[:20]) + (f" … (+{len(outside) - 20})" if len(outside) > 20 else "")
                    log(self.task_dir, f"ЗОНА: изменены файлы вне заявленной `## files_touch`: {shown}")
                    ok = list((self.cfg.get("parallel") or {}).get("shared_ok") or [])
                    hit = find_claim_conflicts(outside, collect_claim_holders(
                        self.cfg, self.repo, exclude_task=self.task), ok)
                    if hit:
                        names = ", ".join(c["task"] for c in hit)
                        log(self.task_dir, f"ЗОНА: выход за зону ПЕРЕСЕКАЕТСЯ с работающей серией ({names}) — "
                                           f"ожидай конфликт при слиянии")
                        self._notify(f"{self.task}: выход за зону записи", f"пересечение с {names}")
                    artifact_desc += (f" ВНИМАНИЕ (оркестратор): изменены файлы вне зоны записи, заявленной планом "
                                      f"в секции `## files_touch`: {shown}. Оцени это как выход за границы задачи.")
                try:
                    verdict, cost, telemetry = reviewer.review(
                        artifact_rel=artifact_desc, phase="impl",
                        task_text=self.task_text(st),
                        members=self._members("impl"),
                        rounds=self.cfg["duo"]["rounds"],
                        rev_file=rev_file, guard=self.guard, artifacts_rel=arts_rel,
                        base_head=st["impl_base_head"],   # pipev3 P1: пак от базы диффа
                        plan_rel=f"{arts_rel}/plan_v{st['plan_version']}.md",   # pipework2 P7
                    )
                except ArtifactMissingError as e:
                    self._halt_missing_artifact(st, "impl", e)
                    return
                except EmptyReviewError as e:
                    self._halt_empty_review(st, "impl", e)
                    return
                st["impl_iteration"] = it
                st["spent_usd"] = round(st.get("spent_usd", 0.0) + cost, 4)
                st.setdefault("review_telemetry", []).append(telemetry)   # pipev3 P1
                st.setdefault("verdicts", []).append({"rev": rev_file.name, "phase": "impl", "verdict": verdict})
                if verdict == "ACCEPT":
                    st["phase"] = "done"
                    self.save_state(st)
                    log(self.task_dir, f"Имплементация ПРИНЯТА (rev{n:03d}). Задача завершена. Дифф: diff_impl_v{cur_v}.patch")
                    # pipev2: коммит-группы размечены исполнителем, разбор на
                    # коммиты — только через явное подтверждение человеком.
                    log(self.task_dir, f"commit_plan: {arts_rel}/impl_report_v{cur_v}.md — человек подтверждает "
                                       f"(помощник: duo commit-by-plan --task {self.task})")
                    if self.isolation == "worktree":
                        log(self.task_dir, f"после мержа серии: git worktree remove {self.work_path}")
                    return
                self.save_state(st)
                # pipev4 П5: эскалация после N итераций без ACCEPT (если включена)
                if self._escalate_if_due(st, "impl", rev_file):
                    return
                if self._boundary_stop(st, "impl", rev_file, arts_rel):
                    return
                nv = cur_v + 1
                log(self.task_dir, f"IMPL: исполнитель дорабатывает по {rev_file.name}")
                self._exec_step(
                    st, "revise_impl", "impl", nv, rev_file.name,
                    lambda resumed: ex2.revise_impl(
                        self.task_text(st),
                        f"{arts_rel}/plan_v{st['plan_version']}.md",
                        nv, rev_file,
                        f"{arts_rel}/diff_impl_v{cur_v}.patch",   # pipework2 P5: arts_rel уже содержит <task>
                        resumed=resumed,
                    ))
                st["impl_version"] = nv
                self.save_state(st)

    # ---------- разовое ревью (пилот) ----------

    def review_once(self, artifact_rel: str, phase: str = "custom", members: list[str] | None = None) -> str:
        self.acquire_lock()
        st = self.load_state()
        self.check_budget(st)
        reviewer = DuoReviewer(self.cfg, self.t, self.task_dir, self.work_path)
        n = self.next_rev_number(st, phase)
        rev_file = self.task_dir / f"rev{n:03d}_{phase}.md"
        arts_rel = f"{self.cfg['artifacts_dir']}/{self.task}"
        log(self.task_dir, f"разовое ревью (rev{n:03d}): {artifact_rel}")
        verdict, cost, telemetry = reviewer.review(
            artifact_rel=artifact_rel, phase=phase,
            task_text=self.task_text(st),
            members=members or self._members("plan"),
            rounds=self.cfg["duo"]["rounds"],
            rev_file=rev_file, guard=self.guard, artifacts_rel=arts_rel,
            base_head=st.get("impl_base_head", ""),   # Н4 rev002_impl: база live-пака в review_once
        )
        st["spent_usd"] = round(st.get("spent_usd", 0.0) + cost, 4)
        st.setdefault("review_telemetry", []).append(telemetry)   # pipev3 P1
        st.setdefault("verdicts", []).append({"rev": rev_file.name, "phase": phase, "verdict": verdict})
        self.save_state(st)
        log(self.task_dir, f"вердикт {verdict}, отчёт: {rev_file.name}, потрачено ${cost:.2f}")
        return verdict


# ----------------------------------------------------------------------------
# commit_plan: парсер таблицы коммит-групп и покрытие (pipev2)
# ----------------------------------------------------------------------------

# грабля 11 (как и для files_touch): исполнитель нумерует и `## commit_plan` — терпим номер
# (`## 12. commit_plan`) и необязательное двоеточие; уровень остаётся строго `##` (шлюз секции
# ниже распознаёт только `## `-строки), хвостовой текст кроме `:` — по-прежнему не секция.
COMMIT_PLAN_HEADER = re.compile(r"^##[ \t]+(?:\d+[.)][ \t]*)?commit_plan[ \t]*:?[ \t]*$", re.I)


# Заголовки колонок commit_plan (грабли 8, 29.10.2026): исполнитель записал таблицу
# `# | файлы | тип | сообщение` с brace-глобом `{plan_v1,impl_report_v1}.md` и аннотацией «(33)»;
# позиционный разбор взял «тип» за сообщение, а `commit-by-plan --yes` молча сделал один
# «docs»-коммит. Теперь колонки определяются по ЗАГОЛОВКУ (позиционный разбор — запасной путь).
_CP_COL_RE = {
    "files": re.compile(r"^(?:файл\w*|files?|globs?|пути|paths?)\b"),
    "message": re.compile(r"^(?:сообщени\w*|commit message|message|msg)\b"),
    "name": re.compile(r"^(?:групп\w*|group|#|№|n|id|name|коммит)$"),
    "rationale": re.compile(r"^(?:rationale|обоснован\w*|пояснени\w*|комментари\w*|примечани\w*|тип|type|почему)$"),
}


def _cp_header_map(cells: list[str]) -> dict[str, int] | None:
    """Строка — заголовок таблицы commit_plan? {роль: индекс колонки}; нужны роли files и message."""
    found: dict[str, int] = {}
    for idx, c in enumerate(cells):
        norm = re.sub(r"\([^)]*\)", "", c.lower()).replace("`", "").replace("*", "")
        norm = re.sub(r"\s+", " ", norm).strip()
        for role, rx in _CP_COL_RE.items():
            if role not in found and rx.match(norm):
                found[role] = idx
                break
    return found if "files" in found and "message" in found else None


def _split_top_level(s: str, seps: str = ",;") -> list[str]:
    """Разбить по разделителям вне фигурных скобок (brace-глоб `{a,b}.md` — один токен)."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch in seps and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _expand_braces(s: str) -> list[str]:
    """`a/{x,y}.md` → [`a/x.md`, `a/y.md`] (вложенность — рекурсивно; `{один}` без запятой не трогаем)."""
    m = re.search(r"\{([^{}]*)\}", s)
    if not m or "," not in m.group(1):
        return [s]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(s[:m.start()] + alt.strip() + s[m.end():]))
    return out


def extract_commit_globs(cell: str) -> list[str]:
    """Ячейка «файлы» commit_plan → список glob'ов от корня репо.

    Бэктик-спаны — глобы безусловно. Текст ВНЕ спанов, если спаны в ячейке есть, — проза
    («untracked серии», «(33)»): из неё берутся только токены, похожие на путь (без пробелов и
    скобок, с `/`, `.` или glob-символом) — так `a.py, `b.py`` даёт оба файла, а «untracked» — ни одного;
    без спанов вся ячейка — список глобов. Разделители `,` `;` вне фигурных скобок; `{a,b}`
    раскрывается; хвостовая аннотация « (N)» отбрасывается; каталог `dir/` → `dir/*`; без дубликатов."""
    has_spans = "`" in cell and bool(re.search(r"`[^`]+`", cell))
    out: list[str] = []

    def _add(tok: str) -> None:
        if tok.startswith("./"):
            tok = tok[2:]
        if not tok:
            return
        if tok.endswith("/"):
            tok += "*"
        for g in _expand_braces(tok):
            if g and g not in out:
                out.append(g)

    for part in re.split(r"(`[^`]*`)", cell):
        is_span = part.startswith("`") and part.endswith("`") and len(part) >= 2
        if is_span:
            part = part[1:-1]
        for tok in _split_top_level(part):
            tok = re.sub(r"\s+\([^()]*\)\s*$", "", tok.strip().strip("`").strip()).strip()
            if has_spans and not is_span and not (
                    re.fullmatch(r"[^\s()`]+", tok) and any(c in tok for c in "/.*?[{")):
                continue
            _add(tok)
    return out


def parse_commit_plan(report_text: str) -> tuple[list[dict], list[str]]:
    """Парсер секции `## commit_plan` из impl_report (pipev2; колонки по заголовку — 29.10.2026).

    Секция = от заголовка (толерантно к регистру) до следующего `## ` или
    конца текста. Строки таблицы начинаются с `|`. Первая строка таблицы, в которой
    распознаны колонки «файлы» и «сообщение» (`_CP_COL_RE`), — заголовок: роли колонок берутся
    из неё (порядок и лишние колонки любые; нет колонки группы — имя = порядковый номер).
    Нет такого заголовка → прежний позиционный разбор: 4 колонки (группа / файлы (glob) /
    сообщение коммита / rationale), строка «| группа | …» пропускается. Разделитель `|---|`
    пропускается всегда. Строка с числом ячеек ≠ ширины таблицы или пустой ячейкой
    файлов/сообщения -> предупреждение, строка пропущена (валидные строки рядом
    парсятся). Ячейка файлов — `extract_commit_globs`. Нет секции ->
    ([], [предупреждение]). Возвращает (groups, warnings);
    group = {"name", "globs", "message", "rationale"}.
    """
    groups: list[dict] = []
    warnings: list[str] = []
    in_section = False
    col: dict[str, int] | None = None       # None — позиционный (легаси) разбор
    ncols = 4
    first_row = True
    for i, line in enumerate(report_text.splitlines()):
        if line.startswith("## "):
            if COMMIT_PLAN_HEADER.match(line):
                in_section = True
                continue
            if in_section:
                break  # следующая секция — конец commit_plan
        if not in_section or not line.lstrip().startswith("|"):
            continue
        raw_cells = [c.strip() for c in line.strip().strip("|").split("|")]
        cells = [c.strip("`").strip() for c in raw_cells]
        joined = "".join(cells)
        if not joined or set(joined) <= {"-", ":", " ", "−"}:
            continue  # разделитель |---|---|
        if first_row:
            first_row = False
            hm = _cp_header_map(cells)
            if hm is not None:
                col, ncols = hm, len(cells)
                continue  # заголовочная строка с распознанными колонками
        if cells[0].lower() == "группа":
            continue  # заголовочная строка таблицы (позиционный режим)
        if len(cells) != ncols:
            word = "колонки" if ncols in (2, 3, 4) else "колонок"
            warnings.append(f"строка {i + 1}: ожидается {ncols} {word}, найдено {len(cells)} — "
                            f"пропущена: {line.strip()[:80]}")
            continue
        if col is None:
            name, files_raw, message, rationale = raw_cells[0].strip("`").strip(), raw_cells[1], cells[2], cells[3]
        else:
            name = cells[col["name"]] if "name" in col else str(len(groups) + 1)
            files_raw, message = raw_cells[col["files"]], cells[col["message"]]
            rationale = cells[col["rationale"]] if "rationale" in col else ""
        globs = extract_commit_globs(files_raw)
        if not globs:
            warnings.append(f"строка {i + 1}: пустая ячейка файлов — пропущена: {line.strip()[:80]}")
            continue
        if not message:
            warnings.append(f"строка {i + 1}: пустое сообщение коммита — пропущена: {line.strip()[:80]}")
            continue
        groups.append({"name": name, "globs": globs, "message": message, "rationale": rationale})
    if not in_section:
        warnings.append("секция commit_plan не найдена")
    return groups, warnings


def plan_covers_path(groups: list[dict], path: str) -> bool:
    """Покрыт ли путь какой-либо группой: точное совпадение либо fnmatch-глоб
    от корня репо (напр. `src/*.py`)."""
    posix_path = path.replace("\\", "/")
    return any(
        path == glob or fnmatch.fnmatch(posix_path, glob)
        for g in groups for glob in g["globs"]
    )


def uncovered_files(groups: list[dict], changed_paths: list[str]) -> tuple[list[str], list[str]]:
    """(uncovered, overlaps): пути, не покрытые ни одной группой; пути, покрытые
    >1 группой — детектор нарушения disjoint-правила промпта (правильная
    таблица файлами не пересекается)."""
    uncovered: list[str] = []
    overlaps: list[str] = []
    for p in changed_paths:
        posix_p = p.replace("\\", "/")
        # считаем ГРУППЫ, а не глобы: два глоба одной группы на одном файле —
        # не пересечение (regular case), пересечение — файл в >1 группе.
        n = sum(1 for g in groups
                if any(p == glob or fnmatch.fnmatch(posix_p, glob) for glob in g["globs"]))
        if n == 0:
            uncovered.append(p)
        elif n > 1:
            overlaps.append(p)
    return uncovered, overlaps


def changed_status_all(repo: Path) -> list[tuple[str, str]]:
    """(XY, path) из `git status --porcelain -z --untracked-files=all`.

    Без -uall свежий untracked-каталог схлопывается в одну запись `?? dir/`
    (файлы внутри не перечислены) — это ломает coverage и pathspec. Отдельный
    вызов, НЕ глобальная правка GitGuard.status(): его поведение едят гарды
    pipev1 (enforce_readonly, untracked_list, ветки clean). R/C-записи в -z
    несут второй NUL-элемент (старый путь) — пропускаем его, парсер не падает.
    """
    rc, out = sh(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo)
    if rc != 0:
        return []
    result: list[tuple[str, str]] = []
    entries = [e for e in out.split("\0") if e]
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) >= 4:
            result.append((e[:2], e[3:]))
            if e[:1] in ("R", "C"):
                i += 1  # у R/C следующий NUL-элемент — старый путь
        i += 1
    return result


def worktree_links_of(cfg: dict) -> tuple[str, ...]:
    """R4 (duoportable): имена provisioning-симлинков из git.worktree_links конфига."""
    return tuple((cfg.get("git") or {}).get("worktree_links") or [])


def is_provisioned_symlink(repo: Path, path: str,
                           links: tuple[str, ...] | list[str] = ()) -> bool:
    """Корневой симлинк, созданный provisioning'ом worktree (имена — git.worktree_links,
    R4): не часть коммита серии (иначе `?? models`/`?? node_modules` попадали в
    uncovered commit-by-plan). links=() — прямой юнит-вызов без конфига."""
    return path in links and (Path(repo) / path).is_symlink()


def changed_files_all(repo: Path) -> list[str]:
    """Пути из changed_status_all() без XY-кодов (для coverage)."""
    return [p for _, p in changed_status_all(repo)]


def is_foreign_series_artifact(repo: Path, path: str, artifacts_rel: str) -> bool:
    """Путь — артефакт ДРУГОЙ серии: `<artifacts_dir>/<иная серия>/…`, где у иной серии на диске
    есть task.md или state.json. Такие файлы ведёт цикл их серии; для commit-by-plan это не
    «файлы вне групп» (Н5: заранее написанный task.md следующей серии блокировал --yes и
    подталкивал оператора коммитить его вручную — а закоммиченный task.md worktree-серии ломает её
    старт, Н2). Общие файлы `<artifacts_dir>/*.md` и каталоги без следов серии — НЕ чужие."""
    root, _, own = artifacts_rel.rpartition("/")
    if not root or not path.startswith(root + "/"):
        return False
    other = path[len(root) + 1:].split("/", 1)
    if len(other) < 2 or other[0] == own:
        return False
    d = Path(repo) / root / other[0]
    return (d / "task.md").exists() or (d / "state.json").exists()


def collect_commit_groups(repo: Path, task_dir: Path, version: int, artifacts_rel: str,
                          worktree_links: tuple[str, ...] | list[str] = ()) -> dict:
    """Снимок для commit-by-plan (чистая функция для юнитов).

    Читает impl_report_v{version}.md, парсит commit_plan, сверяет с
    changed_files_all(). Пути артефактов самой задачи ({artifacts_rel}/)
    исключаются из coverage (коммитятся человеком отдельно по прецеденту
    «chore(pipeline): артефакты …»), но возвращаются списком artifacts.
    worktree_links — provisioning-симлинки (R4), вне coverage. Отсутствующий
    файл отчёта -> предупреждение, без исключения.
    """
    report = task_dir / f"impl_report_v{version}.md"
    if not report.exists():
        return {
            "groups": [], "warnings": [f"файл отчёта не найден: {report} (укажи --version N)"],
            "uncovered": [], "overlaps": [], "artifacts": [], "changed": [],
            "foreign_artifacts": [],
        }
    groups, warnings = parse_commit_plan(report.read_text(encoding="utf-8"))
    changed = [p for p in changed_files_all(repo) if not is_provisioned_symlink(repo, p, worktree_links)]
    # pipev3 P1b: единая граница фильтра is_artifact_path — раньше bare
    # startswith(prefix) пропускал запись «?? pipeline/<task>» (сам каталог
    # артефактов/симлинк worktree попадал в coverage), а untracked_list
    # резал строже — семантики двух точек расходились.
    artifacts = [p for p in changed if is_artifact_path(p, artifacts_rel)]
    foreign = [p for p in changed if not is_artifact_path(p, artifacts_rel)
               and is_foreign_series_artifact(repo, p, artifacts_rel)]
    scope = [p for p in changed if not is_artifact_path(p, artifacts_rel) and p not in foreign]
    uncovered, overlaps = uncovered_files(groups, scope)
    return {
        "groups": groups, "warnings": warnings,
        "uncovered": uncovered, "overlaps": overlaps,
        "artifacts": artifacts, "changed": changed,
        "foreign_artifacts": foreign,     # Н5: чужие серии — не блокируют и не коммитятся этой командой
    }


def group_changed_files(g: dict, changed: list[str]) -> list[str]:
    """Изменённые пути, покрытые группой g (в порядке changed)."""
    return [p for p in changed if plan_covers_path([g], p)]


def artifact_only_group(g: dict, artifacts_rel: str) -> bool:
    """Все глобы группы — пути артефактов серии ({artifacts_rel}/…) по is_artifact_path.

    Такая группа по замыслу коммитится человеком отдельно («chore(pipeline):
    артефакты …») и в commit-by-plan «пустой» ошибкой не считается: раньше план с
    группой «Артефакты серии» целиком блокировал --yes (наблюдение слияния
    30.10.2026, серия devhyg). Свойство чисто глобовое, без сверки с изменёнными
    файлами; парсер групп без глобов не отдаёт (parse_commit_plan), bool() —
    защита от будущих вызовов с пустым списком (vacuous all()).
    """
    return bool(g["globs"]) and all(is_artifact_path(glob, artifacts_rel) for glob in g["globs"])


def commit_group(repo: Path, paths: list[str], message: str) -> tuple[bool, str]:
    """add+commit одной группы (pathspec-ограниченно).

    Ветка rc≠0 / «nothing to commit» (ожидаема при нарушении disjoint-правила,
    ручном коммите между снимком и прогоном, протухшей таблице) —
    (False, урезанный вывод), без исключения: команда не падает.
    """
    if not paths:
        return False, "нет путей (пустая группа)"
    rc, out = sh(["git", "add", "--", *paths], cwd=repo)
    if rc != 0:
        return False, out.strip()[:300]
    rc, out = sh(["git", "commit", "-m", message], cwd=repo)
    if rc != 0:
        return False, out.strip()[:300]
    return True, out.strip()[:300]


def needs_human_phase(st: dict, override: str | None = None) -> str | None:
    """Фаза (plan|impl), из которой серия ушла в needs_human, либо None.

    pipework2 P9: эскалация пишет явное поле escalated_phase; для старых state
    (без поля) — разбор stop_reason, как раньше. Лимит итераций плана/импла
    тоже распознаётся (раньше set-verdict молча ничего не делал — P2).
    override (--phase) побеждает всё: для редких причин (budget/solo/ручная правка).
    """
    if override in ("plan", "impl"):
        return override
    h = st.get("halt")
    if isinstance(h, dict) and h.get("phase") in ("plan", "impl"):   # pipework3: машинная запись halt
        return h["phase"]
    reason = st.get("stop_reason") or ""
    if "эскалация" in reason:
        ep = st.get("escalated_phase")
        if ep in ("plan", "impl"):
            return ep
        return "plan" if "plan" in reason else "impl"
    if "лимит итераций плана" in reason or "фазы plan не найден" in reason:
        return "plan"
    if "лимит итераций имплементации" in reason or "фазы impl не найден" in reason:
        return "impl"
    return None


def cmd_set_verdict(cfg: dict, args) -> None:
    """Ручной вердикт. pipework2 P3: под .lock — не гоняется с живым оркестратором
    (раньше read-modify-write state без блокировки затирал его запись)."""
    p = Pipeline(cfg, args.task)
    p.acquire_lock()
    try:
        _set_verdict_locked(p, args)
    finally:
        p.release_lock()


def last_review_name(task_dir: Path, phase: str) -> str | None:
    """Имя последнего rev{NNN}_<phase>.md (по номеру NNN) или None."""
    found = sorted(task_dir.glob(f"rev*_{phase}.md"))
    return found[-1].name if found else None


def enter_rework(p: Pipeline, st: dict, phase: str, rev_name: str, source: str,
                 note: str = "", keep_session: bool = False) -> None:
    """Перевести серию в «ожидает доработки» (pipework2): фаза остаётся plan/impl,
    маркер st["rework"] говорит run(), что ПЕРВЫМ шагом исполнитель переписывает
    артефакт по ревью rev_name, и только затем идёт ревью новой версии.

    iter_base фиксирует счётчики итераций на момент вмешательства: лимит и
    эскалация считают итерации от него (ручная доработка не «съедает» лимит);
    verdicts-база не даёт прежнему ACCEPT гасить новую эскалацию.
    """
    p.archive_escalation()
    st["phase"] = phase
    st["rework"] = {"phase": phase, "rev": rev_name, "source": source,
                    "note": note, "at": datetime.now().isoformat(timespec="seconds")}
    st["iter_base"] = {"plan": st.get("plan_iteration", 0), "impl": st.get("impl_iteration", 0),
                       "verdicts": len(st.get("verdicts", []))}
    st.pop("escalated_phase", None)
    st.pop("halt", None)
    st.pop("inflight", None)           # ручная доработка заменяет прерванный шаг (сессия может быть новой)
    if not keep_session:
        st.pop("executor_session", None)   # свежая сессия: план/ревью/дифф исполнитель получает явно
    st["stop_reason"] = f"ручная доработка ({phase}) по {rev_name}" + (f": {note}" if note else "")


def _set_verdict_locked(p: Pipeline, args) -> None:
    st = p.load_state()
    st.setdefault("verdicts", []).append({"rev": "manual", "phase": "manual", "verdict": args.verdict, "note": args.note})
    phase_now = st.get("phase")
    nh = None
    if phase_now == "needs_human":
        # pipev4 П5 / pipework2 P2: ручной вердикт выводит из needs_human по ЛЮБОЙ
        # опознанной причине (эскалация, лимит итераций, артефакт не найден)
        nh = needs_human_phase(st, getattr(args, "phase", None))
        if nh is None:
            sys.exit("не удалось определить фазу needs_human по stop_reason "
                     f"({st.get('stop_reason')!r}) — укажи --phase plan|impl")
    if nh is not None and args.verdict == "REVISE":
        rev_name = last_review_name(p.task_dir, nh)
        if not rev_name:
            sys.exit(f"нет ни одного rev*_{nh}.md — нечем руководствоваться при доработке; "
                     f"зарегистрируй своё ревью: `rework --task {p.task} --phase {nh} --review FILE`")
        enter_rework(p, st, nh, rev_name, "set-verdict", args.note)
        p.save_state(st)
        print(f"записан ручной вердикт REVISE: {args.note}\n"
              f"фаза {nh}: доработка по {rev_name}; запусти: run --task {p.task}")
        return
    if args.verdict == "ACCEPT" and phase_now == "plan":
        # Контейнер разовых ревью (`review --task`): плана нет, есть только rev*_custom.
        # ACCEPT такого контейнера закрывает его (done), а НЕ ведёт в impl_pending —
        # иначе сторож запускает исполнителя на несуществующий план (инцидент gt3dec 10.10.2026).
        is_container = not any(p.task_dir.glob("plan_v*.md")) and st.get("rev_counter_custom", 0) > 0
        if is_container:
            st["phase"] = "done"
            st["stop_reason"] = f"review-container closed by set-verdict ACCEPT: {args.note}"
        else:
            st["phase"] = "impl_pending"
    if nh is not None:
        p.archive_escalation()
        st["stop_reason"] = f"ручной вердикт после остановки ({nh}): {args.note}"
        st.pop("escalated_phase", None)
        st.pop("halt", None)
        st.pop("inflight", None)
        st.pop("rework", None)
        if args.verdict == "ACCEPT":
            st["phase"] = "impl_pending" if nh == "plan" else "done"
    p.save_state(st)
    print(f"записан ручной вердикт {args.verdict}: {args.note}")
    if args.verdict == "REVISE" and nh is None:
        print("(фаза не needs_human — вердикт только записан; чтобы отправить артефакт на доработку "
              f"по своему ревью: `rework --task {p.task} --phase plan|impl --review FILE`)")


def cmd_rework(cfg: dict, args) -> None:
    """Отправить артефакт на доработку по внешнему (экспертному) ревью.

    pipework2: заменяет ручную правку state.json (phase/plan_version/executor_session/
    счётчики) — путь, на котором терялись состояния. Под .lock: при живом
    оркестраторе отказ. `--run` отпускает блокировку и запускает run detached.
    """
    p = Pipeline(cfg, args.task)
    p.acquire_lock()
    try:
        st = p.load_state()
        phase = args.phase
        if st.get("phase") == "done" and not args.force:
            sys.exit(f"серия '{args.task}' в phase=done — rework отклонён (если нужно: --force)")
        if phase == "plan" and st.get("plan_version", 0) < 1:
            sys.exit("plan_version=0: плана ещё нет, дорабатывать нечего (контейнер ревью или серия не стартовала)")
        if phase == "impl" and st.get("impl_version", 0) < 1:
            sys.exit("impl_version=0: имплементации ещё нет, дорабатывать нечего")
        src = Path(args.review)
        if not src.is_file():
            sys.exit(f"файл ревью не найден: {src}")
        text = src.read_text(encoding="utf-8")
        verdict = DuoReviewer._parse_verdict(text)
        if verdict == "ACCEPT":
            sys.exit(f"в {src.name} последняя строка VERDICT: ACCEPT — для rework нужно ревью с замечаниями (REVISE)")
        if verdict is None:
            text = text.rstrip() + "\n\n*(дописано командой rework: внешнее ревью зарегистрировано как REVISE)*\nVERDICT: REVISE\n"
        n = p.next_rev_number(st, phase)
        rev_name = f"rev{n:03d}_{phase}.md"
        (p.task_dir / rev_name).write_text(text, encoding="utf-8")
        st.setdefault("verdicts", []).append({"rev": rev_name, "phase": phase, "verdict": "REVISE",
                                             "note": f"rework: внешнее ревью {src.name}"})
        enter_rework(p, st, phase, rev_name, "rework", f"внешнее ревью {src.name}",
                     keep_session=args.keep_session)
        if phase == "plan" and st.get("impl_version", 0) > 0:
            print(f"ВНИМАНИЕ: impl_version={st['impl_version']} уже есть — после принятия нового плана "
                  f"имплементация продолжится с ревью impl_v{st['impl_version']}; нужна новая — повторно rework --phase impl")
        p.save_state(st)
    finally:
        p.release_lock()
    print(f"rework: {args.task} → phase={phase}, ревью зарегистрировано как {rev_name}; "
          f"первым шагом run исполнитель перепишет артефакт")
    if args.run:
        run_detached(cfg, args.task, argv=["--config", str(args.config), "run", "--task", args.task])
    else:
        print(f"запусти: duo run --task {args.task}")


def cmd_hold(cfg: dict, args, hold: bool) -> None:
    """pipework2 P11: hold/release — флаг hold_impl (запрет автопродолжения impl_pending
    сторожем) без ручной правки state.json. .lock НЕ берётся — команда нужна именно
    при живом оркестраторе; безопасность — state_write_lock + merge-on-write в save_state.
    Остальные поля state и heartbeat не трогаются.
    pipegates: DEPRECATED — сторож больше не автопродолжает impl_pending, флаг ничего
    не выключает; поведение сохранено для совместимости (не удалять)."""
    print("ВНИМАНИЕ: DEPRECATED: hold/release устарели — сторож больше не автопродолжает "
          "impl_pending, флаг ничего не выключает; поведение сохранено", file=sys.stderr)
    p = Pipeline(cfg, args.task)
    with state_write_lock(p.task_dir):
        st = p.load_state()
        if hold:
            st["hold_impl"] = True
        else:
            st.pop("hold_impl", None)
        save_json(p.state_file, st)
    phase = st.get("phase")
    print(f"{args.task}: hold_impl={'true' if hold else 'снят'} (phase={phase})")
    if not hold and phase == "impl_pending":
        print("phase=impl_pending: сторож продолжит имплементацию автоматически в ближайший тик (~60 с)")
    if hold and phase in ("done", "needs_human"):
        print(f"(phase={phase}: hold_impl влияет только на автопродолжение impl_pending)")


def staged_paths(work: Path) -> list[str]:
    """Что уже лежит в индексе дерева (git diff --cached): commit-by-plan такой staging не трогает."""
    rc, out = sh(["git", "diff", "--cached", "--name-only"], cwd=work)
    return [x for x in out.splitlines() if x.strip()] if rc == 0 else []


def print_staged_warning(work: Path, staged: list[str]) -> None:
    print("ВНИМАНИЕ: индекс уже содержит застейдженные файлы (чужой staging) — не вмешиваюсь:")
    print("\n".join(staged))
    # Н9 полевой проверки: самый частый источник — собственный `git add -A` оператора перед гейтами
    # (для гейтов индекс не нужен: `duo gates --dirty`). Файлы не пострадают — сбрасывается только индекс.
    print(f"Если это случайный `git add` — сними индекс (правки в файлах сохранятся): git -C {work} reset")


def commit_yes_problems(snap: dict, groups: list[dict], scope: list[str], arts_rel: str,
                        warnings: list[str]) -> list[str]:
    """Аномалии, при которых `--yes` откажет до первого коммита (all-or-nothing). Один источник
    правды для preflight `--yes` и для предсказания в `--dry-run` (Н12: dry-run раньше молчал о
    части отказов и «обещал» зелёное)."""
    # devhyg: группа из одних артефактных глобов — не «пустая» ошибка (артефакты
    # серии коммитятся отдельно); обычная пустая — отказ.
    empties = [g["name"] for g in groups
               if not artifact_only_group(g, arts_rel) and not group_changed_files(g, scope)]
    problems: list[str] = []
    if warnings:
        problems.append(f"предупреждения парсера таблицы: {len(warnings)}")
    if snap["uncovered"]:
        problems.append(f"файлов вне групп (uncovered): {len(snap['uncovered'])}")
    if snap["overlaps"]:
        problems.append(f"файлов в нескольких группах (overlap): {len(snap['overlaps'])}")
    if empties:
        problems.append(f"групп без изменённых файлов: {len(empties)} ({', '.join(empties)})")
    return problems


def cmd_commit_by_plan(cfg: dict, args) -> None:
    """Интерактивный прогон коммит-групп из ## commit_plan (pipev2).

    ПОМОЩНИК человека, не автопилот: коммит группы — только после явного y;
    n/Enter/EOF — пропуск (EOF = «нет» — безопасный дефолт для
    неинтерактивных сред). Код возврата: 0 — чисто; 1 — есть хвосты
    (uncovered, пропущенные/несостоявшиеся группы, warnings парсера).
    .lock НЕ берём: команда не мутирует state.json, сосуществует с живым
    оркестратором (прецедент cmd_status).
    """
    p = Pipeline(cfg, args.task)  # только разрешение путей и state.json
    st = p.load_state()
    version = args.version or st.get("impl_version", 0)
    arts_rel = f"{cfg['artifacts_dir']}/{args.task}"
    yes_all = bool(getattr(args, "yes", False))  # pipev4: --yes = подтверждение всех групп без интерактива
    # pipev3 P2: снимок и коммиты — по дереву серии (worktree), а не p.repo
    snap = collect_commit_groups(p.work_path, p.task_dir, version, arts_rel,
                                 worktree_links_of(cfg))
    groups, warnings = snap["groups"], snap["warnings"]
    print(f"commit_plan: {arts_rel}/impl_report_v{version}.md, групп: {len(groups)}")
    for w in warnings:
        print(f"ВНИМАНИЕ: {w}")
    if snap["uncovered"]:
        print("ВНИМАНИЕ: файлы вне групп (uncovered):")
        for u in snap["uncovered"]:
            print(f"  {u}")
    if snap["overlaps"]:
        print("ВНИМАНИЕ: файлы более чем в одной группе (нарушение disjoint-правила):")
        for o in snap["overlaps"]:
            print(f"  {o}")
    if snap["artifacts"]:
        print(f"артефакты задачи (в coverage не входят, коммитятся отдельно): {snap['artifacts']}")
    if snap["foreign_artifacts"]:
        print(f"артефакты ДРУГИХ серий (не блокируют, эта команда их не коммитит — их ведёт цикл своей "
              f"серии; worktree-серии их коммитить до merge нельзя): {snap['foreign_artifacts']}")
    if not groups:
        print("нечего прогонять: групп нет (см. предупреждения выше)")
        sys.exit(1)

    # pipev3 P1b (Н2 rev002_impl): та же граница is_artifact_path, что и в
    # collect_commit_groups — прежний bare slash-префикс пропускал запись
    # «?? pipeline/<task>» (симлинк артефактов worktree) в scope cmd-пути,
    # и широкий глоб мог закоммитить его как blob.
    scope = [x for x in snap["changed"]
             if not is_artifact_path(x, arts_rel) and x not in snap["foreign_artifacts"]]
    untracked = {path for xy, path in changed_status_all(p.work_path) if xy == "??"}

    if args.dry_run:
        for g in groups:
            files = group_changed_files(g, scope)
            print(f"\n[{g['name']}] {g['message']}")
            print(f"  globs: {', '.join(g['globs'])}")
            for f in files:
                print(f"    {f}{'  (untracked)' if f in untracked else ''}")
            if not files:
                print("    (артефакты серии — коммитятся отдельно)" if artifact_only_group(g, arts_rel)
                      else "    (нет изменённых файлов под глобами группы)")
        staged = staged_paths(p.work_path)
        if staged:
            print()
            print_staged_warning(p.work_path, staged)
        predicted = commit_yes_problems(snap, groups, scope, arts_rel, warnings)
        if staged:
            predicted.insert(0, f"индекс уже содержит застейдженные файлы: {len(staged)}")
        print("\n--dry-run: коммитов нет")
        print("предсказание для `--yes`: ОТКАЖЕТ — " + "; ".join(predicted) if predicted
              else "предсказание для `--yes`: пройдёт (аномалий плана и чужого staging нет)")
        sys.exit(1 if predicted else 0)

    staged = staged_paths(p.work_path)
    if staged:
        print_staged_warning(p.work_path, staged)
        sys.exit(1)

    # preflight «всё или ничего» для --yes (грабли 8, 29.10.2026): без интерактива некому остановить
    # кривую таблицу — серия получила один «docs»-коммит вместо шести. Любая аномалия плана →
    # отказ ДО первого коммита; осознанный частичный прогон — `--allow-partial`.
    if yes_all and not getattr(args, "allow_partial", False):
        problems = commit_yes_problems(snap, groups, scope, arts_rel, warnings)
        if problems:
            print("ОТКАЗ --yes: " + "; ".join(problems) + ". Ни один коммит не сделан.")
            print("Исправь `## commit_plan` в impl_report (или закоммить вручную), либо повтори с "
                  "`--allow-partial` (закоммитятся только валидные группы), либо без `--yes` (интерактив).")
            sys.exit(1)

    print("\nИнтерактив: y — закоммитить группу, n/Enter/EOF — пропустить, Ctrl-C — прервать всё.")
    committed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    empty: list[str] = []
    artifact_groups: list[str] = []
    for g in groups:
        files = group_changed_files(g, scope)
        print(f"\n=== [{g['name']}] {g['message']}")
        if g["rationale"]:
            print(f"    rationale: {g['rationale']}")
        if artifact_only_group(g, arts_rel):
            print("    артефакты серии — коммитятся отдельно (пропущена)")
            artifact_groups.append(g["name"])
            continue
        if not files:
            print("    нет изменённых файлов под глобами группы — пустой коммит не создаём, пропуск")
            empty.append(g["name"])
            continue
        fresh = [f for f in files if f in untracked]
        if fresh:
            print("    новые (untracked) файлы группы:")
            for f in fresh:
                print(f"      + {f}")
        rc, stat = sh(["git", "diff", "--stat", "--", *files], cwd=p.work_path)
        if rc == 0 and stat.strip():
            print(stat.rstrip())
        try:
            ans = "y" if yes_all else input(f"коммитить группу «{g['name']}» ({len(files)} файл(ов))? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "д", "да"):
            skipped.append(g["name"])
            print("    пропущено")
            continue
        ok, out = commit_group(p.work_path, files, g["message"])
        if ok:
            committed.append(g["name"])
            first_line = out.splitlines()[0] if out else ""
            print(f"    OK: закоммичено {first_line}")
        else:
            failed.append(g["name"])
            print(f"    commit НЕ удался: {out}")

    def _named(label: str, names: list[str]) -> str:
        joined = f" ({', '.join(names)})" if names else ""
        return f"{label}: {len(names)}{joined}"

    print("\n--- сводка ---")
    print(_named("закоммичено", committed))
    print(_named("пропущено (n/EOF)", skipped))
    print(_named("commit не удался (rc≠0)", failed))
    print(_named("пустых групп (нет файлов)", empty))
    for name in artifact_groups:
        print(f"группа {name}: артефакты серии — коммитятся отдельно (пропущена)")
    print(f"не покрыто (uncovered): {len(snap['uncovered'])}")
    print(f"пересечения (overlap): {len(snap['overlaps'])}")
    if snap["artifacts"]:
        print(f"артефакты задачи (коммитятся отдельно): {snap['artifacts']}")
    sys.exit(1 if (skipped or failed or empty or snap["uncovered"] or snap["overlaps"] or warnings) else 0)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _detached_argv(cfg: dict, argv: list[str] | None = None) -> list[str]:
    """R1 (duoportable): argv ребёнка с ЯВНЫМ --repo (глобальный флаг — ДО подкоманды):
    detached-процесс не зависит от cwd родителя и DUO_REPO в его окружении."""
    base = list(sys.argv[1:] if argv is None else argv)
    if any(a == "--repo" for a in base):
        return base
    return ["--repo", str(Path(cfg["repo"]).resolve()), *base]


def run_detached(cfg: dict, task: str, argv: list[str] | None = None) -> None:
    """pipev1 П1 (Д-11): ре-экзек себя в новой сессии — оркестратор больше
    не живёт в foreground-группе терминала (abort терминала его не убивает).

    Родитель печатает PID/логи и выходит; .lock берёт ребёнок (как при --fg),
    повторный run на живой задаче отбивается flock'ом как раньше. Умершего при
    старте ребёнка (.lock занят / ошибка конфига) родитель диагностирует сам —
    хвост orchestrator_bg.log в терминал, не трейсбек.
    """
    task_dir = Path(cfg["repo"]) / cfg["artifacts_dir"] / task
    if not task_dir.exists():
        # без mkdir: фантомный pipeline/<typo>/ — паттерн бродячих каталогов,
        # против которого заточен П2; каталоги создаёт только cmd_init.
        sys.exit(f"Задача не найдена: {task_dir}. Сначала `init --task {task}`.")
    bg_log = task_dir / "orchestrator_bg.log"
    # argv=None — ре-экзек текущей командной строки `run ...`; rework --run передаёт свой
    # (в sys.argv тогда лежит `rework ...`, а не `run`)
    cmd = [sys.executable, str(Path(__file__).resolve()), *_detached_argv(cfg, argv), "--fg"]
    with open(bg_log, "ab") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    time.sleep(DETACHED_CONFIRM_SEC)
    if proc.poll() is not None:
        tail = bg_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
        print("оркестратор умер при старте; хвост orchestrator_bg.log:")
        for ln in tail:
            print(f"  {ln}")
        sys.exit(proc.returncode or 1)
    print(f"PID {proc.pid} | detached (новая сессия, жив на момент +{DETACHED_CONFIRM_SEC:.0f}с)")
    print(f"лог: tail -f {task_dir / 'orchestrator.log'} | консоль: tail -f {bg_log}")


TASK_TEMPLATE_MARK = "Опиши задачу здесь."


def task_template(task: str) -> str:
    return f"# Задача: {task}\n\n{TASK_TEMPLATE_MARK}\n"


def task_md_is_placeholder(text: str) -> bool:
    """task.md — пустой файл либо нетронутый шаблон `init` (заголовок + «Опиши задачу здесь.»).
    Заголовок без шаблонной строки (`# задача demo`) шаблоном НЕ считается: это осознанно краткое
    задание. Исполнитель, запущенный на шаблоне, «спланирует пустоту» и сожжёт бюджет."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    body = [ln for ln in lines if not ln.startswith("#")]
    return bool(body) and all(ln == TASK_TEMPLATE_MARK for ln in body)


def cmd_init(cfg: dict, args) -> None:
    repo = Path(cfg["repo"]).resolve()
    task_dir = repo / cfg["artifacts_dir"] / args.task
    stf = task_dir / "state.json"
    pins = resolve_mode_flags(args)
    # Н2: worktree-серия + каталог артефактов серии, закоммиченный в HEAD, — отказ ДО создания
    # чего-либо (раньше ловилось на `run` уже после создания worktree и ветки)
    try:
        old_iso = load_json(stf).get("isolation") if stf.exists() else None
    except (OSError, ValueError, AttributeError):
        old_iso = None          # битый state.json: init его и раньше не читал — не падаем
    iso = pins.get("isolation") or old_iso or (cfg.get("git") or {}).get("isolation", "shared")
    if iso == "worktree":
        tracked = artifacts_tracked_in_ref(repo, "HEAD", f"{cfg['artifacts_dir']}/{args.task}")
        if tracked:
            sys.exit(worktree_artifacts_conflict_msg(args.task, cfg["artifacts_dir"], tracked))
    if not stf.exists() and python_junk_unignored(repo):
        print("ВНИМАНИЕ: Python-проект, а .gitignore не покрывает `__pycache__/` — .pyc от прогона тестов "
              "исполнителем вылезут как «вне files_touch» (предупреждение ревьюеру) и как uncovered в "
              "`commit-by-plan --yes` (блокирует). Добавь `__pycache__/` в .gitignore и закоммить ДО "
              "серии (для worktree-серии — обязательно до `run`: база берётся из HEAD); `duo setup` "
              "делает это сам.", file=sys.stderr)
    # Н1: следы серии есть, а task.md пропал (rm -rf, ручная уборка) — шаблон ниже затрёт смысл
    leftovers = ([c.name for c in task_dir.iterdir() if c.name not in (".lock", "transcripts")]
                 if task_dir.is_dir() else [])
    had_task = (task_dir / "task.md").exists()
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "transcripts").mkdir(exist_ok=True)
    tf = task_dir / "task.md"
    if not tf.exists():
        if args.task_file:
            tf.write_text(Path(args.task_file).read_text(encoding="utf-8"), encoding="utf-8")
        else:
            tf.write_text(task_template(args.task), encoding="utf-8")
            if not had_task and leftovers:
                print(f"ВНИМАНИЕ: task.md серии {args.task} отсутствовал, хотя в каталоге есть другие файлы "
                      f"({', '.join(sorted(leftovers)[:5])}). Записан ШАБЛОН — если задание было, "
                      f"восстанови его из git/бэкапа ДО run (`run` на шаблоне откажет). Чтобы сбросить "
                      f"серию, сохранив task.md, есть `duo reset --task {args.task}`.", file=sys.stderr)
    if not stf.exists():
        new_state = {
            "task": args.task,
            "phase": "plan",
            "plan_version": 0,
            "plan_iteration": 0,
            "impl_version": 0,
            "impl_iteration": 0,
            "spent_usd": 0.0,
            "meter": "per_call_v2",   # pipev3 P0: серия стартует с чиненым метром
            "artifacts_rel": f"{cfg['artifacts_dir']}/{args.task}",
            "created": datetime.now().isoformat(timespec="seconds"),
            "verdicts": [],
        }
        # init --mode/--worktree пинуют режим/изоляцию серии при создании: дальше обычный
        # `run --task X` (и --continue, сторож) идёт в них без повторения флагов
        if pins.get("mode") in ("duo", "pair"):
            new_state["mode"] = pins["mode"]
        if pins.get("isolation"):
            new_state["isolation"] = pins["isolation"]
        save_json(stf, new_state)
    elif pins:
        print(f"state.json уже есть — флаги режима/изоляции не применены: передай их в `run --task {args.task}`")
    print(f"OK: {task_dir}\nОтредактируй task.md, затем: duo run --task {args.task}")


def resolve_mode_flags(args) -> dict:
    """Флаги режима/изоляции CLI → {mode?, isolation?}. `--solo` — псевдоним `--mode solo`;
    противоречие (`--solo --mode pair`) — отказ."""
    out: dict = {}
    mode = getattr(args, "mode", None)
    if getattr(args, "solo", False):
        if mode not in (None, "solo"):
            sys.exit(f"--solo несовместим с --mode {mode}")
        mode = "solo"
    if mode:
        out["mode"] = mode
    if getattr(args, "isolation", None):
        out["isolation"] = args.isolation
    return out


def sup_cfg(cfg: dict) -> dict:
    """Секция supervisor с дефолтами (cfg из тестов/старых config.json может её не содержать)."""
    return {**DEFAULTS["supervisor"], **(cfg.get("supervisor") or {})}


def lock_held(task_dir: Path) -> bool:
    """Держит ли кто-то `.lock` задачи (живой оркестратор). Надёжнее ps/pgrep: flock снимается
    ядром при ЛЮБОЙ смерти процесса (kill -9, OOM, перезагрузка), в отличие от pid-файлов.
    Проба — LOCK_SH|NB на отдельном дескрипторе; держателя не беспокоит (окно гонки с
    стартующим оркестратором — микросекунды)."""
    lock = Path(task_dir) / ".lock"
    try:
        fd = os.open(str(lock), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def classify_task(task_dir: Path, now: float | None = None, stale_sec: float = 300.0,
                  dead_max_age_sec: float = 86400.0) -> dict:
    """Состояние серии для сторожей (чистая функция: lock + state.json; ничего не меняет).

    kind: running — жив (lock занят); done; idle — стоит на пороге (impl_pending, нет запуска,
    эскалация/needs_human без halt); halted — needs_human с машинной записью halt;
    dead — phase активна, lock свободен, state не обновлялся ≥ stale_sec (умер без halt:
    kill -9/OOM/перезагрузка) и не старше dead_max_age_sec (давние «трупы» и ни разу не
    запускавшиеся серии — idle, не dead); starting — активная фаза, lock свободен, state
    свежий (старт или смена процесса); unknown — нет/битый state.json.
    """
    now = time.time() if now is None else now
    task_dir = Path(task_dir)
    try:
        st = load_json(task_dir / "state.json")
    except (OSError, json.JSONDecodeError):
        return {"task": task_dir.name, "kind": "unknown", "phase": "?", "alive": False}
    phase = st.get("phase", "?")
    alive = lock_held(task_dir)
    age = now - float(st.get("state_updated_at") or 0.0)
    h = st.get("halt") if isinstance(st.get("halt"), dict) else None
    if phase == "done":
        kind = "done"
    elif alive:
        kind = "running"
    elif phase == "needs_human":
        kind = "halted" if h else "idle"
    elif phase in ("plan", "impl", "solo"):
        if not st.get("state_updated_at"):
            kind = "idle"                       # серия ещё ни разу не запускалась
        elif age >= dead_max_age_sec:
            kind = "idle"                       # давний труп: за него отвечает человек, не надзор
        else:
            kind = "dead" if age >= stale_sec else "starting"
    else:
        kind = "idle"
    return {"task": st.get("task", task_dir.name), "kind": kind, "phase": phase, "alive": alive,
            "age_sec": round(age), "updated_at": st.get("state_updated_at") or 0.0, "halt": h, "stop_reason": st.get("stop_reason", ""),
            "spent_usd": st.get("spent_usd", 0.0), "inflight": st.get("inflight"),
            "plan_version": st.get("plan_version", 0), "impl_version": st.get("impl_version", 0),
            "plan_iteration": st.get("plan_iteration", 0), "impl_iteration": st.get("impl_iteration", 0),
            "rev_counters": [st.get("rev_counter_plan", 0), st.get("rev_counter_impl", 0)]}


def collect_statuses(arts_root: Path, cfg: dict | None = None) -> list[dict]:
    """pipev1 П4: сводные строки по всем <arts_root>/*/state.json.

    Битые JSON и каталоги без state.json пропускаются (строка в stderr, не
    падаем); сортировка по mtime state.json по убыванию (свежие сверху).
    pipework3: + liveness (alive/kind) по .lock.
    pipefix: при заданном cfg — + колонка «зона» (done_zone_note: несвитая done-серия
    держит зону записи); без cfg поведение прежнее, git не вызывается.
    """
    rows: list[dict] = []
    repo = Path(cfg["repo"]).resolve() if cfg else None
    for st_file in arts_root.glob("*/state.json"):
        try:
            st = load_json(st_file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"ПРОПУСК {st_file}: битый state.json ({e})", file=sys.stderr)
            continue
        info = classify_task(st_file.parent)
        task = st.get("task", st_file.parent.name)
        rows.append({
            "task": task,
            "phase": st.get("phase", "?"),
            "plan_version": st.get("plan_version", 0),
            "plan_iteration": st.get("plan_iteration", 0),
            "impl_version": st.get("impl_version", 0),
            "impl_iteration": st.get("impl_iteration", 0),
            "spent_usd": st.get("spent_usd", 0.0),
            "verdicts": len(st.get("verdicts", [])),
            "mtime": st_file.stat().st_mtime,
            "kind": info["kind"],
            "mode": st.get("mode") or "—",
            "isolation": st.get("isolation") or "—",
            "zone": done_zone_note(cfg, repo, task, st) if cfg and repo else "",
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


_ISO_SHORT = {"shared": "sh", "worktree": "wt"}


def format_status_table(rows: list[dict]) -> str:
    lines = [f"{'задача':<14} {'фаза':<14} {'план':>7} {'импла':>7} {'$':>8} {'ревью':>6} {'процесс':>9} {'режим':>8} зона"]
    for r in rows:
        plan = f"v{r['plan_version']}/i{r['plan_iteration']}"
        impl = f"v{r['impl_version']}/i{r['impl_iteration']}"
        iso = _ISO_SHORT.get(r.get("isolation", "—"), "—")
        mode = f"{r.get('mode', '—')}/{iso}"
        lines.append(f"{r['task']:<14} {r['phase']:<14} {plan:>7} {impl:>7} "
                     f"{r['spent_usd']:>8.2f} {r['verdicts']:>6} {r.get('kind', '?'):>9} {mode:>8} {r.get('zone') or ''}")
    lines.append(f"всего: {len(rows)} задач, ${sum(r['spent_usd'] for r in rows):.2f}")
    return "\n".join(lines)


def accepted_plan_summary(st: dict) -> str | None:
    """Сводка «что принято» для момента решения о запуске имплементации (Н11 полевой проверки):
    версия плана, ревью-приёмка, эрраты, зона записи, потрачено. Только уже имеющиеся поля state —
    ничего не вычисляет заново (число эррат — `errata_applied.items`, его пишет оркестратор при дописывании).
    None — плана ещё нет."""
    pv = int(st.get("plan_version", 0) or 0)
    if pv <= 0:
        return None
    rev = next((v.get("rev") for v in reversed(st.get("verdicts") or [])
                if isinstance(v, dict) and v.get("phase") == "plan" and v.get("verdict") == "ACCEPT"), None)
    parts = [f"план v{pv} принят" + (f" ревью {rev}" if rev else " (вердикт ACCEPT в state не найден)")]
    er = st.get("errata_applied")
    if isinstance(er, dict) and er.get("plan") == f"plan_v{pv}.md":
        parts.append(f"эрраты: {er.get('items', '?')} п. из {er.get('rev', '?')} дописаны в план")
    else:
        parts.append("эрраты оркестратором не дописывались")
    claim = st.get("claim")
    if isinstance(claim, dict):
        if claim.get("declared"):
            paths = [str(x) for x in (claim.get("paths") or [])]
            parts.append(f"зона записи: {len(paths)} путей ({', '.join(paths[:6])}{' …' if len(paths) > 6 else ''})")
        else:
            parts.append("зона записи НЕ заявлена планом")
    parts.append(f"потрачено ${float(st.get('spent_usd', 0) or 0):.2f}")
    return "; ".join(parts)


def cmd_status(cfg: dict, args) -> None:
    if getattr(args, "all", False):
        # precedence: --all выигрывает, --task игнорируется
        arts_root = Path(cfg["repo"]).resolve() / cfg["artifacts_dir"]
        print(format_status_table(collect_statuses(arts_root, cfg)))
        return
    if not args.task:
        sys.exit("укажи --task NAME или --all")
    p = Pipeline(cfg, args.task)
    st = p.load_state()
    extra = float(st.get("budget_extra_usd", 0.0) or 0.0)
    limit = cfg["limits"]["budget_usd"] + extra
    claim = st.get("claim")
    claim_released_now = claim_released(cfg, p.repo, args.task, st) if isinstance(claim, dict) else None
    info = classify_task(p.task_dir, stale_sec=sup_cfg(cfg)["stale_sec"],
                         dead_max_age_sec=sup_cfg(cfg)["dead_max_age_sec"])
    summ = accepted_plan_summary(st)
    tel = [t for t in (st.get("review_telemetry") or []) if isinstance(t, dict)]
    stubs = sum(int(t.get("stub_retries", 0) or 0) for t in tel)
    tretry = sum(int(t.get("transport_retries", 0) or 0) for t in tel)
    empty = sum(int(t.get("empty_retries", 0) or 0) for t in tel)
    wt = p.isolation == "worktree"
    if getattr(args, "json", False):
        # машинный вывод для скриптов оператора: РОВНО один JSON-объект, без прозы (полевой прогон
        # 17.11.2026: `json.load` обычного вывода падал на «Extra data» — JSON состояния + текст)
        print(json.dumps({
            "task": args.task, "phase": st.get("phase"), "mode": st.get("mode"), "isolation": p.isolation,
            "worktree": str(p.work_path) if wt else None,
            "branch": f"{cfg['git']['branch_prefix']}{args.task}" if wt else None,
            "budget": {"spent_usd": round(float(st.get("spent_usd", 0) or 0), 4),
                       "limit_usd": limit, "extra_usd": extra},
            "claim": ({"released": claim_released_now, "paths": len(claim.get("paths") or []),
                       "declared": bool(claim.get("declared"))} if isinstance(claim, dict) else None),
            "process": {"kind": info["kind"], "alive": bool(info["alive"]), "age_sec": info.get("age_sec")},
            "halt": info.get("halt") or None, "stop_reason": info.get("stop_reason") or None,
            "accepted_plan": summ if st.get("phase") in ("impl_pending", "impl", "done") else None,
            "review_transport": {"stub_retries": stubs, "transport_retries": tretry,
                                 "empty_retries": empty, "reviews": len(tel)},
            "state": st,
        }, ensure_ascii=False, indent=2, default=str))
        return
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print(f"бюджет: ${st.get('spent_usd', 0):.2f} / ${limit:g}"
          + (f" (в т.ч. добавка серии ${extra:g})" if extra else ""))
    wt_note = (f", дерево {p.work_path}, ветка {cfg['git']['branch_prefix']}{args.task}" if wt else "")
    print(f"режим: {st.get('mode') or '— (не записан; будет duo)'}, изоляция: {p.isolation}{wt_note}")
    if isinstance(claim, dict):
        state_txt = "отпущена" if claim_released_now else "держит"
        print(f"зона записи: {state_txt}, заявлено путей: {len(claim.get('paths') or [])}"
              f"{'' if claim.get('declared') else ' (зона не заявлена планом)'}")
    print(f"процесс: {info['kind']} (lock {'занят' if info['alive'] else 'свободен'}, "
          f"state обновлён {info.get('age_sec', 0)} с назад)")
    if info.get("halt"):
        h = info["halt"]
        print(f"остановка: kind={h.get('kind')} phase={h.get('phase')} resumable={h.get('resumable')} — {info['stop_reason']}")
    if summ and st.get("phase") in ("impl_pending", "impl", "done"):
        print(f"принято: {summ}")
        if st.get("phase") == "impl_pending":
            print(f"решение за человеком: прочти plan_v{st.get('plan_version')}.md и эрраты, затем "
                  f"`duo run --task {args.task} --continue`")
    if stubs or tretry or empty:
        print(f"транспорт ревью (деградации, на вердикт обычно не влияют): ответов-заглушек {stubs}, "
              f"транспортных повторов {tretry}, пустых ревью {empty} — по {len(tel)} ревью")


# коды выхода `wait` (для скриптов помощника и `notify_on_output`-сторожей)
WAIT_EXIT = {"done": 0, "impl_pending": 0, "halted": 10, "dead": 11, "idle": 12, "timeout": 13}


def cmd_wait(cfg: dict, args) -> int:
    """Блокирующее ожидание «интересного момента» серии (pipework3): печатает строки прогресса
    при смене состояния и ФИНАЛЬНУЮ строку `WAIT-RESULT: <статус> …`, выходит с кодом
    WAIT_EXIT. Заменяет самодельные bash-сторожа помощника: источник истины — state.json +
    flock `.lock` самого оркестратора, а не ps/grep и не тайминги.

    Статусы: done (приёмка impl), impl_pending (план принят, пауза), halted (needs_human с
    причиной — читай stop_reason), dead (оркестратор умер без halt), idle (needs_human без
    машинной записи/нет запуска), timeout (--timeout)."""
    task_dir = Path(cfg["repo"]).resolve() / cfg["artifacts_dir"] / args.task
    if not task_dir.is_dir():
        sys.exit(f"Задача не найдена: {task_dir}")
    stale = sup_cfg(cfg)["stale_sec"]
    t_end = time.time() + args.timeout if args.timeout else None
    last_fp = None
    while True:
        info = classify_task(task_dir, stale_sec=stale, dead_max_age_sec=sup_cfg(cfg)["dead_max_age_sec"])
        fp = (info["phase"], info["kind"], info.get("plan_version"), info.get("impl_version"),
              info.get("plan_iteration"), info.get("impl_iteration"), tuple(info.get("rev_counters", [])),
              (info.get("inflight") or {}).get("step"))
        if fp != last_fp and not args.quiet:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {info['task']}: phase={info['phase']} {info['kind']} "
                  f"plan v{info.get('plan_version')}/i{info.get('plan_iteration')} "
                  f"impl v{info.get('impl_version')}/i{info.get('impl_iteration')} "
                  f"${info.get('spent_usd', 0):.2f}", flush=True)
            last_fp = fp
        status = None
        if info["kind"] == "done":
            status = "done"
        elif info["kind"] == "halted":
            status = "halted"
        elif info["kind"] == "dead":
            status = "dead"
        elif info["kind"] == "idle" and info["phase"] == "impl_pending":
            status = "impl_pending"
        elif info["kind"] == "idle" and not args.keep_waiting_idle:
            status = "idle"
        if status:
            h = info.get("halt") or {}
            print(f"WAIT-RESULT: {status} task={info['task']} phase={info['phase']} "
                  f"halt={h.get('kind', '-')} resumable={h.get('resumable', '-')} "
                  f"spent=${info.get('spent_usd', 0):.2f} :: {str(info.get('stop_reason') or '')[:300]}", flush=True)
            return WAIT_EXIT[status]
        if t_end and time.time() >= t_end:
            print(f"WAIT-RESULT: timeout task={info['task']} phase={info['phase']} kind={info['kind']}", flush=True)
            return WAIT_EXIT["timeout"]
        time.sleep(args.poll)


def _supervisor_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "duo-supervisor" / "state.json"


def _supervisor_load(path: Path) -> dict:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def supervise_tick(cfg: dict, *, auto_resume: bool | None = None, now: float | None = None,
                   state_path: Path | None = None, spawn=None, notify=None, dry_run: bool = False) -> list[dict]:
    """Один тик надзора (stateless между запусками; для systemd-timer / cron). Состояние
    дедупликации алертов и счётчики авто-возобновлений — вне репозитория (XDG_STATE_HOME),
    в репо ничего не пишется (GitGuard вычищал бы лишнее под pipeline/).

    R8 (duoportable): записи состояния и уведомления — на КЛЮЧЕ <абс-корень>::<task>
    (одноимённые серии разных проектов не глушат алерты друг друга), заголовок
    уведомления — `duo <проект>/<task>`; spawn ребёнка идёт с явным --repo.

    dead / halted с разрешённым kind → алерт (один раз на событие) и, при auto_resume,
    `run --task T --resume` detached; потолок max_resumes за resume_window_sec на задачу.
    Остановки limit/budget/artifact_missing/errata — только алерт: решение за человеком."""
    sup = sup_cfg(cfg)
    now = time.time() if now is None else now
    auto = sup["auto_resume"] if auto_resume is None else auto_resume
    spath = state_path or _supervisor_state_path()
    sstate = _supervisor_load(spath)
    repo = Path(cfg["repo"]).resolve()
    arts_root = repo / cfg["artifacts_dir"]
    project = repo.name
    spawn = spawn or (lambda task: run_detached(cfg, task, argv=["--config", str(cfg.get("_config_path", DEFAULT_CONFIG)),
                                                                "run", "--task", task, "--resume"]))
    notify = notify or (lambda title, body: subprocess.run(
        ["notify-send", "-u", "critical", "-t", "0", title, body], timeout=5, capture_output=True))
    actions: list[dict] = []
    baseline = not sstate                       # первый тик: фон — о прошлых done/impl_pending молчим
    for st_file in sorted(arts_root.glob("*/state.json")):
        info = classify_task(st_file.parent, now=now, stale_sec=sup["stale_sec"],
                             dead_max_age_sec=sup["dead_max_age_sec"])
        task = info["task"]
        skey = f"{repo}::{task}"
        rec = sstate.setdefault(skey, {})
        if info["kind"] == "done" or (info["kind"] == "idle" and info["phase"] == "impl_pending"):
            # событие «серия завершена / план принят»: один раз, только свежее (≤ 1 ч)
            ev = f"{info['phase']}:{int(info.get('updated_at', 0))}"
            if rec.get("noticed") != ev:
                rec["noticed"] = ev
                if not baseline and info.get("age_sec", 1e9) <= 3600:
                    label = ("ГОТОВО (impl принят)" if info["phase"] == "done"
                             else "план принят — ждёт запуска имплементации")
                    act = {"task": task, "project": project, "kind": info["phase"], "action": "notice",
                           "reason": label}
                    if (cfg.get("notify") or {}).get("desktop") and not dry_run:
                        with contextlib.suppress(Exception):
                            notify(f"duo {project}/{task}: {label}", f"потрачено ${info.get('spent_usd', 0):.2f}")
                    actions.append(act)
            continue
        if info["kind"] not in ("dead", "halted"):
            rec.pop("alerted", None)
            continue
        event_key = f"{info['kind']}:{(info.get('halt') or {}).get('at', '')}:{info['phase']}:{int(info.get('spent_usd', 0) * 100)}"
        act = {"task": task, "project": project, "kind": info["kind"], "action": "none",
               "reason": info.get("stop_reason", "")}
        if rec.get("alerted") != event_key:
            rec["alerted"] = event_key
            act["action"] = "alert"
            if ((cfg.get("notify") or {}).get("desktop") and not dry_run
                    and not (info.get("halt") or {}).get("notified")):      # оркестратор уже уведомил сам
                with contextlib.suppress(Exception):
                    notify(f"duo {project}/{task}: {info['kind']}",
                           (info.get("stop_reason") or "оркестратор умер без halt")[:200])
        resumable = (info["kind"] == "dead" or
                     ((info.get("halt") or {}).get("resumable") and (info["halt"].get("kind") in sup["auto_resume_kinds"])))
        if auto and resumable:
            recent = [t for t in rec.get("resumes", []) if now - t < sup["resume_window_sec"]]
            if len(recent) >= sup["max_resumes"]:
                act["action"] = "resume-limit"
            elif dry_run:
                act["action"] = "would-resume"
            else:
                recent.append(now)
                rec["resumes"] = recent
                try:
                    spawn(task)
                    act["action"] = "resumed"
                except SystemExit as e:
                    act["action"] = f"resume-failed: {e}"
        rec["resumes"] = [t for t in rec.get("resumes", []) if now - t < sup["resume_window_sec"]]
        actions.append(act)
    if not dry_run:
        spath.parent.mkdir(parents=True, exist_ok=True)
        save_json(spath, sstate)
    return actions


def cmd_supervise(cfg: dict, args, *, single: bool) -> None:
    """Один тик надзора (systemd-timer) либо `--loop --interval N` (без systemd: nohup/tmux).
    R8 (duoportable): single=True — ровно один проект (явный --repo/DUO_REPO либо legacy-ключ
    repo конфига); иначе — все проекты реестра (projects.json). Пустой реестр — понятное
    сообщение и rc=0 (таймер не должен падать)."""
    if single:
        targets = [(Path(cfg["repo"]).resolve().name, cfg)]
    else:
        projects = load_projects()
        if not projects:
            print(f"SUPERVISE: реестр проектов пуст ({_projects_registry_path()}) — нечего надзирать. "
                  "Подключение: `duo setup` в каталоге проекта либо `duo projects --add PATH`")
            return
        targets = [(p.name, cfg_for_repo(Path(args.config), p)) for p in projects]
    while True:
        acts: list[dict] = []
        for project, cfg_i in targets:
            acts.extend(supervise_tick(cfg_i, auto_resume=True if args.resume else None,
                                       dry_run=args.dry_run))
        for a in acts:
            print(f"SUPERVISE: [{a.get('project', '?')}] {a['task']} {a['kind']} → {a['action']} :: "
                  f"{str(a['reason'])[:200]}", flush=True)
        if not acts and not args.loop:
            print("SUPERVISE: все серии в порядке (нет dead/halted)")
        if not args.loop:
            return
        time.sleep(args.interval)


# ----------------------------------------------------------------------------
# R8 (duoportable): реестр проектов для мультипроектного надзора
# ----------------------------------------------------------------------------

def _projects_registry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "duo-pipeline" / "projects.json"


def load_projects() -> list[Path]:
    """Проекты реестра (по одному на строку, абсолютные пути). Нет файла/пусто — [];
    битый JSON — предупреждение в stderr и [] (мультипроектный надзор деградирует до
    «пусто», а не валится и не затирает файл)."""
    path = _projects_registry_path()
    try:
        data = load_json(path)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        print(f"SUPERVISE: реестр проектов {path} не читается ({e}) — работаю с пустым", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        print(f"SUPERVISE: реестр проектов {path}: ожидался JSON-объект — работаю с пустым", file=sys.stderr)
        return []
    out: list[Path] = []
    for p in data.get("projects") or []:
        try:
            rp = Path(str(p)).expanduser().resolve()
        except (TypeError, ValueError):
            continue
        if rp not in out:
            out.append(rp)
    return out


def save_projects(projects: list[Path]) -> None:
    path = _projects_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, {"projects": [str(p) for p in projects]})


def cmd_projects(cfg: dict, args) -> None:
    """R8: реестр проектов. Без флагов — таблица (проект / каталог на месте / .duo /
    активных серий); --add/--remove/--prune меняют реестр (идемпотентны)."""
    reg = _projects_registry_path()
    projects = load_projects()
    if args.prune:
        kept = [p for p in projects if p.is_dir()]
        gone = [p for p in projects if not p.is_dir()]
        if gone:
            save_projects(kept)
            for p in gone:
                print(f"убран (каталога нет): {p}")
        else:
            print("нечего убирать: все каталоги на месте")
        projects = kept
    if args.add:
        p = Path(args.add).expanduser().resolve()
        if not p.is_dir():
            sys.exit(f"--add: каталог не найден: {p}")
        if p in projects:
            print(f"уже в реестре: {p}")
        else:
            projects.append(p)
            save_projects(projects)
            print(f"добавлен: {p}")
    if args.remove:
        p = Path(args.remove).expanduser().resolve()
        if p in projects:
            projects.remove(p)
            save_projects(projects)
            print(f"удалён: {p}")
        else:
            print(f"не найден в реестре: {p}")
    print(f"реестр: {reg}")
    if not projects:
        print("пусто; подключение: `duo setup` в каталоге проекта либо `duo projects --add PATH`")
        return
    print(f"{'проект':<44} {'каталог':>7} {'.duo':>5} {'серий':>6}")
    for p in projects:
        arts = p / cfg.get("artifacts_dir", "pipeline")
        n_series = len(list(arts.glob("*/state.json"))) if p.is_dir() else 0
        print(f"{str(p):<44} {'есть' if p.is_dir() else 'НЕТ':>7} "
              f"{'да' if (p / '.duo' / 'config.json').exists() else '—':>5} {n_series:>6}")


def cmd_errata(cfg: dict, args) -> None:
    """Статус/ручное применение переноса эррат председателя в принятый план (pipework3)."""
    p = Pipeline(cfg, args.task)
    st = p.load_state()
    last = next((v for v in reversed(st.get("verdicts", [])) if v.get("phase") == "plan"), None)
    if not last or last.get("verdict") != "ACCEPT" or not str(last.get("rev", "")).startswith("rev"):
        sys.exit("последний вердикт плана — не ACCEPT председателя: переносить нечего")
    rev_path = p.task_dir / last["rev"]
    plan_path = p.task_dir / f"plan_v{st.get('plan_version', 1)}.md"
    if args.apply:
        p.acquire_lock()
        try:
            status, info = apply_accept_errata(rev_path, plan_path)
        except ErrataError as e:
            p.release_lock()
            sys.exit(f"ЭРРАТЫ: не удалось: {e}")
        p.release_lock()
        print(f"ЭРРАТЫ: {status} {info or ''}")
        return
    body = extract_chairman_errata(rev_path.read_text(encoding="utf-8"))
    if body is None:
        print(f"в {rev_path.name} блока «Эрраты к плану» нет")
        return
    sigs = _errata_signatures(body)
    m = ERRATA_HEADING.search(plan_path.read_text(encoding="utf-8"))
    section = _errata_norm(plan_path.read_text(encoding="utf-8")[m.start():]) if m else ""
    got = sum(1 for sg in sigs if sg in section)
    verdict = "перенесено" if got == len(sigs) else "НЕ перенесено" if got == 0 else "ЧАСТИЧНО"
    print(f"{rev_path.name}: пунктов {len(sigs)}, найдено в {plan_path.name}: {got} ({verdict})"
          + ("" if got == len(sigs) else f"; перенести: errata --task {args.task} --apply"))


def cmd_merge(cfg: dict, args) -> int:
    """Влить принятую worktree-серию (ветка duo/<task>) в ТЕКУЩУЮ ветку основного дерева.

    Порядок безопасности: (1) только worktree-серия в phase=done (--force — иначе), worktree чист
    (коммиты — `commit-by-plan`); (2) сухая проверка `git merge-tree --write-tree` — конфликты
    видны ДО касания дерева; конфликты разрешаются в worktree серии (`git merge <ветка-цели>`),
    не в основном дереве; (3) `git merge --no-ff`; провал — `merge --abort`, дерево как было;
    (4) зона записи отпускается (параллельные серии больше не блокируются), worktree
    удаляется (симлинки provisioning снимаются заранее — `--force` не нужен). Ветка остаётся.
    Код выхода: 0 — слито / нечего сливать / dry-run без конфликтов; 1 — конфликты или отказ."""
    p = Pipeline(cfg, args.task)
    if p.isolation != "worktree":
        sys.exit(f"{args.task}: серия в общем дереве (isolation={p.isolation}) — ветка и правки уже в основном "
                 f"дереве, сливать нечего (`merge` — для серий `--worktree`)")
    p.acquire_lock()
    try:
        return _merge_locked(cfg, args, p)
    finally:
        p.release_lock()


def _merge_locked(cfg: dict, args, p: Pipeline) -> int:
    repo, task = p.repo, args.task
    st = p.load_state()
    arts_rel = f"{cfg['artifacts_dir']}/{task}"
    branch = f"{cfg['git']['branch_prefix']}{task}"
    wt = p.work_path
    if st.get("phase") != "done" and not args.force:
        print(f"ОТКАЗ: серия в phase={st.get('phase')} — сливать можно только принятую (done). "
              f"`--force` — на свой риск")
        return 1
    rc, _ = sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=repo)
    if rc != 0:
        print(f"ОТКАЗ: ветки {branch} нет — сливать нечего")
        return 1
    if (wt / ".git").exists():
        dirty = [x for x in changed_files_all(wt)
                 if not is_provisioned_symlink(wt, x, worktree_links_of(cfg))
                 and not is_artifact_path(x, arts_rel)]
        if dirty:
            print(f"ОТКАЗ: в worktree {wt} есть незакоммиченные изменения ({len(dirty)}): {dirty[:8]}")
            print(f"Закоммить их (`commit-by-plan --task {task}`), затем повтори merge")
            return 1
    # грабли 9 (29.10.2026): `git merge` отказывает при ЛЮБОМ застейдженном-незакоммиченном в индексе
    # основного дерева (даже в файлах, которых слияние не касается; в боевой серии отказ пришёл со списком
    # чужих patch-файлов). Видно заранее — и в --dry-run, который иначе «проходил» и обманывал.
    rc, staged = sh(["git", "diff", "--cached", "--name-only"], cwd=repo)
    if rc == 0 and staged.strip():
        files = staged.split("\n")
        files = [f for f in files if f.strip()]
        print(f"ОТКАЗ: в индексе основного дерева {repo} застейджено, но не закоммичено файлов: {len(files)} "
              f"({files[:6]}). `git merge` с непустым индексом не стартует")
        print("Закоммить их (`git commit -m …`, если это твоя работа) либо сними со стейджа: "
              "`git restore --staged -- <пути>`; затем повтори merge")
        return 1
    _, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    target = cur.strip() or "HEAD"
    rc, cnt = sh(["git", "rev-list", "--count", f"HEAD..{branch}"], cwd=repo)
    n_commits = int(cnt.strip() or 0) if rc == 0 else 0
    if n_commits == 0:
        print(f"нечего сливать: {branch} уже целиком в {target} (новых коммитов нет)")
        if not args.dry_run:
            _finish_merge(cfg, p, st, target, merged=False, keep=args.keep_worktree)
        return 0
    _, stat = sh(["git", "diff", "--stat", f"HEAD...{branch}"], cwd=repo)
    print(f"{branch} → {target}: +{n_commits} коммит(ов)\n{stat.rstrip()}")
    rc, out = sh(["git", "merge-tree", "--write-tree", "--name-only", "HEAD", branch], cwd=repo, timeout=300)
    if rc != 0:
        lines = out.splitlines()
        if rc == 1:
            files = []
            for ln in lines[1:]:
                if not ln.strip():
                    break
                files.append(ln.strip())
            print(f"КОНФЛИКТЫ при слиянии ({len(files)}): {files[:15]}")
            print(f"Разреши их в worktree серии, не в основном дереве:\n"
                  f"  git -C {wt} merge {target}   # разрулить конфликты, закоммитить\n"
                  f"затем повтори `merge --task {task}`")
        else:
            print(f"merge-tree не смог проверить слияние (rc={rc}): {out.strip()[:300]}")
        return 1
    if args.dry_run:
        print("dry-run: конфликтов нет, слияние пройдёт чисто")
        print(f"гейты в чистой копии: запустите `gates --task {task}`")   # pipegates: подсказка, без автозапуска
        return 0
    if not args.yes:
        try:
            ans = input(f"слить {branch} в {target} (--no-ff)? [y/N] ")
        except (EOFError, OSError):
            # грабля 4 (30.10.2026): без tty (закрытый//dev/null stdin) приглашение молча
            # превращалось в «отменено» — отвечаем подсказкой; явный отказ «n» — как раньше
            print("нет tty для подтверждения — повтори с `--yes` (или запусти в интерактивном терминале)")
            return 1
        if ans.strip().lower() not in ("y", "yes", "д", "да"):
            print("отменено")
            return 1
    rc, out = sh(["git", "merge", "--no-ff", "-m", f"merge: {branch} ({task})", branch], cwd=repo, timeout=300)
    if rc != 0:
        sh(["git", "merge", "--abort"], cwd=repo)       # нет MERGE_HEAD (отказ до касания) — безвредно
        print(f"git merge не удался, дерево возвращено как было:\n{out.strip()[:600]}")
        return 1
    print(f"слито: {branch} → {target}")
    _finish_merge(cfg, p, st, target, merged=True, keep=args.keep_worktree)
    return 0


def _finish_merge(cfg: dict, p: Pipeline, st: dict, target: str, merged: bool, keep: bool) -> None:
    """После слияния (или «уже слито»): отпустить зону, записать след в state, убрать worktree."""
    now = datetime.now().isoformat(timespec="seconds")
    claim = st.get("claim")
    if isinstance(claim, dict):
        claim["released"] = True
        claim["released_at"] = now
    _, head = sh(["git", "rev-parse", "HEAD"], cwd=p.repo)
    st["merged"] = {"into": target, "commit": head.strip(), "at": now, "new_commits": merged}
    p.save_state(st)
    log(p.task_dir, f"MERGE: {cfg['git']['branch_prefix']}{p.task} → {target} ({head.strip()[:10]}); зона записи отпущена")
    wt = p.work_path
    if keep or not (wt / ".git").exists():
        return
    # симлинки, созданные provisioning'ом/артефактами, снимаем сами: worktree тогда «чист» для git
    for link in [wt / cfg["artifacts_dir"] / p.task, *(wt / n for n in worktree_links_of(cfg))]:
        if link.is_symlink():
            link.unlink()
    # грабля 1: подтяжка базы могла принести в ветку серии артефакты (симлинк скрывал их
    # удалениями в статусе) — вернём из индекса, иначе worktree «грязный» и remove откажет.
    # rc!=0 (пути нет в индексе) безвреден: обычные серии артефактов в ветке не несут.
    sh(["git", "checkout", "--", f"{cfg['artifacts_dir']}/{p.task}"], cwd=wt)
    rc, out = sh(["git", "worktree", "remove", str(wt)], cwd=p.repo)
    if rc == 0:
        print(f"worktree удалён: {wt} (ветка {cfg['git']['branch_prefix']}{p.task} сохранена)")
    else:
        print(f"worktree НЕ удалён ({out.strip()[:200]}); проверь руками: `git worktree remove {wt}`")


def cmd_claims(cfg: dict, args) -> None:
    """Зоны записи работающих worktree-серий (без записи в state, кроме --drop)."""
    repo = Path(cfg["repo"]).resolve()
    if args.drop:
        if not args.task:
            sys.exit("--drop требует --task")
        p = Pipeline(cfg, args.task)
        if lock_held(p.task_dir):
            sys.exit(f"{args.task}: серия запущена — сначала останови её (иначе работающая серия потеряет защиту)")
        with state_write_lock(p.task_dir):
            st = p.load_state()
            claim = st.get("claim")
            if not isinstance(claim, dict) or claim.get("released"):
                print(f"{args.task}: зона и так не занята")
                return
            claim["released"] = True
            claim["released_at"] = datetime.now().isoformat(timespec="seconds")
            claim["released_by"] = "claims --drop"
            save_json(p.state_file, st)
        log(p.task_dir, "ЗОНА: отпущена вручную (claims --drop)")
        print(f"{args.task}: зона записи отпущена")
        return
    holders = collect_claim_holders(cfg, repo)
    if args.task:
        holders = [h for h in holders if h["task"] == args.task]
    if not holders:
        print("зон записи не занято" if not args.task else f"{args.task}: зону не держит")
        return
    ok = list((cfg.get("parallel") or {}).get("shared_ok") or [])
    for h in holders:
        zone = list(dict.fromkeys(h["declared"] + h["actual"]))
        decl = f"{len(h['declared'])} заявлено" if h["declared_known"] else "зона не заявлена"
        print(f"{h['task']} (phase={h['phase']}): {decl}, {len(h['actual'])} изменено фактически")
        for z in zone[:12]:
            print(f"    {z}")
        if len(zone) > 12:
            print(f"    … ещё {len(zone) - 12}")
    if not args.task and len(holders) > 1:
        shown = False
        for i, a in enumerate(holders):
            for b in holders[i + 1:]:
                pairs = zone_conflicts(list(dict.fromkeys(a["declared"] + a["actual"])),
                                       list(dict.fromkeys(b["declared"] + b["actual"])), ok)
                if pairs:
                    shown = True
                    print(f"ПЕРЕСЕЧЕНИЕ {a['task']} ⇄ {b['task']}: "
                          + "; ".join(f"{m} ⇄ {t}" for m, t in pairs[:5]))
        if not shown:
            print("пересечений между держателями нет")


# ----------------------------------------------------------------------------
# pipegates: гейты поставки в ЧИСТОЙ копии (CLAUDE.md п.6 «гейты поставки = гейты
# CI»): git archive REF + опциональные заглушки тяжёлых модулей (каталог gates.stubs
# проекта, пакеты падают при импорте — как CI-раннер без torch/onnx/…), все шаги — в
# копии, в рабочее дерево проекта команда не пишет, state.json только читает,
# .lock не берёт. R3 (duoportable): НАБОР ШАГОВ И ИХ АРГУМЕНТЫ — из конфига
# (gates.steps; плейсхолдеры {py}/{pybin}/{cpus}); примеры — docs/PROJECT-SETUP.md.
# ----------------------------------------------------------------------------


def gates_dirty_changes(tree: Path, artifacts_dir: str) -> tuple[list[str], list[str]]:
    """Незакоммиченное состояние дерева для `gates --dirty`/предупреждения: (upsert, delete).
    Без переименований (--no-renames: R превращается в D+A — копия получает и новый файл, и без
    старого), без симлинков (provisioning), без всего каталога артефактов `<artifacts_dir>/`
    (это не код под гейтами; в нём сотни чужих файлов серий)."""
    rc, out = sh(["git", "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"], cwd=tree)
    if rc != 0:
        return [], []
    up: list[str] = []
    dele: list[str] = []
    for e in (x for x in out.split("\0") if x):
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        if is_artifact_path(path, artifacts_dir):
            continue
        fp = tree / path
        if fp.is_symlink():
            continue
        if fp.is_file():
            up.append(path)
        elif not fp.exists() and "D" in xy:
            dele.append(path)
    return up, dele


def gates_expand_extra(repo: Path, path: str) -> list[str]:
    """--extra: файл → [файл]; каталог → его файлы под git (tracked + untracked, не игнорируемые;
    так `dashboard/` с 37 файлами не нужно перечислять, а node_modules/кэши не уезжают в копию)."""
    full = repo / path
    if full.is_file():
        return [path]
    if not full.is_dir():
        sys.exit(f"--extra {path!r}: нет файла/каталога в {repo}")
    rc, out = sh(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", path], cwd=repo)
    files = [f for f in out.split("\0") if f and (repo / f).is_file() and not (repo / f).is_symlink()]
    if rc != 0 or not files:
        sys.exit(f"--extra {path!r}: в каталоге нет файлов под git (tracked/untracked, не игнорируемых)")
    return files


def _default_cpus() -> str:
    """Все ядра, доступные процессу, строкой для `taskset -c` ("0-31" или "0-3,8-11").
    Умолчание вместо зашитого списка: фиксированный набор ядер невалиден на машине поменьше."""
    try:
        ids = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        ids = list(range(os.cpu_count() or 1))
    runs: list[tuple[int, int]] = []
    start = prev = ids[0]
    for i in ids[1:]:
        if i == prev + 1:
            prev = i
            continue
        runs.append((start, prev))
        start = prev = i
    runs.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _gates_resolve(cfg: dict) -> dict:
    """Параметры гейтов из конфига (R3): шаги с раскрытыми плейсхолдерами, интерпретатор,
    каталог заглушек. gates.python: None/отсутствует → sys.executable; gates.cpus: None →
    $DUO_GATES_CPUS (машинная настройка), иначе все доступные ядра (_default_cpus); gates.stubs: None/"" → заглушек нет, абсолютный путь →
    он же, относительный → от корня проекта (cfg["repo"])."""
    gates = cfg.get("gates") or {}
    # абсолютный путь БЕЗ раскрытия симлинков: python venv — симлинк на системный интерпретатор,
    # resolve() увёл бы {pybin} в чужой каталог (нет ruff/mypy, пропали бы пакеты venv)
    py = Path(os.path.abspath(str(gates.get("python") or sys.executable)))
    pybin = py.parent
    cpus = str(gates.get("cpus") or os.environ.get("DUO_GATES_CPUS") or _default_cpus())
    stubs = gates.get("stubs")
    stubs_dir: Path | None
    if stubs is None or str(stubs) == "":
        stubs_dir = None
    else:
        sp = Path(str(stubs))
        stubs_dir = sp if sp.is_absolute() else Path(cfg.get("repo") or os.getcwd()).resolve() / sp
    steps = []
    for raw in gates.get("steps") or []:
        argv = [str(a).replace("{py}", str(py)).replace("{pybin}", str(pybin))
                .replace("{cpus}", cpus) for a in (raw.get("argv") or [])]
        steps.append({"name": str(raw.get("name", "")), "argv": argv,
                      "pp": bool(raw.get("pp", False)),
                      "timeout": int(raw.get("timeout", 900)),
                      "summary": str(raw.get("summary") or "last_line")})
    return {"py": py, "pybin": pybin, "cpus": cpus, "stubs_dir": stubs_dir, "steps": steps}


def gates_steps(cfg: dict) -> list[dict]:
    """Разрешённые шаги гейтов (R3) — чистая функция для юнитов/регрессий."""
    return _gates_resolve(cfg)["steps"]


def _gates_default_runner(argv: list[str], *, cwd: Path, env: dict, timeout: float | None) -> tuple[int, str]:
    """Реальный раннер: subprocess без shell. Таймаут → rc=124 + пометка TIMEOUT;
    отсутствующий бин (rc=127) — обычный красный шаг, а не traceback сквозь main."""
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT после {timeout}s: {str(e)[:300]}"
    except FileNotFoundError:
        return 127, f"BIN MISSING: {argv[0]}"


def _gates_summary_extract(mode: str, out: str) -> str:
    """Хвост для строки summary (R3): last_line — последняя строка вывода ДОСЛОВНО
    (итог pytest «N passed»); contracts — строки контрактов lint-imports, как в
    ручном прогоне. Пустой вывод → «(no output)» (эррата Е1)."""
    lines = out.strip().splitlines() or ["(no output)"]
    if mode == "contracts":
        kept = [ln for ln in lines if re.search(r"Contracts:|broken|kept", ln)]
        return " ".join(kept[-2:]) if kept else lines[-1]
    return lines[-1]


def cmd_gates(cfg: dict, args, runner: Callable | None = None) -> int:
    """Гейты поставки в чистой копии (git archive REF). Безопасность: не пишет в
    рабочее дерево проекта (только pipeline/<task>/logs/gates_*), state.json только
    читает, .lock НЕ берётся (сосуществует с живым оркестратором); временную копию
    удаляет (кроме --keep). Шаги — gates.steps конфига (R3); пустой набор — rc=2
    с подсказкой. Код возврата: 0 — все шаги зелёные; 1 — есть красный; 2 — шаги
    не заданы."""
    repo = Path(cfg["repo"]).resolve()
    task_dir = repo / cfg["artifacts_dir"] / args.task
    if not task_dir.exists():
        sys.exit(f"Задача не найдена: {task_dir}. Сначала `init --task {args.task}`.")
    g = _gates_resolve(cfg)
    steps, stubs_dir = g["steps"], g["stubs_dir"]
    if not steps:
        print("gates.steps не заданы в .duo/config.json (пример — docs/PROJECT-SETUP.md)")
        return 2
    cfg_py = (cfg.get("gates") or {}).get("python")
    if cfg_py and not os.access(g["py"], os.X_OK):
        sys.exit(f"gates.python={g['py']} не найден или не исполняем (другой стенд? путь интерпретатора "
                 f"непереносим между машинами). Уберите ключ из .duo/config.json: тогда гейты идут тем "
                 f"интерпретатором, что запустил оркестратор (сейчас {sys.executable}; на машине "
                 f"его выбирает лаунчер bin/duo или переменная DUO_PYTHON).")
    if stubs_dir is not None and not stubs_dir.is_dir():
        sys.exit(f"нет каталога заглушек {stubs_dir} (gates.stubs в конфиге)")
    branch = f"{cfg['git']['branch_prefix']}{args.task}"
    try:
        st = load_json(task_dir / "state.json")
    except (OSError, json.JSONDecodeError):
        st = {}
    ref = args.ref or (branch if st.get("isolation") == "worktree" else "HEAD")
    rc, _ = sh(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo)
    if rc != 0:
        sys.exit(f"REF {ref!r} не найден в {repo}" + (" (серия ещё не коммитила — попробуй `--ref HEAD`)"
                 if ref == branch else ""))
    names = [s["name"] for s in steps]
    if args.only:
        unknown = [s for s in args.only.split(",") if s not in names]
        if unknown:
            sys.exit(f"неизвестные шаги --only: {unknown}; допустимы {names}")
    only = [s for s in names if not args.only or s in args.only.split(",")]
    if args.name and not re.fullmatch(r"[A-Za-z0-9._-]+", args.name):
        sys.exit(f"--name {args.name!r}: допустимы буквы/цифры/._-")
    extras: list[str] = []
    for p in args.extra or []:
        if Path(p).is_absolute():
            # абсолютный путь ВНУТРИ корня проекта — нормализуем к относительному (Н8: бриф говорил
            # «каталог проекта», оператор передал абсолютный); вне корня — отказ
            try:
                p = str(Path(os.path.normpath(p)).relative_to(repo))
            except ValueError:
                sys.exit(f"--extra {p!r}: абсолютный путь вне корня проекта {repo}")
        if ".." in Path(p).parts:
            sys.exit(f"--extra {p!r}: пути без '..', от корня {repo} (абсолютные — только внутри корня)")
        for f in gates_expand_extra(repo, p):
            if f not in extras:
                extras.append(f)

    # Н4: что архивируем vs что реально лежит в дереве серии. Для shared это основное дерево, для
    # worktree-серии (ref=ветка) — её worktree. Если ref совпадает с HEAD того дерева, а в нём есть
    # незакоммиченные правки — копия их НЕ содержит: гейты проверят бейзлайн (в поле: 25 ошибок ruff
    # «старого кода» при зелёной серии). Не молчим; `--dirty` накладывает правки поверх.
    rc_r, ref_sha = sh(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=repo)
    src_tree = repo
    wt_dir = worktree_path(cfg, repo, args.task)
    if ref == branch and (wt_dir / ".git").exists():
        src_tree = wt_dir
    rc_h, tree_head = sh(["git", "rev-parse", "HEAD"], cwd=src_tree)
    d_up, d_del = gates_dirty_changes(src_tree, cfg["artifacts_dir"])
    # файлы, которые `--extra` и так накладывает на копию, к «незакоммиченному, чего копия не содержит»
    # не относятся (иначе предупреждение держится после ровно той правки, которую оно же советует).
    # `--extra` берёт файлы из ОСНОВНОГО дерева: у worktree-серии это не её дерево — там не засчитываем
    covered = set(extras) if src_tree == repo else set()
    d_left = [f for f in d_up if f not in covered]
    dirty_n = len(d_left) + len(d_del)
    overlay = bool(getattr(args, "dirty", False))
    stale_warn = ""
    if dirty_n and not overlay and rc_r == 0 and rc_h == 0 and ref_sha.strip() == tree_head.strip():
        sample = ", ".join((d_left + d_del)[:5]) + (" …" if dirty_n > 5 else "")
        stale_warn = (f"ВНИМАНИЕ: ref={ref} НЕ содержит {dirty_n} незакоммиченных изменений дерева {src_tree} "
                      f"({sample}) — гейты проверят БЕЙЗЛАЙН, а не код серии. Добавь --dirty (HEAD + "
                      f"незакоммиченное), либо --extra <файлы|каталоги>, либо сначала закоммить "
                      f"(commit-by-plan).")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = "gates_" + (f"{args.name}_" if args.name else "") + stamp
    log_dir = task_dir / "logs" / base
    n = 2
    while log_dir.exists():
        log_dir = task_dir / "logs" / f"{base}_{n}"
        n += 1
    copy_dir = Path(tempfile.mkdtemp(prefix=f"duo_gates_{args.task}_"))
    stubs_txt = str(stubs_dir) if stubs_dir is not None else "(off)"
    print(f"gates: task={args.task} ref={ref} copy={copy_dir} stubs={stubs_txt} py={g['py']} "
          f"cpus={g['cpus']} logs={log_dir}")
    if overlay:
        print(f"--dirty: поверх {ref} накладываются незакоммиченные правки {src_tree}: "
              f"изменено/новых {len(d_up)}, удалено {len(d_del)}")
    if stale_warn:
        print(stale_warn)
    try:
        if args.dry_run:
            total = len(steps)
            for i, step in enumerate(steps, 1):
                print(f"[{i}/{total}] {step['name']} → {log_dir / (step['name'] + '.txt')}")
                if step["pp"]:
                    pp_val = f"{stubs_dir}:{copy_dir}" if stubs_dir is not None else str(copy_dir)
                    print(f"  env: PYTHONPATH={pp_val}")
                print(f"  $ {shlex.join(step['argv'])}")
            print("dry-run: шаги не запускались, копия не строилась")
            return 0
        log_dir.mkdir(parents=True)
        arch = subprocess.run(["git", "archive", "--format=tar", ref], cwd=repo,
                              capture_output=True, timeout=120)
        if arch.returncode != 0:
            sys.exit(f"git archive {ref!r} не удался: {arch.stderr.decode(errors='replace')[:300]}")
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tar:
            tar.extractall(copy_dir, filter="data")
        if overlay:
            for p in d_up:
                dst = copy_dir / p
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_tree / p, dst)
            for p in d_del:
                (copy_dir / p).unlink(missing_ok=True)
        for p in extras:
            dst = copy_dir / p
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / p, dst)

        summary = log_dir / "summary.txt"

        def note(msg: str) -> None:
            line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
            with open(summary, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        note(f"gate {args.name or args.task} ref={ref} files={' '.join(extras[:20])}"
             f"{' …(+' + str(len(extras) - 20) + ')' if len(extras) > 20 else ''}"
             f"{f' dirty_overlay=+{len(d_up)}/-{len(d_del)}' if overlay else ''}")
        if stale_warn:
            note(f"WARN: ref не содержит {dirty_n} незакоммиченных изменений — проверен бейзлайн, не код серии")
        run = runner or _gates_default_runner
        red = False
        for step in steps:
            if step["name"] not in only:
                continue
            env = os.environ.copy()
            if step["pp"]:
                env["PYTHONPATH"] = (f"{stubs_dir}:{copy_dir}" if stubs_dir is not None else str(copy_dir))
            print(f"[{step['name']}] {shlex.join(step['argv'])[:120]} …")
            rc, out = run(step["argv"], cwd=copy_dir, env=env, timeout=step["timeout"])
            (log_dir / f"{step['name']}.txt").write_text(out, encoding="utf-8")
            note(f"{step['name']} rc={rc} : {_gates_summary_extract(step['summary'], out)}")
            print(f"    rc={rc}")
            red = red or rc != 0
        note("GATE_DONE")
        print(f"{'FAIL' if red else 'OK'}: логи {log_dir} ({'есть красный шаг' if red else 'все шаги зелёные'})")
        if stale_warn:
            print(f"⚠ результат относится к ref={ref} БЕЗ {dirty_n} незакоммиченных изменений (см. ВНИМАНИЕ выше): "
                  f"зелёный гейт здесь не доказывает, что код серии чист")
        return 1 if red else 0
    finally:
        if args.keep:
            print(f"копия оставлена (--keep): {copy_dir}")
        else:
            shutil.rmtree(copy_dir, ignore_errors=True)


def cmd_selftest(cfg: dict, args) -> None:
    t = ClaudeGlm(cfg)
    print(f"профиль: {cfg['claude_config_dir']}, модель-алиас: {cfg['model']}")
    pair_cfg = cfg.get("pair") or {}
    pair_members = [m for m in (pair_cfg.get("reviewer_plan"), pair_cfg.get("reviewer_impl")) if m]
    for name in set(cfg["duo"]["members_plan"] + cfg["duo"]["members_impl"] + pair_members):
        m = load_member(cfg, name)
        assert m["identity"] and (m["fmt_standalone"] or m["fmt_round2"]), f"секции council-{name} не найдены"
        print(f"council-{name}: секции OK ({len(m['identity'])} симв. identity)")
    t0 = time.time()
    text, sid, cost = t.call(
        "Ответь ровно одним словом: PONG", mode="plan",
        timeout=cfg["timeouts"]["ping_sec"], cwd=Path(cfg["repo"]),
    )
    dt = time.time() - t0
    print(f"транспорт: {text.strip()[:50]!r} за {dt:.1f}s, ${cost:.4f}, session={sid and sid[:8]}…")
    assert "PONG" in text.upper(), "модель не ответила PONG"
    print("SELFTEST OK")


GITIGNORE_MARKER = "# duo-pipeline: рантайм серий (duo setup)"
GITIGNORE_PY_MARKER = "# duo-pipeline: Python-мусор прогонов (duo setup)"
PY_JUNK_ENTRIES = ["__pycache__/", "*.py[cod]"]


def python_junk_unignored(root: Path) -> bool:
    """Python-проект (под git есть *.py), а `__pycache__` не игнорируется: .pyc от прогона тестов
    исполнителем вылезут и как «выход за files_touch» (предупреждение ревьюеру), и как uncovered в
    `commit-by-plan --yes` (all-or-nothing). Н10 полевой проверки. Проверяем вложенный путь
    (корневой шаблон `/__pycache__/` его не покрывает). .pytest_cache/.ruff_cache/.mypy_cache
    себя игнорируют сами (внутри свой .gitignore) — в проверку не входят."""
    rc, out = sh(["git", "ls-files", "-co", "--exclude-standard", "--", "*.py"], cwd=root)
    if rc != 0 or not out.strip():
        return False
    for probe in ("__pycache__/x.pyc", "tests/__pycache__/x.pyc"):
        rc, _ = sh(["git", "check-ignore", "-q", "--", probe], cwd=root)
        if rc != 0:
            return True
    return False


def cmd_setup(cfg: dict, args, repo: Path | None) -> None:
    """R9 (duoportable): подключить проект. repo=None — cwd вне git: `git init` по
    подтверждению (--yes — неинтерактивно; без tty и без --yes — отказ с подсказкой);
    после init при отсутствии коммитов — предупреждение (worktree-сериям нужен коммит).
    Шаги идемпотентны: .duo/config.json не перезаписывается (только каркас при
    отсутствии), .gitignore не дублируется (маркер-комментарий), повторная регистрация
    в реестре — no-op. Повторный `duo setup` печатает «УЖЕ» и выходит rc=0."""
    root = repo
    target: Path | None = None
    if root is not None and detect_repo_root(root) is None:
        # явный --repo на каталог вне git: как `cd <каталог> && duo setup` (git init по подтверждению)
        target, root = root, None
    if root is None:
        cwd = target or Path.cwd()
        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit(f"{cwd} вне git-репозитория и нет tty для подтверждения: выполни `git init` "
                         f"сам либо запусти `duo setup --yes`")
            try:
                ans = input(f"{cwd} не в git: выполнить `git init`? [y/N] ")
            except (EOFError, OSError):
                ans = ""
            if ans.strip().lower() not in ("y", "yes", "д", "да"):
                sys.exit("без git-репозитория setup не выполняется (запусти из каталога проекта "
                         "или `duo setup --yes`)")
        rc, out = sh(["git", "init"], cwd=cwd)
        if rc != 0:
            sys.exit(f"git init не удался: {out.strip()[:300]}")
        root = cwd.resolve()
        print(f"git init: {root}")
        rc, _ = sh(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root)
        if rc != 0:
            print("ВНИМАНИЕ: в репозитории нет ни одного коммита — worktree-серии (`duo run --worktree`) "
                  "заработают после первого коммита; setup его не делает")
    done: list[str] = []
    had: list[str] = []
    # каркас .duo/config.json (существующий не трогаем — это конфиг проекта)
    proj_cfg = root / ".duo" / "config.json"
    if proj_cfg.exists():
        had.append(f".duo/config.json уже есть: {proj_cfg}")
    else:
        proj_cfg.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg.write_text(json.dumps({
            "_comment": "переопределения duo-пайплайна для проекта (слои: DEFAULTS < config.json "
                        "инструмента < этот файл); справочник ключей — docs/PROJECT-SETUP.md "
                        "в репозитории duo-pipeline",
            "executor_env_hint": "",
            "executor_worktree_note": "",
            "gates": {"steps": []},
            "git": {"worktree_links": []},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        done.append(f"создан каркас {proj_cfg} (заполни gates.steps и пр. по мере надобности)")
    # .gitignore: рантайм серий под маркером, без дублей
    arts = str(cfg.get("artifacts_dir", "pipeline"))
    wtd = str((cfg.get("git") or {}).get("worktrees_dir", ".worktrees"))
    entries = [f"{arts}/*/.lock", f"/{wtd}/"]
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [e for e in entries if e not in lines]
    if not missing:
        had.append(f".gitignore уже содержит записи duo ({', '.join(entries)})")
    else:
        out_lines = list(lines)
        if GITIGNORE_MARKER in out_lines:
            i = out_lines.index(GITIGNORE_MARKER)
            out_lines[i + 1:i + 1] = missing
        else:
            if out_lines and out_lines[-1].strip():
                out_lines.append("")
            out_lines += [GITIGNORE_MARKER, *missing]
        gi.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        done.append(f".gitignore: добавлены {missing} (маркер «{GITIGNORE_MARKER}»)")
    # Python-мусор прогонов: для Python-проекта без игнора __pycache__ (Н10) — отдельным блоком
    if python_junk_unignored(root):
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        out_lines = list(lines)
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines += [GITIGNORE_PY_MARKER, *PY_JUNK_ENTRIES]
        gi.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        done.append(f".gitignore: Python-проект — добавлены {PY_JUNK_ENTRIES} (маркер «{GITIGNORE_PY_MARKER}»); "
                    f"без них .pyc от прогона тестов блокируют commit-by-plan --yes")
    # реестр надзора
    if not args.no_register:
        projects = load_projects()
        if root in projects:
            had.append(f"проект уже в реестре ({_projects_registry_path()})")
        else:
            projects.append(root)
            save_projects(projects)
            done.append(f"проект зарегистрирован для надзора (supervise без --repo): {root}")
    else:
        had.append("регистрация в реестре пропущена (--no-register)")
    for m in had:
        print(f"УЖЕ: {m}")
    for m in done:
        print(f"СДЕЛАНО: {m}")
    print(f"корень проекта: {root}")
    if done:
        print("ВАЖНО: закоммить `.duo/` и `.gitignore` ДО первой серии — иначе `commit-by-plan --yes` "
              "увидит их как «не покрытые» и откажет (all-or-nothing)")
    print("дальше: отредактируй .duo/config.json (гейты и пр.), затем `duo init --task <имя>` "
          "и `duo run --task <имя>`")


def main() -> None:
    ap = argparse.ArgumentParser(description="дуо-пайплайн исполнитель↔надсмотрщик на GLM 5.3 (Z.AI)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--repo", help="корень проекта (приоритет: --repo > DUO_REPO > ключ repo конфига > "
                                   "автодетект от cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_mode_flags(parser, *, with_solo: bool) -> None:
        parser.add_argument("--mode", choices=sorted(VALID_MODES) if with_solo else ["duo", "pair"],
                            help="режим серии: duo (исполнитель+председатель+дуэт; по умолчанию), "
                                 "pair (исполнитель+один проверяющий)" + (", solo (см. --solo)" if with_solo else "")
                                 + "; записывается в state серии")
        parser.add_argument("--isolation", choices=sorted(VALID_ISOLATIONS),
                            help="shared — общее дерево (по умолчанию), worktree — своя папка и ветка duo/<task> "
                                 "(нужно для параллельных серий); записывается в state серии")
        parser.add_argument("--worktree", dest="isolation", action="store_const", const="worktree",
                            help="то же, что --isolation worktree")

    p = sub.add_parser("init"); p.add_argument("--task", required=True); p.add_argument("--task-file")
    add_mode_flags(p, with_solo=False)
    p = sub.add_parser("run"); p.add_argument("--task", required=True)
    add_mode_flags(p, with_solo=True)
    p.add_argument("--allow-shared-parallel", action="store_true",
                   help="разрешить запуск, даже если в общем дереве уже работает другая shared-серия (на свой риск)")
    p.add_argument("--ignore-claims", action="store_true",
                   help="стартовать имплементацию, даже если зона записи пересекается с работающей worktree-серией "
                        "(на свой риск: конфликт при слиянии)")
    p.add_argument("--fg", action="store_true",
                   help="foreground, старое поведение (по умолчанию — detached, переживает терминал)")
    p.add_argument("--continue", dest="force_continue", action="store_true",
                   help="продолжить после принятия плана в имплементацию")
    p.add_argument("--solo", action="store_true",
                   help="solo-режим: один исполнитель (glm-5.3-flash), без план-фазы и дуо-ревью")
    p.add_argument("--resume", dest="resume", action="store_true",
                   help="pipework3: возобновить остановку с машинной записью halt (транспорт/лимит/бюджет/артефакт) "
                        "с места остановки; в отличие от --continue НЕ пересекает паузу impl_pending")
    p.add_argument("--add-budget", type=float, default=0.0, metavar="USD",
                   help="pipework3: добавить USD к бюджету ЭТОЙ серии (state.budget_extra_usd), без правки config.json")
    p.add_argument("--add-iterations", type=int, default=0, metavar="N",
                   help="pipework3: добавить N итераций фазе, остановленной по лимиту (state.iter_extra)")
    p = sub.add_parser("status"); p.add_argument("--task")
    p.add_argument("--all", action="store_true", help="сводка по всем задачам")
    p.add_argument("--json", action="store_true",
                   help="с --task: ровно один JSON-объект (состояние + производные поля), без текстовых строк")
    p = sub.add_parser("wait", help="блокирующее ожидание остановки/приёмки серии; код выхода см. WAIT_EXIT")
    p.add_argument("--task", required=True)
    p.add_argument("--poll", type=float, default=15.0, help="период опроса, с")
    p.add_argument("--timeout", type=float, default=0.0, help="потолок ожидания, с (0 — без потолка)")
    p.add_argument("--quiet", action="store_true", help="печатать только финальную строку")
    p.add_argument("--keep-waiting-idle", action="store_true",
                   help="не завершаться на idle (needs_human без машинной записи/не запущено)")
    p = sub.add_parser("supervise", help="один тик надзора по всем сериям (для systemd-timer): dead/halted → алерт/авто-возобновление")
    p.add_argument("--resume", action="store_true", help="разрешить авто-возобновление (иначе — config supervisor.auto_resume)")
    p.add_argument("--dry-run", action="store_true", help="только показать, что сделал бы надзор (без алертов/запусков/записи состояния)")
    p.add_argument("--loop", action="store_true", help="не выходить: тик каждые --interval секунд (замена старому watchdog.sh)")
    p.add_argument("--interval", type=float, default=60.0, help="период тика в режиме --loop, с")
    p = sub.add_parser("errata", help="статус/применение переноса эррат председателя в принятый план")
    p.add_argument("--task", required=True)
    p.add_argument("--apply", action="store_true", help="дописать эрраты (идемпотентно; под .lock)")
    p = sub.add_parser("review"); p.add_argument("--task", required=True)
    p.add_argument("--artifact", required=True, help="путь к объекту ревью относительно repo")
    p.add_argument("--phase", default="custom")
    p.add_argument("--members", help="напр. socrates,feynman")
    p = sub.add_parser("set-verdict"); p.add_argument("--task", required=True)
    p.add_argument("--verdict", required=True, choices=["ACCEPT", "REVISE"])
    p.add_argument("--note", default="ручной вердикт пользователя")
    p.add_argument("--phase", choices=["plan", "impl"], default=None,
                   help="фаза needs_human, если не опознана по stop_reason")
    p = sub.add_parser("hold", help="DEPRECATED: флаг hold_impl — сторож больше не автопродолжает "
                                     "impl_pending; поведение сохранено для совместимости")
    p.add_argument("--task", required=True)
    p = sub.add_parser("release", help="DEPRECATED: снять флаг hold_impl — сторож больше не "
                                       "автопродолжает impl_pending; поведение сохранено")
    p.add_argument("--task", required=True)
    p = sub.add_parser("rework"); p.add_argument("--task", required=True)
    p.add_argument("--phase", required=True, choices=["plan", "impl"],
                   help="какой артефакт дорабатывать")
    p.add_argument("--review", required=True, help="файл внешнего ревью (регистрируется как rev{NNN}_<phase>.md)")
    p.add_argument("--keep-session", action="store_true",
                   help="не сбрасывать executor_session (по умолчанию — свежая сессия)")
    p.add_argument("--run", action="store_true", help="сразу запустить run (detached)")
    p.add_argument("--force", action="store_true", help="разрешить rework для phase=done")
    p = sub.add_parser("commit-by-plan"); p.add_argument("--task", required=True)
    p.add_argument("--version", type=int, default=None,
                   help="номер impl_report_v{N} (по умолчанию state.impl_version)")
    p.add_argument("--dry-run", action="store_true",
                   help="показать группы и покрытие, без интерактива и коммитов")
    p.add_argument("--yes", action="store_true",
                   help="подтвердить все группы без интерактива (для ассистента/CI); при любой аномалии "
                        "плана (предупреждения парсера, uncovered, overlap, пустые группы) — отказ до "
                        "первого коммита (all-or-nothing)")
    p.add_argument("--allow-partial", action="store_true",
                   help="с --yes: не отказывать при аномалиях плана, закоммитить только валидные группы")
    p = sub.add_parser("reset", help="сброс серии с чистого листа: state/планы/ревью/логи/транскрипты уезжают в "
                                     "pipeline/<task>/_reset_<метка>/ (не удаляются), task.md СОХРАНЯЕТСЯ; "
                                     "снимает worktree и ветку duo/<task> (при наработках — отказ без --force)")
    p.add_argument("--task", required=True)
    p.add_argument("--dry-run", action="store_true", help="показать, что будет сделано; ничего не менять")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (в неинтерактиве без --yes — отказ)")
    p.add_argument("--force", action="store_true",
                   help="обойти отказ по наработкам: phase=done, грязный worktree, неслитые коммиты ветки "
                        "(живую серию — lock занят — не обходит)")
    p = sub.add_parser("merge", help="влить принятую worktree-серию (ветка duo/<task>) в текущую ветку основного дерева")
    p.add_argument("--task", required=True)
    p.add_argument("--dry-run", action="store_true", help="только проверить: что войдёт и будут ли конфликты")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p.add_argument("--keep-worktree", action="store_true", help="после слияния не удалять worktree серии")
    p.add_argument("--force", action="store_true", help="разрешить слияние серии не в phase=done")
    p = sub.add_parser("claims", help="зоны записи работающих worktree-серий; --drop отпускает зону брошенной серии")
    p.add_argument("--task", help="показать/отпустить зону только этой серии")
    p.add_argument("--drop", action="store_true", help="отпустить зону серии --task (серия не должна быть запущена)")
    p = sub.add_parser("gates", help="гейты поставки в чистой копии (git archive REF): шаги из gates.steps "
                                     "конфига (плейсхолдеры {py}/{pybin}/{cpus}); rc≠0 при красном шаге")
    p.add_argument("--task", required=True)
    p.add_argument("--ref", help="что архивировать (по умолчанию duo/<task> для worktree-серий, иначе HEAD)")
    p.add_argument("--name", help="метка прогона (в имя каталога логов и summary)")
    p.add_argument("--only", help="подмножество шагов через запятую (имена из gates.steps конфига)")
    p.add_argument("--extra", nargs="+", metavar="PATH",
                   help="доп. файлы или КАТАЛОГИ из основного дерева (незакоммиченное/неслитое), пути от "
                        "корня repo; каталог — его файлы под git (tracked+untracked, без игнорируемых)")
    p.add_argument("--dirty", action="store_true",
                   help="наложить на копию незакоммиченные правки дерева серии (изменённые, новые, удалённые "
                        "файлы; без каталога артефактов) — иначе для незакоммиченной серии проверяется "
                        "бейзлайн HEAD (будет ВНИМАНИЕ)")
    p.add_argument("--dry-run", action="store_true", help="только печать плана шагов и команд")
    p.add_argument("--keep", action="store_true", help="не удалять временную копию (для разбора)")
    p = sub.add_parser("archive", help="уборка завершённой серии pipeline/<task>: без снимка "
                                       "tar.zst и дайджеста удаление невозможно; ничего не коммитит")
    p.add_argument("--task", help="имя серии (каталог pipeline/<task>)")
    p.add_argument("--list", action="store_true",
                   help="таблица всех серий с вердиктом «можно/нельзя архивировать + причина»")
    p.add_argument("--dry-run", action="store_true", help="показать всё, что сделал бы, ничего не меняя")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждение (в неинтерактиве без --yes — отказ)")
    p.add_argument("--min-age-days", type=int, default=14,
                   help="карантин после слияния, дней (по умолчанию 14)")
    p.add_argument("--archive-root", help="корень архива (по умолчанию ~/pipeline_archive/<ГГГГММДД>)")
    p.add_argument("--force-young", action="store_true", help="обойти карантин (на свой риск)")
    p.add_argument("--print-digest-template", action="store_true",
                   help="напечатать заготовку дайджеста `### <task>` c механической подстановкой "
                        "(поля-суждения — <заполнить>); <заполнить> в индексе блокирует архивацию")
    p = sub.add_parser("projects", help="реестр проектов для мультипроектного надзора (supervise без --repo)")
    p.add_argument("--add", metavar="PATH", help="добавить проект в реестр")
    p.add_argument("--remove", metavar="PATH", help="убрать проект из реестра")
    p.add_argument("--prune", action="store_true", help="убрать проекты, каталогов которых больше нет")
    p = sub.add_parser("setup", help="подключить проект: git init (по подтверждению), каркас .duo/config.json, "
                                     "записи .gitignore, регистрация в реестре надзора")
    p.add_argument("--yes", action="store_true",
                   help="подтверждать неинтерактивно (напр., git init вне репозитория)")
    p.add_argument("--no-register", action="store_true", help="не регистрировать проект в реестре надзора")
    sub.add_parser("selftest")

    args = ap.parse_args()
    # R1: корень проекта ДО сборки cfg — от него зависит проектный слой .duo/config.json
    # (R2). Legacy-ключ `repo` читаем ТОЛЬКО из слоя --config: полный cfg собирается
    # позже, а --repo/DUO_REPO приоритетнее.
    config_path = Path(args.config)
    legacy_repo = (_read_json_layer(config_path) or {}).get("repo")
    repo_root: Path | None
    if args.cmd == "projects":
        repo_root = None                 # корень не нужен: таблица ходит по реестру
    else:
        repo_root = resolve_repo(flag=args.repo, legacy=legacy_repo, cmd=args.cmd)
    cfg = load_config(config_path, repo=repo_root)
    cfg["_config_path"] = str(config_path.resolve())
    cfg["repo"] = str((repo_root or Path.cwd()).resolve())

    if args.cmd == "init":
        cmd_init(cfg, args)
    elif args.cmd == "selftest":
        cmd_selftest(cfg, args)
    elif args.cmd == "status":
        cmd_status(cfg, args)
    elif args.cmd == "wait":
        sys.exit(cmd_wait(cfg, args))
    elif args.cmd == "supervise":
        cmd_supervise(cfg, args, single=repo_root is not None)
    elif args.cmd == "projects":
        cmd_projects(cfg, args)
    elif args.cmd == "setup":
        cmd_setup(cfg, args, repo_root)
    elif args.cmd == "errata":
        cmd_errata(cfg, args)
    elif args.cmd == "run":
        pins = resolve_mode_flags(args)    # противоречивые флаги — отказ в родителе, не в detached-ребёнке
        if args.force_continue:
            # Н11: сводка «что принято» — в выводе самой команды, а не только в state.json (detached-
            # ребёнок пишет в файл; родитель до этого молчал про то, ЧТО именно запускается)
            try:
                st0 = load_json(Path(cfg["repo"]).resolve() / cfg["artifacts_dir"] / args.task / "state.json")
            except (OSError, ValueError):
                st0 = {}
            if st0.get("phase") == "impl_pending":
                summ = accepted_plan_summary(st0)
                if summ:
                    print(f"запуск имплементации: {summ}")
        if not args.fg:
            run_detached(cfg, args.task)   # pipev1 П1: detached по умолчанию
            return
        Pipeline(cfg, args.task, mode=pins.get("mode"), isolation=pins.get("isolation")).run(
            force_continue=args.force_continue, add_budget=args.add_budget, add_iterations=args.add_iterations,
            resume=args.resume, allow_shared_parallel=args.allow_shared_parallel,
            ignore_claims=args.ignore_claims)
    elif args.cmd == "review":
        members = args.members.split(",") if args.members else None
        Pipeline(cfg, args.task).review_once(args.artifact, args.phase, members)
    elif args.cmd == "set-verdict":
        cmd_set_verdict(cfg, args)
    elif args.cmd == "rework":
        cmd_rework(cfg, args)
    elif args.cmd == "hold":
        cmd_hold(cfg, args, True)
    elif args.cmd == "release":
        cmd_hold(cfg, args, False)
    elif args.cmd == "commit-by-plan":
        cmd_commit_by_plan(cfg, args)
    elif args.cmd == "reset":
        # логика — в соседнем модуле (как archive): ленивый импорт, см. комментарий ниже
        sys.path.insert(0, str(SCRIPT_DIR))
        from reset_cmd import cmd_reset
        sys.exit(cmd_reset(cfg, args))
    elif args.cmd == "merge":
        sys.exit(cmd_merge(cfg, args))
    elif args.cmd == "claims":
        cmd_claims(cfg, args)
    elif args.cmd == "gates":
        sys.exit(cmd_gates(cfg, args))
    elif args.cmd == "archive":
        # логика — в соседнем модуле (duo_pipeline.py уже перегружен); импорт ЛЕНИВЫЙ:
        # существующие тесты грузят этот файл importlib-ом без каталога на sys.path и
        # archive не диспетчеризуют — не должны видеть соседний модуль при импорте
        sys.path.insert(0, str(SCRIPT_DIR))
        from archive_cmd import cmd_archive
        sys.exit(cmd_archive(cfg, args))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
