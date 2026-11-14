"""duoportable (R1–R10): юниты переносимости пайплайна на любые git-проекты.

Здесь — то, что не покрыто файлами отдельных фич (gates/archive/pipev3):
разрешение корня проекта (R1), трёхслойный конфиг (R2), guard.never_revert (R5),
executor_worktree_note (R7), реестр проектов и мультипроектный надзор (R8),
setup (R9), .duo/config.json этого репозитория (R10) и DoD-сценарий эраты Е1
(provisioning-симлинки git.worktree_links невидимы для ВСЕХ dirty-проверок).
Сквозной запуск «из другого каталога» — эрата Е2 — проверялся вручную (отчёт).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from tests.unit.test_duo_pipemodes import _log_text, _pcfg, _state, _wt_series, _zone
from tests.unit.test_duo_pipev3 import (
    DUO_PIPELINE_PATH,
    _cfg,
    _git,
    _head,
    _init_repo,
    _mk_task,
    duo,
)
from tests.unit.test_duo_pipework3 import _mk_state

REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_at(path: Path) -> Path:
    """Песочница с ЗАДАННЫМ именем каталога (проект = имя корня в надзоре R8):
    _init_repo из pipev3 всегда создаёт tmp_path/'repo' — двум проектам нужны
    разные имена."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (path / "README.md").write_text("hello", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def _proj_ns(**over):
    d = {"prune": False, "add": None, "remove": None}
    d.update(over)
    return argparse.Namespace(**d)


def _sup_ns(config: Path, **over):
    d = {"config": str(config), "resume": False, "dry_run": True, "loop": False, "interval": 1}
    d.update(over)
    return argparse.Namespace(**d)


def _setup_ns(**over):
    d = {"yes": False, "no_register": False}
    d.update(over)
    return argparse.Namespace(**d)


def _fake_executor(cfg: dict, state: dict) -> duo.Executor:
    """Executor без транспорта — только поля, которые читает _wt_block (R7)."""
    p = object.__new__(duo.Executor)
    p.cfg = cfg
    p.state = state
    p.repo = Path(cfg["repo"])
    p.task = state.get("task", "demo")
    return p


# ---------------------------------------------------------------------------
# R1: автокорень проекта
# ---------------------------------------------------------------------------

def test_defaults_have_no_repo_pin():
    assert "repo" not in duo.DEFAULTS                       # корень больше не в конфиге по умолчанию
    assert duo.DEFAULTS["git"]["worktree_links"] == []
    assert duo.DEFAULTS["gates"]["steps"] == []
    assert duo.DEFAULTS["guard"] == {"never_revert": []}
    assert duo.DEFAULTS["executor_worktree_note"] == ""


def test_detect_repo_root_from_subdir_and_linked_worktree(tmp_path: Path):
    repo = _init_repo(tmp_path).resolve()
    sub = repo / "docs" / "deep"
    sub.mkdir(parents=True)
    assert duo.detect_repo_root(sub) == repo                # из глубины каталогов
    wt = tmp_path / "elsewhere-wt"
    _git(repo, "worktree", "add", str(wt), "-b", "side")
    assert duo.detect_repo_root(wt) == repo                 # из linked worktree — ОСНОВНОЕ дерево
    assert duo.detect_repo_root(tmp_path) is None           # вне git


def test_resolve_repo_priority_flag_env_legacy_autodetect(tmp_path: Path, monkeypatch):
    a, b, c = _repo_at(tmp_path / "alpha"), _repo_at(tmp_path / "beta"), _repo_at(tmp_path / "gamma")
    monkeypatch.setenv("DUO_REPO", str(b))
    assert duo.resolve_repo(flag=str(a), legacy=str(c), cmd="run") == a.resolve()   # флаг выше всех
    assert duo.resolve_repo(flag=None, legacy=str(c), cmd="run") == b.resolve()     # DUO_REPO выше legacy
    monkeypatch.delenv("DUO_REPO")
    assert duo.resolve_repo(flag=None, legacy=str(c), cmd="run") == c.resolve()     # legacy-ключ --config
    (a / "docs").mkdir()
    assert duo.resolve_repo(flag=None, legacy=None, cmd="run",
                            cwd=a / "docs") == a.resolve()                          # автодетект от cwd
    monkeypatch.setenv("DUO_REPO", str(tmp_path / "nope"))
    with pytest.raises(SystemExit, match="каталог не найден"):
        duo.resolve_repo(flag=None, legacy=None, cmd="run")
    monkeypatch.delenv("DUO_REPO")                             # legacy-ветка проверяется без DUO_REPO
    with pytest.raises(SystemExit, match="несуществующий каталог"):
        duo.resolve_repo(flag=None, legacy=str(tmp_path / "gone"), cmd="run")


def test_resolve_repo_outside_git_and_special_cmds(tmp_path: Path):
    # обычная команда вне git — отказ с подсказкой, а не автодетект чужого корня
    with pytest.raises(SystemExit, match=r"--repo PATH"):
        duo.resolve_repo(flag=None, legacy=None, cmd="run", cwd=tmp_path)
    assert duo.resolve_repo(flag=None, legacy=None, cmd="projects", cwd=tmp_path) is None
    assert duo.resolve_repo(flag=None, legacy=None, cmd="setup", cwd=tmp_path) is None
    # supervise без явного указания — режим реестра: автодетект НЕ применяется
    inside = _repo_at(tmp_path / "alpha")
    assert duo.resolve_repo(flag=None, legacy=None, cmd="supervise", cwd=inside) is None


# ---------------------------------------------------------------------------
# R2: трёхслойный конфиг
# ---------------------------------------------------------------------------

def test_load_config_layers_and_precedence(tmp_path: Path):
    repo = _init_repo(tmp_path)
    tool = tmp_path / "tool.json"
    tool.write_text(json.dumps({
        "model": "toolmodel", "limits": {"budget_usd": 5.0},
        "duo": {"rounds": 9}, "tags": ["a"],
    }), encoding="utf-8")
    (repo / ".duo").mkdir()
    (repo / ".duo" / "config.json").write_text(json.dumps({
        "model": "projmodel", "limits": {"max_plan_iterations": 7}, "tags": ["b"],
    }), encoding="utf-8")
    cfg = duo.load_config(tool, repo=repo)
    assert cfg["model"] == "projmodel"                      # проектный слой выше инструментального
    assert cfg["limits"]["budget_usd"] == 5.0               # нижний слой дополняет по ключам
    assert cfg["limits"]["max_plan_iterations"] == 7
    assert cfg["duo"]["rounds"] == 9
    assert cfg["tags"] == ["b"]                             # списки замещаются целиком, не склеиваются
    assert cfg["_project_config_path"] == str((repo / ".duo" / "config.json").resolve())
    # без проектного файла путь не пиннится
    bare = tmp_path / "bare"
    bare.mkdir()
    assert duo.load_config(tool, repo=bare)["_project_config_path"] is None


def test_load_config_expands_paths(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DUO_TEST_DIR", str(tmp_path / "shared"))
    layer = tmp_path / "tool.json"
    layer.write_text(json.dumps({
        "claude_config_dir": "~/.claude-glm",
        "council_agents_dir": "$DUO_TEST_DIR/agents",
        "gates": {"python": "~/env311/bin/python", "stubs": "$DUO_TEST_DIR/gate_stubs"},
    }), encoding="utf-8")
    cfg = duo.load_config(layer)
    assert cfg["claude_config_dir"] == str(home / ".claude-glm")
    assert cfg["council_agents_dir"] == str(tmp_path / "shared" / "agents")
    assert cfg["gates"]["python"] == str(home / "env311" / "bin" / "python")
    assert cfg["gates"]["stubs"] == str(tmp_path / "shared" / "gate_stubs")


def test_read_json_layer_rejects_broken(tmp_path: Path):
    assert duo._read_json_layer(None) is None
    assert duo._read_json_layer(tmp_path / "missing.json") is None
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(SystemExit, match="некорректный JSON"):
        duo._read_json_layer(p)
    with pytest.raises(SystemExit, match="некорректный JSON"):    # битый слой валит load_config
        duo.load_config(p)
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SystemExit, match="JSON-объект"):
        duo._read_json_layer(p)
    p.write_text("{}", encoding="utf-8")
    assert duo._read_json_layer(p) == {}                        # пустой объект — слой без ключей


def test_cfg_for_repo_adds_service_keys(tmp_path: Path):
    repo = _repo_at(tmp_path / "alpha")
    (repo / ".duo").mkdir()
    (repo / ".duo" / "config.json").write_text('{"model": "proj"}', encoding="utf-8")
    tool = tmp_path / "tool.json"
    tool.write_text('{"model": "tool"}', encoding="utf-8")
    cfg = duo.cfg_for_repo(tool, repo)
    assert cfg["model"] == "proj" and cfg["repo"] == str(repo.resolve())
    assert cfg["_config_path"] == str(tool.resolve())


# ---------------------------------------------------------------------------
# R5: guard.never_revert
# ---------------------------------------------------------------------------

def test_gitguard_never_revert_from_config(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "protected.txt").write_text("v1", encoding="utf-8")
    (repo / "free.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "files")
    td = tmp_path / "td"
    td.mkdir()                                                 # enforce_readonly пишет лог отката в task_dir
    g = duo.GitGuard(repo, ["protected.txt"])
    before = g.snapshot_before_review("pipeline/demo")
    (repo / "protected.txt").write_text("owner-edit", encoding="utf-8")   # правка хозяина/живой серии
    (repo / "free.txt").write_text("reviewer-edit", encoding="utf-8")     # правка ревьюера
    g.enforce_readonly(before, "pipeline/demo", td)
    assert (repo / "protected.txt").read_text(encoding="utf-8") == "owner-edit"  # не откачен
    assert (repo / "free.txt").read_text(encoding="utf-8") == "v1"               # откачен
    # без списка — прежнее поведение: откатывается всё tracked-изменённое
    _git(repo, "checkout", "--", "protected.txt")
    g2 = duo.GitGuard(repo)
    before2 = g2.snapshot_before_review("pipeline/demo")
    (repo / "protected.txt").write_text("v2", encoding="utf-8")
    g2.enforce_readonly(before2, "pipeline/demo", td)
    assert (repo / "protected.txt").read_text(encoding="utf-8") == "v1"      # защита не действует


def test_pipeline_wires_never_revert_into_guards(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _cfg(tmp_path, repo)
    cfg["guard"] = {"never_revert": ["data/big.bin", "models/"]}
    p = duo.Pipeline(cfg, "demo")
    assert p.guard.never_revert == ("data/big.bin", "models/")
    assert p.main_guard.never_revert == ("data/big.bin", "models/")


# ---------------------------------------------------------------------------
# R7: executor_worktree_note (без пиннингов vca в коде)
# ---------------------------------------------------------------------------

OLD_VCA_WT_BLOCK = (
    "Изоляция: ты работаешь в отдельном git-worktree `/tmp/x` (ветка `duo/demo`); параллельно "
    "могут идти другие серии в своих worktree. Правь файлы только в этом дереве: основное дерево и "
    "чужие worktree не трогай, API-сервер :8001 не перезапускай.\n"
)


def test_executor_worktree_note_byte_exact():
    base = {"repo": "/tmp/x", "git": {"branch_prefix": "duo/"}}
    assert _fake_executor(dict(base), {"isolation": "shared"})._wt_block() == ""   # shared — пусто
    assert _fake_executor(dict(base), {"isolation": "worktree", "task": "demo"})._wt_block().endswith(
        "чужие worktree не трогай.\n")                                             # без заметки — прежний хвост
    p = _fake_executor({**base, "executor_worktree_note": "API-сервер :8001 не перезапускай."},
                       {"isolation": "worktree", "task": "demo"})
    assert p._wt_block() == OLD_VCA_WT_BLOCK   # vca-значение ключа = прежний блок байт-в-байт
    q = _fake_executor({**base, "executor_worktree_note": "  Свои услуги лежат в systemd.  "},
                       {"isolation": "worktree", "task": "demo"})
    assert q._wt_block().endswith("чужие worktree не трогай, Свои услуги лежат в systemd.\n")


def test_no_vca_pins_in_sources():
    # пины старого окружения vca (:8001, пути дерева vca, LINK_TARGETS) и старые пути
    # запуска в ПОДСКАЗКАХ (фактический re-exec в run_detached идёт по Path(__file__) — код)
    pins = (":8001", "vca-platform", "LINK_TARGETS",
            "tools/dev/duo_pipeline", "python duo_pipeline.py", "python {Path(__file__).name}")
    for src in (DUO_PIPELINE_PATH, DUO_PIPELINE_PATH.parent / "provision_worktree.py",
                DUO_PIPELINE_PATH.parent / "archive_cmd.py"):
        text = src.read_text(encoding="utf-8")
        for pin in pins:
            assert pin not in text, f"{src.name}: пин старого окружения: {pin}"


# ---------------------------------------------------------------------------
# R8: реестр проектов и мультипроектный надзор
# ---------------------------------------------------------------------------

def test_projects_registry_roundtrip(tmp_path: Path, capsys):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    duo.save_projects([a, b])
    assert duo.load_projects() == [a.resolve(), b.resolve()]
    reg = duo._projects_registry_path()
    assert reg == Path(tmp_path / "xdg-config") / "duo-pipeline" / "projects.json"   # XDG-изоляция
    reg.write_text("{", encoding="utf-8")                       # битый JSON — деградация, не краш
    assert duo.load_projects() == []
    assert "не читается" in capsys.readouterr().err
    reg.write_text("[1]", encoding="utf-8")                     # не-объект — так же
    assert duo.load_projects() == []
    assert "JSON-объект" in capsys.readouterr().err
    duo.save_projects([a, a, b])                                # дубликат схлопывается
    assert duo.load_projects() == [a.resolve(), b.resolve()]


def test_cmd_projects_table_and_management(tmp_path: Path, capsys):
    alpha = _repo_at(tmp_path / "alpha")
    _mk_task(alpha, "t1")                                       # одна живая серия
    beta = _repo_at(tmp_path / "beta")
    (beta / ".duo").mkdir()
    (beta / ".duo" / "config.json").write_text("{}", encoding="utf-8")
    gone = tmp_path / "gone"
    gone.mkdir()
    duo.save_projects([alpha, gone])
    duo.cmd_projects({}, _proj_ns())
    out = capsys.readouterr().out
    assert "alpha" in out and "gone" in out                     # таблица с проектами
    duo.cmd_projects({}, _proj_ns(add=str(beta)))
    assert str(beta.resolve()) in capsys.readouterr().out
    projects = duo.load_projects()
    assert projects == [alpha.resolve(), gone.resolve(), beta.resolve()]
    duo.cmd_projects({}, _proj_ns(add=str(beta)))               # повторный add идемпотентен
    assert duo.load_projects().count(beta.resolve()) == 1
    with pytest.raises(SystemExit, match="каталог не найден"):
        duo.cmd_projects({}, _proj_ns(add=str(tmp_path / "nope")))
    gone.rmdir()
    duo.cmd_projects({}, _proj_ns(prune=True))
    assert "убран" in capsys.readouterr().out
    assert gone.resolve() not in duo.load_projects()
    duo.cmd_projects({}, _proj_ns(remove=str(beta)))
    assert beta.resolve() not in duo.load_projects()
    final = capsys.readouterr().out
    duo.cmd_projects({}, _proj_ns())
    row_alpha = [ln for ln in capsys.readouterr().out.splitlines() if "alpha" in ln]
    assert row_alpha and row_alpha[0].rstrip().endswith("1")    # колонка «серий» = 1


def test_supervise_state_keys_are_per_project(tmp_path: Path):
    alpha, beta = _repo_at(tmp_path / "alpha"), _repo_at(tmp_path / "beta")
    cfga = {"repo": str(alpha), "artifacts_dir": "pipeline"}
    cfgb = {"repo": str(beta), "artifacts_dir": "pipeline"}
    now = 5_000_000.0
    _mk_state(alpha / "pipeline", "t", phase="impl", state_updated_at=now - 1000)
    _mk_state(beta / "pipeline", "t", phase="impl", state_updated_at=now - 1000)
    sp = tmp_path / "sup.json"

    def tick(cfg):
        return {a["task"]: a for a in duo.supervise_tick(
            cfg, now=now, state_path=sp, spawn=lambda t: None, notify=lambda a, b: None)}

    assert tick(cfga)["t"]["action"] == "alert"
    assert tick(cfgb)["t"]["action"] == "alert"                 # одноимённые серии не глушат друг друга
    keys = set(json.loads(sp.read_text(encoding="utf-8")))
    assert f"{alpha.resolve()}::t" in keys and f"{beta.resolve()}::t" in keys
    assert tick(cfga)["t"]["action"] == "none"                  # дедуп — на СВОЁМ ключе
    assert tick(cfgb)["t"]["action"] == "none"


def test_supervise_notify_header_has_project(tmp_path: Path):
    alpha = _repo_at(tmp_path / "alpha")
    cfg = {"repo": str(alpha), "artifacts_dir": "pipeline", "notify": {"desktop": True}}
    now = 6_000_000.0
    _mk_state(alpha / "pipeline", "fix", phase="impl", state_updated_at=now - 1000)
    titles: list[str] = []
    duo.supervise_tick(cfg, now=now, state_path=tmp_path / "s.json",
                       spawn=lambda t: None, notify=lambda a, b: titles.append(a))
    assert titles == ["duo alpha/fix: dead"]


def test_cmd_supervise_empty_registry_message(tmp_path: Path, capsys):
    duo.cmd_supervise({}, _sup_ns(tmp_path / "tool.json"), single=False)
    assert "реестр проектов пуст" in capsys.readouterr().out


def test_cmd_supervise_registry_fanout(tmp_path: Path, capsys):
    alpha, beta = _repo_at(tmp_path / "alpha"), _repo_at(tmp_path / "beta")
    _mk_state(alpha / "pipeline", "one", phase="impl", state_updated_at=time.time() - 1000)
    _mk_state(beta / "pipeline", "two", phase="impl", state_updated_at=time.time() - 1000)
    duo.save_projects([alpha, beta])
    tool = tmp_path / "tool.json"
    tool.write_text("{}", encoding="utf-8")
    duo.cmd_supervise({}, _sup_ns(tool), single=False)
    out = capsys.readouterr().out
    assert "[alpha] one dead → alert" in out
    assert "[beta] two dead → alert" in out
    # single=True — ровно один проект (явный --repo/legacy), реестр не читается
    capsys.readouterr()
    duo.save_projects([])
    cfg = {"repo": str(alpha), "artifacts_dir": "pipeline"}
    duo.cmd_supervise(cfg, _sup_ns(tool), single=True)
    assert "[alpha] one dead → alert" in capsys.readouterr().out


def test_detached_argv_prepends_repo_flag(tmp_path: Path):
    repo = _repo_at(tmp_path / "alpha")
    cfg = {"repo": str(repo)}
    av = duo._detached_argv(cfg, ["run", "--task", "t"])
    assert av == ["--repo", str(repo.resolve()), "run", "--task", "t"]
    same = ["--repo", "/elsewhere", "run", "--task", "t"]
    assert duo._detached_argv(cfg, same) == same                # явный --repo не дублируется


# ---------------------------------------------------------------------------
# R9: setup
# ---------------------------------------------------------------------------

def test_setup_in_git_repo_idempotent(tmp_path: Path, capsys):
    repo = _repo_at(tmp_path / "alpha")
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(), repo=repo)
    out1 = capsys.readouterr().out
    pc = repo / ".duo" / "config.json"
    assert pc.exists() and "СДЕЛАНО" in out1
    gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert duo.GITIGNORE_MARKER in gi and "pipeline/*/.lock" in gi and "/.worktrees/" in gi
    assert repo.resolve() in duo.load_projects()                # зарегистрирован для надзора
    pc.write_text('{"model": "custom"}', encoding="utf-8")      # конфиг проекта — не трогаем
    duo.cmd_setup(cfg, _setup_ns(), repo=repo)
    out2 = capsys.readouterr().out
    assert out2.count("УЖЕ") == 3 and "СДЕЛАНО" not in out2     # всё уже сделано, rc=0
    assert pc.read_text(encoding="utf-8") == '{"model": "custom"}'
    duo.cmd_setup(cfg, _setup_ns(no_register=True), repo=repo)
    assert "--no-register" in capsys.readouterr().out


def test_setup_gitignore_no_duplicates(tmp_path: Path):
    repo = _repo_at(tmp_path / "alpha")
    (repo / ".gitignore").write_text("pipeline/*/.lock\n", encoding="utf-8")   # половина уже есть
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(no_register=True), repo=repo)
    gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert gi.count("pipeline/*/.lock") == 1 and "/.worktrees/" in gi


def test_setup_outside_git_with_yes(tmp_path: Path, monkeypatch, capsys):
    d = tmp_path / "plain"
    d.mkdir()
    monkeypatch.chdir(d)
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(yes=True), repo=None)
    out = capsys.readouterr().out
    assert "git init" in out and "нет ни одного коммита" in out          # предупреждение про worktree
    assert (d / ".git").is_dir() and (d / ".duo" / "config.json").exists()
    assert d.resolve() in duo.load_projects()


def test_setup_outside_git_no_tty_refuses(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="нет tty"):
        duo.cmd_setup({"artifacts_dir": "pipeline"}, _setup_ns(), repo=None)


def test_setup_explicit_repo_outside_git_inits_that_dir(tmp_path: Path, monkeypatch, capsys):
    """`duo setup --repo <не-git каталог>` ≡ `cd <каталог> && duo setup`: git init именно там,
    а не молчаливый каркас в каталоге без git (после которого init/run падали бы на git)."""
    target = tmp_path / "plain"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(yes=True), repo=target)
    out = capsys.readouterr().out
    assert (target / ".git").is_dir() and (target / ".duo" / "config.json").exists()
    assert not (elsewhere / ".git").exists() and not (elsewhere / ".duo").exists()
    assert "git init" in out and target.resolve() in duo.load_projects()


def test_setup_commit_hint_only_when_something_created(tmp_path: Path, capsys):
    """Каркас/.gitignore после setup — незакоммиченные файлы: commit-by-plan --yes считает их
    «не покрытыми» и отказывает. Подсказка печатается, когда setup что-то создал, и молчит на no-op."""
    repo = _repo_at(tmp_path / "alpha")
    cfg = {"artifacts_dir": "pipeline", "git": {"worktrees_dir": ".worktrees"}}
    duo.cmd_setup(cfg, _setup_ns(), repo=repo)
    assert "закоммить `.duo/` и `.gitignore`" in capsys.readouterr().out
    duo.cmd_setup(cfg, _setup_ns(), repo=repo)
    assert "закоммить `.duo/`" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# R10: .duo/config.json этого репозитория
# ---------------------------------------------------------------------------

def test_repo_duo_config_loads():
    cfg = duo.load_config(None, repo=REPO_ROOT)
    assert cfg["_project_config_path"] == str(REPO_ROOT / ".duo" / "config.json")
    steps = cfg["gates"]["steps"]
    assert {s["name"] for s in steps} >= {"ruff", "unit"}       # гейты этого репо
    assert all(s.get("argv") for s in steps)
    nv = cfg["guard"]["never_revert"]
    assert {"duo_pipeline.py", "archive_cmd.py", "provision_worktree.py"} <= set(nv)


# ---------------------------------------------------------------------------
# Эрата Е1 (DoD): provisioning-симлинки невидимы для всех dirty-проверок
# ---------------------------------------------------------------------------

def test_e1_worktree_links_invisible_to_dirty_checks(tmp_path: Path):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    cfg["git"]["worktree_links"] = ["data"]
    (repo / "data").mkdir()                                     # тяжёлый untracked-ресурс
    (repo / "data" / "big.bin").write_text("x" * 64, encoding="utf-8")
    p = _wt_series(tmp_path, repo, "a", cfg, _zone("README.md"))
    wt = p.work_path
    assert (wt / "data").is_symlink()                           # provisioning слинковал по конфигу
    st = p.load_state()
    st["claim"] = {"declared": True, "paths": ["README.md"]}    # зона заявлена (как после claim-gate)
    # 1) series_changed_paths: правка серии видна, симлинк — нет
    (wt / "README.md").write_text("edited", encoding="utf-8")
    assert duo.series_changed_paths(cfg, repo, "a", st) == ["README.md"]
    # 2) _zone_outside: симлинк вне заявленной зоны не считается нарушением зоны
    assert p._zone_outside(st, "pipeline/a") == []
    (wt / "notes.txt").write_text("n", encoding="utf-8")
    assert p._zone_outside(st, "pipeline/a") == ["notes.txt"]
    # 3) _sync_base: dirty пуст несмотря на симлинк → fast-forward проходит
    (wt / "notes.txt").unlink()
    _git(wt, "checkout", "--", "README.md")
    (repo / "README.md").write_text("main moved", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "main2")
    p._sync_base(st, "pipeline/a")
    assert st["worktree_base"] == _head(repo)
    assert "не подтягиваю" not in _log_text(p)
    assert _state(p.task_dir)["worktree_base"] == _head(repo)
