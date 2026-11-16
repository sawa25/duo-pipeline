#!/usr/bin/env python3
"""reset_cmd — подкоманда `reset` оркестратора: сброс серии «с чистого листа».

Зачем (полевой отчёт, находка Н1): единственным способом перезапустить серию, отказавшую
на старте, был `rm -rf pipeline/<task>` — он уносил и `task.md`, а `init` молча писал на его
место шаблон («Опиши задачу здесь»). `reset` делает то же безопасно:

* `task.md` НЕ трогается никогда (печатается, что сохранён);
* всё остальное в каталоге серии (state.json, планы, ревью, отчёты, логи, транскрипты,
  диффы) НЕ удаляется, а переезжает в `pipeline/<task>/_reset_<ГГГГММДД-ЧЧММСС>/` — внутри
  серии, чтобы `archive` уносил это вместе с ней, а коммит-покрытие считало артефактом;
* worktree `.worktrees/<task>` и ветка `duo/<task>` снимаются, но НЕ при наработках: грязное
  дерево или коммиты ветки, не слитые в HEAD основного дерева, — отказ (обход — `--force`);
* принятая серия (phase=done) и живая (lock занят) — отказ (живую не обойти ничем).

Самодостаточен: не импортирует duo_pipeline (тот лениво импортирует этот модуль в
диспетчере); стандартная библиотека + subprocess для git. Ничего не коммитит и не мержит.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TASK_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
BACKUP_PREFIX = "_reset_"
KEEP_NAMES = {"task.md", ".lock"}      # .lock — файл блокировки, не артефакт


def _sh(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _paths(cfg: dict, task: str) -> dict:
    repo = Path(cfg["repo"]).resolve()
    git = cfg.get("git") or {}
    arts = cfg.get("artifacts_dir", "pipeline")
    return {
        "repo": repo,
        "arts": arts,
        "task_dir": repo / arts / task,
        "wt": repo / git.get("worktrees_dir", ".worktrees") / task,
        "branch": f"{git.get('branch_prefix', 'duo/')}{task}",
    }


def _dirty_entries(wt: Path, arts: str, task: str) -> list[str]:
    """Незакоммиченные реальные изменения worktree (симлинки provisioning и симлинк
    артефактов серии — не наработки)."""
    rc, out = _sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=wt)
    if rc != 0:
        return [f"git status не удался: {out.strip()[:120]}"]
    res = []
    for line in out.splitlines():
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        if (wt / rel).is_symlink():
            continue
        if rel == f"{arts}/{task}" or rel.startswith(f"{arts}/{task}/"):
            continue
        res.append(line.rstrip())
    return res


def _unmerged_commits(repo: Path, branch: str) -> int:
    """Коммиты ветки, которых нет в HEAD основного дерева (0 — наработок нет/ветка слита)."""
    rc, out = _sh(["git", "rev-list", "--count", f"HEAD..{branch}"], cwd=repo)
    try:
        return int(out.strip()) if rc == 0 else 0
    except ValueError:
        return 0


def _branch_exists(repo: Path, branch: str) -> bool:
    rc, _ = _sh(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=repo)
    return rc == 0


def _movable(task_dir: Path) -> list[Path]:
    return sorted(c for c in task_dir.iterdir()
                  if c.name not in KEEP_NAMES and not c.name.startswith(BACKUP_PREFIX))


def cmd_reset(cfg: dict, args) -> int:
    task = args.task
    if not TASK_NAME_RE.fullmatch(task or ""):
        print(f"ОШИБКА: недопустимое имя серии: {task!r}", file=sys.stderr)
        return 2
    P = _paths(cfg, task)
    repo, task_dir, wt, branch, arts = P["repo"], P["task_dir"], P["wt"], P["branch"], P["arts"]
    if not task_dir.is_dir():
        print(f"ОШИБКА: серия не найдена: {task_dir}", file=sys.stderr)
        return 2

    # блокировка серии на всё время reset: живой оркестратор → отказ (--force не обходит)
    lock_fd = open(task_dir / ".lock", "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"ОТКАЗ: серия {task} запущена (lock занят) — сначала останови процесс; reset не трогает живую серию.",
              file=sys.stderr)
        lock_fd.close()
        return 2
    try:
        return _reset_locked(args, P, task)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def _reset_locked(args, P: dict, task: str) -> int:
    repo, task_dir, wt, branch, arts = P["repo"], P["task_dir"], P["wt"], P["branch"], P["arts"]
    st = _load_json(task_dir / "state.json")
    force = bool(getattr(args, "force", False))
    has_wt = (wt / ".git").exists()
    has_branch = _branch_exists(repo, branch)
    to_move = _movable(task_dir)
    task_md = task_dir / "task.md"

    refusals: list[str] = []
    if st.get("phase") == "done":
        refusals.append("серия принята (phase=done): reset выбросит результат приёмки")
    dirty: list[str] = []
    if has_wt:
        dirty = _dirty_entries(wt, arts, task)
        if dirty:
            refusals.append(f"в worktree {wt} незакоммиченные изменения ({len(dirty)}): " + "; ".join(dirty[:3])
                            + (" …" if len(dirty) > 3 else ""))
    ahead = _unmerged_commits(repo, branch) if has_branch else 0
    if ahead:
        refusals.append(f"в ветке {branch} {ahead} коммит(ов), не слитых в HEAD основного дерева "
                        f"(наработки серии; после -D останутся только в reflog)")

    print(f"reset серии {task}:")
    print(f"  состояние: phase={st.get('phase', '—')}, mode={st.get('mode', '—')}, "
          f"isolation={st.get('isolation', '—')}, потрачено ${float(st.get('spent_usd', 0) or 0):.2f} "
          f"(счётчик бюджета после reset начнётся заново)")
    if task_md.is_file():
        n = len(task_md.read_text(encoding="utf-8", errors="replace").splitlines())
        print(f"  task.md: СОХРАНЯЕТСЯ без изменений ({n} строк)")
    else:
        print("  task.md: ОТСУТСТВУЕТ — после reset напиши задание до `run` (шаблон `run` не примет)")
    print(f"  в _reset_<метка>/ переедет: {len(to_move)} объект(ов)"
          + (f" ({', '.join(c.name for c in to_move[:6])}{' …' if len(to_move) > 6 else ''})" if to_move else ""))
    if has_wt or has_branch:
        print("  git: " + ("снять worktree " + str(wt) if has_wt else "worktree нет")
              + "; " + ("удалить ветку " + branch if has_branch else "ветки нет"))
    for r in refusals:
        print(f"  {'ВНИМАНИЕ (--force)' if force else 'ОТКАЗ'}: {r}")

    if getattr(args, "dry_run", False):
        print("dry-run: ничего не изменено")
        return 0
    if refusals and not force:
        print("reset отменён. Разбери наработки (merge/commit/сохранить патч) или повтори с --force.",
              file=sys.stderr)
        return 3
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print("ОТКАЗ: без --yes в неинтерактивном режиме reset не выполняется.", file=sys.stderr)
            return 3
        if input("Выполнить reset? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            print("отменено")
            return 3

    # 1) git (сначала: при сбое файлы серии остаются на месте)
    if has_wt:
        # симлинки (артефакты серии, provisioning) снимаем сами — git их и так не разыменовывает,
        # но каталог артефактов основного дерева не должен зависеть от поведения git
        arts_link = wt / arts / task
        if arts_link.is_symlink():
            arts_link.unlink()
        rc, out = _sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, timeout=300)
        if rc != 0:
            print(f"ОШИБКА: git worktree remove не удался: {out.strip()[:300]} — reset прерван, "
                  f"файлы серии не тронуты", file=sys.stderr)
            return 1
    _sh(["git", "worktree", "prune"], cwd=repo)
    if has_branch:
        rc, out = _sh(["git", "branch", "-D", branch], cwd=repo)
        if rc != 0:
            print(f"ОШИБКА: git branch -D {branch} не удался: {out.strip()[:300]}", file=sys.stderr)
            return 1

    # 2) файлы серии — в _reset_<метка>/ (task.md и .lock остаются)
    moved = 0
    backup = None
    if to_move:
        backup = task_dir / f"{BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir()
        for c in to_move:
            shutil.move(str(c), str(backup / c.name))
            moved += 1
    print(f"OK: reset выполнен. Перенесено объектов: {moved}"
          + (f" → {backup}" if backup else "")
          + ("; worktree и ветка сняты" if (has_wt or has_branch) else ""))
    pin = f" --mode {st['mode']}" if st.get("mode") in ("duo", "pair") else ""
    pin += " --worktree" if st.get("isolation") == "worktree" else ""
    print(f"Дальше: duo init --task {task}{pin}   (прежние пины режима/изоляции — {pin.strip() or 'не было'}), затем run")
    return 0
