#!/usr/bin/env bash
# Установка duo на этой машине. Идемпотентно. Репозиторий — единственный источник истины:
# всё, что ставится в $HOME, — симлинки на файлы отсюда.
#
#   bash install.sh                 лаунчер ~/.local/bin/duo + скилл для Cursor и Claude Code
#   bash install.sh --personas      + персоны council-*2.md в ~/.claude/agents симлинками
#                                     (прежние файлы — в бэкап <имя>.bak-<время>, если отличались)
#   bash install.sh --systemd       + user-таймер duo-supervise (надзор по ВСЕМ проектам реестра);
#                                     ЗАМЕНЯЕТ ранее установленный duo-supervise.* (напр. таймер
#                                     прежней встроенной копии) — это и есть переключение надзора
#   bash install.sh --check         только показать состояние, ничего не менять
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
DO_PERSONAS=0; DO_SYSTEMD=0; CHECK=0
for a in "$@"; do
    case "$a" in
        --personas) DO_PERSONAS=1 ;;
        --systemd) DO_SYSTEMD=1 ;;
        --check) CHECK=1 ;;
        -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "неизвестный аргумент: $a" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

# link SRC DST — симлинк DST -> SRC; существующий не-наш объект уходит в бэкап.
link() {
    local src="$1" dst="$2"
    if [[ -L "$dst" && "$(readlink -f "$dst")" == "$(readlink -f "$src")" ]]; then
        say "ok      $dst"
        return
    fi
    if (( CHECK )); then say "TODO    $dst -> $src"; return; fi
    mkdir -p "$(dirname "$dst")"
    if [[ -e "$dst" || -L "$dst" ]]; then
        if [[ ! -L "$dst" ]] && cmp -s "$src" "$dst" 2>/dev/null; then
            rm -f "$dst"                              # идентичная копия — бэкап не нужен
        else
            mv "$dst" "$dst.bak-$STAMP"
            say "backup  $dst.bak-$STAMP"
        fi
    fi
    ln -s "$src" "$dst"
    say "linked  $dst -> $src"
}

link "$ROOT/bin/duo" "$HOME/.local/bin/duo"
if [[ -d "$ROOT/skill" ]]; then
    link "$ROOT/skill" "$HOME/.cursor/skills/duo-pipeline"
    link "$ROOT/skill" "$HOME/.claude/skills/duo-pipeline"
fi

if (( DO_PERSONAS )); then
    for f in "$ROOT"/personas/council-*.md; do
        [[ -e "$f" ]] || continue
        link "$f" "$HOME/.claude/agents/$(basename "$f")"
    done
fi

if (( DO_SYSTEMD )); then
    UD="$HOME/.config/systemd/user"
    if (( CHECK )); then
        say "systemd: $(systemctl --user is-enabled duo-supervise.timer 2>&1 || true)"
    else
        mkdir -p "$UD"
        sed "s#@ROOT@#$ROOT#g" "$ROOT/systemd/duo-supervise.service.in" > "$UD/duo-supervise.service"
        install -m 0644 "$ROOT/systemd/duo-supervise.timer" "$UD/duo-supervise.timer"
        systemctl --user daemon-reload
        systemctl --user enable --now duo-supervise.timer
        systemctl --user list-timers 'duo-supervise*' --no-pager
        say "OK. Проверка: journalctl --user -u duo-supervise.service -n 5 --no-pager"
    fi
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) say "ВНИМАНИЕ: ~/.local/bin нет в PATH — добавь его или вызывай $ROOT/bin/duo" ;;
esac
