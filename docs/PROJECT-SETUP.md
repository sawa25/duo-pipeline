# Подключение любого git-проекта к duo-пайплайну (duoportable)

Инструмент живёт в СВОЁМ репозитории (`/home/sawa/GitHub/duo-pipeline/`); проект,
который им управляется, хранит у себя только свои данные: артефакты серий
`pipeline/<task>/` и тонкие переопределения `.duo/config.json`. Ниже — как
подключить проект и какие ключи проектного конфига существуют (серия
`duoportable`, R1–R10). Рабочие команды серий — `docs/CHEATSHEET-CONTROL.md`.

`duo` в командах — лаунчер `bin/duo` этого репозитория. Ставится один раз на машину:
`bash /home/sawa/GitHub/duo-pipeline/install.sh` (симлинк `~/.local/bin/duo`, скилл для
Cursor и Claude Code); развёрнуто — `python /home/sawa/GitHub/duo-pipeline/duo_pipeline.py <команда>`.
Интерпретатор лаунчер берёт сам: `$DUO_PYTHON` → env311 → python3.12/3.11 (нужен ≥ 3.11).

## Быстрое подключение: `duo setup`

```bash
cd /home/sawa/GitHub/<проект>
duo setup                # в каталоге проекта
```

Что делает (все шаги идемпотентны, повторный запуск печатает «УЖЕ» и выходит rc=0):

1. каталог вне git → `git init` по подтверждению (`--yes` — неинтерактивно;
   без tty и без `--yes` — отказ с подсказкой); после init при нулевом числе
   коммитов — предупреждение (worktree-сериям нужен хотя бы один коммит);
2. каркас `.duo/config.json` — ТОЛЬКО если файла нет (существующий конфиг
   проекта не перезаписывается никогда);
3. `.gitignore`: рантайм серий под маркером
   `# duo-pipeline: рантайм серий (duo setup)` — записи `pipeline/*/.lock` и
   `/.worktrees/` (дубликатов не делает);
4. регистрация проекта в реестре надзора
   `${XDG_CONFIG_HOME:-~/.config}/duo-pipeline/projects.json`
   (`--no-register` — пропустить).

**После `setup` закоммить `.duo/` и `.gitignore` ДО первой серии.** Незакоммиченные файлы
`commit-by-plan --yes` считает «не покрытыми» и отказывает (all-or-nothing).

Дальше: `duo init --task <имя>` → `duo run --task <имя>` (как обычно,
см. шпаргалку). Управление реестром: `duo projects [--add PATH] [--remove PATH]
[--prune]` — таблица «проект / каталог на месте / .duo / активных серий».

## Корень проекта (R1): как оркестратор находит репозиторий

Приоритет (каждый следующий используется, если предыдущих нет):

1. флаг `--repo PATH` (глобальный, ДО подкоманды: `duo --repo <путь> run --task X`);
2. переменная окружения `DUO_REPO`;
3. legacy-ключ `repo` в конфиг-слое `--config` (совместимость со старым
   `config.json`, где корень был запиннен);
4. автодетект от текущего каталога: `git rev-parse --git-common-dir` — работает
   из любого подкаталога проекта и из linked worktree (возвращает ОСНОВНОЕ дерево).

Нет корня ни одним способом: обычные команды отказываются с подсказкой
(`--repo PATH` / `DUO_REPO` / `duo setup`), а не трейсбеком. `projects`, `setup`
работают и без корня. `supervise` без явного указания (флаг/DUO_REPO/legacy)
идёт в режим реестра — автодетект НЕ применяется (таймер не должен надзирать за
тем репо, в каталоге которого случайно живёт). Detached-перезапуск всегда
передаёт ребёнку явный `--repo` — серия не зависит от cwd родителя.

## Конфиг (R2): три слоя

    DEFAULTS (в duo_pipeline.py)  <  config.json инструмента (--config)  <  <проект>/.duo/config.json

- слои сливаются `deep_merge`'ом: словари — по ключам, **списки замещаются
  целиком** (чтобы проектный `gates.steps` не склеивался с чужим);
- пути в `claude_config_dir`, `council_agents_dir`, `gates.python`, `gates.stubs`
  раскрывают `~` и `$VAR`;
- собранный cfg содержит `_project_config_path` — путь проектного слоя, если
  файл существует (иначе `null`);
- старый конфиг с ключом `repo` продолжает работать (см. R1, п. 3).

## Ключи проектного слоя (`.duo/config.json`)

| ключ | зачем |
|---|---|
| `executor_env_hint` | строка в промпт исполнителя: окружение проекта (интерпретатор, `conda run …`, стенд) |
| `executor_worktree_note` | приписка в worktree-блок промпта: сервисы/ресурсы, которые нельзя перезапускать (R7) |
| `gates.python` | интерпретатор гейтов (null → `sys.executable` оркестратора) |
| `gates.stubs` | каталог заглушек для pytest-шагов (null → `gate_stubs/` инструмента; `""` → заглушки выключены) |
| `gates.cpus` | список ядер для `taskset` (подставляется в `{cpus}`) |
| `gates.steps` | шаги гейтов поставки — см. ниже (R3) |
| `git.isolation` | `shared` \| `worktree` — умолчание изоляции новых серий |
| `git.worktrees_dir` | каталог worktree-серий (обычно `.worktrees`) |
| `git.worktree_links` | имена корневых каталогов, которые provisioning симлинкает в свежий worktree (R4; `.env` не линкуется никогда) |
| `guard.never_revert` | tracked-пути, которые ревьюер НЕ откатывает при «ловушке отката» (правки хозяина; R5) |
| `archive.guard_exempt_files` | файлы, ссылки на которые НЕ блокируют `archive` (дополняют builtin-набор; R6) |
| `archive.guard_skip_dirs` | каталоги, внутрь которых охрана `archive` не заходит (дополняют builtin-набор) |
| `archive.merge_target_refs` | ветки, в которые должна быть слита серия для архивации (дополняют builtin `HEAD`, `main`) |
| `supervisor`, `notify`, `limits`, `timeouts`, `duo`, `pair`, `review`, `parallel`, `solo` | те же секции, что в config.json инструмента (см. `_comment`-поля там) |

### Гейты поставки (R3): `gates.steps`

Шаг — dict с ключами: `name` (имя = имя лог-файла), `argv` (список строки
команды), `pp` (нужен ли `PYTHONPATH=<заглушки>:<копия>` — как у pytest-шагов),
`timeout` (сек), `summary` (`last_line` — последняя строка вывода дословно;
`contracts` — строки контрактов lint-imports). Плейсхолдеры в `argv`:
`{py}` — полный путь интерпретатора, `{pybin}` — его каталог, `{cpus}` —
`gates.cpus`. Прогон — в чистой копии (`git archive REF` + файлы серии):
`duo gates --task X [--ref REF] [--only ruff,unit] [--extra …] [--dry-run]`.
Пустой `gates.steps` → rc=2 с подсказкой (проект без гейтов — нормальная
ситуация). Полный набор аргументов — `duo gates --help`.

**Гейты — локальное зеркало CI проекта**, а не отдельная система: `gates.steps` должны
повторять команды CI проекта (в vca — таргеты `make lint` / `make unit` / `make integration`),
и правятся они в том же коммите, что и CI. Кто потребитель каждого из трёх контуров (CI
проекта, гейты серии, CI самого инструмента) и что покрывает каждый — раздел «CI и гейты»
в `docs/CHEATSHEET-CONTROL.md`.

### Пример: vca-platform (реальный проектный слой после этапа 2, 2026-11-16)

Так выглядит `/home/sawa/GitHub/vca-platform/.duo/config.json` (там же длинный
`executor_env_hint`). Эквивалентность прежнему hardcode проверена сверкой старой встроенной
копии оркестратора и нового инструмента на реальном vca: шаги гейтов (argv/pp/timeout),
наборы охраны архива, ветки слияния, симлинки worktree, текст worktree-блока промпта и
`executor_env_hint` совпали байт-в-байт. Пиннинг — `tests/unit/test_duo_gates.py`,
`test_duo_portable.py`:

```json
{
  "executor_env_hint": "Интерпретатор: conda run -n env311 …",
  "executor_worktree_note": "API-сервер :8001 не перезапускай.",
  "gates": {
    "python": "~/anaconda3/envs/env311/bin/python",
    "cpus": "0-11",
    "steps": [
      {"name": "ruff",  "pp": false, "timeout": 900,
       "argv": ["{pybin}/ruff", "check", "."], "summary": "last_line"},
      {"name": "mypy",  "pp": false, "timeout": 900,
       "argv": ["{pybin}/mypy"], "summary": "last_line"},
      {"name": "li",    "pp": false, "timeout": 900,
       "argv": ["{pybin}/lint-imports"], "summary": "contracts"},
      {"name": "unit",  "pp": true, "timeout": 7200,
       "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "{cpus}",
                "nice", "-n", "10", "{py}", "-m", "pytest", "tests/unit", "tests/arch"],
       "summary": "last_line"},
      {"name": "integ", "pp": true, "timeout": 7200,
       "argv": ["env", "-u", "DATA_DIR", "-u", "LOG_DIR", "taskset", "-c", "{cpus}",
                "nice", "-n", "10", "{py}", "-m", "pytest", "-m", "integration and not gpu"],
       "summary": "last_line"}
    ]
  },
  "git": {"worktree_links": ["data", "models", "node_modules"]},
  "archive": {
    "guard_skip_dirs": ["data", "models", "vca_platform.egg-info"],
    "guard_exempt_files": ["doc/99-TODO.md",
                           "tests/fixtures/mocalib_a3/a3_registry.json"],
    "merge_target_refs": ["dev/speedlim"]
  }
}
```

(`tests/arch` в примере — путь vca; в других проектах свои. Списки шагов
замещают целиком — проектный набор не смешивается с чужим. `archive.*` —
прежние значения охраны vca: тяжёлые каталоги не сканируются, указатели
99-TODO/a3_registry не блокируют удаление серии; те же значения держит охранный
тест vca, а их совпадение с конфигом проверяет
`tests/arch/test_archive_guard_config_sync.py` в vca. `guard.never_revert` в примере
не задан: прежнее значение vca (`tools/dev/duo_pipeline/`) защищало каталог
оркестратора ВНУТРИ дерева vca — после переноса кода в отдельный репозиторий
его там больше нет; при живом аналоге (напр. генерируемые данные хозяина)
добавь список путей. Шаг `li` (lint-imports) строже CI vca: в `ci.yml` его нет.)

## Мультипроектный надзор (R8)

`supervise` без явного корня обходит ВСЕ проекты реестра
(`~/.config/duo-pipeline/projects.json`): для каждого — свой cfg
(слои `--config` + `<проект>/.duo/config.json`), состояние надзора и уведомления
— на ключах `<абс-корень>::<task>` (одноимённые серии разных проектов не
глушат алерты друг друга), заголовок уведомления `duo <проект>/<task>`,
строки лога `SUPERVISE: [<проект>] <task> …`. Пустой реестр — сообщение и rc=0.
Systemd-таймер ставится командой `bash /home/sawa/GitHub/duo-pipeline/install.sh --systemd`
(юнит из `systemd/duo-supervise.service.in`, без `WorkingDirectory` проекта — корень больше
не нужен: supervise ходит по реестру). Установка ЗАМЕНЯЕТ ранее поставленный
`duo-supervise.*` (например, старый таймер vca-platform): проекты, за которыми он следил,
должны быть в реестре (`duo setup` или `duo projects --add PATH`) — иначе после замены их
серии останутся без надзора.

**Переход со старого таймера vca (сделан 2026-11-16).** Прежние юниты сохранены рядом как
`~/.config/systemd/user/duo-supervise.{service,timer}.bak-<время>` (откат: вернуть файлы,
`systemctl --user daemon-reload`). Состояние надзора лежит в том же
`~/.local/state/duo-supervisor/state.json`, но под новыми ключами `<корень>::<task>`; старые
записи (ключ — голое имя задачи) не мигрировались и остаются безвредным хвостом. Первый тик
по новым ключам молча «запоминает» события старше часа: повторных уведомлений о старых
`done`/`impl_pending` нет (до переключения это проверено `duo supervise --dry-run`).

## Границы

- Код инструмента — только в репозитории duo-pipeline; проект не получает
  копий скриптов.
- `.duo/config.json` — конфиг ПРОЕКТА: оркестратор не перезаписывает его
  (каркас создаёт лишь при отсутствии).
- Реестр и состояние надзора — в XDG-каталогах пользователя, не в git-проектах.
