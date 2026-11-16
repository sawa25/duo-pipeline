"""pipegates: подкоманда `gates` — гейты поставки в чистой копии (git archive REF +
проектные заглушки gates.stubs, падающие при импорте), DEPRECATED-пометка hold/release,
подсказка гейтов в `merge --dry-run`, обратная совместимость конфига/соседних серий.

R3 (duoportable): набор шагов и их argv — из gates.steps КОНФИГА (плейсхолдеры
{py}/{pybin}/{cpus}); песочница _gcfg несёт пять типовых шагов (ruff/mypy/import-linter/
два pytest) в формате конфига (байт-в-байт инвариант — test_gates_steps_from_config_byte_exact).

Паттерн test_duo_pipev3/pipemodes: модуль оркестратора — importlib-ом, git-песочницы —
в tmp_path, тяжёлые команды гейтов НЕ исполняются: раннер внедряется параметром
`runner=` в cmd_gates (DI, как spawn/notify в supervise_tick) и возвращает canned
(rc, out); его замыкание снимает argv/env/cwd и листинг копии, пока та жива.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_duo_pipemodes import _done_series, _human_commit, _merge_ns, _pcfg
from tests.unit.test_duo_pipev3 import _cfg, _git, _init_repo, _mk_task, duo
from tests.unit.test_duo_pipework2 import _set_state

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STUB_PKGS = ("llama_cpp", "onnx", "onnxruntime", "pynvml", "torch")
PY = Path(os.path.abspath(sys.executable))   # {py} по умолчанию = sys.executable, БЕЗ раскрытия симлинков
PYBIN = PY.parent


def _gates_ns(task="demo", ref=None, name=None, only=None, extra=None, dry_run=False, keep=False):
    return argparse.Namespace(task=task, ref=ref, name=name, only=only, extra=extra,
                              dry_run=dry_run, keep=keep)


def _sample_steps():
    """Пять типовых шагов гейтов (линтеры + два pytest-прогона); формат — как в gates.steps
    конфига проекта; регрессию держит test_gates_steps_from_config_byte_exact."""
    return json.loads(json.dumps([
        {"name": "ruff", "argv": ["{pybin}/ruff", "check", "."], "pp": False, "timeout": 900},
        {"name": "mypy", "argv": ["{pybin}/mypy"], "pp": False, "timeout": 900},
        {"name": "li", "argv": ["{pybin}/lint-imports"], "pp": False, "timeout": 900,
         "summary": "contracts"},
        {"name": "unit", "pp": True, "timeout": 7200,
         "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "{cpus}",
                  "nice", "-n", "10", "{py}", "-m", "pytest", "tests/unit", "tests/arch"]},
        {"name": "integ", "pp": True, "timeout": 7200,
         "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "{cpus}",
                  "nice", "-n", "10", "{py}", "-m", "pytest", "-m", "integration and not gpu"]},
    ]))


def _stubs(tmp_path: Path) -> Path:
    """Каталог пакетов-заглушек песочницы. Инструмент своих заглушек не поставляет: набор —
    дело проекта (gates.stubs), здесь это типичные «тяжёлые» зависимости."""
    d = tmp_path / "gate_stubs"
    for pkg in STUB_PKGS:
        (d / pkg).mkdir(parents=True, exist_ok=True)
        (d / pkg / "__init__.py").write_text(
            f"raise ImportError('stub: {pkg} is not installed in CI')\n", encoding="utf-8")
    return d


def _gcfg(tmp_path: Path, repo: Path, **over) -> dict:
    """cfg песочницы гейтов: типовой набор шагов и каталог заглушек в конфиге (R3)."""
    cfg = _cfg(tmp_path, repo)
    cfg["gates"] = {"cpus": "0-11", "stubs": str(_stubs(tmp_path)), "steps": _sample_steps()}
    cfg.update(over)
    return cfg


def _subdir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gate_repo(tmp_path: Path) -> Path:
    """Мини-проект для чистой копии: pyproject + один тест в базовом коммите; ветка
    duo/demo с feature.txt (REF); незакоммиченный dirty.txt (рабочее дерево ≠ REF);
    серия demo с isolation=worktree (REF по умолчанию — ветка duo/demo)."""
    repo = _init_repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\ntestpaths = ['tests']\n", encoding="utf-8")
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_ok.py").write_text("def test_ok():\n    assert True\n",
                                                        encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "duo/demo")
    (repo / "feature.txt").write_text("фича ветки duo/demo", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-qm", "feat: demo")
    (repo / "dirty.txt").write_text("незакоммиченное рабочее дерево", encoding="utf-8")
    td = _mk_task(repo, "demo")
    _set_state(td, isolation="worktree", phase="impl")
    return repo


def _step_name(argv: list[str]) -> str:
    if "pytest" in argv:
        return "unit" if "tests/unit" in argv else "integ"
    return {"ruff": "ruff", "mypy": "mypy", "lint-imports": "li"}.get(Path(argv[0]).name,
                                                                     Path(argv[0]).name)


def _fake_runner(results: dict[str, tuple[int, str]], calls: list):
    """Раннер-подмена: ничего не исполняет, снимает вызов и листинг копии."""
    def run(argv, *, cwd, env, timeout):
        cwd = Path(cwd)
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout": timeout,
                      "ls": sorted(p.name for p in cwd.iterdir())})
        rc, out = results.get(_step_name(argv), (0, "ok"))
        return rc, out
    return run


def _last_log_dir(repo: Path, task="demo") -> Path:
    logs = sorted((repo / "pipeline" / task / "logs").glob("gates_*"))
    assert logs, "нет каталогов gates_*"
    return logs[-1]


# --- (а0) R3: шаги из конфига, байт-в-байт / пустой набор / стабы ------------------


def test_gates_steps_from_config_byte_exact():
    """R3-регрессия: конфиг с пятью типовыми шагами разрешается БАЙТ-В-БАЙТ в ожидаемые
    argv/pp/timeout/summary. {py} по умолчанию = sys.executable (gates.python не задан)."""
    pinned = [
        {"name": "ruff", "pp": False, "timeout": 900, "summary": "last_line",
         "argv": [f"{PYBIN}/ruff", "check", "."]},
        {"name": "mypy", "pp": False, "timeout": 900, "summary": "last_line",
         "argv": [f"{PYBIN}/mypy"]},
        {"name": "li", "pp": False, "timeout": 900, "summary": "contracts",
         "argv": [f"{PYBIN}/lint-imports"]},
        {"name": "unit", "pp": True, "timeout": 7200, "summary": "last_line",
         "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "0-11",
                  "nice", "-n", "10", str(PY), "-m", "pytest",
                  "tests/unit", "tests/arch"]},
        {"name": "integ", "pp": True, "timeout": 7200, "summary": "last_line",
         "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "0-11",
                  "nice", "-n", "10", str(PY), "-m", "pytest",
                  "-m", "integration and not gpu"]},
    ]
    assert duo.gates_steps({"gates": {"cpus": "0-11", "steps": _sample_steps()}}) == pinned


def test_gates_python_symlink_not_resolved(tmp_path: Path):
    """python venv — симлинк на системный интерпретатор: {py}/{pybin} обязаны остаться в каталоге
    venv (там ruff/mypy/пакеты), а не уйти в каталог цели симлинка (resolve() ломал venv-проекты)."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path(sys.executable).resolve())
    g = duo._gates_resolve({"gates": {"python": str(venv_bin / "python"), "cpus": "0-1",
                                      "steps": [{"name": "r", "argv": ["{pybin}/ruff", "{py}", "{cpus}"]}]}})
    assert g["py"] == venv_bin / "python" and g["pybin"] == venv_bin
    assert g["steps"][0]["argv"] == [f"{venv_bin}/ruff", f"{venv_bin}/python", "0-1"]


def test_gates_no_steps_rc2_hint(tmp_path: Path, capsys):
    """R3: gates.steps не заданы → rc=2 и подсказка про .duo/config.json (не rc=0/трейсбек)."""
    repo = _gate_repo(tmp_path)
    assert duo.cmd_gates(_cfg(tmp_path, repo), _gates_ns()) == 2
    out = capsys.readouterr().out
    assert "gates.steps" in out and "PROJECT-SETUP.md" in out


def test_gates_only_unknown_names_from_config(tmp_path: Path):
    """--only валидируется по именам КОНФИГА (не по бывшему hardcode-набору)."""
    repo = _gate_repo(tmp_path)
    cfg = _gcfg(tmp_path, repo)
    cfg["gates"]["steps"] = [{"name": "lint", "argv": ["{pybin}/ruff", "check", "."],
                              "pp": False, "timeout": 60}]
    with pytest.raises(SystemExit, match="допустимы \\['lint'\\]"):
        duo.cmd_gates(cfg, _gates_ns(only="ruff"), runner=_fake_runner({}, []))


def test_gates_stubs_default_off_relative_and_custom(tmp_path: Path, capsys):
    """gates.stubs: ключ не задан или '' — заглушки не подмешиваются (PYTHONPATH=копия);
    относительный путь — от корня проекта; абсолютный — как есть; путь печатается в заголовке."""
    repo = _gate_repo(tmp_path)
    for variant in ("absent", ""):
        cfg = _gcfg(tmp_path, repo)
        if variant == "absent":
            cfg["gates"].pop("stubs")
        else:
            cfg["gates"]["stubs"] = variant
        calls: list = []
        assert duo.cmd_gates(cfg, _gates_ns(), runner=_fake_runner({}, calls)) == 0
        assert calls[3]["env"]["PYTHONPATH"] == str(calls[3]["cwd"])   # unit: только копия
    capsys.readouterr()                                           # сброс: дальше только dry-run'ы
    rel = repo / ".duo" / "stubs"
    rel.mkdir(parents=True)
    cfg_rel = _gcfg(tmp_path, repo)
    cfg_rel["gates"]["stubs"] = ".duo/stubs"
    assert duo.cmd_gates(cfg_rel, _gates_ns(dry_run=True)) == 0
    assert f"stubs={repo.resolve() / '.duo' / 'stubs'}" in capsys.readouterr().out.splitlines()[0]
    custom = _subdir(tmp_path, "mystubs")
    cfg_abs = _gcfg(tmp_path, repo)
    cfg_abs["gates"]["stubs"] = str(custom)
    assert duo.cmd_gates(cfg_abs, _gates_ns(dry_run=True)) == 0
    assert f"stubs={custom}" in capsys.readouterr().out.splitlines()[0]


def test_gates_stubs_missing_dir_refuses(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    cfg = _gcfg(tmp_path, repo)
    cfg["gates"]["stubs"] = ".duo/нет_такого"
    with pytest.raises(SystemExit, match="нет каталога заглушек"):
        duo.cmd_gates(cfg, _gates_ns(), runner=_fake_runner({}, []))


def test_gates_explicit_python_missing_refuses_with_hint(tmp_path: Path):
    """Явный gates.python, которого нет на этой машине (путь другого стенда), — внятный отказ
    ДО запуска шагов, с подсказкой убрать ключ; без ключа проверка не срабатывает."""
    repo = _gate_repo(tmp_path)
    cfg = _gcfg(tmp_path, repo)
    cfg["gates"]["python"] = str(tmp_path / "другой_стенд" / "bin" / "python")
    calls: list = []
    with pytest.raises(SystemExit, match=r"gates\.python=.*не найден.*Уберите ключ"):
        duo.cmd_gates(cfg, _gates_ns(), runner=_fake_runner({}, calls))
    assert not calls                                            # ни одного шага не запущено
    cfg["gates"].pop("python")
    assert duo.cmd_gates(cfg, _gates_ns(), runner=_fake_runner({}, calls)) == 0


def test_gates_cpus_env_override_between_config_and_default(monkeypatch):
    """Приоритет: gates.cpus проекта > $DUO_GATES_CPUS (машинная настройка) > все ядра."""
    cfg = {"gates": {"steps": [{"name": "u", "argv": ["{cpus}"]}]}}
    monkeypatch.setenv("DUO_GATES_CPUS", "2-3")
    assert duo._gates_resolve(cfg)["cpus"] == "2-3"
    cfg["gates"]["cpus"] = "0-1"
    assert duo._gates_resolve(cfg)["cpus"] == "0-1"
    monkeypatch.delenv("DUO_GATES_CPUS")
    del cfg["gates"]["cpus"]
    assert duo._gates_resolve(cfg)["cpus"] == duo._default_cpus()


def test_default_cpus_format_and_fallback(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3, 8, 9, 11}, raising=False)
    assert duo._default_cpus() == "0-3,8-9,11"
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {5}, raising=False)
    assert duo._default_cpus() == "5"

    def boom(_pid):
        raise OSError("нет affinity")
    monkeypatch.setattr(os, "sched_getaffinity", boom, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert duo._default_cpus() == "0-3"


def test_gates_cpus_default_is_all_available_and_valid():
    """gates.cpus не задан → все ядра процесса (не зашитое 0-11: на машине поменьше оно невалидно)."""
    g = duo._gates_resolve({"gates": {"steps": [{"name": "u", "argv": ["taskset", "-c", "{cpus}", "true"]}]}})
    assert g["cpus"] == duo._default_cpus() and g["steps"][0]["argv"][2] == g["cpus"]
    if shutil.which("taskset"):
        assert subprocess.run(["taskset", "-c", g["cpus"], "true"], timeout=30).returncode == 0


# --- (а) dry-run: план шагов и команд ---------------------------------------------


def test_gates_dry_run_plan_and_commands(tmp_path: Path, capsys):
    repo = _gate_repo(tmp_path)
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(dry_run=True)) == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "task=demo" in header and "ref=duo/demo" in header and "cpus=0-11" in header
    assert f"stubs={_stubs(tmp_path)}" in header
    # последовательность шагов ровно canonическая
    marks = [f"[{i}/5] {n}" for i, n in enumerate(("ruff", "mypy", "li", "unit", "integ"), 1)]
    assert [out.index(m) for m in marks] == sorted(out.index(m) for m in marks)
    # команды один в один как в ручном gate-прогоне
    assert f"$ {PYBIN}/ruff check ." in out
    assert f"$ {PYBIN}/mypy" in out
    assert f"$ {PYBIN}/lint-imports" in out
    wrap = f"env -u DATA_DIR -u LOG_DIR taskset -c 0-11 nice -n 10 {PY}"
    assert f"$ {wrap} -m pytest tests/unit tests/arch" in out
    assert f"$ {wrap} -m pytest -m 'integration and not gpu'" in out
    unit_line = [ln for ln in out.splitlines() if "tests/unit tests/arch" in ln][0].split("$ ")[1]
    assert "-q" not in unit_line.split()                       # своего -q НЕТ (addopts уже есть)
    # PYTHONPATH со стабами первым — только pytest-шагам
    assert out.count(f"env: PYTHONPATH={_stubs(tmp_path)}:") == 2
    # dry-run ничего не создаёт и копию убирает
    assert not (repo / "pipeline" / "demo" / "logs").exists()
    assert not Path(re.search(r"copy=(\S+)", header).group(1)).exists()


def test_gates_ref_default_head_for_shared_and_override(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)                                # shared-серия: isolation в state нет
    _mk_task(repo, "demo")
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(dry_run=True)) == 0
    assert "ref=HEAD" in capsys.readouterr().out.splitlines()[0]
    repo2 = _gate_repo(_subdir(tmp_path, "second"))
    assert duo.cmd_gates(_gcfg(tmp_path / "second", repo2), _gates_ns(ref="duo/demo", dry_run=True)) == 0
    assert "ref=duo/demo" in capsys.readouterr().out.splitlines()[0]


def test_gates_missing_ref_refuses_before_steps(tmp_path: Path, capsys):
    repo = _gate_repo(tmp_path)
    calls: list = []
    with pytest.raises(SystemExit):
        duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(ref="duo/nope"), runner=_fake_runner({}, calls))
    assert not calls                                            # ни одного шага не запущено


def test_gates_cpus_from_config_and_compat(tmp_path: Path, capsys):
    repo = _gate_repo(tmp_path)
    cfg = _gcfg(tmp_path, repo)
    cfg["gates"]["cpus"] = "4-5"
    assert duo.cmd_gates(cfg, _gates_ns(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert out.count("taskset -c 4-5") == 2
    assert "taskset -c 0-11" not in out


def test_gates_only_filter_runs_subset(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    calls: list = []
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(only="ruff,unit"),
                         runner=_fake_runner({}, calls)) == 0
    assert [_step_name(c["argv"]) for c in calls] == ["ruff", "unit"]
    assert sorted(p.name for p in _last_log_dir(repo).iterdir()) == ["ruff.txt", "summary.txt",
                                                                     "unit.txt"]


# --- (б) чистая копия из REF, а не рабочего дерева ---------------------------------


def test_gates_copy_from_ref_not_worktree(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    calls: list = []
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(), runner=_fake_runner({}, calls)) == 0
    assert len(calls) == 5 and len({str(c["cwd"]) for c in calls}) == 1
    ls = calls[0]["ls"]
    assert "pyproject.toml" in ls and "feature.txt" in ls      # содержимое REF (ветки)
    assert "dirty.txt" not in ls                               # незакоммиченное НЕ попало
    assert calls[3]["env"]["PYTHONPATH"] == f"{_stubs(tmp_path)}:{calls[3]['cwd']}"   # стабы первыми
    # линтерам PYTHONPATH не подменяется (наследуется окружение, как в ручном прогоне)
    assert calls[0]["env"].get("PYTHONPATH") == os.environ.get("PYTHONPATH")
    summary = (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")
    assert "ref=duo/demo" in summary.splitlines()[0]


def test_gates_extra_files_copied(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    calls: list = []
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(extra=["dirty.txt"]),
                         runner=_fake_runner({}, calls)) == 0
    assert "dirty.txt" in calls[0]["ls"]
    summary = (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")
    assert "files=dirty.txt" in summary.splitlines()[0]


# --- (г) красный/зелёный шаг, summary, дословный хвост pytest ----------------------


def test_gates_red_step_fails_and_summary(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    calls: list = []
    rc = duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(),
                       runner=_fake_runner({"ruff": (1, "E1: что-то не так\n1 error")}, calls))
    assert rc == 1
    log = _last_log_dir(repo)
    assert (log / "ruff.txt").read_text(encoding="utf-8") == "E1: что-то не так\n1 error"
    summary = (log / "summary.txt").read_text(encoding="utf-8")
    assert re.search(r"ruff rc=1 : 1 error$", summary, re.M)
    assert "GATE_DONE" in summary


def test_gates_green_verbatim_pytest_tail(tmp_path: Path):
    repo = _gate_repo(tmp_path)
    results = {"unit": (0, "collected 12 items\n....\n12 passed in 3.4s\n"),
               "integ": (0, "3 passed in 1.1s\n"), "li": (0, "Contracts: 42 kept, 0 broken.\n")}
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(),
                         runner=_fake_runner(results, [])) == 0
    summary = (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")
    assert re.search(r"unit rc=0 : 12 passed in 3\.4s$", summary, re.M)   # дословно
    assert re.search(r"integ rc=0 : 3 passed in 1\.1s$", summary, re.M)
    assert "Contracts: 42 kept, 0 broken." in summary                     # li — строки контрактов


def test_gates_empty_output_fallback_no_output(tmp_path: Path):
    """Эррата Е1: пустой вывод шага → «(no output)», не IndexError."""
    repo = _gate_repo(tmp_path)
    assert duo.cmd_gates(_gcfg(tmp_path, repo), _gates_ns(),
                         runner=_fake_runner({"ruff": (0, ""), "mypy": (0, "  \n")}, [])) == 0
    summary = (_last_log_dir(repo) / "summary.txt").read_text(encoding="utf-8")
    assert re.search(r"ruff rc=0 : \(no output\)$", summary, re.M)
    assert re.search(r"mypy rc=0 : \(no output\)$", summary, re.M)


# --- (д) временная копия: удалена / оставлена с --keep -----------------------------


def test_gates_temp_copy_removed_and_kept(tmp_path: Path, capsys):
    repo = _gate_repo(tmp_path)
    cfg = _gcfg(tmp_path, repo)
    calls: list = []
    assert duo.cmd_gates(cfg, _gates_ns(), runner=_fake_runner({}, calls)) == 0
    copy = calls[0]["cwd"]
    assert not copy.exists()                                   # по умолчанию убрана
    assert duo.cmd_gates(cfg, _gates_ns(keep=True), runner=_fake_runner({}, [])) == 0
    out = capsys.readouterr().out
    kept = Path(re.search(r"копия оставлена \(--keep\): (\S+)", out).group(1))
    assert kept.exists() and kept.is_dir()
    shutil.rmtree(kept, ignore_errors=True)                    # не мусорим в /tmp из теста


# --- (е) hold/release: DEPRECATED в stderr, поведение прежнее -----------------------


def test_hold_release_deprecated_stderr(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo, "demo")
    cfg = _cfg(tmp_path, repo)
    duo.cmd_hold(cfg, argparse.Namespace(task="demo"), True)
    io_ = capsys.readouterr()
    assert "DEPRECATED" in io_.err
    assert "hold_impl=true" in io_.out                          # прежняя печать на месте
    assert duo.load_json(td / "state.json")["hold_impl"] is True
    duo.cmd_hold(cfg, argparse.Namespace(task="demo"), False)
    io_ = capsys.readouterr()
    assert "DEPRECATED" in io_.err
    assert "hold_impl" not in duo.load_json(td / "state.json")  # снят, как раньше


# --- merge --dry-run подсказывает gates --------------------------------------------


def test_merge_dry_run_hints_gates(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    cfg = _pcfg(tmp_path, repo)
    _done_series(tmp_path, repo, cfg)
    _human_commit(repo)
    assert duo.cmd_merge(cfg, _merge_ns(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "конфликтов нет" in out
    assert "гейты в чистой копии: запустите `gates --task a`" in out


# --- (ж) обратная совместимость: конфиг без gates, соседние серии -------------------


def test_compat_config_without_gates_key(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"repo": str(tmp_path), "model": "test"}), encoding="utf-8")
    cfg = duo.load_config(cfg_file)                             # user-конфиг БЕЗ gates
    assert cfg["gates"]["cpus"] is None                         # умолчание из DEFAULTS: все ядра
    base = json.loads(json.dumps(duo.DEFAULTS))
    duo.deep_merge(base, {"repo": "/x", "limits": {"budget_usd": 1.0}})
    assert "cpus" in base["gates"]                              # merge ключ не теряет
    hand = _cfg(tmp_path, tmp_path)                             # самодельный cfg без gates
    assert duo._gates_resolve(hand)["cpus"] == duo._default_cpus()


def test_compat_state_json_parse(tmp_path: Path):
    """state.json серии читается как есть."""
    # state.json не отслеживается git (им управляет оркестратор) — в чистой копии/CI его нет;
    # поэтому читаем синтетический слепок того же формата
    state_dst = tmp_path / "pipegates_state.json"
    state_dst.write_text(json.dumps({"task": "pipegates", "mode": "pair", "isolation": "worktree"}),
                         encoding="utf-8")
    st = duo.load_json(state_dst)
    assert st["task"] == "pipegates" and st["isolation"] == "worktree"
