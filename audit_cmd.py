"""`duo audit` — read-only аудит действий исполнителя по журналу его сессии claude CLI.

Зачем (полевой отчёт 20.11.2026, Н25): `pipeline/<task>/transcripts/*.md` хранят только промпт и финальную
реплику; какие инструменты вызывал исполнитель (Bash, Read, Write…), видно лишь в полном журнале
сессии — `<claude_config_dir>/projects/<слаг каталога>/<session_id>.jsonl`. Оркестратор знает и id
сессии (`state.json`), и каталог профиля, поэтому проверка границ задания («не читал `.env`, не звал
`adb` напрямую, писал только в `pipeline/<task>/`») не должна требовать догадок о путях и самодельных
разборщиков.

Что команда НЕ делает: не блокирует исполнение (для этого — `executor_deny`, технические запреты CLI),
не меняет `state.json`, не копирует сессии без явного `--archive` (в журнале сессии лежат РЕЗУЛЬТАТЫ
инструментов — содержимое прочитанных файлов и вывод команд, включая секреты).

Коды выхода: 0 — нарушений нет (или правил нет — тогда только счётчики); 1 — есть нарушения;
2 — проверить нельзя (нет state/сессии, битые правила): «не нашли журнал» НЕ значит «чисто».
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# инструменты записи → ключ с путём в input
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}
SCRIPT_EXT = (".py", ".sh", ".bash")      # исполняемое: его тело сканируется на deny_script
PREVIEW = 160                              # длина цитаты нарушения в выводе


class AuditConfigError(ValueError):
    """Некорректные правила `audit` в конфиге (битый regex, не список)."""


def session_ids(state: dict) -> list[str]:
    """id сессий исполнителя серии: история `executor_sessions` (пишется с версии Н25) и последняя
    `executor_session` (у серий старше этой версии — единственная). Порядок сохраняется, дубли убираются."""
    out: list[str] = []
    for sid in [*(state.get("executor_sessions") or []), state.get("executor_session")]:
        if isinstance(sid, str) and sid and sid not in out:
            out.append(sid)
    return out


def find_session_files(config_dir: Path, ids: list[str]) -> tuple[list[tuple[str, Path]], list[str]]:
    """(найденные [(id, путь)], потерянные id). Каталог проекта в `projects/` называется слагом cwd
    сессии (для worktree-серии — слаг worktree), поэтому ищем по id глобом, а не вычисляем слаг."""
    found: list[tuple[str, Path]] = []
    missing: list[str] = []
    for sid in ids:
        hits = sorted(Path(config_dir).glob(f"projects/*/{sid}.jsonl"))
        if hits:
            found.append((sid, hits[0]))
        else:
            missing.append(sid)
    return found, missing


PLAN_ZONE_TOKEN = "@plan"


def compile_rules(cfg_audit: dict, task: str, plan_zone: list[str] | None = None) -> dict:
    """Правила из `audit` конфига: regex-списки (deny_bash — по тексту Bash-команд; deny_read — по путям
    Read; deny_script — по ТЕЛУ записанных .py/.sh) и write_zones (каталоги записи, `{task}` подставляется).

    Токен `@plan` в write_zones (Н31) — «зона записи, заявленная принятым планом» (`## files_touch`: файлы,
    каталоги, globs). `plan_zone` — раскрытый вызывающим список (None — плана с такой секцией нет: solo,
    серия до принятия плана); тогда токен ничего не добавляет, а `plan_unresolved` честно говорит об этом.
    Проект-широкая зона (`src/`) для кодовой серии слишком грубая: запись в чужой модуль проходила бы."""
    out: dict = {"deny_bash": [], "deny_read": [], "deny_script": [], "write_zones": [],
                 "plan_zone": [], "plan_requested": False, "plan_unresolved": False}
    cfg_audit = {} if cfg_audit is None else cfg_audit
    if not isinstance(cfg_audit, dict):
        raise AuditConfigError("audit должен быть объектом {deny_bash, deny_read, deny_script, write_zones}")
    for key in ("deny_bash", "deny_read", "deny_script"):
        raw = (cfg_audit or {}).get(key) or []
        if not isinstance(raw, list):
            raise AuditConfigError(f"audit.{key} должен быть списком строк-regex")
        for pat in raw:
            try:
                out[key].append(re.compile(str(pat)))
            except re.error as e:
                raise AuditConfigError(f"audit.{key}: некорректный regex {pat!r}: {e}") from e
    zones = (cfg_audit or {}).get("write_zones") or []
    if not isinstance(zones, list):
        raise AuditConfigError("audit.write_zones должен быть списком каталогов")
    out["plan_requested"] = any(str(z).strip() == PLAN_ZONE_TOKEN for z in zones)
    out["write_zones"] = [os.path.normpath(str(z).replace("{task}", task)) for z in zones
                          if str(z).strip() != PLAN_ZONE_TOKEN]
    if out["plan_requested"]:
        out["plan_unresolved"] = plan_zone is None
        out["plan_zone"] = list(plan_zone or [])
    # проверка записи включена, если есть статические зоны либо `@plan` раскрыт (даже в пустой список:
    # «план заявил, что ничего не пишем» — тогда любая запись вне статических зон нарушение)
    out["zone_active"] = bool(out["write_zones"]) or (out["plan_requested"] and not out["plan_unresolved"])
    return out


def _local_time(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "—"


def _zone_verdict(path: str, root: Path, zones: list[str], plan_zone: list[str] | tuple = ()) -> str | None:
    """None — запись в разрешённой зоне; иначе краткая причина. `plan_zone` — пути/globs из плана
    (точное совпадение или fnmatch от корня дерева серии; `dir/*` пересекает `/`)."""
    absolute = path if os.path.isabs(path) else os.path.join(str(root), path)
    rel = os.path.relpath(os.path.normpath(absolute), str(root))
    if rel == ".." or rel.startswith(".." + os.sep):
        return "вне дерева серии"
    if any(rel == z or rel.startswith(z + os.sep) for z in zones):
        return None
    if any(rel == z or fnmatch.fnmatch(rel, z) for z in plan_zone):
        return None
    return "вне write_zones" if not plan_zone else "вне write_zones и зоны плана"


def _written_text(name: str, inp: dict) -> str:
    if name == "Write":
        return str(inp.get("content") or "")
    if name == "MultiEdit":
        return "\n".join(str(e.get("new_string") or "") for e in (inp.get("edits") or []) if isinstance(e, dict))
    return str(inp.get("new_string") or "")


def audit_files(files: list[tuple[str, Path]], rules: dict, root: Path) -> dict:
    """Разбор журналов сессий. Дедуп по `tool_use.id`: возобновлённая сессия может нести историю прежней,
    и одни и те же вызовы не должны считаться дважды. Ловушка отчёта: сканировать надо ИСПОЛНЯЕМОЕ (Bash,
    пути Read, тела .py/.sh), а не все Write/Edit — исполнитель пишет в отчётах «прямого adb не было»."""
    tools: dict[str, int] = {}
    findings: list[dict] = []
    seen: set[str] = set()
    for sid, path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                msg = ev.get("message") if isinstance(ev, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    uid = str(b.get("id") or "")
                    if uid and uid in seen:
                        continue
                    seen.add(uid)
                    name = str(b.get("name") or "?")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tools[name] = tools.get(name, 0) + 1
                    t = _local_time(ev.get("timestamp"))

                    def hit(kind: str, rule: str, subject: str, _t=t, _name=name, _sid=sid) -> None:
                        findings.append({"kind": kind, "rule": rule, "tool": _name, "time": _t,
                                         "session": _sid,
                                         # многострочные heredoc-команды — в одну строку вывода
                                         "subject": " ".join(subject.split())[:PREVIEW]})

                    if name == "Bash":
                        cmd = str(inp.get("command") or "")
                        for rx in rules["deny_bash"]:
                            if rx.search(cmd):
                                hit("bash", rx.pattern, cmd)
                    elif name == "Read":
                        fp = str(inp.get("file_path") or "")
                        for rx in rules["deny_read"]:
                            if rx.search(fp):
                                hit("read", rx.pattern, fp)
                    elif name in WRITE_TOOLS:
                        fp = str(inp.get(WRITE_TOOLS[name]) or "")
                        if rules.get("zone_active", bool(rules["write_zones"])) and fp:
                            why = _zone_verdict(fp, root, rules["write_zones"], rules.get("plan_zone") or ())
                            if why:
                                hit("write", why, fp)
                        if rules["deny_script"] and fp.endswith(SCRIPT_EXT):
                            body = _written_text(name, inp)
                            for rx in rules["deny_script"]:
                                if rx.search(body):
                                    hit("script", rx.pattern, fp)
    return {"tools": tools, "findings": findings, "events": sum(tools.values())}


def archive_sessions(task_dir: Path, files: list[tuple[str, Path]]) -> list[Path]:
    """Копия журналов сессий в `pipeline/<task>/audit/` (gzip, права 0600). Опт-ин: в журнале лежат
    результаты инструментов — прочитанные файлы и вывод команд, возможно секреты; каталог закрыт
    собственным `.gitignore` (`*`), чтобы `git add -A` и `commit-by-plan` его не подхватили."""
    out_dir = Path(task_dir) / "audit"
    out_dir.mkdir(exist_ok=True)
    (out_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    written: list[Path] = []
    for sid, src in files:
        dst = out_dir / f"session_{sid}.jsonl.gz"
        fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz, open(src, "rb") as fin:
            shutil.copyfileobj(fin, gz)
        written.append(dst)
    return written


def cmd_audit(cfg: dict, args, task_dir: Path, work_root: Path, state: dict,
              plan_zone: list[str] | None = None) -> int:
    """Диспетчер: `duo audit --task X [--json] [--archive]`. Пути/state резолвит вызывающий (Pipeline);
    `plan_zone` — зона из `## files_touch` принятого плана для токена `@plan` (None — нет)."""
    task = args.task
    as_json = bool(getattr(args, "json", False))

    def finish(obj: dict, text_lines: list[str]) -> int:
        if as_json:
            print(json.dumps(obj, ensure_ascii=False, indent=2))
        else:
            print("\n".join(text_lines))
        return obj["rc"]

    try:
        rules = compile_rules(cfg.get("audit"), task, plan_zone)
    except AuditConfigError as e:
        return finish({"task": task, "rc": 2, "error": str(e)}, [f"аудит невозможен: {e}"])
    counts = {k: len(rules[k]) for k in ("deny_bash", "deny_read", "deny_script", "write_zones")}
    counts["write_zones"] += len(rules["plan_zone"])
    checked = any(counts.values()) or rules["zone_active"]
    ids = session_ids(state)
    files, missing = find_session_files(Path(cfg["claude_config_dir"]), ids)
    if not files:
        why = ("в state.json нет id сессии исполнителя (серия ещё не вызывала исполнителя)" if not ids else
               f"журналы сессий не найдены в {cfg['claude_config_dir']}/projects/ (id: {', '.join(ids)}); "
               f"claude CLI чистит старые сессии — «журнала нет» НЕ означает «нарушений нет»")
        return finish({"task": task, "rc": 2, "error": why, "missing": missing}, [f"аудит невозможен: {why}"])

    res = audit_files(files, rules, work_root)
    findings = res["findings"]
    archived: list[str] = []
    if getattr(args, "archive", False):
        archived = [str(p) for p in archive_sessions(task_dir, files)]
    rc = 1 if findings else 0
    obj = {"task": task, "rc": rc, "checked": checked, "rules": counts,
           "sessions": [{"id": sid, "path": str(p), "bytes": p.stat().st_size} for sid, p in files],
           "plan_zone": (rules["plan_zone"] if rules["plan_requested"] and not rules["plan_unresolved"] else None),
           "missing": missing, "events": res["events"], "tools": res["tools"],
           "findings": findings, "archived": archived}
    mb = sum(p.stat().st_size for _, p in files) / 1e6
    tools_txt = ", ".join(f"{k} {v}" for k, v in sorted(res["tools"].items(), key=lambda kv: -kv[1])) or "—"
    lines = [f"АУДИТ действий исполнителя: {task}",
             f"сессии: {len(files)} ({', '.join(sid[:8] + '…' for sid, _ in files)}; {mb:.1f} МБ)"
             + (f"; журнал не найден для: {', '.join(m[:8] + '…' for m in missing)}" if missing else ""),
             f"вызовов инструментов: {res['events']} — {tools_txt}",
             "правила: " + " ".join(f"{k}={v}" for k, v in counts.items())
             + (f" (в т.ч. из плана {len(rules['plan_zone'])})" if rules["plan_zone"] else "")]
    if rules["plan_unresolved"]:
        lines.append("write_zones: `@plan` НЕ раскрыт — у серии нет принятого плана с секцией `## files_touch` "
                     "(solo / план без секции): вклад плана в зону пуст, действуют только статические зоны"
                     + ("" if rules["zone_active"] else "; запись НЕ проверялась"))
    if not checked:
        lines.append("правил нет (`audit` в .duo/config.json пуст): нарушения НЕ проверялись, только счётчики")
    lines.append(f"НАРУШЕНИЙ: {len(findings)}")
    for f in findings:
        lines.append(f"  [{f['kind']}] {f['time']} {f['tool']}: {f['subject']}   (правило: {f['rule']})")
    if archived:
        lines.append(f"архив сессий (gzip, 0600, закрыт .gitignore; МОЖЕТ СОДЕРЖАТЬ СЕКРЕТЫ — не коммитить): "
                     f"{', '.join(archived)}")
    lines.append("ограничение: аудит видит вызовы инструментов исполнителя, но не то, что делал запущенный им "
                 "скрипт (см. audit.deny_script); журналы ревьюеров не включены")
    return finish(obj, lines)


if __name__ == "__main__":
    sys.exit("запускай через `duo audit` (duo_pipeline.py)")
