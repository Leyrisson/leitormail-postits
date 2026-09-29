#!/bin/bash
# Pássaro voando 1x: janela transparente + partículas, atravessa a tela
# Dependências: python3-gobject (gtk3) + hyprland (hyprctl)
set -e
BIN="$(mktemp --suffix=.py)"
BASE="$(dirname "$(realpath "$0")")"
BIRD_ENTITLEMENT=""
chmod +x "$BIN" 2>/dev/null || true
WM="$(printenv XDG_CURRENT_DESKTOP 2>/dev/null || echo 'hyprland')"
if [ "$WM" != "hyprland" ]; then
    # sem hyprland: só a notificação já foi dada, sai sem animar (sem travar)
    rm -f "$BIN"
    exit 0
fi
cat > "$BIN" <<'PYEOF'
import gi, math, os, subprocess, time, sys
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gdk, GLib

def get_screen():
    out = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True)
    try:
        import json
        mons = json.loads(out.stdout)
    except Exception:
        return 1920, 1080, 0, 0, 1.0, 1.0
    for m in mons:
        if m.get("focused"):
            x, y = m["x"], m["y"]
            return int(m["width"]), int(m["height"]), x, y, float(m["scale"]), float(m["scale"])
    return 1920, 1080, 0, 0, 1.0, 1.0

W, H, OX, OY, S, S2 = get_screen()
W = int(W / S); H = int(H / S)
BW = int(120 / S); BH = int(110 / S)
TRAIL = []

draw_handler = None

class Bird(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_title("passarinho_omarchy")
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_visual(self.get_screen().get_rgba_visual())
        self.set_app_paintable(True)
        self.resize(BW, BH)
        self.move(int(OX + W * 1.02), int(OY + H * 0.04))
        global da
        da = Gtk.DrawingArea()
        da.connect("draw", self.on_draw)
        self.add(da)
        self.st = time.time()
        self.tick = 0
        self.show_all()
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.present()
        # força camada acima das demais janelas (painter
        # encontra por título): pin + float garante que o Hyprland
        # não a enterre assim que outra janela receber foco.
        subprocess.run(["hyprctl", "dispatch", "pin", "title:passarinho_omarchy"],
                       capture_output=True, text=True, check=False)
        GLib.timeout_add(12, self.animate)

    def on_draw(self, widget, cr):
        global TRAIL
        t = time.time() - self.st
        p = min(1.0, t / 6.0)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cx = int(OX + W * (1.0 - p) * 1.04 - BW * 0.2)
        cy = int(OY + H * (0.04 + 0.22 * math.sin(p * math.pi)))
        bw = int(BW * S); bx = int(cx * S); by = int(cy * S)
        cr.save()
        cr.scale(S, S)
        cr.translate(-(cx - W - BW), 0)
        self.move(cx, cy)
        # corpo do passarinho (emojis grandes sobre fundo transparente)
        cr.set_font_size(34)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(cx + 8, cy)
        cr.show_text("🐦")
        cr.stroke()
        # trilha de estrelinhas caindo
        draw_trail(cr, t, cx, cy)
        cr.restore()
        return False

def draw_trail(cr, t, cx, cy):
    for i in range(14):
        off = (i * 0.36 + t * 1.6) % 3.2
        sx = cx - 6 - off * 16
        sy = cy + 6 + (off * 9) + 8 * math.sin(t * 2 + i)
        alpha = max(0.0, 1.0 - off / 3.2)
        cr.set_source_rgba(1.0, 0.85, 0.4, alpha)
        cr.arc(sx, sy, 2.2, 0, 2 * math.pi)
        cr.fill()

bird = Bird()
Gtk.main()
PYEOF
python3 "$BIN" &
PID=$!
sleep 6
kill "$PID" 2>/dev/null || true
rm -f "$BIN"
exit 0
