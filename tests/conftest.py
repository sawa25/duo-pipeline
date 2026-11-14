"""Изоляция тестов от машины (duoportable R8): автouse-фикстура на КАЖДЫЙ тест.

Ни один тест не читает и не пишет живые ~/.config/duo-pipeline/projects.json
(реестр проектов) и ~/.local/state/duo-supervisor/state.json (состояние надзора):
XDG-переменные указывают во временный каталог теста. DUO_REPO из окружения
машины убран — иначе он подменял бы разрешение корня в resolve_repo().
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _xdg_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("DUO_REPO", raising=False)
