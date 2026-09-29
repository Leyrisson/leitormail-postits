#!/bin/bash
# Widget de barra + alternador (on/off) do LeitorMail.
# Modos:
#   alternar.sh            → imprime JSON estilo waybar com o estado
#   alternar.sh toggle     → liga/desliga a automação (para/inicia o timer)
#   alternar.sh estado     → imprime apenas "on" ou "off" (para o widget QML)
set -u
BASE="$HOME/.config/omarchy-voice/plugins/leitormail"
STATE_DIR="$HOME/.local/state/omarchy-voice"
FLAG="$STATE_DIR/leitormail_on"
LOG="$STATE_DIR/leitormail.log"

estado() {
    [ -f "$FLAG" ] && [ "$(cat "$FLAG")" = "1" ] && echo on || echo off
}

cor() {
    case "$1" in on) echo "#6ab04c";; *) echo "#e0e0e0";; esac
}

fechar_postit() {
    hyprctl clients -j 2>/dev/null | python3 -c "
import json,sys
try:
    for c in json.load(sys.stdin):
        if 'mail_postit' in (c.get('title') or ''):
            print(c['address'])
except Exception:
    pass" 2>/dev/null | while read -r addr; do
        hyprctl dispatch closewindow "address:$addr" >/dev/null 2>&1
    done
}

toggle() {
    if [ "$(estado)" = "on" ]; then
        echo 0 > "$FLAG"
        systemctl --user stop leitormail.timer 2>/dev/null
        fechar_postit
        notify-send -a LeitorMail "LeitorMail desligado" >/dev/null 2>&1 &
    else
        echo 1 > "$FLAG"
        systemctl --user start leitormail.timer 2>/dev/null
        systemctl --user enable leitormail.timer >/dev/null 2>&1
        # post-it desligado a pedido do dono (2026-09-29): o monitor volta a
        # notificar, mas as notas na tela só com omarchy-mail-postit.
        notify-send -a LeitorMail "LeitorMail ligado" >/dev/null 2>&1 &
    fi
}

case "${1:-}" in
    toggle)
        toggle
        ;;
    estado)
        printf '%s\n' "$(estado)"
        exit 0
        ;;
esac

S="$(estado)"
printf '{"text":"%s","tooltip":"LeitorMail: %s\\n3 e-mails a cada 16 min","class":"leitor-%s","color":"%s"}\n' \
    "$([ "$S" = on ] && echo "󰶼" || echo "󰶸")" "$S" "$S" "$(cor "$S")"