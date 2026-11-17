"""fieldfix3: правки по третьему/четвёртому полевым прогонам (серии pair+worktree на малых проектах).

Н13  подсказка исполнителю про симлинк `pipeline/<task>` в worktree (`Path.resolve()` уводит в основное
     дерево); блок изоляции теперь есть и в solo-промпте (раньше был только в pair/duo).
Н16  `gates`: файлы, покрытые `--extra`, не считаются в «ВНИМАНИЕ: ref НЕ содержит N незакоммиченных»
     (раньше предупреждение держалось после ровно той правки, которую оно советует).
Н15  `status --task NAME --json`: ровно один JSON-объект без прозы (обычный вывод = JSON состояния +
     текстовые строки, `json.load` на нём падает «Extra data» — споткнулся оператор).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from tests.unit.test_duo_fieldfix import (
    _dirty_shared_repo,
    _gates_one_step,
    _snap_runner,
    _wcfg,
)
from tests.unit.test_duo_fieldfix2 import _accepted_state, _run_cli
from tests.unit.test_duo_gates import _gates_ns
from tests.unit.test_duo_pipev3 import _cfg, _init_repo, _mk_task, duo
from tests.unit.test_duo_portable import _fake_executor

# --- Н13 -------------------------------------------------------------------------

_BASE = {"repo": "/tmp/x", "git": {"branch_prefix": "duo/"}, "artifacts_dir": "pipeline"}
_WT_STATE = {"isolation": "worktree", "task": "demo", "artifacts_rel": "pipeline/demo"}


def test_wt_block_warns_about_artifacts_symlink():
    block = _fake_executor(dict(_BASE), dict(_WT_STATE))._wt_block()
    assert "`pipeline/demo`" in block and "симлинк" in block
    assert "Path(__file__).resolve()" in block and "realpath" in block     # что именно не делать
    assert "Path(__file__).absolute()" in block                            # и что делать вместо
    assert block.count("\n") == 2                                          # две строки: изоляция + симлинк


def test_wt_block_symlink_path_follows_artifacts_dir_without_state_key():
    # старые state без artifacts_rel: путь собирается из конфига и имени задачи
    cfg = {**_BASE, "artifacts_dir": "ci/runs"}
    block = _fake_executor(cfg, {"isolation": "worktree", "task": "t9"})._wt_block()
    assert "`ci/runs/t9`" in block


def test_wt_block_empty_in_shared_so_prompts_stay_byte_exact():
    assert _fake_executor(dict(_BASE), {"isolation": "shared", "task": "demo"})._wt_block() == ""
    shared = _fake_executor(dict(_BASE), {"isolation": "shared", "task": "demo", "artifacts_rel": "pipeline/demo"})
    assert "симлинк" not in shared._common(solo=True) and "симлинк" not in shared._common()


@pytest.mark.parametrize("solo", [False, True])
def test_prompts_carry_isolation_block_in_worktree(solo: bool):
    # solo + worktree допустим (`run --solo --worktree`), но solo-промпт блока изоляции не содержал:
    # исполнитель не знал ни про параллельные серии, ни про симлинк артефактов
    p = _fake_executor(dict(_BASE), {**_WT_STATE, "mode": "pair"})
    text = p._common(solo=solo)
    assert "Изоляция: ты работаешь в отдельном git-worktree" in text and "симлинк" in text


# --- Н15 -------------------------------------------------------------------------


def _ns(**kw):
    return types.SimpleNamespace(**{"task": "demo", "all": False, "json": False, **kw})


def test_status_json_is_a_single_parsable_object(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)
    st = duo.load_json(td / "state.json")
    st.update(_accepted_state(mode="pair"))
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), _ns(json=True))
    obj = json.loads(capsys.readouterr().out)                              # весь stdout — один JSON
    assert obj["task"] == "demo" and obj["phase"] == "impl_pending" and obj["mode"] == "pair"
    assert obj["budget"]["spent_usd"] == 3.1234 and obj["budget"]["limit_usd"] == 100.0
    assert set(obj["claim"]) == {"released", "paths", "declared"}
    assert obj["claim"]["paths"] == 3 and obj["claim"]["declared"] is True
    assert obj["process"]["kind"] and isinstance(obj["process"]["alive"], bool)
    assert "план v2 принят" in obj["accepted_plan"]
    assert obj["review_transport"] == {"stub_retries": 0, "transport_retries": 0, "empty_retries": 0, "reviews": 0}
    assert obj["state"]["plan_version"] == 2                               # полное состояние — внутри
    assert obj["isolation"] == "shared" and obj["worktree"] is None and obj["branch"] is None


def test_status_json_degradation_and_no_summary_before_accept(tmp_path: Path, capsys):
    repo = _init_repo(tmp_path)
    td = _mk_task(repo)                                                     # phase=plan, план не принят
    st = duo.load_json(td / "state.json")
    st["review_telemetry"] = [{"stub_retries": 2, "transport_retries": 1, "empty_retries": 0},
                              {"stub_retries": 0, "transport_retries": 0, "empty_retries": 1}]
    duo.save_json(td / "state.json", st)
    duo.cmd_status(_cfg(tmp_path, repo), _ns(json=True))
    obj = json.loads(capsys.readouterr().out)
    assert obj["accepted_plan"] is None and obj["claim"] is None
    assert obj["review_transport"] == {"stub_retries": 2, "transport_retries": 1, "empty_retries": 1, "reviews": 2}


def test_status_plain_output_is_unchanged_and_not_pure_json(tmp_path: Path, capsys):
    # без --json — прежний вывод (JSON состояния + строки): ничего не сломано, но это НЕ один JSON
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    duo.cmd_status(_cfg(tmp_path, repo), _ns())
    out = capsys.readouterr().out
    assert "бюджет: $" in out and "процесс:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_status_json_flag_via_cli(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"repo": str(repo)}), encoding="utf-8")
    _run_cli(monkeypatch, cfg_path, "status", "--task", "demo", "--json")
    assert json.loads(capsys.readouterr().out)["task"] == "demo"
    # --all главнее: таблица, не JSON (флаг --json относится только к --task)
    _run_cli(monkeypatch, cfg_path, "status", "--all", "--json")
    out = capsys.readouterr().out
    assert "demo" in out and "бюджет:" not in out                        # таблица, не карточка задачи


# --- Н16 -------------------------------------------------------------------------


def _gates_out(tmp_path: Path, repo: Path, capsys, **ns_over) -> str:
    assert duo.cmd_gates(_gates_one_step(tmp_path, repo), _gates_ns(**ns_over), runner=_snap_runner({})) == 0
    return capsys.readouterr().out


def test_gates_warning_excludes_files_covered_by_extra(tmp_path: Path, capsys):
    repo = _dirty_shared_repo(tmp_path)               # грязно: app.py (M), new.py (??), old.py (D)
    out = _gates_out(tmp_path, repo, capsys)
    assert "НЕ содержит 3 незакоммиченных" in out                          # без extra — как раньше
    (tmp_path / "second").mkdir()
    repo2 = _dirty_shared_repo(tmp_path / "second")
    out = _gates_out(tmp_path / "second", repo2, capsys, extra=["app.py"])
    assert "НЕ содержит 2 незакоммиченных" in out and "new.py" in out and "app.py" not in out.split("ВНИМАНИЕ")[1]


def test_gates_extra_cannot_cover_deletion_so_warning_stays(tmp_path: Path, capsys):
    repo = _dirty_shared_repo(tmp_path)
    out = _gates_out(tmp_path, repo, capsys, extra=["app.py", "new.py"])
    assert "НЕ содержит 1 незакоммиченных" in out and "old.py" in out      # удалённый файл extra не накладывает


def test_gates_no_warning_when_extra_covers_everything(tmp_path: Path, capsys):
    repo = _dirty_shared_repo(tmp_path)
    (repo / "old.py").write_text("old", encoding="utf-8")                  # удаление отменено → грязны только app/new
    out = _gates_out(tmp_path, repo, capsys, extra=["app.py", "new.py"])
    assert "ВНИМАНИЕ" not in out and "⚠" not in out


def test_gates_worktree_series_extra_does_not_silence_warning(tmp_path: Path, capsys):
    # `--extra` берёт файлы из ОСНОВНОГО дерева; грязь worktree-серии лежит в другом дереве —
    # засчитывать её покрытой нельзя, предупреждение обязано остаться
    repo = _init_repo(tmp_path)
    _mk_task(repo)
    cfg = _wcfg(tmp_path, repo)
    p = duo.Pipeline(cfg, "demo")
    st = p.load_state()
    st["isolation"] = "worktree"
    p.save_state(st)
    p._ensure_worktree(st)
    (repo / ".worktrees" / "demo" / "impl.py").write_text("правка в worktree", encoding="utf-8")
    (repo / "impl.py").write_text("другое содержимое в основном дереве", encoding="utf-8")
    gcfg = _gates_one_step(tmp_path, repo)
    gcfg["git"] = cfg["git"]
    assert duo.cmd_gates(gcfg, _gates_ns(extra=["impl.py"]), runner=_snap_runner({})) == 0
    out = capsys.readouterr().out
    assert "НЕ содержит 1 незакоммиченных" in out and "impl.py" in out
