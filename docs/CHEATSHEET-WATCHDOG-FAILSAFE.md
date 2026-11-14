# Надзор за дуо-пайплайном: штатные средства вместо внешних костылей

> Переписано 2026-10-21 (pipework3). Прежняя схема «живой bash-сторож `watchdog.sh` + одноходовка
> `watchdog_failsafe.sh` под systemd-oneshot» выведена: failsafe **не мог** перезапустить сторожа
> (systemd по завершении oneshot-сервиса убивает всю cgroup, в логе при этом стояло «перезапуск
> удался»), а сам сторож ловил смену фаз по тикам и не отличал «оркестратор умер» от «ждёт человека».

## Принцип

Источник истины о серии — **сам оркестратор**: `state.json` (phase, `halt`, `inflight`,
`state_updated_at`) + `flock` на `pipeline/<task>/.lock`. Замок держится всю жизнь процесса и снимается
ядром при ЛЮБОЙ смерти (kill -9, OOM, перезагрузка) — надёжнее `ps`/`pgrep` (тем более в песочнице, где
паттерн совпадает с командной строкой самой песочницы). Всё остальное — тонкие потребители этой правды.

Оркестратор сам гарантирует:

- любая ошибка (транспорт, бюджет, лимит итераций, «артефакт не записан», краш) — **чистая остановка**:
  `phase=needs_human` + `stop_reason` + `halt{kind,phase,resumable}` + строка `СТОП:` в `orchestrator.log`
  (+ `notify-send`, если `notify.desktop`); молчаливой смерти процесса больше нет;
- лимит итераций и бюджет проверяются **на границе итерации** (после ревью и синтеза председателя): начатая
  итерация всегда доходит до вердикта; остановка оставляет маркер доработки — `run --resume` продолжает
  ровно с неё;
- прерванный шаг исполнителя (halt или смерть процесса) возобновляется в той же сессии (`inflight`).

## Три потребителя

| слой | кто | для кого | что делает |
|---|---|---|---|
| 1 | `duo_pipeline.py wait --task X` | **агент/скрипт помощника** | блокирует до остановки/приёмки; последняя строка `WAIT-RESULT: done\|impl_pending\|halted\|dead\|idle\|timeout …`, код выхода 0/0/10/11/12/13 |
| 2 | `duo_pipeline.py supervise` (systemd-timer, раз в 2 мин) | **человек/автопилот** | `dead`/`halted` → алерт (один раз на событие) и авто-возобновление `transport`/`crash` (≤ `max_resumes` за окно); `done`/`impl_pending` → уведомление |
| 3 | `status --all` / `status --task X` | кто угодно | колонка «процесс» (running/dead/halted/idle/done), в `--task` — `lock`, возраст state, причина остановки |

### Слой 1: `wait` вместо самодельного bash-цикла + `notify_on_output`

Прежний рецепт (цикл `sleep 30` + разбор `phase` из state.json) заменяется одной командой; её вывод удобно
скармливать `notify_on_output` (паттерн `WAIT-RESULT`), а в обычном режиме — блокирующим вызовом:

```bash
cd /home/sawa/GitHub/vca-platform
conda run -n env311 python tools/dev/duo_pipeline/duo_pipeline.py wait --task <task> --poll 30
# фон + событийный сигнал агенту:  notify_on_output pattern "WAIT-RESULT|Error|Traceback"
```

Коды: `0` — `done` (impl принят) или `impl_pending` (план принят, пауза перед имплементацией); `10` — `halted`
(в строке `halt=<kind> resumable=<…>` и причина); `11` — `dead` (процесс умер без записи halt — `run --resume`);
`12` — `idle` (`needs_human` без машинной записи, напр. эскалация, либо серия не запускалась); `13` — `--timeout`.
Живое состояние по смене фазы/версий печатается строками `[HH:MM:SS] task: phase=… running …` (`--quiet` — без них).

Ограничение, как и у любого `notify_on_output`-цикла: он не переживает сессию агента — после рестарта сессии
`wait` запускается заново (он идемпотентен: при уже наступившем событии отвечает мгновенно).

### Слой 2: `supervise` + один systemd user-таймер

Установка (идемпотентно; заменяет и снимает старый `duo-watchdog-failsafe.timer`):

```bash
bash /home/sawa/GitHub/vca-platform/tools/dev/duo_pipeline/systemd/install.sh
journalctl --user -u duo-supervise.service -n 5 --no-pager      # вывод последнего тика
systemctl --user list-timers 'duo-supervise*' --no-pager        # следующее срабатывание
python tools/dev/duo_pipeline/duo_pipeline.py supervise --dry-run   # что сделал бы надзор (ничего не меняет)
```

Свойства:

- без состояния между тиками (дедупликация алертов и счётчики авто-возобновлений — в
  `~/.local/state/duo-supervisor/state.json`, **вне репозитория**: GitGuard вычищает посторонние untracked-файлы
  под `pipeline/`); первый тик — baseline (о прошлых `done`/`impl_pending` молчит);
- `KillMode=process` в `duo-supervise.service` — обязателен: иначе systemd убьёт вместе с oneshot-сервисом
  оркестратор, который supervise запустил detached;
- авто-возобновляются **только** `transport`/`crash` (`supervisor.auto_resume_kinds`); `limit`, `budget`,
  `errata`, `artifact_missing`, эскалации — решение человека (алерт без запуска); потолок —
  `supervisor.max_resumes` за `resume_window_sec` на задачу (защита от платного цикла);
- «мёртв» = активная фаза + `.lock` свободен + state не обновлялся ≥ `supervisor.stale_sec` (300 с) и не старше
  `dead_max_age_sec` (сутки: давние «трупы» и ни разу не запускавшиеся серии — `idle`, не тревога);
- автопродолжение `impl_pending` **не делается**: запуск имплементации — решение эксперта после приёмки плана.

Без systemd: `setsid nohup bash tools/dev/duo_pipeline/watchdog.sh >/dev/null 2>&1 &` (тонкая обёртка над
`supervise --loop --interval 60`) либо cron `*/2 * * * * … duo_pipeline.py supervise`.

## Что делать при остановке (шпаргалка помощника)

```bash
python tools/dev/duo_pipeline/duo_pipeline.py status --task X          # halt.kind, stop_reason, процесс
python tools/dev/duo_pipeline/duo_pipeline.py run --task X --resume    # transport / crash / artifact_missing
python tools/dev/duo_pipeline/duo_pipeline.py run --task X --resume --add-iterations 3   # limit
python tools/dev/duo_pipeline/duo_pipeline.py run --task X --resume --add-budget 40      # budget
```

Подробно по видам остановок — `pipeline/CHEATSHEET-CONTROL.md` («Остановки»).

## Если сломалось

- `systemctl --user status duo-supervise.timer` — состояние; таймер не стреляет → `loginctl show-user sawa -p Linger`
  (должно быть `yes`; включить: `sudo loginctl enable-linger sawa`);
- алерты не приходят на рабочий стол → у user-сервиса нет D-Bus: проверь
  `systemctl --user show-environment | rg DBUS`; в `duo-supervise.service` задан
  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus`;
- хочется «просто посмотреть»: `supervise --dry-run` и `status --all`.

## Другие «вечные» сторожа (ночные калибровки и т.п.)

Паттерн прежний, но с поправкой на урок systemd: одноходовка «жив ли цель → нет: перезапустить» под
`Type=oneshot` обязана иметь `KillMode=process` (или запускать цель через `systemd-run --user --collect`),
быть идемпотентной (`flock`) и проверять **результат** перезапуска не по `pgrep` через 3 секунды, а по
устойчивому признаку (замок/heartbeat цели).
