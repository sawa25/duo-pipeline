# duo-pipeline

Оркестратор «исполнитель ↔ ревью» на GLM (Z.AI) через Claude Code CLI.
Режимы: **solo** (один исполнитель), **pair** (исполнитель + один проверяющий),
**duo** (исполнитель + дуэт + председатель).

Единый источник истины: код, конфиг по умолчанию, шпаргалки и скилл лежат здесь; проект
хранит у себя только `pipeline/<task>/` (артефакты серий) и необязательный `.duo/config.json`.

## Не пропусти конец фазы

Серия идёт в фоне, а её конец (план принят / код принят / остановка) **тихий**. Сразу после каждого
`duo run` и `duo run --continue` запускайте сторожа В ФОНЕ: `duo wait --task NAME --timeout 3600 --quiet`
(агенту — с `notify_on_output` по `WAIT-RESULT`). Подробно — раздел «СТОРОЖА» в
[docs/CHEATSHEET-CONTROL.md](docs/CHEATSHEET-CONTROL.md).

## Требования

Linux, Python ≥ 3.11 (только stdlib), git, [Claude Code CLI](https://docs.claude.com/en/docs/claude-code)
(`claude`), профиль модели (по умолчанию `~/.claude-glm` — ключ `claude_config_dir`).
Для гейтов и `make check` — `ruff` и `pytest` в используемом интерпретаторе.

## Установка на машине

```bash
git clone https://github.com/sawa25/duo-pipeline.git && cd duo-pipeline
bash install.sh --personas     # лаунчер ~/.local/bin/duo, скилл, персоны ревьюеров
bash install.sh --check        # что установлено
cd /путь/к/проекту && duo setup   # подключить проект (.duo/config.json, реестр проектов)
```

Лаунчер `bin/duo` сам выбирает интерпретатор: `$DUO_PYTHON` > `env311` в anaconda/miniconda >
`python3.12`/`3.11`/`python3` (нужен ≥ 3.11) и добавляет `~/.local/bin` в `PATH` (там живёт
`claude`; в неинтерактивном ssh и systemd его иначе может не быть).

## Документация

- Управление сериями: [docs/CHEATSHEET-CONTROL.md](docs/CHEATSHEET-CONTROL.md)
- Подключение проекта и несколько машин: [docs/PROJECT-SETUP.md](docs/PROJECT-SETUP.md)
- Надзор и отказоустойчивость: [docs/CHEATSHEET-WATCHDOG-FAILSAFE.md](docs/CHEATSHEET-WATCHDOG-FAILSAFE.md)
- Полевая проверка инструмента на проекте (бриф оператору и контролёру): [docs/FIELD-TEST-BRIEF.md](docs/FIELD-TEST-BRIEF.md)
- Гейты разработки самого инструмента: `make check`

Состояние: инструмент отлажен на личных стендах автора; настройки под чужую среду (модель,
профиль, окружение гейтов) делаются через `.duo/config.json` проекта.
