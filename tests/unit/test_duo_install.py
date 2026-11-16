"""install.sh и bin/duo: установка на машину и лаунчер (HOME всегда во временном каталоге;
ключ --systemd здесь НЕ вызывается — он трогает живой user-systemd)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "install.sh"
LAUNCHER = REPO_ROOT / "bin" / "duo"


def _run(args: list[str], home: Path, **env_over: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DUO_REPO", "DUO_PYTHON")}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".state")})
    env.update(env_over)
    return subprocess.run(args, capture_output=True, text=True, timeout=120, env=env, cwd=str(home))


def test_install_creates_links_and_is_idempotent(tmp_path: Path):
    p = _run(["bash", str(INSTALL)], tmp_path)
    assert p.returncode == 0, p.stderr
    links = {
        tmp_path / ".local/bin/duo": LAUNCHER,
        tmp_path / ".cursor/skills/duo-pipeline": REPO_ROOT / "skill",
        tmp_path / ".claude/skills/duo-pipeline": REPO_ROOT / "skill",
    }
    for dst, src in links.items():
        assert dst.is_symlink() and dst.resolve() == src.resolve(), dst
    assert not (tmp_path / ".claude/agents").exists()        # персоны — только по --personas
    again = _run(["bash", str(INSTALL)], tmp_path)
    assert again.returncode == 0 and "linked" not in again.stdout and "backup" not in again.stdout
    assert again.stdout.count("ok      ") == 3


def test_install_check_changes_nothing(tmp_path: Path):
    p = _run(["bash", str(INSTALL), "--check", "--personas"], tmp_path)
    assert p.returncode == 0 and "TODO" in p.stdout
    assert not (tmp_path / ".local").exists() and not (tmp_path / ".claude").exists()


def test_install_personas_backup_only_when_different(tmp_path: Path):
    agents = tmp_path / ".claude/agents"
    agents.mkdir(parents=True)
    same = REPO_ROOT / "personas" / "council-feynman2.md"
    (agents / same.name).write_bytes(same.read_bytes())          # идентичная копия — без бэкапа
    (agents / "council-torvalds2.md").write_text("старая правленая версия", encoding="utf-8")
    p = _run(["bash", str(INSTALL), "--personas"], tmp_path)
    assert p.returncode == 0, p.stderr
    names = sorted(x.name for x in agents.iterdir())
    assert [n for n in names if ".bak-" in n] == [n for n in names if n.startswith("council-torvalds2.md.bak-")]
    assert len([n for n in names if ".bak-" in n]) == 1
    for src in (REPO_ROOT / "personas").glob("council-*.md"):
        dst = agents / src.name
        assert dst.is_symlink() and dst.resolve() == src.resolve()
    backup = next(agents.glob("council-torvalds2.md.bak-*"))
    assert backup.read_text(encoding="utf-8") == "старая правленая версия"


def test_install_rejects_unknown_flag(tmp_path: Path):
    p = _run(["bash", str(INSTALL), "--bogus"], tmp_path)
    assert p.returncode == 2 and "неизвестный аргумент" in p.stderr


def test_launcher_runs_orchestrator_via_symlink(tmp_path: Path):
    """Лаунчер находит корень инструмента через readlink -f симлинка в ~/.local/bin."""
    link = tmp_path / ".local/bin/duo"
    link.parent.mkdir(parents=True)
    link.symlink_to(LAUNCHER)
    p = _run([str(link), "--help"], tmp_path, DUO_PYTHON=sys.executable)
    assert p.returncode == 0, p.stderr
    assert "setup" in p.stdout and "--repo" in p.stdout


def test_launcher_bad_duo_python_fails_loudly(tmp_path: Path):
    p = _run([str(LAUNCHER), "--help"], tmp_path, DUO_PYTHON=str(tmp_path / "нет-такого-python"))
    assert p.returncode != 0


def test_launcher_reads_machine_env_file_and_local_bin(tmp_path: Path):
    """Машинные настройки — из ~/.config/duo-pipeline/env (вне git), а ~/.local/bin добавляется в
    PATH (там `claude`; в неинтерактивном ssh/systemd его иначе может не быть). «Интерпретатор» —
    скрипт-зонд, печатающий то, что увидел дочерний процесс."""
    (tmp_path / ".local/bin").mkdir(parents=True)
    probe = tmp_path / "probe"
    probe.write_text('#!/bin/sh\necho "CPUS=$DUO_GATES_CPUS"\necho "PATH=$PATH"\necho "PY=$DUO_PYTHON"\n',
                     encoding="utf-8")
    probe.chmod(0o755)
    env_file = tmp_path / ".config" / "duo-pipeline" / "env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"DUO_PYTHON={probe}\nDUO_GATES_CPUS=0-3\n", encoding="utf-8")
    p = _run([str(LAUNCHER)], tmp_path, PATH="/usr/bin:/bin")
    assert p.returncode == 0, p.stderr
    assert "CPUS=0-3" in p.stdout
    assert f"PY={probe}" in p.stdout                            # выбранный интерпретатор экспортирован
    assert f"PATH={tmp_path}/.local/bin:/usr/bin:/bin" in p.stdout


def test_launcher_is_executable_and_shipped_files_exist():
    assert os.access(LAUNCHER, os.X_OK) and os.access(INSTALL, os.X_OK)
    assert (REPO_ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: duo-pipeline\n")
    assert (REPO_ROOT / "systemd" / "duo-supervise.service.in").exists()
    assert not (REPO_ROOT / "systemd" / "install.sh").exists()                 # старой копии в systemd/ нет
    assert "@ROOT@/bin/duo supervise" in (REPO_ROOT / "systemd" / "duo-supervise.service.in").read_text(
        encoding="utf-8")
